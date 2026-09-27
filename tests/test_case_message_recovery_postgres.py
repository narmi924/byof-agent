"""Saved Case requests remain readable after closure or run changes without new writes."""

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from test_human_tasks_postgres import human_context as human_context

from packages import auth
from packages.agent.cases import create_case, message_case, recover_case_input
from packages.agent.cases_store import CaseInput, CaseRecord
from packages.domain.models import Snapshot
from packages.integrations.factory_http import ConnectorError
from packages.persistence import LoginSession, Membership, User
from packages.planning.store import FactoryState, SnapshotRecord
from packages.settings import Settings
from services.api import cases as api_cases
from services.api.main import create_app

CREATE_MESSAGE = "Follow the machine state and wait for the owner to confirm."
MESSAGE = "The maintenance owner is checking the remaining work; keep the original records."


@pytest.fixture
def recovery(human_context):
    ctx = human_context
    actor = ctx.actors["planner"]
    other_id = "other-planner-" + uuid4().hex
    with Session(ctx.engine) as db, db.begin():
        db.add(User(user_id=other_id, username=other_id, active=True, password_hash="not-a-login"))
        db.flush()
        db.add(Membership(user_id=other_id, factory_id=ctx.factory, role="planner"))
    other = auth.Principal(
        user_id=other_id,
        username=other_id,
        grants=(auth.Grant(factory_id=ctx.factory, role="planner"),),
    )
    ctx.actors["other_planner"] = other
    created = create_case(
        ctx.engine, actor, ctx.factory, "original-create", CREATE_MESSAGE, start_new=True
    )
    message_case(ctx.engine, actor, ctx.factory, created["case_id"], "original-message", MESSAGE)
    return SimpleNamespace(
        engine=ctx.engine,
        factory=ctx.factory,
        actor=actor,
        other=other,
        case_id=created["case_id"],
        other_case_id=ctx.case_id,
        snapshot=ctx.snapshot,
    )


def ledger(ctx):
    with Session(ctx.engine) as db:
        return {
            model.__tablename__: [
                dict(row)
                for row in db.execute(
                    select(model.__table__)
                    .where(model.factory_id == ctx.factory)
                    .order_by(*model.__table__.primary_key.columns)
                ).mappings()
            ]
            for model in (CaseRecord, CaseInput, FactoryState, SnapshotRecord)
        }


def close_case(ctx, state="RESOLVED"):
    with Session(ctx.engine) as db, db.begin():
        case = db.get(CaseRecord, ctx.case_id, with_for_update=True)
        assert case is not None
        case.state = state
        case.version += 1
        case.updated_at = datetime.now(UTC)
        case.closure = {"summary": "Recorded the closed state and basis needed by the test."}


def change_run(ctx, *, replay=False):
    raw = ctx.snapshot.model_dump(mode="json", exclude={"content_hash"})
    raw.update(run_id=uuid4().hex, snapshot_id=uuid4().hex)
    if replay:
        raw["source"]["source_system"] = "factory-simulator-replay"
    current = Snapshot.model_validate(raw)
    with Session(ctx.engine) as db, db.begin():
        state = db.get(FactoryState, ctx.factory, with_for_update=True)
        assert state is not None
        db.add(
            SnapshotRecord(
                snapshot_id=current.snapshot_id,
                factory_id=ctx.factory,
                content_hash=current.content_hash,
                document=current.model_dump(mode="json"),
                created_at=datetime.now(UTC),
            )
        )
        state.snapshot_id = current.snapshot_id
        state.run_id = current.run_id
        state.source_revision = current.source.source_revision
        state.last_synced_at = datetime.now(UTC)
    return current


def recover_message(ctx, **overrides):
    return message_case(
        ctx.engine,
        overrides.get("actor", ctx.actor),
        overrides.get("factory", ctx.factory),
        overrides.get("case_id", ctx.case_id),
        overrides.get("request_id", "original-message"),
        overrides.get("message", MESSAGE),
    )


@pytest.mark.parametrize("state", ["RESOLVED", "HANDED_OFF", "CANCELLED"])
def test_original_message_after_closure_returns_current_case_without_reopening_or_waking(
    recovery, state
):
    ctx = recovery
    close_case(ctx, state)
    before = ledger(ctx)
    saved_case = next(row for row in before["cases"] if row["case_id"] == ctx.case_id)

    result = recover_message(ctx)

    assert result["case_id"] == ctx.case_id
    assert result["state"] == state
    assert result["version"] == saved_case["version"]
    assert result["updated_at"] == saved_case["updated_at"]
    assert result["closure"] == saved_case["closure"]
    assert len(before["case_inputs"]) == 2
    assert ledger(ctx) == before
    with pytest.raises(auth.AccessError) as error:
        recover_message(ctx, request_id="new-message")
    assert error.value.code == "CASE_CLOSED"
    assert ledger(ctx) == before


@pytest.mark.parametrize("variant", ["message", "case", "actor"])
def test_saved_message_cannot_be_recovered_with_changed_body_case_or_actor(recovery, variant):
    ctx = recovery
    close_case(ctx)
    before = ledger(ctx)
    overrides = {
        "message": {"message": "This is another piece of information."},
        "case": {"case_id": ctx.other_case_id},
        "actor": {"actor": ctx.other},
    }[variant]
    with pytest.raises(auth.AccessError) as error:
        recover_message(ctx, **overrides)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert ledger(ctx) == before


@pytest.mark.parametrize("variant", ["revoked-role", "inactive-user"])
def test_original_request_still_requires_current_authorization_after_closure(recovery, variant):
    ctx = recovery
    close_case(ctx)
    with Session(ctx.engine) as db, db.begin():
        if variant == "revoked-role":
            db.execute(delete(Membership).where(Membership.user_id == ctx.actor.user_id))
        else:
            db.get(User, ctx.actor.user_id).active = False
    before = ledger(ctx)
    for action in (
        lambda: recover_message(ctx),
        lambda: create_case(
            ctx.engine, ctx.actor, ctx.factory, "original-create", CREATE_MESSAGE, start_new=True
        ),
        lambda: recover_case_input(
            ctx.engine, ctx.actor, ctx.factory, "original-message", MESSAGE, case_id=ctx.case_id
        ),
    ):
        with pytest.raises(auth.AccessError) as error:
            action()
        assert error.value.code == "AUTHORIZATION_REVOKED"
    assert ledger(ctx) == before


@pytest.mark.parametrize("replay", [False, True])
def test_run_change_allows_only_readback_of_original_request_and_keeps_new_write_gates(
    recovery, replay
):
    ctx = recovery
    current = change_run(ctx, replay=replay)
    before = ledger(ctx)
    recovered = recover_message(ctx)
    created = create_case(
        ctx.engine, ctx.actor, ctx.factory, "original-create", CREATE_MESSAGE, start_new=True
    )
    assert recovered["case_id"] == created["case_id"] == ctx.case_id
    assert recovered["run_id"] == ctx.snapshot.run_id != current.run_id
    assert ledger(ctx) == before
    with pytest.raises(auth.AccessError) as error:
        recover_message(ctx, request_id="new-message")
    assert error.value.code == "SOURCE_RUN_CHANGED"
    if replay:
        with pytest.raises(auth.AccessError) as error:
            create_case(ctx.engine, ctx.actor, ctx.factory, "new-create", CREATE_MESSAGE, True)
        assert error.value.code == "REPLAY_READ_ONLY"
    assert ledger(ctx) == before


@pytest.mark.parametrize("variant", ["message", "actor", "start-new"])
def test_original_create_binding_is_verified_before_replay_readback(recovery, variant):
    ctx = recovery
    change_run(ctx, replay=True)
    before = ledger(ctx)
    actor = ctx.other if variant == "actor" else ctx.actor
    message = "Different case content." if variant == "message" else CREATE_MESSAGE
    with pytest.raises(auth.AccessError) as error:
        create_case(
            ctx.engine,
            actor,
            ctx.factory,
            "original-create",
            message,
            start_new=variant != "start-new",
        )
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert ledger(ctx) == before


def test_cross_factory_id_cannot_read_or_rebind_original_message(recovery):
    ctx = recovery
    other_factory = "other-recovery-" + uuid4().hex
    with Session(ctx.engine) as db, db.begin():
        db.add(Membership(user_id=ctx.actor.user_id, factory_id=other_factory, role="planner"))
    actor = ctx.actor.model_copy(
        update={"grants": (*ctx.actor.grants, auth.Grant(factory_id=other_factory, role="planner"))}
    )
    before = ledger(ctx)
    with pytest.raises(auth.AccessError) as error:
        recover_message(ctx, actor=actor, factory=other_factory)
    assert error.value.code == "NOT_FOUND"
    assert ledger(ctx) == before


@pytest.fixture
def recovery_api(recovery):
    ctx = recovery
    password = "test-only-case-recovery-password"
    with Session(ctx.engine) as db, db.begin():
        db.get(User, ctx.actor.user_id).password_hash = auth.hasher.hash(password)
    settings = Settings(
        _env_file=None,
        environment="test",
        legacy_password_login_enabled=True,
        database_url=SecretStr(os.environ["TEST_DATABASE_URL"]),
        public_origin="http://testserver",
    )
    with TestClient(create_app(settings)) as client:
        headers = {"Origin": settings.public_origin}
        login = client.post(
            "/api/login",
            headers=headers,
            json={"username": ctx.actor.username, "password": password},
        )
        assert login.status_code == 200
        headers["X-CSRF-Token"] = login.json()["csrf_token"]
        try:
            yield SimpleNamespace(ctx=ctx, client=client, headers=headers)
        finally:
            with ctx.engine.begin() as db:
                db.execute(delete(LoginSession).where(LoginSession.user_id == ctx.actor.user_id))


@pytest.mark.parametrize("kind", ["create", "message"])
def test_api_recovers_original_request_after_replay_with_source_offline_without_new_work(
    recovery_api, monkeypatch, kind
):
    api = recovery_api
    ctx = api.ctx
    close_case(ctx)
    change_run(ctx, replay=True)
    before = ledger(ctx)
    offline_source = Mock(side_effect=ConnectorError("Source unavailable for this regression"))
    monkeypatch.setattr(api_cases, "source", offline_source)
    base = f"/api/factories/{ctx.factory}/cases"
    path = base if kind == "create" else f"{base}/{ctx.case_id}/messages"
    body = (
        {"request_id": "original-create", "message": CREATE_MESSAGE, "start_new": True}
        if kind == "create"
        else {"request_id": "original-message", "message": MESSAGE}
    )

    response = api.client.post(path, headers=api.headers, json=body)

    assert response.status_code == 200
    assert response.json()["case_id"] == ctx.case_id
    assert response.json()["state"] == "RESOLVED"
    assert response.json()["run_id"] == ctx.snapshot.run_id
    offline_source.assert_not_called()
    assert ledger(ctx) == before

    assert api.client.post(path, json=body).status_code == 403
    altered = api.client.post(
        path, headers=api.headers, json={**body, "message": "Different content."}
    )
    assert altered.status_code == 409 and altered.json()["code"] == "IDEMPOTENCY_CONFLICT"
    offline_source.assert_not_called()
    original_input = next(
        row for row in before["case_inputs"] if row["input_key"] == "user:" + body["request_id"]
    )
    assert original_input["payload"]["actor_id"] == ctx.actor.user_id

    fresh = api.client.post(path, headers=api.headers, json={**body, "request_id": "fresh-request"})
    if kind == "create":
        assert fresh.status_code == 503
        offline_source.assert_called_once()
    else:
        assert fresh.status_code == 503
        offline_source.assert_called_once()
    assert ledger(ctx) == before
