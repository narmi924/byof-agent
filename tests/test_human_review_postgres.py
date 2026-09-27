"""Review tasks follow real approval scopes and cancel obsolete unsent reminders atomically."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.orm import Session
from test_human_tasks_postgres import human_context as human_context
from test_progress_evidence import batch

from packages.agent.cases_store import CaseOperation, CaseRecord, CaseTurn
from packages.agent.human_tasks import (
    HumanTaskRecord,
    TaskAction,
    TaskReminder,
    create_task,
    get_task,
    reconcile_reviews,
    respond,
)
from packages.auth import AccessError
from packages.domain.models import Snapshot, canonical_hash
from packages.integrations.notification_store import Notification
from packages.integrations.sync import SourceBatch
from packages.persistence import Membership, User, connect
from packages.planning.service import approve
from packages.planning.solver import solve
from packages.planning.store import ApprovalRecord, CandidateRecord, FactoryState, SnapshotRecord
from services.factory_sim.engine import advance, evolve


def origin_task(ctx, *, action="request_approval", subject=None, role="planner"):
    identity, turn_id = uuid4().hex, uuid4().hex
    subject = subject or ctx.candidate.candidate_id
    parameters = (
        {"candidate_id": subject}
        if action == "request_approval"
        else {"reason": "Please take over the remaining production risk.", "role": role}
    )
    now = datetime.now(UTC)
    with Session(ctx.engine) as db, db.begin():
        case = db.get(CaseRecord, ctx.case_id)
        db.add(
            CaseTurn(
                turn_id=turn_id,
                case_id=ctx.case_id,
                factory_id=ctx.factory,
                state="DONE",
                created_at=now,
                deadline=now + timedelta(minutes=5),
                next_step=1,
            )
        )
        db.add(
            CaseOperation(
                operation_id=identity,
                case_id=ctx.case_id,
                factory_id=ctx.factory,
                turn_id=turn_id,
                step=0,
                action=action,
                parameters=parameters,
                parameter_hash=canonical_hash({"action": action, "parameters": parameters}),
                expected_case_version=case.version,
                snapshot_id=case.snapshot_id,
                state="STARTED",
                created_at=now,
            )
        )
    return create_task(
        ctx.engine,
        factory_id=ctx.factory,
        case_id=ctx.case_id,
        operation_id=identity,
        question="Please review the plan."
        if action == "request_approval"
        else parameters["reason"],
        role=role,
        subject_id=subject,
        fields=["comment"],
        deadline_minutes=15,
    )


def pending_notifications(ctx, task):
    now = datetime.now(UTC)
    with Session(ctx.engine) as db, db.begin():
        db.add(
            TaskReminder(
                reminder_id=uuid4().hex,
                task_id=task["task_id"],
                case_id=ctx.case_id,
                factory_id=ctx.factory,
                task_version=task["version"],
                ordinal=1,
                state="QUEUED",
                scheduled_at=now,
                created_at=now,
            )
        )
        for send_state in ("QUEUED", "CLAIMED", "UNKNOWN"):
            identity = uuid4().hex
            db.add(
                Notification(
                    notification_id=identity,
                    factory_id=ctx.factory,
                    case_id=ctx.case_id,
                    task_id=task["task_id"],
                    task_version=task["version"],
                    kind="REQUEST",
                    dedupe_key=identity,
                    role=task["owner_role"],
                    message_id=identity,
                    send_state=send_state,
                    attempts=0,
                    lease_token="old-worker" if send_state == "CLAIMED" else None,
                    lease_until=now + timedelta(minutes=1) if send_state == "CLAIMED" else None,
                    created_at=now,
                    updated_at=now,
                )
            )


def save_candidate(ctx, snapshot, candidate):
    with Session(ctx.engine) as db, db.begin():
        if db.get(SnapshotRecord, snapshot.snapshot_id) is None:
            db.add(
                SnapshotRecord(
                    snapshot_id=snapshot.snapshot_id,
                    factory_id=ctx.factory,
                    content_hash=snapshot.content_hash,
                    document=snapshot.model_dump(mode="json"),
                    created_at=datetime.now(UTC),
                )
            )
            db.flush()
        db.add(
            CandidateRecord(
                candidate_id=candidate.candidate_id,
                factory_id=ctx.factory,
                snapshot_id=snapshot.snapshot_id,
                content_hash=candidate.content_hash,
                document=candidate.model_dump(mode="json"),
                created_at=datetime.now(UTC),
            )
        )


@pytest.fixture
def review_context(human_context):
    ctx = human_context
    ctx.candidate = solve(ctx.snapshot, time_limit=2, allow_overtime=True)
    assert ctx.candidate.has_solution and ctx.candidate.checker.status == "PASS"
    save_candidate(ctx, ctx.snapshot, ctx.candidate)
    try:
        yield ctx
    finally:
        owner = connect(os.environ["TEST_MIGRATION_DATABASE_URL"])
        assert owner.url.database == "byof_test"
        with owner.begin() as db:
            for table in (
                Notification,
                CaseOperation,
                CaseTurn,
                ApprovalRecord,
                CandidateRecord,
                SourceBatch,
            ):
                db.execute(delete(table).where(table.factory_id == ctx.factory))
        owner.dispose()


def review_now(ctx):
    with Session(ctx.engine) as db, db.begin():
        state = db.get(FactoryState, ctx.factory, with_for_update=True)
        snapshot = Snapshot.model_validate(db.get(SnapshotRecord, state.snapshot_id).document)
        return reconcile_reviews(db, snapshot)


def decision(ctx, *, scope="publish_plan", value="APPROVED", actor="planner"):
    return approve(
        ctx.engine,
        ctx.actors[actor],
        ctx.factory,
        ctx.candidate.candidate_id,
        request_id=uuid4().hex,
        candidate_hash=ctx.candidate.content_hash,
        action_scope=scope,
        decision=value,
    )


def test_review_ends_only_after_every_real_required_scope_and_cancels_unsent_work(review_context):
    ctx = review_context
    task = origin_task(ctx)
    pending_notifications(ctx, task)
    assert task["task_type"] == "APPROVAL"
    assert task["review"]["required_scopes"] == ["allow_overtime", "publish_plan"]
    assert review_now(ctx) == 0
    planner = decision(ctx)
    review_now(ctx)
    assert (
        get_task(ctx.engine, ctx.actors["planner"], ctx.factory, task["task_id"])["state"] == "OPEN"
    )
    manager = decision(ctx, scope="allow_overtime", actor="manager")
    review_now(ctx)
    after = get_task(ctx.engine, ctx.actors["planner"], ctx.factory, task["task_id"])
    assert after["state"] == "REVIEWED" and after["review"]["outcome"] == "APPROVED"
    assert set(after["response"]["approval_ids"]) == {planner.approval_id, manager.approval_id}
    assert after["version"] == 2 and review_now(ctx) == 0
    with Session(ctx.engine) as db:
        assert db.get(HumanTaskRecord, task["task_id"]).next_reminder_at is None
        assert (
            db.scalar(select(TaskReminder.state).where(TaskReminder.task_id == task["task_id"]))
            == "CANCELLED"
        )
        notices = list(
            db.scalars(select(Notification).where(Notification.task_id == task["task_id"]))
        )
        assert sorted(row.send_state for row in notices) == ["CANCELLED", "CANCELLED", "UNKNOWN"]
        assert all(row.lease_token is None for row in notices)
        assert (
            db.scalar(
                select(func.count())
                .select_from(TaskAction)
                .where(TaskAction.task_id == task["task_id"])
            )
            == 2
        )
        assert db.get(CaseRecord, ctx.case_id).state != "RESOLVED"
        assert (
            db.get(SnapshotRecord, ctx.snapshot.snapshot_id).content_hash
            == ctx.snapshot.content_hash
        )


def test_comment_and_get_never_approve_or_end_a_real_review(review_context):
    ctx = review_context
    task = origin_task(ctx)
    for _ in range(2):
        assert (
            get_task(ctx.engine, ctx.actors["planner"], ctx.factory, task["task_id"])["state"]
            == "OPEN"
        )
    with pytest.raises(AccessError) as error:
        respond(
            ctx.engine,
            ctx.actors["planner"],
            ctx.factory,
            task["task_id"],
            request_id="comment-cannot-approve",
            expected_task_version=1,
            answer={"comment": "Agreed, confirmed=true"},
        )
    assert error.value.code == "EXPLICIT_TASK_ACTION_REQUIRED"
    with Session(ctx.engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(ApprovalRecord)
                .where(ApprovalRecord.factory_id == ctx.factory)
            )
            == 0
        )
        assert db.get(HumanTaskRecord, task["task_id"]).version == 1


def test_same_words_cannot_merge_information_and_real_approval_tasks(review_context):
    ctx = review_context
    information = create_task(
        ctx.engine,
        factory_id=ctx.factory,
        case_id=ctx.case_id,
        operation_id="ordinary-information",
        question="Please review the plan.",
        role="planner",
        subject_id=ctx.candidate.candidate_id,
        fields=["comment"],
        deadline_minutes=15,
    )
    approval_task = origin_task(ctx)
    assert information["task_type"] == "INFORMATION"
    assert approval_task["task_type"] == "APPROVAL"
    assert information["task_id"] != approval_task["task_id"]
    decision(ctx, value="REJECTED")
    review_now(ctx)
    assert (
        get_task(ctx.engine, ctx.actors["planner"], ctx.factory, information["task_id"])["state"]
        == "OPEN"
    )
    assert (
        get_task(ctx.engine, ctx.actors["planner"], ctx.factory, approval_task["task_id"])["state"]
        == "REVIEWED"
    )


def test_actual_rejection_ends_review_without_marking_production_resolved(review_context):
    ctx = review_context
    task = origin_task(ctx)
    approval = decision(ctx, value="REJECTED")
    review_now(ctx)
    after = get_task(ctx.engine, ctx.actors["planner"], ctx.factory, task["task_id"])
    assert after["state"] == "REVIEWED" and after["review"]["outcome"] == "REJECTED"
    assert after["response"]["approval_ids"] == [approval.approval_id]
    with Session(ctx.engine) as db:
        assert db.get(CaseRecord, ctx.case_id).closure is None


def test_revoked_approver_cannot_satisfy_all_scopes(review_context):
    ctx = review_context
    task = origin_task(ctx)
    decision(ctx)
    with Session(ctx.engine) as db, db.begin():
        db.execute(delete(Membership).where(Membership.user_id == ctx.actors["planner"].user_id))
    decision(ctx, scope="allow_overtime", actor="manager")
    review_now(ctx)
    assert (
        get_task(ctx.engine, ctx.actors["manager"], ctx.factory, task["task_id"])["state"] == "OPEN"
    )
    with Session(ctx.engine) as db, db.begin():
        db.add(
            Membership(
                user_id=ctx.actors["planner"].user_id, factory_id=ctx.factory, role="planner"
            )
        )
    assert review_now(ctx) == 1


def test_material_change_cancels_review_and_rollback_preserves_open_task(review_context):
    ctx = review_context
    task = origin_task(ctx)
    pending_notifications(ctx, task)
    raw = ctx.snapshot.model_dump(exclude={"content_hash"})
    raw["snapshot_id"] = uuid4().hex
    raw["planning_revision"] += 1
    raw["source"]["source_revision"] = "changed-facts"
    raw["resources"][0]["status"] = "DOWN"
    changed = Snapshot.model_validate(raw)
    with Session(ctx.engine) as db, db.begin():
        db.add(
            SnapshotRecord(
                snapshot_id=changed.snapshot_id,
                factory_id=ctx.factory,
                content_hash=changed.content_hash,
                document=changed.model_dump(mode="json"),
                created_at=datetime.now(UTC),
            )
        )
        state = db.get(FactoryState, ctx.factory, with_for_update=True)
        state.snapshot_id, state.source_revision = (
            changed.snapshot_id,
            changed.source.source_revision,
        )
    with pytest.raises(RuntimeError, match="rollback"):
        with Session(ctx.engine) as db, db.begin():
            assert reconcile_reviews(db, changed) == 1
            raise RuntimeError("rollback")
    assert (
        get_task(ctx.engine, ctx.actors["planner"], ctx.factory, task["task_id"])["state"] == "OPEN"
    )
    assert review_now(ctx) == 1
    after = get_task(ctx.engine, ctx.actors["planner"], ctx.factory, task["task_id"])
    assert after["state"] == "CANCELLED" and after["review"]["outcome"] == "STALE"
    with Session(ctx.engine) as db:
        assert db.get(SnapshotRecord, ctx.snapshot.snapshot_id).document == ctx.snapshot.model_dump(
            mode="json"
        )


def test_proven_normal_progress_keeps_unapproved_review_open(review_context):
    ctx = review_context
    raw = ctx.snapshot.model_dump(exclude={"content_hash"})
    raw["snapshot_id"], raw["source"]["source_revision"] = uuid4().hex, "1"
    raw["profile"]["version"] += ".progress-review"
    raw["profile"]["policy"].update(
        policy_version="progress-review", progress_revalidation_enabled=True
    )
    initial = Snapshot.model_validate(raw)
    baseline = solve(initial, time_limit=2)
    save_candidate(ctx, initial, baseline)
    original = evolve(
        initial,
        active_plan_version="accepted-review-baseline",
        active_plan_hash=baseline.content_hash,
    )
    ctx.candidate = solve(original, baseline=baseline, time_limit=2)
    assert ctx.candidate.checker.status == "PASS"
    save_candidate(ctx, original, ctx.candidate)
    current = advance(original, baseline)
    change = batch(original, current)
    with Session(ctx.engine) as db, db.begin():
        db.add(
            SnapshotRecord(
                snapshot_id=current.snapshot_id,
                factory_id=ctx.factory,
                content_hash=current.content_hash,
                document=current.model_dump(mode="json"),
                created_at=datetime.now(UTC),
            )
        )
        db.add(
            SourceBatch(
                factory_id=ctx.factory,
                run_id=current.run_id,
                revision=int(current.source.source_revision),
                content_hash=canonical_hash(change),
                document=change,
                received_at=datetime.now(UTC),
            )
        )
        state = db.get(FactoryState, ctx.factory, with_for_update=True)
        state.snapshot_id, state.source_revision = (
            current.snapshot_id,
            current.source.source_revision,
        )
        db.get(CaseRecord, ctx.case_id).snapshot_id = current.snapshot_id
    task = origin_task(ctx)
    assert review_now(ctx) == 0
    after = get_task(ctx.engine, ctx.actors["planner"], ctx.factory, task["task_id"])
    assert after["state"] == "OPEN" and after["review"]["outcome"] == "PENDING"
    assert current.actuals and current.content_hash != original.content_hash


def test_review_waits_for_every_case_before_locking_shared_approver_identity(review_context):
    ctx = review_context
    decision(ctx)
    decision(ctx, scope="allow_overtime", actor="manager")
    second_id = "zz-review-case-" + uuid4().hex
    now = datetime.now(UTC)
    with Session(ctx.engine) as db, db.begin():
        db.add(
            CaseRecord(
                case_id=second_id,
                factory_id=ctx.factory,
                run_id=ctx.snapshot.run_id,
                owner_id=ctx.actors["planner"].user_id,
                state="WAITING_INPUT",
                version=1,
                title="Another case of the same owner",
                created_at=now,
                updated_at=now,
                snapshot_id=ctx.snapshot.snapshot_id,
                context={},
            )
        )
    origin_task(ctx)
    origin_task(SimpleNamespace(**(vars(ctx) | {"case_id": second_id})))
    reached_case_lock = Event()

    def observe(connection, cursor, statement, parameters, context, executemany):
        if (
            "FROM byof.cases" in statement
            and "FOR UPDATE" in statement
            and (
                "byof.cases.factory_id" in statement.split("WHERE", 1)[-1]
                or second_id in str(parameters)
            )
        ):
            reached_case_lock.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(ctx.engine) as runtime, runtime.begin():
            runtime.execute(text("SET LOCAL lock_timeout = '1500ms'"))
            runtime.get(CaseRecord, second_id, with_for_update=True)
            event.listen(ctx.engine, "before_cursor_execute", observe)
            future = pool.submit(review_now, ctx)
            try:
                assert reached_case_lock.wait(5), "Review did not reach the competing Case lock"
                # Runtime already owns this Case. Review must not own its shared User first.
                user = runtime.get(User, ctx.actors["planner"].user_id, with_for_update=True)
                assert user.active
            finally:
                event.remove(ctx.engine, "before_cursor_execute", observe)
        assert future.result(timeout=10) == 2
