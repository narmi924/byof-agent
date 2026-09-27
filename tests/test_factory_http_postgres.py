"""Real HTTP and PostgreSQL boundary tests; no SQLite or in-memory persistence substitute."""

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
from sqlalchemy import delete, select, update
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from packages.auth import AccessError, Grant, Principal
from packages.domain.models import Snapshot, batch_operations
from packages.domain.skf import load_skf_snapshot
from packages.integrations.factory_http import ConnectorError, FactoryHTTP
from packages.integrations.sync import SourceBatch
from packages.persistence import Membership, User, connect
from packages.planning.service import claim_job, complete_job, request_solve, synchronize
from packages.planning.store import (
    ApprovalRecord,
    CandidateRecord,
    FactoryState,
    SnapshotRecord,
    SolveJob,
)
from packages.settings import Settings
from scripts.import_factory import import_initial
from services.factory_sim.main import create_app
from services.factory_sim.storage import SourceAction, SourceChange, SourceRun, World


@pytest.fixture
def source():
    url = os.getenv("TEST_DATABASE_URL")
    sim_url = os.getenv("TEST_FACTORY_DATABASE_URL")
    owner_url = os.getenv("TEST_MIGRATION_DATABASE_URL", os.getenv("MIGRATION_DATABASE_URL", ""))
    if not url or not sim_url or not owner_url:
        pytest.skip("Explicit PostgreSQL app/simulator/owner test URLs are required")
    engines = [connect(value) for value in (url, sim_url, owner_url)]
    if any(e.url.database != "byof_test" for e in engines):
        pytest.fail("Test cleanup is restricted to byof_test")
    app_engine, sim_engine, owner = engines
    factory = "test-" + uuid4().hex
    raw = load_skf_snapshot().model_dump(mode="json", exclude={"content_hash"})
    raw["factory_id"] = factory
    raw["profile"]["factory_id"] = factory
    initial = import_initial(sim_engine, Snapshot.model_validate(raw))
    token = uuid4().hex
    app = create_app(
        Settings(
            _env_file=None,
            factory_database_url=SecretStr(sim_url),
            factory_api_token=SecretStr(token),
            factory_execution_token=SecretStr(""),
            factory_control_token=SecretStr(""),
        )
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.01)
    assert server.started
    connector = FactoryHTTP(f"http://127.0.0.1:{port}", token)
    user_id = "source-planner-" + uuid4().hex
    actor = Principal(
        user_id=user_id, username=user_id, grants=(Grant(factory_id=factory, role="planner"),)
    )
    with Session(app_engine) as db, db.begin():
        db.add(User(user_id=user_id, username=user_id, password_hash="not-a-login", active=True))
        db.flush()
        db.add(Membership(user_id=user_id, factory_id=factory, role="planner"))
    try:
        yield app_engine, sim_engine, connector, initial, actor
    finally:
        connector.close()
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive()
        with owner.begin() as db:
            for table in (
                ApprovalRecord,
                CandidateRecord,
                SolveJob,
                FactoryState,
                SnapshotRecord,
                SourceBatch,
                SourceAction,
                SourceChange,
                SourceRun,
                World,
            ):
                db.execute(delete(table).where(table.factory_id == factory))
            db.execute(delete(Membership).where(Membership.user_id == user_id))
            db.execute(delete(User).where(User.user_id == user_id))
        for engine in engines:
            engine.dispose()


def test_complete_baseline_crosses_real_http_and_is_immutable(source):
    engine, sim_engine, connector, initial, actor = source
    caps = connector.capabilities()
    assert caps.read_snapshot and caps.query_detail and not caps.accept_plan
    fetched = synchronize(engine, connector, initial.factory_id)
    assert fetched.content_hash == initial.content_hash
    batches, operations = batch_operations(fetched)
    assert (
        len(fetched.orders),
        sum(o.quantity for o in fetched.orders),
        len(batches),
        len(operations),
    ) == (6, 5400, 108, 864)
    assert synchronize(engine, connector, initial.factory_id).content_hash == fetched.content_hash
    with Session(engine) as db:
        records = db.scalars(
            select(SnapshotRecord).where(SnapshotRecord.factory_id == initial.factory_id)
        ).all()
        assert len(records) == 1
        with pytest.raises(ProgrammingError):
            db.execute(
                update(SnapshotRecord)
                .where(SnapshotRecord.snapshot_id == fetched.snapshot_id)
                .values(document={})
            )
        db.rollback()
    with pytest.raises(ValueError, match="already exists"):
        import_initial(sim_engine, initial)


def test_reader_auth_and_private_control_are_not_accessible(source):
    engine, _, connector, initial, _ = source
    base = str(connector.client.base_url)
    with httpx.Client(base_url=base) as client:
        assert (
            client.get(
                "/factory/v1/snapshot", params={"factory_id": initial.factory_id}
            ).status_code
            == 401
        )
    assert (
        connector.client.post("/simulator/v1/clock/advance", json={"minutes": 1}).status_code == 404
    )
    assert (
        connector.client.get(
            "/factory/v1/objects/future-events/secret", params={"factory_id": initial.factory_id}
        ).status_code
        == 404
    )
    with Session(engine) as db, pytest.raises(ProgrammingError):
        db.get(World, initial.factory_id)


def test_same_revision_changed_payload_and_late_snapshot_rejected(source):
    engine, sim_engine, connector, initial, _ = source
    synchronize(engine, connector, initial.factory_id)
    raw = initial.model_dump(mode="json", exclude={"content_hash"})
    raw["orders"][0]["priority_weight"] += 1
    changed = Snapshot.model_validate(raw)
    with sim_engine.begin() as db:
        db.execute(
            update(World)
            .where(World.factory_id == initial.factory_id)
            .values(document=changed.model_dump(mode="json"))
        )
    with pytest.raises(AccessError, match="same version"):
        synchronize(engine, connector, initial.factory_id)
    raw["source"]["source_revision"] = "0"
    raw["snapshot_id"] += "-late"
    changed = Snapshot.model_validate(raw)
    with sim_engine.begin() as db:
        db.execute(
            update(World)
            .where(World.factory_id == initial.factory_id)
            .values(document=changed.model_dump(mode="json"))
        )
    with pytest.raises(AccessError, match="old snapshot version"):
        synchronize(engine, connector, initial.factory_id)
    with Session(engine) as db:
        assert db.get(FactoryState, initial.factory_id).snapshot_id == initial.snapshot_id


def test_concurrent_request_is_idempotent_and_lease_fences_stale_worker(source):
    engine, _, connector, initial, actor = source
    synchronize(engine, connector, initial.factory_id)
    request_id = str(uuid4())

    def enqueue():
        return request_solve(
            engine,
            actor,
            initial.factory_id,
            request_id=request_id,
            allow_overtime=False,
            time_limit=5,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(lambda _: enqueue(), range(2)))
    assert jobs[0].job_id == jobs[1].job_id
    with pytest.raises(AccessError, match="same request ID"):
        request_solve(
            engine,
            actor,
            initial.factory_id,
            request_id=request_id,
            allow_overtime=True,
            time_limit=5,
        )
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = [x for x in pool.map(lambda _: claim_job(engine), range(2)) if x]
    assert len(claimed) == 1
    old = claimed[0]
    with engine.begin() as db:
        db.execute(
            update(SolveJob)
            .where(SolveJob.job_id == old.job_id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    new = claim_job(engine)
    assert new and new.lease_token != old.lease_token and new.attempts == 2
    assert complete_job(engine, old, None, "STALE_RESULT") is False
    assert complete_job(engine, new, None, "EXPECTED_TEST_FAILURE") is True
    assert complete_job(engine, new, None, "DUPLICATE") is False
    with Session(engine) as db:
        final = db.get(SolveJob, new.job_id)
        assert final.state == "FAILED" and final.error_code == "EXPECTED_TEST_FAILURE"


def test_no_scope_grant_no_job(source):
    engine, _, connector, initial, actor = source
    synchronize(engine, connector, initial.factory_id)
    outsider = Principal(
        user_id="other",
        username="other",
        grants=(Grant(factory_id="other-factory", role="planner"),),
    )
    with pytest.raises(AccessError):
        request_solve(
            engine,
            outsider,
            initial.factory_id,
            request_id=str(uuid4()),
            allow_overtime=False,
            time_limit=5,
        )
    with Session(engine) as db:
        assert (
            list(db.scalars(select(SolveJob).where(SolveJob.factory_id == initial.factory_id)))
            == []
        )


@pytest.mark.parametrize(
    "origin",
    [
        "https://user:pass@example.com",
        "https://example.com/path",
        "file:///etc",
        "http://untrusted.invalid",
        "https://example.com?private=1",
    ],
)
def test_connector_rejects_unregistered_origin_shapes(origin):
    with pytest.raises(ConnectorError):
        FactoryHTTP(origin, "local-test-token")


def test_connector_never_follows_redirect_or_accepts_cross_factory_payload():
    requests = []

    def redirect(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://untrusted.invalid"})

    connector = FactoryHTTP("http://127.0.0.1", "test", transport=httpx.MockTransport(redirect))
    try:
        with pytest.raises(ConnectorError):
            connector.snapshot("expected")
        assert len(requests) == 1
    finally:
        connector.close()


def test_different_source_run_cannot_restore_old_approved_history(source):
    engine, sim_engine, connector, initial, _ = source
    synchronize(engine, connector, initial.factory_id)
    raw = initial.model_dump(mode="json", exclude={"content_hash"})
    raw.update(run_id="different-run", snapshot_id="different-" + uuid4().hex)
    changed = Snapshot.model_validate(raw)
    with sim_engine.begin() as db:
        db.execute(
            update(World)
            .where(World.factory_id == initial.factory_id)
            .values(document=changed.model_dump(mode="json"))
        )
    with pytest.raises(AccessError) as rejected:
        synchronize(engine, connector, initial.factory_id)
    assert rejected.value.code == "SOURCE_RUN_CHANGED"
    with Session(engine) as db:
        state = db.get(FactoryState, initial.factory_id)
        assert state.run_id == initial.run_id and state.snapshot_id == initial.snapshot_id


def test_real_solver_worker_and_approval_replay_preserves_later_rejection(source):
    from fastapi.testclient import TestClient

    from packages import auth
    from packages.persistence import LoginSession, Membership, User
    from packages.planning.service import approve, workspace
    from services.api.main import create_app as api_app
    from services.solver_worker.main import run_once

    engine, sim_engine, connector, initial, _ = source
    raw = initial.model_dump(mode="json", exclude={"content_hash"})
    raw["orders"] = [raw["orders"][0]]
    raw["orders"][0]["quantity"] = 50
    small = Snapshot.model_validate(raw)
    with sim_engine.begin() as db:
        db.execute(
            update(World)
            .where(World.factory_id == initial.factory_id)
            .values(document=small.model_dump(mode="json"))
        )
    synchronize(engine, connector, initial.factory_id)
    uid = uuid4().hex
    actor = Principal(
        user_id=uid, username=uid, grants=(Grant(factory_id=initial.factory_id, role="planner"),)
    )
    with Session(engine) as db, db.begin():
        db.add(
            User(
                user_id=uid,
                username=uid,
                password_hash=auth.hasher.hash("test-only-password"),
                active=True,
            )
        )
        db.flush()
        db.add(Membership(user_id=uid, factory_id=initial.factory_id, role="planner"))
    try:
        job = request_solve(
            engine,
            actor,
            initial.factory_id,
            request_id=uuid4().hex,
            allow_overtime=False,
            time_limit=3,
        )
        assert run_once(engine)
        data = workspace(engine, initial.factory_id)
        assert data["jobs"][0]["job_id"] == job.job_id
        assert data["jobs"][0]["state"] == "SUCCEEDED"
        candidate = data["candidates"][0]["candidate"]
        assert (
            candidate.has_solution
            and candidate.checker.status == "PASS"
            and len(candidate.assignments) == 8
        )
        kwargs = {
            "candidate_hash": candidate.content_hash,
            "action_scope": "publish_plan",
            "decision": "APPROVED",
            "request_id": "first-decision",
        }

        def accept():
            return approve(engine, actor, initial.factory_id, candidate.candidate_id, **kwargs)

        with ThreadPoolExecutor(max_workers=2) as pool:
            repeated = list(pool.map(lambda _: accept(), range(2)))
        assert repeated[0].approval_id == repeated[1].approval_id
        reject = approve(
            engine,
            actor,
            initial.factory_id,
            candidate.candidate_id,
            **{**kwargs, "decision": "REJECTED", "request_id": "later-reject"},
        )
        assert accept().approval_id != reject.approval_id
        latest = workspace(engine, initial.factory_id)["candidates"][0]
        assert latest["state"] == "CANDIDATE" and len(latest["approvals"]) == 2
        with pytest.raises(AccessError) as conflict:
            approve(
                engine,
                actor,
                initial.factory_id,
                candidate.candidate_id,
                **{**kwargs, "decision": "REJECTED"},
            )
        assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
        settings = Settings(
            _env_file=None,
            legacy_password_login_enabled=True,
            database_url=SecretStr(engine.url.render_as_string(hide_password=False)),
            factory_api_url=str(connector.client.base_url),
            factory_api_token=SecretStr(connector.client.headers["authorization"][7:]),
        )
        with TestClient(api_app(settings)) as client:
            headers = {"Origin": settings.public_origin}
            assert (
                client.post(
                    "/api/login",
                    json={"username": uid, "password": "test-only-password"},
                    headers=headers,
                ).status_code
                == 200
            )
            csrf = client.post("/api/csrf", headers=headers).json()["csrf_token"]
            headers["X-CSRF-Token"] = csrf
            path = (
                f"/api/factories/{initial.factory_id}/candidates/{candidate.candidate_id}/approvals"
            )
            assert client.get(path).status_code == 405
            assert (
                client.post(
                    path,
                    json={**kwargs, "request_id": "missing-csrf"},
                    headers={"Origin": settings.public_origin},
                ).status_code
                == 403
            )
            assert (
                client.post(
                    path,
                    json={
                        **kwargs,
                        "request_id": "manager-scope",
                        "action_scope": "allow_overtime",
                    },
                    headers=headers,
                ).status_code
                == 403
            )
            assert client.get("/api/factories/other/workspace").status_code == 403
        from packages.domain.execution import SimulatorCommand
        from services.factory_sim.service import command

        order = small.orders[0].model_dump(mode="json")
        order["order_id"] += "-new"
        command(
            sim_engine,
            initial.factory_id,
            SimulatorCommand(
                request_id="new-order", run_id=initial.run_id, kind="order.add", payload=order
            ),
        )
        synchronize(engine, connector, initial.factory_id)
        with pytest.raises(AccessError) as stale:
            approve(
                engine,
                actor,
                initial.factory_id,
                candidate.candidate_id,
                **{**kwargs, "request_id": "after-new-order"},
            )
        assert stale.value.code == "STALE_CANDIDATE"
        assert workspace(engine, initial.factory_id)["candidates"][0]["state"] == "STALE"
        with engine.begin() as db:
            db.execute(delete(Membership).where(Membership.user_id == uid))
        with pytest.raises(AccessError) as revoked:
            accept()
        assert revoked.value.code == "FORBIDDEN"
    finally:
        with engine.begin() as db:
            db.execute(delete(LoginSession).where(LoginSession.user_id == uid))
            db.execute(delete(Membership).where(Membership.user_id == uid))
            db.execute(delete(User).where(User.user_id == uid))


def test_connector_rejects_cross_factory_payload():
    connector = FactoryHTTP(
        "http://127.0.0.1",
        "test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=load_skf_snapshot().model_dump(mode="json"))
        ),
    )
    try:
        with pytest.raises(ConnectorError, match="scope"):
            connector.snapshot("other-factory")
    finally:
        connector.close()
