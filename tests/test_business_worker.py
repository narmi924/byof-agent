"""Real thread-pool scheduling with controlled model waits; no database or external calls."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, Thread, get_ident
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from packages.providers.registry import ProviderUnavailable
from packages.settings import Settings
from services.worker import main as worker


@pytest.fixture
def runtime(monkeypatch):
    state = SimpleNamespace(
        calls=[],
        closes=[],
        readers=[],
        writers=[],
        submissions=0,
        executors=[],
    )
    state.settings = Settings(
        _env_file=None,
        database_url=SecretStr("postgresql+psycopg://byof_app:unit@127.0.0.1/byof_test"),
        factory_api_token=SecretStr("unit-reader"),
        factory_execution_token=SecretStr("unit-writer"),
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
            state.writers.append(self)

        def close(self):
            state.closes.append("writer")

    class Model:
        def complete(self, prompt):
            return "controlled response"

    class Executor(ThreadPoolExecutor):
        def __init__(self, *args, **kwargs):
            assert kwargs["max_workers"] == 1
            state.executors.append(self)
            super().__init__(*args, **kwargs)

        def submit(self, *args, **kwargs):
            state.submissions += 1
            return super().submit(*args, **kwargs)

    state.engine, state.model = Engine(), Model()
    monkeypatch.setattr(worker, "connect", lambda url: state.engine)
    monkeypatch.setattr(worker, "FactoryHTTP", Reader)
    monkeypatch.setattr(worker, "FactoryExecution", Writer)
    monkeypatch.setattr(worker, "configured_model", lambda settings: state.model)
    monkeypatch.setattr(worker, "ThreadPoolExecutor", Executor)

    def record(name):
        def call(*args):
            assert args[0] is state.engine
            state.calls.append((name, get_ident()))
            return False

        return call

    for name in (
        "synchronize_due",
        "ingest_sources",
        "wake_completed_jobs",
        "tick_reminders",
        "deliver_one",
    ):
        monkeypatch.setattr(worker, name, record(name))
    monkeypatch.setattr(worker, "process_case", lambda *args: True)
    monkeypatch.setattr(
        worker, "process_one", lambda *args: pytest.fail("No legacy fallback for an owned Case")
    )
    return state


def test_blocked_model_keeps_facts_reminders_and_outbox_running_in_one_agent_slot(
    runtime, monkeypatch
):
    started, release = Event(), Event()
    model_threads = []
    polling_thread = get_ident()

    def complete(prompt):
        model_threads.append(get_ident())
        started.set()
        assert release.wait(3), "Test must release the controlled model wait"
        return "done"

    def process(engine, connector, model):
        assert connector is not runtime.readers[0]
        assert model.complete("controlled") == "done"
        return True

    monkeypatch.setattr(runtime.model, "complete", complete)
    monkeypatch.setattr(worker, "process_case", process)
    ticks = 0

    def pause(seconds):
        nonlocal ticks
        assert seconds == 1
        ticks += 1
        assert started.wait(3)
        assert runtime.submissions == 1
        assert len(runtime.calls) == ticks * 5
        assert not release.is_set()
        if ticks == 4:
            release.set()

    try:
        worker.run_worker(runtime.settings, should_stop=lambda: ticks == 4, pause=pause)
    finally:
        release.set()
    assert len(model_threads) == 1 and model_threads[0] != polling_thread
    assert runtime.submissions == 1 and len(runtime.executors) == 1
    assert [name for name, _ in runtime.calls] == [
        "synchronize_due",
        "ingest_sources",
        "wake_completed_jobs",
        "tick_reminders",
        "deliver_one",
    ] * 4
    assert {thread for _, thread in runtime.calls} == {polling_thread}
    assert runtime.closes == ["reader:1", "writer", "reader:0", "engine"]


def test_failed_agent_future_is_consumed_once_and_does_not_stop_fact_polling(
    runtime, monkeypatch, caplog
):
    failed, recovered = Event(), Event()
    calls = 0
    lock = Lock()

    def process(engine, connector, model):
        nonlocal calls
        with lock:
            calls += 1
            attempt = calls
        if attempt == 1:
            failed.set()
            raise RuntimeError("unit-private-model-response-must-not-be-logged")
        recovered.set()
        return True

    monkeypatch.setattr(worker, "process_case", process)
    ticks = 0

    def pause(seconds):
        nonlocal ticks
        ticks += 1
        assert (failed if ticks == 1 else recovered).wait(3)
        # Wait for the real executor's completion callback without a timing-based sleep.
        barrier = runtime.executors[0].submit(lambda: None)
        barrier.result(timeout=3)

    worker.run_worker(runtime.settings, should_stop=lambda: ticks == 3, pause=pause)
    assert calls == 3
    assert len(runtime.calls) == 15
    assert caplog.text.count("AGENT_TASK_FAILED") == 1
    assert "unit-private-model-response" not in caplog.text
    assert all(reader.closed for reader in runtime.readers)
    assert runtime.closes[-3:] == ["writer", "reader:0", "engine"]


def test_once_cli_waits_for_agent_completion_then_closes_all_resources(runtime, monkeypatch):
    entered, release, finished = Event(), Event(), Event()
    failures = []

    def process(*args):
        entered.set()
        assert release.wait(3)
        return True

    monkeypatch.setattr(worker, "process_case", process)
    monkeypatch.setattr(worker, "Settings", lambda: runtime.settings)

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
        assert entered.wait(3)
        assert not finished.is_set()
        assert runtime.closes == []
        assert len(runtime.calls) == 5 and runtime.submissions == 1
    finally:
        release.set()
        thread.join(timeout=3)
    assert not thread.is_alive() and not failures
    assert finished.is_set()
    assert runtime.closes == ["reader:1", "writer", "reader:0", "engine"]


def test_case_owns_priority_and_legacy_path_only_runs_when_no_case_claimed(runtime, monkeypatch):
    order = []
    monkeypatch.setattr(worker, "process_case", lambda *args: order.append("case") or False)
    monkeypatch.setattr(worker, "process_one", lambda *args: order.append("legacy") or True)
    worker.run_worker(runtime.settings, once=True)
    assert order == ["case", "legacy"]
    assert runtime.submissions == 1


def test_unconfigured_model_does_not_disable_manual_business_processing(
    runtime, monkeypatch, caplog
):
    def unavailable(settings):
        raise ProviderUnavailable("NO_CONFIG")

    def process(engine, connector, model):
        assert isinstance(model, worker.UnavailableModel)
        model.complete("controlled")
        pytest.fail("An unconfigured model cannot return success")

    monkeypatch.setattr(worker, "configured_model", unavailable)
    monkeypatch.setattr(worker, "process_case", process)
    worker.run_worker(runtime.settings, once=True)
    assert len(runtime.calls) == 5
    assert caplog.text.count("AGENT_TASK_FAILED") == 1
    assert runtime.closes == ["reader:1", "writer", "reader:0", "engine"]


def test_missing_writer_keeps_read_and_human_processing_but_never_delivers(runtime):
    settings = runtime.settings.model_copy(update={"factory_execution_token": SecretStr("")})
    worker.run_worker(settings, once=True)
    assert [name for name, _ in runtime.calls] == [
        "synchronize_due",
        "ingest_sources",
        "wake_completed_jobs",
        "tick_reminders",
    ]
    assert runtime.writers == []
    assert runtime.closes == ["reader:1", "reader:0", "engine"]


def test_partial_resource_initialization_closes_already_created_connections(runtime, monkeypatch):
    def broken_writer(*args):
        raise RuntimeError("writer unavailable")

    monkeypatch.setattr(worker, "FactoryExecution", broken_writer)
    with pytest.raises(RuntimeError, match="writer unavailable"):
        worker.run_worker(runtime.settings, once=True)
    assert runtime.closes == ["reader:0", "engine"]
    assert runtime.submissions == 0


def test_polling_failure_closes_resources_without_hiding_the_failure(runtime, monkeypatch):
    def failed_sync(*args):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(worker, "synchronize_due", failed_sync)
    with pytest.raises(RuntimeError, match="database unavailable"):
        worker.run_worker(runtime.settings, once=True)
    assert runtime.closes == ["writer", "reader:0", "engine"]
    assert runtime.submissions == 0


def test_writer_close_failure_still_closes_reader_and_engine(runtime, monkeypatch):
    writer_type = worker.FactoryExecution

    class BrokenClose(writer_type):
        def close(self):
            super().close()
            raise RuntimeError("close failed")

    monkeypatch.setattr(worker, "FactoryExecution", BrokenClose)
    with pytest.raises(RuntimeError, match="close failed"):
        worker.run_worker(runtime.settings, once=True)
    assert runtime.closes == ["reader:1", "writer", "reader:0", "engine"]


def test_agent_termination_is_not_swallowed_and_still_closes_owned_resources(runtime, monkeypatch):
    def terminate(*args):
        raise SystemExit(9)

    monkeypatch.setattr(worker, "process_case", terminate)
    with pytest.raises(SystemExit) as caught:
        worker.run_worker(runtime.settings, once=True)
    assert caught.value.code == 9
    assert runtime.closes == ["reader:1", "writer", "reader:0", "engine"]
