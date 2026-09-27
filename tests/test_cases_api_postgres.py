"""Authenticated HTTP task handling against real PostgreSQL and source HTTP facts."""

from contextlib import ExitStack
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session
from test_case_runtime_postgres import case_context as case_context
from test_dynamic_factory_postgres import control, snapshot
from test_dynamic_factory_postgres import dynamic_source as dynamic_source
from test_publication_postgres import publishing as publishing

from packages import auth
from packages.agent.cases_store import CaseInput, CaseOperation, CaseRecord, CaseTurn
from packages.agent.human_tasks import HumanTaskRecord, TaskAction, TaskReminder, create_task
from packages.persistence import LoginSession, Membership, User
from packages.planning.store import FactoryState, SnapshotRecord, SolveJob
from packages.settings import Settings
from services.api.main import create_app


@pytest.fixture
def case_api(case_context):
    context, case = case_context
    source, _, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id
    password = "test-only-case-password"
    password_hash = auth.hasher.hash(password)
    accounts = {"planner": actor.user_id}
    for role in ("maintainer", "warehouse", "manager", "admin", "outsider", "sim_admin"):
        accounts[role] = "case-http-" + uuid4().hex
    with Session(engine) as db, db.begin():
        db.get(User, actor.user_id).password_hash = password_hash
        for role, user_id in accounts.items():
            if role == "planner":
                continue
            db.add(
                User(user_id=user_id, username=user_id, password_hash=password_hash, active=True)
            )
            db.flush()
            db.add(
                Membership(
                    user_id=user_id,
                    factory_id=factory + "-other" if role == "outsider" else factory,
                    role="planner" if role == "outsider" else role,
                )
            )
    settings = Settings(
        _env_file=None,
        environment="test",
        legacy_password_login_enabled=True,
        database_url=SecretStr(engine.url.render_as_string(hide_password=False)),
        factory_api_url=str(source[0].base_url),
        factory_api_token=SecretStr(source[1]["reader"]),
        factory_control_token=SecretStr(source[1]["controller"]),
    )
    app = create_app(settings)
    with ExitStack() as stack:
        clients = {}

        def login(role="planner"):
            if role in clients:
                return clients[role]
            client = stack.enter_context(TestClient(app))
            headers = {"Origin": settings.public_origin}
            result = client.post(
                "/api/login",
                json={"username": accounts[role], "password": password},
                headers=headers,
            )
            assert result.status_code == 200
            assert "HttpOnly" in result.headers["set-cookie"]
            assert client.get("/api/session").json()["user_id"] == accounts[role]
            renewed = client.post("/api/csrf", json={}, headers=headers)
            assert renewed.status_code == 200
            headers["X-CSRF-Token"] = renewed.json()["csrf_token"]
            clients[role] = client, headers
            return clients[role]

        try:
            yield SimpleNamespace(
                context=context,
                source=source,
                case=case,
                engine=engine,
                factory=factory,
                accounts=accounts,
                login=login,
                app=app,
                origin=settings.public_origin,
                base=f"/api/factories/{factory}",
            )
        finally:
            with engine.begin() as db:
                db.execute(delete(LoginSession).where(LoginSession.user_id.in_(accounts.values())))
                extra_ids = [uid for role, uid in accounts.items() if role != "planner"]
                db.execute(delete(Membership).where(Membership.user_id.in_(extra_ids)))
                db.execute(delete(User).where(User.user_id.in_(extra_ids)))


def information_task(ctx, *, fields=None):
    return create_task(
        ctx.engine,
        case_id=ctx.case["case_id"],
        factory_id=ctx.factory,
        operation_id="http-information-task",
        question="Please confirm the machine recovery time and remaining work.",
        role="maintainer",
        subject_id=ctx.source[2].resources[0].resource_id,
        fields=fields or ["repair_eta", "remaining_minutes", "remaining_setup_minutes"],
        deadline_minutes=15,
    )


def response_input(task, **changes):
    return {
        "request_id": str(uuid4()),
        "expected_task_version": task["version"],
        "answer": {
            "repair_eta": "2030-09-16T11:30:00+08:00",
            "remaining_minutes": 27,
            "remaining_setup_minutes": 0,
        },
        **changes,
    }


def ledger(ctx):
    with Session(ctx.engine) as db:
        case = db.get(CaseRecord, ctx.case["case_id"])
        counts = tuple(
            db.scalar(
                select(func.count()).select_from(table).where(table.factory_id == ctx.factory)
            )
            for table in (CaseInput, CaseOperation, CaseTurn, TaskAction, TaskReminder)
        )
        tasks = [
            (row.task_id, row.version, row.state, row.response, row.reminders_count)
            for row in db.scalars(
                select(HumanTaskRecord)
                .where(HumanTaskRecord.factory_id == ctx.factory)
                .order_by(HumanTaskRecord.task_id)
            )
        ]
        return (case.version, case.state, case.updated_at, counts, tasks)


def normalized_case(document):
    # PostgreSQL may return the same instant in its session timezone after the first commit.
    return {
        **document,
        **{
            field: datetime.fromisoformat(document[field]).astimezone(UTC)
            for field in ("created_at", "updated_at")
        },
    }


def test_assistant_balance_is_bound_to_the_current_synced_snapshot(case_api):
    client, _ = case_api.login("manager")
    response = client.get(f"{case_api.base}/assistant")
    assert response.status_code == 200
    with Session(case_api.engine) as db:
        current = db.get(FactoryState, case_api.factory)
    assert response.json()["material_balance"] == {
        "snapshot_id": current.snapshot_id,
        "shortfalls": [],
    }


def test_authenticated_get_and_link_scanners_do_not_acknowledge_or_change_tasks(case_api):
    ctx = case_api
    task = information_task(ctx)
    before, source_hash = ledger(ctx), snapshot(ctx.source).content_hash
    with TestClient(ctx.app) as anonymous:
        assert anonymous.get(f"{ctx.base}/cases").status_code == 401
        assert anonymous.get(f"{ctx.base}/human-tasks/{task['task_id']}").status_code == 401
    client, _ = ctx.login("maintainer")
    cases = client.get(f"{ctx.base}/cases")
    assert cases.status_code == 200 and cases.json()["cases"][0]["case_id"] == ctx.case["case_id"]
    detail = client.get(f"{ctx.base}/cases/{ctx.case['case_id']}").json()
    assert detail["inputs"][0]["payload"]["message"] == "Query the current machines and follow up"
    response = client.get(f"{ctx.base}/human-tasks", params={"case_id": ctx.case["case_id"]})
    assert [item["task_id"] for item in response.json()["tasks"]] == [task["task_id"]]
    read = client.get(f"{ctx.base}/human-tasks/{task['task_id']}").json()
    assert read["state"] == "OPEN" and read["response"] is None
    assert read["send_state"] == "NOT_ENABLED" and read["delivery_state"] == "UNAVAILABLE"
    for action in ("responses", "transfers", "cancellations"):
        assert client.get(f"{ctx.base}/human-tasks/{task['task_id']}/{action}").status_code == 405
    assert client.get(f"{ctx.base}/cases/{ctx.case['case_id']}/messages").status_code == 405
    assert ledger(ctx) == before
    assert snapshot(ctx.source).content_hash == source_hash


def test_case_post_requires_origin_csrf_and_synchronizes_actual_http_facts(case_api):
    ctx = case_api
    client, headers = ctx.login()
    body = {"request_id": str(uuid4()), "message": "Keep checking this stop."}
    before = ledger(ctx)
    assert client.post(f"{ctx.base}/cases", json=body).status_code == 403
    assert (
        client.post(f"{ctx.base}/cases", json=body, headers={"Origin": ctx.origin}).status_code
        == 403
    )
    assert (
        client.post(
            f"{ctx.base}/cases", json=body, headers={**headers, "Origin": "https://untrusted.test"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{ctx.base}/cases", json={**body, "confirmed": True}, headers=headers
        ).status_code
        == 422
    )
    assert ledger(ctx) == before
    resource = ctx.source[2].resources[0].resource_id
    assert (
        control(ctx.source, "http-downtime", "resource.down", {"resource_id": resource}).status_code
        == 200
    )
    changed = snapshot(ctx.source)
    result = client.post(f"{ctx.base}/cases", json=body, headers=headers)
    assert result.status_code == 200 and result.json()["case_id"] == ctx.case["case_id"]
    with Session(ctx.engine) as db:
        state = db.get(FactoryState, ctx.factory)
        saved = db.get(SnapshotRecord, state.snapshot_id)
        assert state.source_revision == changed.source.source_revision
        assert saved.content_hash == changed.content_hash
        new_input = db.scalar(
            select(CaseInput).where(CaseInput.input_key == "user:" + body["request_id"])
        )
        assert new_input.case_id == ctx.case["case_id"]
        assert new_input.payload == {
            "actor_id": ctx.accounts["planner"],
            "message": body["message"],
        }
    recorded = ledger(ctx)
    repeated = client.post(f"{ctx.base}/cases", json=body, headers=headers)
    assert repeated.status_code == 200
    assert normalized_case(repeated.json()) == normalized_case(result.json())
    assert ledger(ctx) == recorded
    conflict = client.post(
        f"{ctx.base}/cases", json={**body, "message": "Other content"}, headers=headers
    )
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert ledger(ctx) == recorded


def test_logged_in_reply_wakes_only_same_case_once_without_overwriting_source_facts(case_api):
    ctx = case_api
    task = information_task(ctx)
    client, headers = ctx.login("maintainer")
    path = f"{ctx.base}/human-tasks/{task['task_id']}/responses"
    body = response_input(task)
    before, source_before = ledger(ctx), snapshot(ctx.source)
    assert client.post(path, json=body, headers={"Origin": ctx.origin}).status_code == 403
    assert ledger(ctx) == before
    result = client.post(path, json=body, headers=headers)
    assert result.status_code == 200
    reply = result.json()
    assert reply["state"] == "RESPONDED" and reply["version"] == task["version"] + 1
    assert reply["response"]["source"] == "authenticated_human_information"
    assert reply["response"]["actor_id"] == ctx.accounts["maintainer"]
    assert reply["response"]["answer"] == {
        "repair_eta": "2030-09-16T03:30:00+00:00",
        "remaining_minutes": 27,
        "remaining_setup_minutes": 0,
    }
    with Session(ctx.engine) as db:
        inputs = list(
            db.scalars(
                select(CaseInput).where(
                    CaseInput.factory_id == ctx.factory, CaseInput.kind == "human_task.responded"
                )
            )
        )
        assert len(inputs) == 1
        wake = inputs[0]
        assert wake.case_id == ctx.case["case_id"] and wake.turn_id is None
        assert wake.payload["task_id"] == task["task_id"]
        assert wake.payload["response"] == reply["response"]
        case = db.get(CaseRecord, ctx.case["case_id"])
        assert case.version == before[0] + 1 and case.closure is None
        state = db.get(FactoryState, ctx.factory)
        stored = db.get(SnapshotRecord, state.snapshot_id)
        assert stored.content_hash == source_before.content_hash
    after = ledger(ctx)
    assert client.post(path, json=body, headers=headers).json() == reply
    assert ledger(ctx) == after
    conflict = client.post(
        path, json={**body, "answer": {**body["answer"], "remaining_minutes": 28}}, headers=headers
    )
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert ledger(ctx) == after
    assert snapshot(ctx.source).model_dump(mode="json") == source_before.model_dump(mode="json")


def test_replies_reject_extra_confirmation_timezone_and_noninteger_work_without_wake(case_api):
    ctx = case_api
    task = information_task(ctx)
    client, headers = ctx.login("maintainer")
    path = f"{ctx.base}/human-tasks/{task['task_id']}/responses"
    body = response_input(task)
    before = ledger(ctx)
    invalid = [
        {**body, "confirmed": True},
        {**body, "answer": {**body["answer"], "confirmed": True}},
        {**body, "answer": {**body["answer"], "repair_eta": "2030-09-16T11:30:00"}},
        *[
            {**body, "answer": {**body["answer"], "remaining_minutes": value}}
            for value in (None, True, -1, 1.5, "27")
        ],
    ]
    for data in invalid:
        response = client.post(path, json=data, headers=headers)
        assert response.status_code == 422
        assert ledger(ctx) == before
    planner, planner_headers = ctx.login("planner")
    assert planner.post(path, json=body, headers=planner_headers).status_code == 403
    assert ledger(ctx) == before


def test_factory_scope_role_inboxes_and_live_revocation_are_checked_before_data_access(case_api):
    ctx = case_api
    task = information_task(ctx)
    before = ledger(ctx)
    outsider, outside_headers = ctx.login("outsider")
    for task_id in (task["task_id"], "not-present"):
        assert outsider.get(f"{ctx.base}/human-tasks/{task_id}").status_code == 403
        assert (
            outsider.post(
                f"{ctx.base}/human-tasks/{task_id}/responses",
                json=response_input(task),
                headers=outside_headers,
            ).status_code
            == 403
        )
    assert outsider.get(f"{ctx.base}/cases/{ctx.case['case_id']}").status_code == 403
    assert outsider.get(f"{ctx.base}/cases/missing").status_code == 403
    warehouse, _ = ctx.login("warehouse")
    assert warehouse.get(f"{ctx.base}/human-tasks").json()["tasks"] == []
    assert warehouse.get(f"{ctx.base}/human-tasks/{task['task_id']}").status_code == 403
    planner, _ = ctx.login("planner")
    assert planner.get(f"{ctx.base}/human-tasks").json()["tasks"][0]["task_id"] == task["task_id"]
    admin, admin_headers = ctx.login("admin")
    assert admin.get(f"{ctx.base}/human-tasks").status_code == 403
    assert (
        admin.post(
            f"{ctx.base}/human-tasks/{task['task_id']}/responses",
            json=response_input(task),
            headers=admin_headers,
        ).status_code
        == 403
    )
    maintainer, headers = ctx.login("maintainer")
    with ctx.engine.begin() as db:
        db.execute(
            delete(Membership).where(
                Membership.user_id == ctx.accounts["maintainer"],
                Membership.factory_id == ctx.factory,
            )
        )
    assert maintainer.get(f"{ctx.base}/human-tasks").status_code == 403
    assert (
        maintainer.post(
            f"{ctx.base}/human-tasks/{task['task_id']}/responses",
            json=response_input(task),
            headers=headers,
        ).status_code
        == 403
    )
    with ctx.engine.begin() as db:
        db.execute(
            update(User).where(User.user_id == ctx.accounts["maintainer"]).values(active=False)
        )
    assert maintainer.get(f"{ctx.base}/cases").status_code == 401
    assert ledger(ctx) == before


def test_transfer_cancel_and_message_are_versioned_and_wake_same_case(case_api):
    ctx = case_api
    task = information_task(ctx)
    client, headers = ctx.login("manager")
    path = f"{ctx.base}/human-tasks/{task['task_id']}"
    body = {
        "request_id": str(uuid4()),
        "expected_task_version": 1,
        "target_role": "warehouse",
        "target_owner_id": ctx.accounts["warehouse"],
        "reason": "Please ask the warehouse to check the related receipt.",
    }
    result = client.post(path + "/transfers", json=body, headers=headers)
    assert result.status_code == 200
    transferred = result.json()
    assert (
        transferred["owner_role"] == "warehouse"
        and transferred["owner_id"] == ctx.accounts["warehouse"]
    )
    assert transferred["version"] == 2 and transferred["state"] == "OPEN"
    before = ledger(ctx)
    assert client.post(path + "/transfers", json=body, headers=headers).json() == transferred
    assert ledger(ctx) == before
    stale = client.post(
        path + "/cancellations",
        json={"request_id": str(uuid4()), "expected_task_version": 1, "reason": "Invalid question"},
        headers=headers,
    )
    assert stale.status_code == 409
    assert ledger(ctx) == before
    cancelled = client.post(
        path + "/cancellations",
        json={
            "request_id": str(uuid4()),
            "expected_task_version": 2,
            "reason": "The question is no longer valid; keep the history.",
        },
        headers=headers,
    )
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "CANCELLED"
    planner, planner_headers = ctx.login("planner")
    message = {
        "request_id": str(uuid4()),
        "message": "Cancel this invalid follow-up question and keep checking the orders.",
    }
    replied = planner.post(
        f"{ctx.base}/cases/{ctx.case['case_id']}/messages", json=message, headers=planner_headers
    )
    assert replied.status_code == 200 and replied.json()["case_id"] == ctx.case["case_id"]
    with Session(ctx.engine) as db:
        inputs = list(db.scalars(select(CaseInput).where(CaseInput.case_id == ctx.case["case_id"])))
        assert len([row for row in inputs if row.kind == "human_task.transferred"]) == 1
        assert len([row for row in inputs if row.kind == "human_task.cancelled"]) == 1
        assert len([row for row in inputs if row.payload.get("message") == message["message"]]) == 1
        record = db.get(HumanTaskRecord, task["task_id"])
        assert record.next_reminder_at is None and record.state == "CANCELLED"


def test_replay_retains_readable_history_but_rejects_official_case_and_task_writes(case_api):
    ctx = case_api
    task = information_task(ctx)
    admin, admin_headers = ctx.login("sim_admin")
    path = f"/api/admin/factories/{ctx.factory}/simulator/replays"
    result = admin.post(
        path,
        json={"request_id": str(uuid4()), "expected_run_id": ctx.source[2].run_id},
        headers=admin_headers,
    )
    assert result.status_code == 200
    assert result.json()["run_id"] != ctx.source[2].run_id
    assert snapshot(ctx.source).source.source_system == "factory-simulator-replay"
    before = ledger(ctx)
    planner, headers = ctx.login("planner")
    maintainer, maintain_headers = ctx.login("maintainer")
    body = {"request_id": str(uuid4()), "message": "A case that must not run in a replay."}
    response = planner.post(ctx.base + "/cases", json=body, headers=headers)
    assert response.status_code == 409 and response.json()["code"] == "REPLAY_READ_ONLY"
    response = planner.post(
        ctx.base + f"/cases/{ctx.case['case_id']}/messages", json=body, headers=headers
    )
    assert response.status_code == 409 and response.json()["code"] == "SOURCE_RUN_CHANGED"
    response = maintainer.post(
        f"{ctx.base}/human-tasks/{task['task_id']}/responses",
        json=response_input(task),
        headers=maintain_headers,
    )
    assert response.status_code == 409 and response.json()["code"] == "REPLAY_READ_ONLY"
    assert maintainer.get(f"{ctx.base}/human-tasks/{task['task_id']}").json()["state"] == "OPEN"
    assert planner.get(f"{ctx.base}/cases/{ctx.case['case_id']}").status_code == 200
    assert ledger(ctx) == before


def test_simulator_only_account_can_enter_workbench_without_planning_authority(case_api):
    ctx = case_api
    client, headers = ctx.login("sim_admin")
    factories = client.get("/api/factories")
    assert factories.status_code == 200
    assert {item["factory_id"] for item in factories.json()["factories"]} == {ctx.factory}
    assert client.get(ctx.base + "/workspace").status_code == 200
    assert client.get(f"/api/admin/factories/{ctx.factory}/simulator").status_code == 200
    assert (
        client.get(ctx.base.replace(ctx.factory, ctx.factory + "-other") + "/workspace").status_code
        == 403
    )
    assert (
        client.post(
            ctx.base + "/solve", json={"request_id": "sim-cannot-plan"}, headers=headers
        ).status_code
        == 403
    )
    assert (
        client.post(
            ctx.base + "/cases",
            json={"request_id": "sim-cannot-agent", "message": "Schedule"},
            headers=headers,
        ).status_code
        == 403
    )


@pytest.mark.parametrize("role", ["planner", "manager", "maintainer", "sim_admin", "outsider"])
def test_business_acceptance_and_outside_case_study_routes_are_not_available(case_api, role):
    ctx = case_api
    client, headers = ctx.login(role)
    before = ledger(ctx)
    for method, path in (
        ("get", f"/api/admin/factories/{ctx.factory}/business-studies"),
        ("post", f"/api/admin/factories/{ctx.factory}/business-accept"),
        ("post", ctx.base + "/assistant/business-studies"),
    ):
        result = getattr(client, method)(path, headers=headers)
        assert result.status_code == 404
    assert ledger(ctx) == before


def test_simulator_command_cannot_bypass_disabled_business_acceptance(case_api):
    ctx = case_api
    client, headers = ctx.login("sim_admin")
    before = snapshot(ctx.source)
    before_ledger = ledger(ctx)
    result = client.post(
        f"/api/admin/factories/{ctx.factory}/simulator/commands",
        headers=headers,
        json={
            "request_id": "no-business-command-bypass",
            "run_id": before.run_id,
            "kind": "business.accept",
            "payload": {},
        },
    )
    assert result.status_code == 409 and result.json()["code"] == "BUSINESS_ACCEPT_UNSUPPORTED"
    assert snapshot(ctx.source).content_hash == before.content_hash
    assert ledger(ctx) == before_ledger


def test_assistant_business_results_require_current_factory_case(case_api):
    ctx = case_api
    visible_job = str(uuid4())
    with Session(ctx.engine) as db, db.begin():
        state = db.get(FactoryState, ctx.factory)
        for job_id, factory_id, case_id in (
            (visible_job, ctx.factory, ctx.case["case_id"]),
            (str(uuid4()), ctx.factory, None),
            (str(uuid4()), ctx.factory, "missing-case"),
            (str(uuid4()), ctx.factory + "-other", ctx.case["case_id"]),
        ):
            db.add(
                SolveJob(
                    job_id=job_id,
                    factory_id=factory_id,
                    request_id=job_id,
                    requester_id=ctx.accounts["planner"],
                    snapshot_id=state.snapshot_id,
                    case_id=case_id,
                    allow_overtime=False,
                    time_limit=1,
                    state="FAILED",
                    error_code="TEST_RESULT",
                    created_at=datetime.now(UTC),
                    business_request={"kind": "material_shortage"},
                )
            )
    try:
        manager, _ = ctx.login("manager")
        result = manager.get(ctx.base + "/assistant")
        assert result.status_code == 200
        studies = result.json()["business_studies"]
        assert [row["job_id"] for row in studies] == [visible_job]
        assert studies[0]["case_id"] == ctx.case["case_id"]
        for role in ("maintainer", "sim_admin", "outsider"):
            client, _ = ctx.login(role)
            assert client.get(ctx.base + "/assistant").status_code == 403
    finally:
        with Session(ctx.engine) as db, db.begin():
            db.execute(delete(SolveJob).where(SolveJob.factory_id == ctx.factory + "-other"))
