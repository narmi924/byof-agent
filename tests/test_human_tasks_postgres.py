"""Human information and reminder state are serialized in real PostgreSQL transactions."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from packages.agent.cases_store import CaseInput, CaseRecord
from packages.agent.human_tasks import (
    HumanTaskRecord,
    TaskAction,
    TaskReminder,
    cancel,
    create_task,
    get_task,
    list_tasks,
    respond,
    tick_reminders,
    transfer,
)
from packages.auth import AccessError, Grant, Principal
from packages.domain.models import Snapshot
from packages.domain.skf import load_skf_snapshot
from packages.persistence import Membership, User, connect
from packages.planning.store import FactoryState, SnapshotRecord


@pytest.fixture
def human_context():
    app_url, owner_url = os.getenv("TEST_DATABASE_URL"), os.getenv("TEST_MIGRATION_DATABASE_URL")
    if not app_url or not owner_url:
        pytest.skip("Explicit PostgreSQL application and owner test URLs required")
    engine, owner = connect(app_url), connect(owner_url)
    assert engine.url.database == owner.url.database == "byof_test"
    factory, other_factory = "human-" + uuid4().hex, "other-" + uuid4().hex
    actors = {}
    now = datetime.now(UTC)
    case_id = "case-" + uuid4().hex
    raw = load_skf_snapshot(development=True).model_dump(mode="json", exclude={"content_hash"})
    raw["factory_id"] = raw["profile"]["factory_id"] = factory
    raw["snapshot_id"], raw["run_id"] = uuid4().hex, uuid4().hex
    snapshot = Snapshot.model_validate(raw)
    with Session(engine) as db, db.begin():
        db.add(
            SnapshotRecord(
                snapshot_id=snapshot.snapshot_id,
                factory_id=factory,
                content_hash=snapshot.content_hash,
                document=snapshot.model_dump(mode="json"),
                created_at=now,
            )
        )
        db.add(
            FactoryState(
                factory_id=factory,
                snapshot_id=snapshot.snapshot_id,
                run_id=snapshot.run_id,
                source_revision=snapshot.source.source_revision,
                last_synced_at=now,
            )
        )
        for name, role in (
            ("planner", "planner"),
            ("maintainer", "maintainer"),
            ("other_maintainer", "maintainer"),
            ("warehouse", "warehouse"),
            ("manager", "manager"),
            ("admin", "admin"),
            ("outsider", "maintainer"),
        ):
            user_id = name + "-" + uuid4().hex
            scope = other_factory if name == "outsider" else factory
            db.add(
                User(user_id=user_id, username=user_id, password_hash="not-a-login", active=True)
            )
            db.flush()
            db.add(Membership(user_id=user_id, factory_id=scope, role=role))
            actors[name] = Principal(
                user_id=user_id, username=user_id, grants=(Grant(factory_id=scope, role=role),)
            )
        db.add(
            CaseRecord(
                case_id=case_id,
                factory_id=factory,
                run_id=snapshot.run_id,
                owner_id=actors["planner"].user_id,
                state="WAITING_INPUT",
                version=1,
                title="Check the machine recovery time",
                created_at=now,
                updated_at=now,
                active_turn_id=None,
                snapshot_id=snapshot.snapshot_id,
                context={},
                closure=None,
                error_code=None,
            )
        )
    try:
        yield SimpleNamespace(
            engine=engine, factory=factory, case_id=case_id, actors=actors, snapshot=snapshot
        )
    finally:
        with owner.begin() as db:
            for table in (
                TaskReminder,
                TaskAction,
                HumanTaskRecord,
                CaseInput,
                CaseRecord,
                FactoryState,
                SnapshotRecord,
            ):
                db.execute(delete(table).where(table.factory_id == factory))
            for actor in actors.values():
                db.execute(delete(Membership).where(Membership.user_id == actor.user_id))
                db.execute(delete(User).where(User.user_id == actor.user_id))
        engine.dispose()
        owner.dispose()


def task(context, operation_id="ask", **overrides):
    return create_task(
        context.engine,
        **{
            "case_id": context.case_id,
            "factory_id": context.factory,
            "operation_id": operation_id,
            "question": "Please provide the machine recovery time.",
            "role": "maintainer",
            "subject_id": context.snapshot.resources[0].resource_id,
            "fields": ["repair_eta"],
            "deadline_minutes": 15,
            **overrides,
        },
    )


def reply(context, record, request_id="reply", actor="maintainer", answer=None, version=None):
    return respond(
        context.engine,
        context.actors[actor],
        context.factory,
        record["task_id"],
        request_id=request_id,
        expected_task_version=record["version"] if version is None else version,
        answer={"repair_eta": "2026-09-14T10:30:00+08:00"} if answer is None else answer,
    )


def count(context, model):
    with Session(context.engine) as db:
        return db.scalar(
            select(func.count()).select_from(model).where(model.factory_id == context.factory)
        )


def due_now(context, record):
    with Session(context.engine) as db, db.begin():
        row = db.get(HumanTaskRecord, record["task_id"], with_for_update=True)
        row.next_reminder_at = datetime.now(UTC) - timedelta(seconds=1)


def test_creation_deduplicates_concurrent_questions_and_retains_operation_identity(human_context):
    ctx = human_context
    with ThreadPoolExecutor(max_workers=4) as pool:
        created = list(pool.map(lambda index: task(ctx, f"operation-{index}"), range(4)))
    assert len({row["task_id"] for row in created}) == count(ctx, HumanTaskRecord) == 1
    assert count(ctx, TaskAction) == 4
    record = created[0]
    assert record["state"] == "OPEN" and record["version"] == 1
    assert record["send_state"] == "NOT_ENABLED" and record["delivery_state"] == "UNAVAILABLE"
    assert record["clock"] == "real" and record["owner_id"] is None
    assert datetime.fromisoformat(record["due_at"]) > datetime.now(UTC)
    reply(ctx, record)
    repeated = task(ctx, "operation-1")
    assert repeated["task_id"] == record["task_id"] and repeated["state"] == "RESPONDED"
    assert task(ctx, "new-question-after-response")["task_id"] != record["task_id"]
    with pytest.raises(AccessError) as conflict:
        task(ctx, "operation-1", question="Another question")
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"


def test_get_and_role_scoped_list_do_not_acknowledge_or_mutate_a_task(human_context):
    ctx = human_context
    record = task(ctx)
    task(ctx, "warehouse-task", role="warehouse", fields=["receipt_eta"])
    before = get_task(ctx.engine, ctx.actors["maintainer"], ctx.factory, record["task_id"])
    assert get_task(ctx.engine, ctx.actors["maintainer"], ctx.factory, record["task_id"]) == before
    assert len(list_tasks(ctx.engine, ctx.actors["planner"], ctx.factory)) == 2
    assert len(list_tasks(ctx.engine, ctx.actors["manager"], ctx.factory)) == 2
    assert len(list_tasks(ctx.engine, ctx.actors["maintainer"], ctx.factory)) == 1
    assert count(ctx, TaskAction) == 2 and count(ctx, CaseInput) == count(ctx, TaskReminder) == 0
    with Session(ctx.engine) as db:
        assert db.get(CaseRecord, ctx.case_id).version == 1
    for actor in ("admin", "outsider", "warehouse"):
        with pytest.raises(AccessError):
            get_task(ctx.engine, ctx.actors[actor], ctx.factory, record["task_id"])


def test_reply_records_authenticated_information_once_without_claiming_verified_facts(
    human_context,
):
    ctx = human_context
    record = task(
        ctx, fields=["repair_eta", "remaining_minutes", "remaining_setup_minutes", "comment"]
    )
    answer = {
        "repair_eta": "2026-09-14T10:30:00+08:00",
        "remaining_minutes": 12,
        "remaining_setup_minutes": 0,
        "comment": "Reported after a shop floor recheck.",
    }
    responded = reply(ctx, record, answer=answer)
    assert responded["state"] == "RESPONDED" and responded["version"] == 2
    assert responded["response"]["source"] == "authenticated_human_information"
    assert responded["response"]["actor_id"] == ctx.actors["maintainer"].user_id
    assert responded["response"]["answer"]["repair_eta"] == "2026-09-14T02:30:00+00:00"
    assert responded["response"]["answer"]["remaining_setup_minutes"] == 0
    assert "confirmed" not in responded["response"]
    assert reply(ctx, record, answer=answer) == responded
    assert count(ctx, CaseInput) == 1 and count(ctx, TaskAction) == 2
    with Session(ctx.engine) as db:
        event = db.scalar(select(CaseInput).where(CaseInput.case_id == ctx.case_id))
        assert (
            event.kind == "human_task.responded" and event.payload["task_id"] == record["task_id"]
        )
        assert event.payload["response"] == responded["response"]
        assert db.get(CaseRecord, ctx.case_id).version == 2
    with pytest.raises(AccessError) as conflict:
        reply(ctx, record, answer={**answer, "remaining_minutes": 99})
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize(
    "answer",
    [
        {"remaining_minutes": True},
        {"remaining_minutes": -1},
        {"remaining_minutes": 1.5},
        {"remaining_minutes": "12"},
        {"remaining_minutes": None},
        {},
        {"remaining_minutes": 12, "confirmed": True},
    ],
)
def test_invalid_remaining_or_model_confirmation_never_records_a_response(human_context, answer):
    ctx = human_context
    record = task(ctx, fields=["remaining_minutes"])
    with pytest.raises(AccessError) as invalid:
        reply(ctx, record, answer=answer)
    assert invalid.value.status == 422
    assert (
        get_task(ctx.engine, ctx.actors["planner"], ctx.factory, record["task_id"])["state"]
        == "OPEN"
    )
    assert count(ctx, CaseInput) == 0 and count(ctx, TaskAction) == 1


def test_naive_time_and_stale_task_version_are_rejected(human_context):
    ctx = human_context
    record = task(ctx)
    with pytest.raises(AccessError) as naive:
        reply(ctx, record, answer={"repair_eta": "2026-09-14T10:30:00"})
    assert naive.value.code == "INVALID_TASK_ANSWER"
    with pytest.raises(AccessError) as stale:
        reply(ctx, record, version=2)
    assert stale.value.code == "TASK_VERSION_CHANGED"
    assert count(ctx, CaseInput) == 0


def test_live_membership_and_account_rechecked_even_with_a_cached_principal(human_context):
    ctx = human_context
    record = task(ctx)
    for actor in ("planner", "manager", "admin", "outsider", "warehouse"):
        with pytest.raises(AccessError):
            reply(ctx, record, request_id="forbidden-" + actor, actor=actor)
    with ctx.engine.begin() as db:
        db.execute(delete(Membership).where(Membership.user_id == ctx.actors["maintainer"].user_id))
    with pytest.raises(AccessError) as revoked:
        reply(ctx, record)
    assert revoked.value.code == "FORBIDDEN"
    with Session(ctx.engine) as db, db.begin():
        db.get(User, ctx.actors["other_maintainer"].user_id).active = False
    with pytest.raises(AccessError):
        reply(ctx, record, actor="other_maintainer")
    assert count(ctx, CaseInput) == 0


def test_concurrent_distinct_replies_have_one_winner_and_no_lost_answer(human_context):
    ctx = human_context
    record = task(ctx, fields=["remaining_minutes"])

    def answer(index):
        try:
            return reply(
                ctx, record, request_id=f"reply-{index}", answer={"remaining_minutes": index + 10}
            )
        except AccessError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(answer, range(2)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "TASK_VERSION_CHANGED" in results
    saved = get_task(ctx.engine, ctx.actors["planner"], ctx.factory, record["task_id"])
    assert saved["response"]["answer"]["remaining_minutes"] in (10, 11)
    assert count(ctx, CaseInput) == 1 and count(ctx, TaskAction) == 2


def test_transfer_preserves_history_cancels_old_reminders_and_retries_without_reverting(
    human_context,
):
    ctx = human_context
    record = task(ctx)
    due_now(ctx, record)
    assert tick_reminders(ctx.engine) == 1
    args = {
        "request_id": "transfer",
        "expected_task_version": 1,
        "target_role": "warehouse",
        "target_owner_id": ctx.actors["warehouse"].user_id,
        "reason": "Please ask the receiving owner to check.",
    }
    moved = transfer(ctx.engine, ctx.actors["planner"], ctx.factory, record["task_id"], **args)
    assert moved["version"] == 2 and moved["owner_role"] == "warehouse"
    assert moved["reminders_count"] == 1
    with pytest.raises(AccessError):
        reply(ctx, moved)
    with pytest.raises(AccessError):
        get_task(ctx.engine, ctx.actors["maintainer"], ctx.factory, record["task_id"])
    responded = reply(ctx, moved, actor="warehouse")
    assert responded["version"] == 3
    assert (
        transfer(ctx.engine, ctx.actors["planner"], ctx.factory, record["task_id"], **args) == moved
    )
    assert (
        get_task(ctx.engine, ctx.actors["planner"], ctx.factory, record["task_id"])["version"] == 3
    )
    with Session(ctx.engine) as db:
        reminder = db.scalar(select(TaskReminder).where(TaskReminder.task_id == record["task_id"]))
        assert reminder.state == "CANCELLED"
        events = list(db.scalars(select(CaseInput).where(CaseInput.case_id == ctx.case_id)))
        assert {event.kind for event in events} == {
            "human_task.transferred",
            "human_task.responded",
        }
        assert (
            next(e for e in events if e.kind == "human_task.transferred").payload["previous"][
                "owner_role"
            ]
            == "maintainer"
        )
    assert count(ctx, TaskAction) == 3


def test_transfer_rejects_unsupported_role_and_wrong_factory_owner(human_context):
    ctx = human_context
    record = task(ctx)
    with pytest.raises(AccessError) as role:
        transfer(
            ctx.engine,
            ctx.actors["manager"],
            ctx.factory,
            record["task_id"],
            request_id="unsupported",
            expected_task_version=1,
            target_role="admin",
            reason="Transfer",
        )
    assert role.value.status == 422
    with pytest.raises(AccessError) as outside:
        transfer(
            ctx.engine,
            ctx.actors["manager"],
            ctx.factory,
            record["task_id"],
            request_id="outside",
            expected_task_version=1,
            target_role="maintainer",
            target_owner_id=ctx.actors["outsider"].user_id,
            reason="Transfer",
        )
    assert outside.value.code == "INVALID_TASK_OWNER"
    assert count(ctx, TaskAction) == 1 and count(ctx, CaseInput) == 0


def test_reminders_have_real_intervals_two_intents_and_one_escalation(human_context):
    ctx = human_context
    record = task(ctx)
    assert tick_reminders(ctx.engine) == 0
    for ordinal in (1, 2):
        due_now(ctx, record)
        assert tick_reminders(ctx.engine) == 1
        assert tick_reminders(ctx.engine) == 0
        with Session(ctx.engine) as db:
            current = db.get(HumanTaskRecord, record["task_id"])
            assert current.reminders_count == ordinal and current.state == "OPEN"
            assert current.next_reminder_at - datetime.now(UTC) > timedelta(minutes=14)
    due_now(ctx, record)
    assert tick_reminders(ctx.engine) == 1
    assert tick_reminders(ctx.engine) == 0
    escalated = get_task(ctx.engine, ctx.actors["planner"], ctx.factory, record["task_id"])
    assert escalated["state"] == "ESCALATED" and escalated["version"] == 2
    assert escalated["reminders_count"] == count(ctx, TaskReminder) == 2
    assert count(ctx, CaseInput) == 1
    replied = reply(ctx, escalated)
    assert replied["state"] == "RESPONDED" and count(ctx, CaseInput) == 2
    with Session(ctx.engine) as db:
        assert {
            r.state
            for r in db.scalars(
                select(TaskReminder).where(TaskReminder.task_id == record["task_id"])
            )
        } == {"CANCELLED"}


def test_simultaneous_reminder_ticks_do_not_duplicate_intents(human_context):
    ctx = human_context
    record = task(ctx)
    due_now(ctx, record)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: tick_reminders(ctx.engine), range(4)))
    assert sum(results) == count(ctx, TaskReminder) == 1
    assert (
        get_task(ctx.engine, ctx.actors["planner"], ctx.factory, record["task_id"])[
            "reminders_count"
        ]
        == 1
    )


def test_cancel_wakes_case_once_and_prevents_late_response_and_unsent_reminders(human_context):
    ctx = human_context
    record = task(ctx)
    due_now(ctx, record)
    assert tick_reminders(ctx.engine) == 1
    args = {
        "request_id": "cancel",
        "expected_task_version": 1,
        "reason": "The question has a new handling basis.",
    }
    cancelled = cancel(ctx.engine, ctx.actors["planner"], ctx.factory, record["task_id"], **args)
    assert cancelled["state"] == "CANCELLED" and cancelled["version"] == 2
    assert (
        cancel(ctx.engine, ctx.actors["planner"], ctx.factory, record["task_id"], **args)
        == cancelled
    )
    with pytest.raises(AccessError) as late:
        reply(ctx, cancelled)
    assert late.value.code == "TASK_CLOSED"
    assert tick_reminders(ctx.engine) == 0
    assert count(ctx, CaseInput) == 1 and count(ctx, TaskAction) == 2
    with Session(ctx.engine) as db:
        assert (
            db.scalar(select(TaskReminder).where(TaskReminder.task_id == record["task_id"])).state
            == "CANCELLED"
        )


def test_response_retry_revalidates_identity_and_cannot_impersonate_first_responder(human_context):
    ctx = human_context
    record = task(ctx)
    reply(ctx, record)
    with pytest.raises(AccessError) as other:
        reply(ctx, record, actor="other_maintainer")
    assert other.value.code == "IDEMPOTENCY_CONFLICT"
    with ctx.engine.begin() as db:
        db.execute(delete(Membership).where(Membership.user_id == ctx.actors["maintainer"].user_id))
    with pytest.raises(AccessError) as revoked:
        reply(ctx, record)
    assert revoked.value.code == "FORBIDDEN"
    assert count(ctx, CaseInput) == 1


@pytest.mark.parametrize("terminal", ["RESOLVED", "HANDED_OFF", "CANCELLED"])
def test_finished_case_cancels_pending_tasks_before_the_next_reminder_deadline(
    human_context, terminal
):
    ctx = human_context
    record = task(ctx)
    due_now(ctx, record)
    assert tick_reminders(ctx.engine) == 1
    with Session(ctx.engine) as db, db.begin():
        db.get(CaseRecord, ctx.case_id, with_for_update=True).state = terminal
    with pytest.raises(AccessError) as closed:
        task(ctx, "new-operation", question="A new question")
    assert closed.value.code == "CASE_CLOSED"
    with pytest.raises(AccessError) as response:
        reply(ctx, record)
    assert response.value.code == "TASK_CLOSED"
    assert tick_reminders(ctx.engine) == 1
    assert tick_reminders(ctx.engine) == 0
    with Session(ctx.engine) as db:
        saved = db.get(HumanTaskRecord, record["task_id"])
        assert saved.state == "CANCELLED" and saved.next_reminder_at is None
        assert db.get(CaseRecord, ctx.case_id).state == terminal
        assert (
            db.scalar(select(TaskReminder).where(TaskReminder.task_id == record["task_id"])).state
            == "CANCELLED"
        )
    assert count(ctx, CaseInput) == 1


def test_nonfinite_json_reply_is_a_controlled_rejection_even_when_request_id_exists(human_context):
    ctx = human_context
    record = task(ctx, fields=["remaining_minutes"])
    reply(ctx, record, answer={"remaining_minutes": 10})
    with pytest.raises(AccessError) as invalid:
        reply(ctx, record, answer={"remaining_minutes": float("nan")})
    assert invalid.value.status == 422
    assert count(ctx, CaseInput) == 1


def switch_current_source(context, mode):
    raw = context.snapshot.model_dump(mode="json", exclude={"content_hash"})
    raw["snapshot_id"] = uuid4().hex
    if mode == "new_run":
        raw["run_id"] = uuid4().hex
    else:
        raw["source"]["source_system"] = "factory-simulator-replay"
    latest = Snapshot.model_validate(raw)
    with Session(context.engine) as db, db.begin():
        state = db.get(FactoryState, context.factory, with_for_update=True)
        db.add(
            SnapshotRecord(
                snapshot_id=latest.snapshot_id,
                factory_id=context.factory,
                content_hash=latest.content_hash,
                document=latest.model_dump(mode="json"),
                created_at=datetime.now(UTC),
            )
        )
        state.snapshot_id, state.run_id = latest.snapshot_id, latest.run_id
        state.source_revision = latest.source.source_revision
    return latest


@pytest.mark.parametrize(
    "mode,code", [("new_run", "SOURCE_RUN_CHANGED"), ("replay", "REPLAY_READ_ONLY")]
)
@pytest.mark.parametrize("action", ["create", "respond", "transfer", "cancel"])
def test_task_write_checks_current_run_inside_its_transaction(human_context, mode, code, action):
    ctx = human_context
    record = task(ctx)
    switch_current_source(ctx, mode)
    with pytest.raises(AccessError) as rejected:
        if action == "create":
            task(ctx, "new-question", question="Question raised after the switch")
        elif action == "respond":
            reply(ctx, record)
        elif action == "transfer":
            transfer(
                ctx.engine,
                ctx.actors["planner"],
                ctx.factory,
                record["task_id"],
                request_id="transfer-after-switch",
                expected_task_version=1,
                target_role="manager",
                reason="Transfer after the switch",
            )
        else:
            cancel(
                ctx.engine,
                ctx.actors["planner"],
                ctx.factory,
                record["task_id"],
                request_id="cancel-after-switch",
                expected_task_version=1,
                reason="Cancel after the switch",
            )
    assert rejected.value.code == code
    saved = get_task(ctx.engine, ctx.actors["planner"], ctx.factory, record["task_id"])
    assert saved["version"] == 1 and saved["state"] == "OPEN" and saved["response"] is None
    assert count(ctx, TaskAction) == 1 and count(ctx, CaseInput) == 0


@pytest.mark.parametrize(
    "mode,code", [("new_run", "SOURCE_RUN_CHANGED"), ("replay", "REPLAY_READ_ONLY")]
)
def test_source_switch_cannot_bypass_checks_via_original_request_id(human_context, mode, code):
    ctx = human_context
    record = task(ctx)
    reply(ctx, record)
    switch_current_source(ctx, mode)
    for retry in (lambda: task(ctx), lambda: reply(ctx, record)):
        with pytest.raises(AccessError) as rejected:
            retry()
        assert rejected.value.code == code
    assert count(ctx, TaskAction) == 2 and count(ctx, CaseInput) == 1


def test_old_run_reminders_are_cancelled_without_blocking_new_run_tasks(human_context):
    ctx = human_context
    old = task(ctx)
    due_now(ctx, old)
    assert tick_reminders(ctx.engine) == 1
    latest = switch_current_source(ctx, "new_run")
    new_case_id = "case-" + uuid4().hex
    now = datetime.now(UTC)
    with Session(ctx.engine) as db, db.begin():
        db.add(
            CaseRecord(
                case_id=new_case_id,
                factory_id=ctx.factory,
                run_id=latest.run_id,
                owner_id=ctx.actors["planner"].user_id,
                state="WAITING",
                version=1,
                title="Information to confirm in the new run",
                created_at=now,
                updated_at=now,
                snapshot_id=latest.snapshot_id,
                context={},
            )
        )
    new_context = SimpleNamespace(**{**vars(ctx), "case_id": new_case_id, "snapshot": latest})
    new = task(new_context, "current-run-task")
    due_now(new_context, new)
    assert tick_reminders(ctx.engine) == 2
    assert tick_reminders(ctx.engine) == 0
    with Session(ctx.engine) as db:
        stopped = db.get(HumanTaskRecord, old["task_id"])
        continuing = db.get(HumanTaskRecord, new["task_id"])
        assert stopped.state == "CANCELLED" and stopped.next_reminder_at is None
        assert continuing.state == "OPEN" and continuing.reminders_count == 1
        assert (
            db.scalar(select(TaskReminder).where(TaskReminder.task_id == old["task_id"])).state
            == "CANCELLED"
        )
        assert (
            db.scalar(select(TaskReminder).where(TaskReminder.task_id == new["task_id"])).state
            == "QUEUED"
        )
        cancellation = db.scalar(
            select(TaskAction).where(
                TaskAction.task_id == old["task_id"], TaskAction.kind == "CANCEL"
            )
        )
        assert cancellation.actor_id is None and cancellation.payload == {
            "reason": "SOURCE_RUN_CHANGED"
        }
    assert count(ctx, CaseInput) == 0


def test_replay_cancels_open_and_escalated_reminders_without_waking_cases(human_context):
    ctx = human_context
    escalated = task(ctx)
    for _ in range(3):
        due_now(ctx, escalated)
        assert tick_reminders(ctx.engine) == 1
    open_task = task(ctx, "second-question", question="Another item to confirm")
    assert count(ctx, CaseInput) == 1
    switch_current_source(ctx, "replay")
    assert tick_reminders(ctx.engine) == 2
    assert tick_reminders(ctx.engine) == 0
    with Session(ctx.engine) as db:
        for task_id in (escalated["task_id"], open_task["task_id"]):
            saved = db.get(HumanTaskRecord, task_id)
            assert saved.state == "CANCELLED" and saved.next_reminder_at is None
        assert set(
            db.scalars(select(TaskReminder.state).where(TaskReminder.factory_id == ctx.factory))
        ) == {"CANCELLED"}
    assert count(ctx, CaseInput) == 1
