"""Only explicit authenticated responsibility acceptance can hand a Case to a human."""

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from test_human_review_postgres import origin_task, pending_notifications
from test_human_tasks_postgres import human_context as human_context
from test_human_tasks_postgres import task as information_task

from packages import auth
from packages.agent.cases_store import CaseInput, CaseOperation, CaseRecord, CaseTurn, add_input
from packages.agent.human_tasks import (
    HumanTaskRecord,
    TaskAction,
    TaskReminder,
    accept_handoff,
    get_task,
    respond,
)
from packages.auth import AccessError
from packages.domain.models import Snapshot
from packages.integrations.notification_store import Notification
from packages.persistence import LoginSession, Membership, User, connect
from packages.planning.store import FactoryState, SnapshotRecord
from packages.settings import Settings
from services.api.main import create_app


@pytest.fixture
def handoff_context(human_context):
    ctx = human_context
    try:
        yield ctx
    finally:
        owner = connect(os.environ["TEST_MIGRATION_DATABASE_URL"])
        assert owner.url.database == "byof_test"
        with owner.begin() as db:
            for table in (Notification, CaseOperation, CaseTurn):
                db.execute(delete(table).where(table.factory_id == ctx.factory))
            db.execute(
                delete(LoginSession).where(
                    LoginSession.user_id.in_([a.user_id for a in ctx.actors.values()])
                )
            )
        owner.dispose()


def task(ctx, role="planner"):
    return origin_task(ctx, action="handoff", subject=ctx.case_id, role=role)


def payload(record, **changes):
    return {
        "request_id": "explicit-handoff",
        "expected_task_version": record["version"],
        "expected_case_version": record["case_version"],
        "expected_snapshot_hash": record["snapshot_hash"],
        "accept_responsibility": True,
        "accept_risks": True,
        "responsibility_summary": "I will check the repair progress and follow the next schedule.",
        "risk_summary": "The machine recovery time is not confirmed and orders still risk delay.",
        **changes,
    }


def accept(ctx, record, *, actor="planner", **changes):
    return accept_handoff(
        ctx.engine, ctx.actors[actor], ctx.factory, record["task_id"], **payload(record, **changes)
    )


def test_handoff_cancels_pending_case_timer_without_faking_a_processed_turn(handoff_context):
    ctx = handoff_context
    record = task(ctx)
    with Session(ctx.engine) as db, db.begin():
        case = db.get(CaseRecord, ctx.case_id, with_for_update=True)
        add_input(
            db,
            case,
            input_key="handoff-future-timer",
            kind="TIMER",
            payload={"reason": "recheck"},
            available_at=datetime.now(UTC) + timedelta(minutes=30),
        )
    record = get_task(ctx.engine, ctx.actors["planner"], ctx.factory, record["task_id"])
    accepted = accept(ctx, record)
    assert accepted["state"] == "ACCEPTED"
    with Session(ctx.engine) as db:
        timer = db.scalar(
            select(CaseInput).where(CaseInput.case_id == ctx.case_id, CaseInput.kind == "TIMER")
        )
        assert timer.cancelled_at is not None and timer.cancellation_reason == "CASE_HANDED_OFF"
        assert timer.turn_id is None
        assert db.get(CaseRecord, ctx.case_id).state == "HANDED_OFF"


def test_explicit_handoff_fences_agent_and_retains_risk_evidence_without_altering_facts(
    handoff_context,
):
    ctx = handoff_context
    record = task(ctx)
    other = information_task(ctx, "still-waiting-information")
    pending_notifications(ctx, record)
    pending_notifications(ctx, other)
    now, turn_id = datetime.now(UTC), uuid4().hex
    with Session(ctx.engine) as db, db.begin():
        case = db.get(CaseRecord, ctx.case_id)
        case.context = {"unknowns": ["repair_eta"], "risk": "delivery"}
        case.active_turn_id = turn_id
        db.add(
            CaseTurn(
                turn_id=turn_id,
                factory_id=ctx.factory,
                case_id=ctx.case_id,
                state="RUNNING",
                created_at=now,
                deadline=now + timedelta(minutes=2),
                lease_until=now + timedelta(minutes=1),
                lease_token="old-agent",
                model_pending=True,
            )
        )
    record = get_task(ctx.engine, ctx.actors["planner"], ctx.factory, record["task_id"])
    first = accept(ctx, record)
    assert first["state"] == "ACCEPTED" and first["task_type"] == "HANDOFF"
    assert first["response"]["outcome"] == "HANDED_OFF"
    assert first["owner_id"] == ctx.actors["planner"].user_id
    assert first["case_version"] == record["case_version"] + 1
    assert accept(ctx, record) == first
    with Session(ctx.engine) as db:
        case, turn = db.get(CaseRecord, ctx.case_id), db.get(CaseTurn, turn_id)
        assert case.state == "HANDED_OFF" and case.active_turn_id is None
        assert case.context["unknowns"] == ["repair_eta"]
        closing = case.closure["closing_evidence"]
        assert closing["snapshot_hash"] == ctx.snapshot.content_hash
        assert closing["risk_summary"] == payload(record)["risk_summary"]
        assert closing["responsibility_summary"] == payload(record)["responsibility_summary"]
        assert closing["actor_id"] == ctx.actors["planner"].user_id
        assert closing["source"] == "authenticated_human_handoff"
        assert turn.state == "CANCELLED" and turn.lease_token is None and turn.lease_until is None
        assert not turn.model_pending
        assert db.get(HumanTaskRecord, other["task_id"]).state == "CANCELLED"
        assert list(
            db.scalars(select(TaskReminder.state).where(TaskReminder.factory_id == ctx.factory))
        ) == ["CANCELLED", "CANCELLED"]
        notices = list(
            db.scalars(select(Notification).where(Notification.factory_id == ctx.factory))
        )
        assert sorted(row.send_state for row in notices) == ["CANCELLED"] * 4 + ["UNKNOWN"] * 2
        assert (
            db.scalar(
                select(func.count())
                .select_from(TaskAction)
                .where(TaskAction.kind == "ACCEPT_HANDOFF", TaskAction.factory_id == ctx.factory)
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(CaseInput)
                .where(CaseInput.factory_id == ctx.factory)
            )
            == 1
        )
        assert db.get(SnapshotRecord, ctx.snapshot.snapshot_id).document == ctx.snapshot.model_dump(
            mode="json"
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"accept_responsibility": False},
        {"accept_risks": False},
        {"accept_responsibility": "true"},
        {"accept_risks": 1},
        {"risk_summary": "  "},
        {"responsibility_summary": ""},
    ],
)
def test_handoff_requires_both_explicit_acceptances_and_meaningful_summaries(
    handoff_context, changes
):
    ctx = handoff_context
    record = task(ctx)
    with pytest.raises(AccessError):
        accept(ctx, record, **changes)
    with Session(ctx.engine) as db:
        assert db.get(CaseRecord, ctx.case_id).closure is None
        assert db.get(HumanTaskRecord, record["task_id"]).state == "OPEN"


def test_comment_or_information_task_cannot_accept_responsibility(handoff_context):
    ctx = handoff_context
    record = task(ctx)
    with pytest.raises(AccessError, match="ordinary reply"):
        respond(
            ctx.engine,
            ctx.actors["planner"],
            ctx.factory,
            record["task_id"],
            request_id="comment",
            expected_task_version=1,
            answer={"comment": "I take over"},
        )
    ordinary = information_task(ctx, role="planner", fields=["comment"])
    with pytest.raises(AccessError) as error:
        accept(ctx, ordinary)
    assert error.value.code == "HANDOFF_TASK_REQUIRED"


@pytest.mark.parametrize(
    "change,code",
    [
        ("case", "CASE_VERSION_CHANGED"),
        ("task", "TASK_VERSION_CHANGED"),
        ("snapshot", "SNAPSHOT_CHANGED"),
        ("run", "SOURCE_RUN_CHANGED"),
        ("replay", "REPLAY_READ_ONLY"),
        ("role", "FORBIDDEN"),
        ("inactive", "FORBIDDEN"),
    ],
)
def test_handoff_rechecks_versions_current_run_and_live_roles(handoff_context, change, code):
    ctx = handoff_context
    record = task(ctx)
    with Session(ctx.engine) as db, db.begin():
        case = db.get(CaseRecord, ctx.case_id, with_for_update=True)
        if change == "case":
            add_input(
                db,
                case,
                "new-urgent-order",
                "USER",
                {"message": "The new rush order must be assessed again."},
            )
        elif change == "task":
            db.get(HumanTaskRecord, record["task_id"]).version += 1
        elif change == "role":
            db.execute(
                delete(Membership).where(Membership.user_id == ctx.actors["planner"].user_id)
            )
        elif change == "inactive":
            db.get(User, ctx.actors["planner"].user_id).active = False
        else:
            raw = ctx.snapshot.model_dump(exclude={"content_hash"})
            raw["snapshot_id"] = uuid4().hex
            if change == "run":
                raw["run_id"] = "another-run"
            elif change == "replay":
                raw["source"]["source_system"] = "factory-simulator-replay"
            else:
                raw["resources"][0]["status"] = "DOWN"
            snapshot = Snapshot.model_validate(raw)
            db.add(
                SnapshotRecord(
                    snapshot_id=snapshot.snapshot_id,
                    factory_id=ctx.factory,
                    content_hash=snapshot.content_hash,
                    document=snapshot.model_dump(mode="json"),
                    created_at=datetime.now(UTC),
                )
            )
            state = db.get(FactoryState, ctx.factory, with_for_update=True)
            state.snapshot_id, state.run_id = snapshot.snapshot_id, snapshot.run_id
    with pytest.raises(AccessError) as error:
        accept(ctx, record)
    assert error.value.code == code
    with Session(ctx.engine) as db:
        assert db.get(CaseRecord, ctx.case_id).closure is None
        assert db.get(HumanTaskRecord, record["task_id"]).state == "OPEN"


def test_manager_handoff_requires_manager_and_current_assignee(handoff_context):
    ctx = handoff_context
    record = task(ctx, role="manager")
    with pytest.raises(AccessError):
        accept(ctx, record)
    assert accept(ctx, record, actor="manager")["state"] == "ACCEPTED"


@pytest.mark.parametrize("same_request", [True, False])
def test_concurrent_acceptance_commits_one_handoff_action(handoff_context, same_request):
    ctx = handoff_context
    record = task(ctx)

    def attempt(index):
        try:
            return accept(ctx, record, request_id="same" if same_request else f"request-{index}")
        except AccessError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sum(isinstance(result, dict) for result in results) == (2 if same_request else 1)
    with Session(ctx.engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(TaskAction)
                .where(TaskAction.factory_id == ctx.factory, TaskAction.kind == "ACCEPT_HANDOFF")
            )
            == 1
        )
        assert db.get(CaseRecord, ctx.case_id).version == record["case_version"] + 1


def test_http_handoff_requires_login_csrf_and_post_and_get_is_read_only(handoff_context):
    ctx = handoff_context
    record = task(ctx)
    password = "explicit-local-handoff-test"
    with Session(ctx.engine) as db, db.begin():
        db.get(User, ctx.actors["planner"].user_id).password_hash = auth.hasher.hash(password)
    settings = Settings(
        _env_file=None,
        environment="test",
        legacy_password_login_enabled=True,
        database_url=SecretStr(ctx.engine.url.render_as_string(hide_password=False)),
    )
    app = create_app(settings)
    url = f"/api/factories/{ctx.factory}/human-tasks/{record['task_id']}"
    with ExitStack() as stack:
        anonymous = stack.enter_context(TestClient(app))
        assert anonymous.post(url + "/handoffs", json=payload(record)).status_code == 401
        client = stack.enter_context(TestClient(app))
        origin = {"Origin": settings.public_origin}
        logged = client.post(
            "/api/login",
            json={"username": ctx.actors["planner"].username, "password": password},
            headers=origin,
        )
        assert logged.status_code == 200
        headers = {**origin, "X-CSRF-Token": logged.json()["csrf_token"]}
        assert client.get(url + "/handoffs").status_code == 405
        assert client.get(url).json()["state"] == "OPEN"
        assert (
            client.post(url + "/handoffs", json=payload(record), headers=origin).status_code == 403
        )
        result = client.post(url + "/handoffs", json=payload(record), headers=headers)
        assert result.status_code == 200 and result.json()["state"] == "ACCEPTED"
        closed = client.get(f"/api/factories/{ctx.factory}/cases/{ctx.case_id}").json()
        assert closed["state"] == "HANDED_OFF"
