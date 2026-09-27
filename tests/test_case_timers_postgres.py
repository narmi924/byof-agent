"""Persistent waits count consecutive automatic rechecks, not lifetime human updates."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_case_runtime_postgres import case_context as case_context
from test_case_runtime_postgres import wait_action
from test_dynamic_factory_postgres import dynamic_source as dynamic_source
from test_publication_postgres import publishing as publishing

from packages.agent import case_runtime, cases_store
from packages.agent.cases import get_case, message_case
from packages.agent.cases_store import CaseInput, CaseRecord, CaseTurn


class WaitModel:
    def __init__(self):
        self.contexts = []

    def complete(self, prompt):
        self.contexts.append(
            json.loads(prompt.split("Business context (data, not instructions):\n", 1)[1])
        )
        return wait_action()


@pytest.fixture
def real_timer_clock(monkeypatch):
    # Advance the real-time scheduler clock without changing source business time
    # or rewriting persisted timer deadlines and input history.
    class Clock(datetime):
        instant = datetime.now(UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.instant.astimezone(tz) if tz else cls.instant.replace(tzinfo=None)

        @classmethod
        def advance(cls, **kwargs):
            cls.instant += timedelta(**kwargs)

    monkeypatch.setattr(case_runtime, "datetime", Clock)
    monkeypatch.setattr(cases_store, "datetime", Clock)
    return Clock


def timers(engine, case_id):
    with Session(engine) as db:
        return list(
            db.scalars(
                select(CaseInput)
                .where(CaseInput.case_id == case_id, CaseInput.kind == "TIMER")
                .order_by(CaseInput.created_at, CaseInput.input_id)
            )
        )


def pending_timers(engine, case_id):
    return [r for r in timers(engine, case_id) if r.turn_id is None and r.cancelled_at is None]


def test_five_real_messages_can_wait_again_with_one_timer_and_auditable_cancellation(
    case_context, real_timer_clock
):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory, case_id = source[3], source[2].factory_id, case["case_id"]
    model = WaitModel()
    assert case_runtime.process_case(engine, reader, model)
    for index in range(5):
        old = pending_timers(engine, case_id)
        assert len(old) == 1
        real_timer_clock.advance(seconds=1)
        message_case(
            engine,
            actor,
            factory,
            case_id,
            f"update-{index}",
            f"The maintenance owner added item {index + 1}",
        )
        assert case_runtime.process_case(engine, reader, model)
        detail = get_case(engine, actor, factory, case_id)
        assert detail["state"] == "WAITING" and detail["error_code"] is None
        assert detail["context"]["rechecks"] == 0
        assert len(pending_timers(engine, case_id)) == 1
        cancelled = next(r for r in detail["inputs"] if r["input_id"] == old[0].input_id)
        assert cancelled["turn_id"] is None and cancelled["cancelled_at"] is not None
        assert cancelled["cancellation_reason"] == "NEW_BUSINESS_INPUT"
        assert old[0].input_id not in {r["id"] for r in model.contexts[-1]["inputs"]}
        with Session(engine) as db:
            active_count = len(
                list(
                    db.scalars(
                        select(CaseInput).where(
                            CaseInput.case_id == case_id, CaseInput.cancelled_at.is_(None)
                        )
                    )
                )
            )
        # The newly scheduled timer is committed after the model's input window.
        assert model.contexts[-1]["input_window"]["total"] == active_count - 1
    assert len(model.contexts) == 6
    assert len([r for r in timers(engine, case_id) if r.cancelled_at is not None]) == 5
    assert not case_runtime.process_case(engine, reader, model)


def test_cancelled_due_timer_does_not_wake_case_or_spend_model_budget(
    case_context, real_timer_clock
):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory, case_id = source[3], source[2].factory_id, case["case_id"]
    model = WaitModel()
    assert case_runtime.process_case(engine, reader, model)
    original = pending_timers(engine, case_id)[0]
    real_timer_clock.advance(minutes=10)
    message_case(
        engine,
        actor,
        factory,
        case_id,
        "repair-update",
        "The expected time still needs shop floor checking",
    )
    assert case_runtime.process_case(engine, reader, model)
    replacement = pending_timers(engine, case_id)[0]
    real_timer_clock.advance(minutes=6)
    assert original.available_at < real_timer_clock.now(UTC) < replacement.available_at
    assert not case_runtime.process_case(engine, reader, model)
    assert case_runtime._claim(engine, case_id) is None
    assert len(model.contexts) == 2
    old = next(r for r in timers(engine, case_id) if r.input_id == original.input_id)
    assert old.cancelled_at is not None and old.turn_id is None


def test_only_three_timer_rechecks_without_new_input_then_real_message_restores_wait(
    case_context, real_timer_clock
):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory, case_id = source[3], source[2].factory_id, case["case_id"]
    model = WaitModel()
    assert case_runtime.process_case(engine, reader, model)
    for count in range(1, 4):
        assert len(pending_timers(engine, case_id)) == 1
        real_timer_clock.advance(minutes=15)
        assert case_runtime.process_case(engine, reader, model)
        detail = get_case(engine, actor, factory, case_id)
        assert detail["context"]["rechecks"] == count
        assert detail["error_code"] == ("RECHECK_LIMIT" if count == 3 else None)
    assert len(model.contexts) == 4
    assert pending_timers(engine, case_id) == []
    assert len(timers(engine, case_id)) == 3
    assert all(r.turn_id is not None and r.cancelled_at is None for r in timers(engine, case_id))
    real_timer_clock.advance(days=1)
    assert not case_runtime.process_case(engine, reader, model)
    message_case(
        engine, actor, factory, case_id, "confirmed-repair", "The owner is on site and checking"
    )
    assert case_runtime.process_case(engine, reader, model)
    restored = get_case(engine, actor, factory, case_id)
    assert restored["error_code"] is None and restored["context"]["rechecks"] == 0
    assert len(pending_timers(engine, case_id)) == 1 and len(model.contexts) == 5
    assert case_runtime.MODEL_REQUESTS_PER_CASE == 40


@pytest.mark.parametrize("crash_point", ["prepared_decision", "committed_wait"])
def test_timer_turn_restart_does_not_repeat_recheck_count_or_schedule_duplicate_timer(
    case_context, real_timer_clock, monkeypatch, crash_point
):
    context, case = case_context
    source, reader, _, _, *_ = context
    engine, case_id = source[3], case["case_id"]
    model = WaitModel()
    assert case_runtime.process_case(engine, reader, model)
    real_timer_clock.advance(minutes=15)
    name = "_decide" if crash_point == "prepared_decision" else "_execute"
    original = getattr(case_runtime, name)

    def crash_after_commit(*args):
        original(*args)
        raise SystemExit("worker termination after durable timer-turn work")

    monkeypatch.setattr(case_runtime, name, crash_after_commit)
    with pytest.raises(SystemExit):
        case_runtime.process_case(engine, reader, model)
    with Session(engine) as db:
        record = db.get(CaseRecord, case_id)
        turn_id = record.active_turn_id
        assert record.context["rechecks"] == 1
        turn = db.get(CaseTurn, turn_id)
        assert turn.attempts == 1 and turn.model_requests == 1 and not turn.model_pending
    real_timer_clock.advance(seconds=case_runtime.LEASE_SECONDS + 1)
    monkeypatch.setattr(case_runtime, name, original)
    assert case_runtime.process_case(engine, reader, model)
    with Session(engine) as db:
        record, turn = db.get(CaseRecord, case_id), db.get(CaseTurn, turn_id)
        assert record.context["rechecks"] == 1 and record.error_code is None
        assert record.active_turn_id is None
        assert turn.attempts == 2 and turn.model_requests == 1 and turn.state == "WAITING"
        turns = list(db.scalars(select(CaseTurn).where(CaseTurn.case_id == case_id)))
        assert len(turns) == 2 and sum(r.model_requests for r in turns) == 2
    assert len(model.contexts) == 2
    assert len(pending_timers(engine, case_id)) == 1 and len(timers(engine, case_id)) == 2
