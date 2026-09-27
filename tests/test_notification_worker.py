"""Notification scheduling uses real threads without database or external SMTP calls."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread, get_ident
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from packages.settings import Settings
from services.worker import main as worker


@pytest.fixture
def mail_runtime(monkeypatch):
    state = SimpleNamespace(
        polls=[],
        closes=[],
        readers=[],
        models=[],
        deliveries=[],
        executors={},
        futures={"byof-agent": [], "byof-mail": []},
        submitted={"byof-agent": Event(), "byof-mail": Event()},
    )
    state.settings = Settings(
        _env_file=None,
        environment="test",
        database_url=SecretStr("postgresql+psycopg://byof_app:unit@127.0.0.1/byof_test"),
        factory_api_token=SecretStr("unit-reader"),
        factory_execution_token=SecretStr("unit-writer"),
        smtp_mode="capture",
    )

    class Engine:
        def dispose(self):
            state.closes.append("engine")

    class Reader:
        def __init__(self, origin, token):
            self.index = len(state.readers)
            self.closed = False
            state.readers.append(self)

        def close(self):
            assert not self.closed
            self.closed = True
            state.closes.append(f"reader:{self.index}")

    class Writer:
        def __init__(self, origin, token):
            pass

        def close(self):
            state.closes.append("writer")

    class Model:
        def complete(self, prompt):
            state.models.append(get_ident())
            return "controlled response"

    class Executor(ThreadPoolExecutor):
        def __init__(self, *args, **kwargs):
            self.slot = kwargs["thread_name_prefix"]
            assert kwargs["max_workers"] == 1
            assert self.slot not in state.executors
            state.executors[self.slot] = self
            super().__init__(*args, **kwargs)

        def submit(self, *args, **kwargs):
            future = super().submit(*args, **kwargs)
            state.futures[self.slot].append(future)
            state.submitted[self.slot].set()
            return future

    state.engine, state.model = Engine(), Model()
    monkeypatch.setattr(worker, "connect", lambda url: state.engine)
    monkeypatch.setattr(worker, "FactoryHTTP", Reader)
    monkeypatch.setattr(worker, "FactoryExecution", Writer)
    monkeypatch.setattr(worker, "configured_model", lambda settings: state.model)
    monkeypatch.setattr(worker, "UserModelRouter", lambda catalog, fallback: fallback)
    monkeypatch.setattr(worker, "ThreadPoolExecutor", Executor)

    def record_poll(name):
        def call(*args):
            assert args[0] is state.engine
            state.polls.append((name, get_ident()))
            return False

        return call

    for name in (
        "synchronize_due",
        "ingest_sources",
        "wake_completed_jobs",
        "tick_reminders",
        "deliver_one",
    ):
        monkeypatch.setattr(worker, name, record_poll(name))

    def process(engine, connector, model):
        assert engine is state.engine and model is state.model
        assert connector is not state.readers[0]
        assert model.complete("controlled") == "controlled response"
        return True

    monkeypatch.setattr(worker, "process_case", process)
    monkeypatch.setattr(
        worker, "process_one", lambda *args: pytest.fail("Case ownership excludes legacy fallback")
    )
    monkeypatch.setattr(
        worker,
        "deliver_notification",
        lambda *args: pytest.fail("Each test must install its controlled notification effect"),
    )
    return state


@pytest.mark.timeout(15)
def test_blocked_smtp_preserves_model_and_business_progress_in_two_single_worker_slots(
    mail_runtime, monkeypatch
):
    started, release = Event(), Event()
    polling_thread = get_ident()

    def send(engine, settings):
        assert engine is mail_runtime.engine and settings is mail_runtime.settings
        mail_runtime.deliveries.append(get_ident())
        started.set()
        assert release.wait(5), "The test must release its controlled SMTP wait"
        return True

    monkeypatch.setattr(worker, "deliver_notification", send)
    ticks = 0

    def pause(seconds):
        nonlocal ticks
        assert seconds == 1
        ticks += 1
        assert started.wait(3)
        assert mail_runtime.futures["byof-agent"][-1].result(timeout=3) is True
        assert len(mail_runtime.models) == ticks
        assert len(mail_runtime.polls) == ticks * 5
        assert len(mail_runtime.futures["byof-mail"]) == 1
        assert not mail_runtime.futures["byof-mail"][0].done()
        assert not release.is_set()
        if ticks == 4:
            release.set()

    try:
        worker.run_worker(mail_runtime.settings, should_stop=lambda: ticks == 4, pause=pause)
    finally:
        release.set()
    assert set(mail_runtime.executors) == {"byof-agent", "byof-mail"}
    assert len(mail_runtime.futures["byof-agent"]) == 4
    assert len(mail_runtime.deliveries) == 1
    assert len(set(mail_runtime.models)) == 1
    assert len({polling_thread, *mail_runtime.models, *mail_runtime.deliveries}) == 3
    assert [name for name, _ in mail_runtime.polls] == [
        "synchronize_due",
        "ingest_sources",
        "wake_completed_jobs",
        "tick_reminders",
        "deliver_one",
    ] * 4
    assert {thread for _, thread in mail_runtime.polls} == {polling_thread}
    assert all(reader.closed for reader in mail_runtime.readers)
    assert mail_runtime.closes[-3:] == ["writer", "reader:0", "engine"]


@pytest.mark.timeout(15)
def test_failed_mail_future_logs_only_generic_code_and_next_polls_and_deliveries_continue(
    mail_runtime, monkeypatch, caplog
):
    private_message = "unit-private-recipient-and-smtp-password-must-not-appear"
    accepted = []

    def send(engine, settings):
        mail_runtime.deliveries.append(get_ident())
        if len(mail_runtime.deliveries) == 1:
            raise RuntimeError(private_message)
        accepted.append(len(mail_runtime.deliveries))
        return True

    monkeypatch.setattr(worker, "deliver_notification", send)
    ticks = 0

    def pause(seconds):
        nonlocal ticks
        ticks += 1
        future = mail_runtime.futures["byof-mail"][-1]
        if ticks == 1:
            with pytest.raises(RuntimeError, match=private_message):
                future.result(timeout=3)
        else:
            assert future.result(timeout=3) is True
        assert mail_runtime.futures["byof-agent"][-1].result(timeout=3) is True

    worker.run_worker(mail_runtime.settings, should_stop=lambda: ticks == 3, pause=pause)
    assert len(mail_runtime.deliveries) == 3 and accepted == [2, 3]
    assert len(mail_runtime.models) == 3
    assert len(mail_runtime.polls) == 15
    assert [record.getMessage() for record in caplog.records] == ["NOTIFICATION_TASK_FAILED"]
    assert private_message not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert all(reader.closed for reader in mail_runtime.readers)
    assert mail_runtime.closes[-3:] == ["writer", "reader:0", "engine"]


@pytest.mark.timeout(15)
@pytest.mark.parametrize("first", ["byof-agent", "byof-mail"])
def test_once_cli_waits_for_both_slots_before_closing_shared_resources(
    mail_runtime, monkeypatch, first
):
    entered = {slot: Event() for slot in ("byof-agent", "byof-mail")}
    release = {slot: Event() for slot in entered}
    finished = Event()
    failures = []

    def complete(prompt):
        mail_runtime.models.append(get_ident())
        entered["byof-agent"].set()
        assert release["byof-agent"].wait(5)
        return "controlled response"

    def send(engine, settings):
        mail_runtime.deliveries.append(get_ident())
        entered["byof-mail"].set()
        assert release["byof-mail"].wait(5)
        return True

    monkeypatch.setattr(mail_runtime.model, "complete", complete)
    monkeypatch.setattr(worker, "deliver_notification", send)
    monkeypatch.setattr(worker, "Settings", lambda: mail_runtime.settings)

    def run():
        try:
            worker.main(["--once"])
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    thread = Thread(target=run)
    thread.start()
    try:
        for slot in entered:
            assert entered[slot].wait(3)
            assert mail_runtime.submitted[slot].wait(3)
        assert not finished.is_set() and mail_runtime.closes == []
        assert len(mail_runtime.polls) == 5
        assert all(len(futures) == 1 for futures in mail_runtime.futures.values())
        assert set(mail_runtime.executors) == {"byof-agent", "byof-mail"}
        release[first].set()
        assert mail_runtime.futures[first][0].result(timeout=3) is True
        other = "byof-mail" if first == "byof-agent" else "byof-agent"
        assert not mail_runtime.futures[other][0].done()
        assert not finished.is_set()
        assert mail_runtime.closes == (["reader:1"] if first == "byof-agent" else [])
    finally:
        for event in release.values():
            event.set()
        thread.join(timeout=3)
    assert not thread.is_alive() and not failures
    assert finished.is_set()
    assert mail_runtime.closes == ["reader:1", "writer", "reader:0", "engine"]


def test_disabled_mail_does_not_allocate_or_call_notification_slot(mail_runtime):
    settings = mail_runtime.settings.model_copy(update={"smtp_mode": "disabled"})
    worker.run_worker(settings, once=True)
    assert set(mail_runtime.executors) == {"byof-agent"}
    assert mail_runtime.futures["byof-mail"] == [] and mail_runtime.deliveries == []
    assert len(mail_runtime.models) == 1 and len(mail_runtime.polls) == 5
    assert mail_runtime.closes == ["reader:1", "writer", "reader:0", "engine"]
