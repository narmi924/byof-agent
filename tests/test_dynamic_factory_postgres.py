"""Physical effects and source action identities across real HTTP and PostgreSQL."""

import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
import uvicorn
from pydantic import SecretStr
from sqlalchemy import delete, func, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from packages.domain.models import Approval, Snapshot
from packages.domain.skf import load_skf_snapshot
from packages.persistence import connect
from packages.planning.solver import solve
from packages.settings import Settings
from scripts.import_factory import import_initial
from services.factory_sim.main import create_app
from services.factory_sim.service import run_due_tick
from services.factory_sim.storage import SourceAction, SourceChange, SourceRun, World


@pytest.fixture
def dynamic_source(request):
    urls = [
        os.getenv(key)
        for key in ("TEST_DATABASE_URL", "TEST_FACTORY_DATABASE_URL", "TEST_MIGRATION_DATABASE_URL")
    ]
    if not all(urls):
        pytest.skip("Explicit PostgreSQL application/source/owner test URLs required")
    app_engine, sim_engine, owner = [connect(url) for url in urls]
    assert all(e.url.database == "byof_test" for e in (app_engine, sim_engine, owner))
    factory = "dynamic-" + uuid4().hex
    original = load_skf_snapshot(development=True)
    raw = original.model_dump(mode="json", exclude={"content_hash"})
    raw["factory_id"] = raw["profile"]["factory_id"] = factory
    raw["snapshot_id"] = factory + "-initial"
    if getattr(request, "param", {}).get("risk_fixture", False):
        # Keep fast risk-projection tests independent of the 864-operation demo factory.
        raw["orders"][0]["quantity"] *= 3
        raw["profile"]["version"] = "risk-fixture/1"
        raw["profile"]["policy"]["policy_version"] = "risk-fixture/1"
        raw["profile"]["policy"]["progress_revalidation_enabled"] = True
    if getattr(request, "param", {}).get("progress_revalidation", False):
        raw["profile"]["version"] = "progress-fixture/1"
        raw["profile"]["policy"]["policy_version"] = "progress-fixture/1"
        raw["profile"]["policy"]["progress_revalidation_enabled"] = True
    initial = import_initial(sim_engine, Snapshot.model_validate(raw))
    tokens = {role: uuid4().hex for role in ("reader", "writer", "controller")}
    app = create_app(
        Settings(
            _env_file=None,
            factory_database_url=SecretStr(urls[1]),
            factory_api_token=SecretStr(tokens["reader"]),
            factory_execution_token=SecretStr(
                "" if getattr(request, "param", {}).get("read_only", False) else tokens["writer"]
            ),
            factory_control_token=SecretStr(tokens["controller"]),
        )
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.01)
    assert server.started
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
        headers={"Authorization": "Bearer " + tokens["reader"]},
    )
    try:
        yield client, tokens, initial, app_engine, sim_engine
    finally:
        client.close()
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive()
        with owner.begin() as db:
            for table in (SourceAction, SourceChange, SourceRun, World):
                db.execute(delete(table).where(table.factory_id == factory))
        for engine in (app_engine, sim_engine, owner):
            engine.dispose()


def snapshot(source):
    client, _, initial, *_ = source
    response = client.get("/factory/v1/snapshot", params={"factory_id": initial.factory_id})
    response.raise_for_status()
    return Snapshot.model_validate(response.json())


def plan_input(source, operation_id="publish-1"):
    state = snapshot(source)
    candidate = solve(state, time_limit=2)
    assert candidate.checker.status == "PASS"
    now = datetime.now(UTC)
    approval = Approval(
        approval_id="approval",
        factory_id=state.factory_id,
        candidate_hash=candidate.content_hash,
        binding=candidate.binding,
        approver_id="authorized-test-planner",
        approver_role="planner",
        action_scope="publish_plan",
        decision="APPROVED",
        decided_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    return {
        "operation_id": operation_id,
        "factory_id": state.factory_id,
        "run_id": state.run_id,
        "expected_source_revision": state.source.source_revision,
        "expected_snapshot_hash": state.content_hash,
        "expected_active_plan_version": state.active_plan_version,
        "candidate": candidate.model_dump(mode="json"),
        "approvals": [approval.model_dump(mode="json")],
    }


def send_plan(source, payload):
    client, tokens, *_ = source
    return client.post(
        "/factory/v1/plans", json=payload, headers={"Authorization": "Bearer " + tokens["writer"]}
    )


def control(source, request_id, kind, payload=None):
    client, tokens, initial, *_ = source
    return client.post(
        f"/simulator/v1/factories/{initial.factory_id}/commands",
        json={
            "request_id": request_id,
            "run_id": initial.run_id,
            "kind": kind,
            "payload": payload or {},
        },
        headers={"Authorization": "Bearer " + tokens["controller"]},
    )


def test_conditional_acceptance_receipt_lost_retry_has_one_effect(dynamic_source):
    source = dynamic_source
    client, _, initial, _, sim_engine = source
    payload = plan_input(source)
    first = send_plan(source, payload)
    assert first.status_code == 200 and first.json()["source_state"] == "ACTIVE"
    received = client.get(
        "/factory/v1/actions/publish-1",
        params={"factory_id": initial.factory_id, "run_id": initial.run_id},
    ).json()
    assert received == first.json()
    retried = send_plan(source, payload)
    assert retried.json() == received
    current = snapshot(source)
    assert current.active_plan_hash == payload["candidate"]["content_hash"]
    assert current.source.source_revision == "2" and current.actuals == ()
    with Session(sim_engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(SourceAction)
                .where(SourceAction.factory_id == initial.factory_id)
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(SourceChange)
                .where(SourceChange.factory_id == initial.factory_id)
            )
            == 1
        )
    conflict = dict(payload, expected_source_revision="changed")
    response = send_plan(source, conflict)
    assert response.status_code == 409 and response.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert snapshot(source) == current


def test_clock_step_duplicate_concurrent_posts_do_not_repeat_production(dynamic_source):
    source = dynamic_source
    assert send_plan(source, plan_input(source)).json()["source_state"] == "ACTIVE"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: control(source, "step-3", "clock.step", {"minutes": 3}), range(2))
        )
    assert results[0].json() == results[1].json()
    current = snapshot(source)
    assert current.snapshot_clock == source[2].snapshot_clock + timedelta(minutes=3)
    assert current.actuals[0].remaining_minutes == 7
    assert sum((s.end_at - s.start_at).total_seconds() for s in current.actuals[0].segments) == 180
    assert current.source.source_revision == "5"
    before_stock = current.inventory
    assert control(source, "step-3", "clock.step", {"minutes": 3}).json() == results[0].json()
    assert snapshot(source).inventory == before_stock


def test_reader_cannot_control_publish_or_query_private_state(dynamic_source):
    client, _, initial, app_engine, _ = dynamic_source
    payload = plan_input(dynamic_source)
    assert client.post("/factory/v1/plans", json=payload).status_code == 403
    endpoint = f"/simulator/v1/factories/{initial.factory_id}"
    assert client.get(endpoint).status_code == 403
    assert (
        client.post(
            endpoint + "/commands",
            json={"request_id": "attack", "run_id": initial.run_id, "kind": "clock.step"},
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/factory/v1/objects/future-events/1", params={"factory_id": initial.factory_id}
        ).status_code
        == 404
    )
    with Session(app_engine) as db, pytest.raises(ProgrammingError):
        db.execute(select(SourceChange).limit(1))
    assert snapshot(dynamic_source) == initial


def test_changed_clock_rejects_old_approval_without_activating_or_producing(dynamic_source):
    payload = plan_input(dynamic_source)
    assert control(dynamic_source, "tick", "clock.step").status_code == 200
    before = snapshot(dynamic_source)
    rejected = send_plan(dynamic_source, payload).json()
    assert (
        rejected["source_state"] == "REJECTED"
        and rejected["error_code"] == "SOURCE_CONDITIONS_CHANGED"
    )
    assert rejected["effective_at"] is None
    assert snapshot(dynamic_source) == before
    assert before.active_plan_version is None and before.actuals == ()


def test_real_approval_expiry_uses_real_time_not_business_clock(dynamic_source):
    payload = plan_input(dynamic_source)
    payload["approvals"][0].update(
        decided_at="2025-01-01T00:00:00Z", expires_at="2025-01-02T00:00:00Z"
    )
    response = send_plan(dynamic_source, payload)
    assert response.status_code == 200
    assert response.json()["error_code"] == "APPROVAL_REQUIRED"
    assert snapshot(dynamic_source).active_plan_version is None


def test_changes_are_repeatable_paginated_facts_and_invalid_controls_rollback(dynamic_source):
    source = dynamic_source
    assert send_plan(source, plan_input(source)).json()["source_state"] == "ACTIVE"
    assert control(source, "ticks", "clock.step", {"minutes": 3}).status_code == 200
    client, _, initial, *_ = source
    params = {"factory_id": initial.factory_id, "run_id": initial.run_id, "after": 1, "limit": 2}
    first = client.get("/factory/v1/changes", params=params).json()
    assert first["next_cursor"] == "3" and first["has_more"]
    assert client.get("/factory/v1/changes", params=params).json() == first
    second = client.get("/factory/v1/changes", params=dict(params, after=3)).json()
    assert second["next_cursor"] == second["watermark"] == "5" and not second["has_more"]
    assert any(
        e["event_type"] == "execution.progress" for c in second["changes"] for e in c["events"]
    )
    before = snapshot(source)
    response = control(source, "bad-order", "order.add", {"order_id": "missing-everything"})
    assert response.status_code == 422
    assert snapshot(source) == before
    response = control(source, "bad-step", "clock.step", {"minutes": True})
    assert response.status_code == 422 and snapshot(source) == before


def test_persistent_clock_worker_serializes_parallel_ticks_and_pause(dynamic_source):
    source = dynamic_source
    assert control(source, "run", "clock.run", {"interval_ms": 100}).status_code == 200
    time.sleep(0.12)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: run_due_tick(source[4]), range(2)))
    assert sum(outcomes) == 1
    assert snapshot(source).snapshot_clock == source[2].snapshot_clock + timedelta(minutes=1)
    assert (
        control(source, "invalid-step", "clock.step").json()["code"] == "PAUSE_BEFORE_SINGLE_STEP"
    )
    assert control(source, "pause", "clock.pause").status_code == 200
    time.sleep(0.12)
    assert run_due_tick(source[4]) is False
