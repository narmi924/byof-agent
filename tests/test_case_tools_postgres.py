"""Actual PostgreSQL tool effects, recovery and execution-based closure boundaries."""

import os
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session
from test_dynamic_factory_postgres import control
from test_dynamic_factory_postgres import dynamic_source as dynamic_source
from test_publication_postgres import approve_and_commit
from test_publication_postgres import publishing as publishing

from packages.agent import case_tools
from packages.agent.case_tools import execute_operation, recover_operation
from packages.agent.cases import create_case
from packages.agent.cases_store import CaseInput, CaseOperation, CaseRecord, CaseTurn
from packages.agent.human_tasks import HumanTaskRecord, TaskAction, TaskReminder, create_task
from packages.auth import AccessError, Grant, Principal
from packages.domain.models import Candidate, Snapshot, canonical_hash
from packages.persistence import Membership, User, connect
from packages.planning.publication import Publication, deliver_one
from packages.planning.service import request_solve, synchronize
from packages.planning.store import (
    ApprovalRecord,
    CandidateRecord,
    FactoryState,
    SnapshotRecord,
    SolveJob,
)


@pytest.fixture
def tools_case(publishing):
    source, _, _, actor, candidate_id, _ = publishing
    engine, factory = source[3], source[2].factory_id
    result = create_case(
        engine,
        actor,
        factory,
        "tool-case",
        "Handle the current production plan and follow its execution.",
    )
    with Session(engine) as db, db.begin():
        case = db.get(CaseRecord, result["case_id"])
        case.context = {**case.context, "candidate_ids": [candidate_id]}
    try:
        yield publishing, result["case_id"]
    finally:
        owner = connect(os.environ["TEST_MIGRATION_DATABASE_URL"])
        assert owner.url.database == "byof_test"
        with owner.begin() as db:
            for table in (
                TaskReminder,
                TaskAction,
                HumanTaskRecord,
                CaseOperation,
                CaseTurn,
                CaseInput,
                CaseRecord,
            ):
                db.execute(delete(table).where(table.factory_id == factory))
        owner.dispose()


def operation(context, action, parameters):
    publishing, case_id = context
    source, _, _, _, _, _ = publishing
    engine, factory = source[3], source[2].factory_id
    now = datetime.now(UTC)
    with Session(engine, expire_on_commit=False) as db, db.begin():
        case = db.get(CaseRecord, case_id)
        current = db.get(FactoryState, factory)
        turn = CaseTurn(
            turn_id=uuid4().hex,
            case_id=case_id,
            factory_id=factory,
            state="RUNNING",
            created_at=now,
            deadline=now + timedelta(minutes=2),
            next_step=0,
        )
        db.add(turn)
        row = CaseOperation(
            operation_id=uuid4().hex,
            case_id=case_id,
            factory_id=factory,
            turn_id=turn.turn_id,
            step=0,
            action=action,
            parameters=parameters,
            parameter_hash=canonical_hash({"action": action, "parameters": parameters}),
            expected_case_version=case.version,
            snapshot_id=current.snapshot_id,
            state="STARTED",
            created_at=now,
        )
        db.add(row)
    return row


def run(context, action, parameters):
    op = operation(context, action, parameters)
    publishing, _ = context
    return execute_operation(publishing[0][3], publishing[3], op)


def count(engine, table, factory):
    with Session(engine) as db:
        return db.scalar(select(func.count()).select_from(table).where(table.factory_id == factory))


def test_query_uses_current_source_snapshot_identity_and_policy(tools_case):
    context, _ = tools_case
    source, reader, _, _, _, _ = context
    engine, factory = source[3], source[2].factory_id
    assert control(source, "query-new-clock", "clock.step").status_code == 200
    current = synchronize(engine, reader, factory)
    result = run(
        tools_case,
        "query",
        {"entity": "inventory", "identity": current.inventory[0].material_id, "offset": 0},
    )
    assert result["status"] == "OK" and result["snapshot_hash"] == current.content_hash
    assert result["source_revision"] == current.source.source_revision
    assert result["items"] == [current.inventory[0].model_dump(mode="json")]
    assert result["total"] == 1 and result["truncated"] is False
    policy = run(tools_case, "query", {"entity": "policy", "identity": None, "offset": 0})
    assert policy["items"] == [current.profile.policy.model_dump(mode="json")]
    products = run(tools_case, "query", {"entity": "products", "identity": None, "offset": 0})
    assert products["items"] == sorted(
        [product.model_dump(mode="json") for product in current.profile.products],
        key=lambda product: product["product_id"],
    )


def test_query_pagination_has_no_hidden_extra_rows_and_exposes_stale_age(tools_case):
    context, _ = tools_case
    source, _, _, actor, _, _ = context
    engine, factory = source[3], source[2].factory_id
    with Session(engine) as db, db.begin():
        state = db.get(FactoryState, factory)
        raw = dict(db.get(SnapshotRecord, state.snapshot_id).document)
        raw.pop("content_hash")
        raw["snapshot_id"] = uuid4().hex
        raw["workers"] = [
            {
                **raw["workers"][index % len(raw["workers"])],
                "worker_id": f"paged-worker-{index:03d}",
            }
            for index in range(63)
        ]
        current = Snapshot.model_validate(raw)
        db.add(
            SnapshotRecord(
                snapshot_id=current.snapshot_id,
                factory_id=factory,
                content_hash=current.content_hash,
                document=current.model_dump(mode="json"),
                created_at=datetime.now(UTC),
            )
        )
        state.snapshot_id = current.snapshot_id
        state.last_synced_at = datetime.now(UTC) - timedelta(seconds=31)
    first = run(tools_case, "query", {"entity": "workers", "identity": None, "offset": 0})
    last = run(tools_case, "query", {"entity": "workers", "identity": None, "offset": 50})
    assert first["data_freshness"] == "STALE" and first["total"] == 63
    assert len(first["items"]) == 50 and first["next_offset"] == 50 and first["truncated"]
    assert len(last["items"]) == 13 and not last["truncated"] and last["next_offset"] is None
    assert {r["worker_id"] for r in first["items"]}.isdisjoint(
        r["worker_id"] for r in last["items"]
    )
    op = operation(tools_case, "solve_scenario", {"allow_overtime": False, "time_limit": 2})
    assert execute_operation(engine, actor, op)["code"] == "FRESH_FACTS_REQUIRED"


def test_solver_job_is_durable_idempotent_and_recoverable_after_new_case_input(tools_case):
    context, case_id = tools_case
    source, _, _, actor, _, _ = context
    engine, factory = source[3], source[2].factory_id
    op = operation(tools_case, "solve_scenario", {"allow_overtime": False, "time_limit": 2})
    assert recover_operation(engine, actor, op) is None
    result = execute_operation(engine, actor, op)
    assert result["status"] == "PENDING" and result["job_state"] == "QUEUED"
    assert "candidate_id" not in result
    with Session(engine) as db, db.begin():
        case = db.get(CaseRecord, case_id)
        case.version += 1
        case.context = {**case.context, "new_input": "A new event arrived while waiting"}
    assert recover_operation(engine, actor, op) == result
    assert execute_operation(engine, actor, op)["job_id"] == result["job_id"]
    assert (
        count(engine, SolveJob, factory) == 2
    )  # One fixture candidate plus exactly one queued request.
    with Session(engine) as db:
        job = db.get(SolveJob, result["job_id"])
        assert job.request_id == op.operation_id and job.snapshot_id == result["snapshot_id"]
        assert job.case_id == case_id


def test_solver_recovery_rejects_a_job_bound_to_a_different_case(tools_case):
    context, _ = tools_case
    source, _, _, actor, *_ = context
    engine = source[3]
    op = operation(tools_case, "solve_scenario", {"allow_overtime": False, "time_limit": 2})
    result = execute_operation(engine, actor, op)
    assert result["status"] == "PENDING"
    with Session(engine) as db, db.begin():
        db.get(SolveJob, result["job_id"]).case_id = "another-case"
    recovered = recover_operation(engine, actor, op)
    assert recovered["status"] == "REJECTED" and recovered["code"] == "IDEMPOTENCY_CONFLICT"


def test_candidate_comparison_and_approval_request_never_grant_approval(tools_case):
    context, _ = tools_case
    source, _, _, actor, candidate_id, _ = context
    engine, factory = source[3], source[2].factory_id
    comparison = run(tools_case, "compare_candidates", {"candidate_ids": [candidate_id]})
    assert comparison["status"] == "OK" and len(comparison["candidates"]) == 1
    with Session(engine) as db:
        saved = db.get(CandidateRecord, candidate_id).document
    compared = comparison["candidates"][0]
    assert compared["objective"] == saved["objective"]
    assert compared["native_status"] == saved["native_status"]
    assert compared["current_checker"]["status"] == "PASS"
    op = operation(tools_case, "request_approval", {"candidate_id": candidate_id})
    requested = execute_operation(engine, actor, op)
    assert requested["status"] == "PENDING" and requested["approval_state"] == "NOT_GRANTED"
    assert requested["required_action"] == "EXPLICIT_APPROVAL_POST"
    assert requested["task"]["owner_role"] == "planner"
    assert requested["task"]["fields"] == ["comment"]
    assert recover_operation(engine, actor, op) == requested
    assert count(engine, ApprovalRecord, factory) == count(engine, Publication, factory) == 0


def test_stale_candidate_is_visible_but_cannot_create_approval_task(tools_case):
    context, _ = tools_case
    source, reader, _, _, candidate_id, _ = context
    engine, factory = source[3], source[2].factory_id
    assert control(source, "candidate-stale-clock", "clock.step").status_code == 200
    synchronize(engine, reader, factory)
    comparison = run(tools_case, "compare_candidates", {"candidate_ids": [candidate_id]})
    assert comparison["code"] == "STALE_CANDIDATE"
    assert comparison["candidates"][0]["current"] is False
    requested = run(tools_case, "request_approval", {"candidate_id": candidate_id})
    assert requested["code"] == "STALE_CANDIDATE"
    assert count(engine, HumanTaskRecord, factory) == 0


def test_information_task_recovery_handles_reused_open_task_without_notifications(tools_case):
    context, _ = tools_case
    source, _, _, actor, _, _ = context
    engine, factory = source[3], source[2].factory_id
    params = {
        "question": "When can the machine recover?",
        "role": "maintainer",
        "subject_id": source[2].resources[0].resource_id,
        "fields": ["repair_eta", "remaining_minutes"],
        "deadline_minutes": 15,
    }
    first = operation(tools_case, "request_information", params)
    second = operation(tools_case, "request_information", params)
    requested = execute_operation(engine, actor, first)
    reused = execute_operation(engine, actor, second)
    assert requested["task_id"] == reused["task_id"]
    assert requested["task"]["send_state"] == "NOT_ENABLED"
    assert recover_operation(engine, actor, second) == reused
    assert count(engine, HumanTaskRecord, factory) == 1 and count(engine, TaskAction, factory) == 2
    missing = {**params, "subject_id": "foreign-resource"}
    assert run(tools_case, "request_information", missing)["code"] == "SUBJECT_NOT_FOUND"
    assert count(engine, HumanTaskRecord, factory) == 1


def test_preference_proposal_is_case_scoped_durable_and_inactive(tools_case):
    context, case_id = tools_case
    source, _, _, actor, _, _ = context
    engine, factory = source[3], source[2].factory_id
    with Session(engine) as db:
        before = db.get(FactoryState, factory).snapshot_id
    op = operation(tools_case, "propose_preference", {"selection": "stability_first"})
    assert recover_operation(engine, actor, op) is None
    result = execute_operation(engine, actor, op)
    assert result["status"] == "PENDING" and result["proposal"]["state"] == "PENDING_CONFIRMATION"
    assert result["proposal"]["scope_type"] == "CASE" and result["proposal"]["scope_id"] == case_id
    assert result["proposal"]["bounds"] is None
    assert result["proposal"]["objective_order"][:2] == ["changed_operations", "total_start_shift"]
    assert recover_operation(engine, actor, op) == execute_operation(engine, actor, op) == result
    with Session(engine) as db:
        case = db.get(CaseRecord, case_id)
        assert list(case.context["pending_preference"]) == [op.operation_id]
        assert db.get(FactoryState, factory).snapshot_id == before


def test_wait_and_handoff_do_not_close_or_transfer_case(tools_case):
    context, case_id = tools_case
    source, _, _, _, _, _ = context
    engine, factory = source[3], source[2].factory_id
    waiting = run(
        tools_case,
        "wait",
        {"reason": "Waiting for the machine owner to reply.", "recheck_minutes": 12},
    )
    assert (
        waiting["status"] == "WAITING"
        and waiting["recheck_minutes"] == 12
        and waiting["clock"] == "real"
    )
    handoff = run(
        tools_case,
        "handoff",
        {"reason": "An owner must confirm the production boundary.", "role": "manager"},
    )
    assert handoff["status"] == "PENDING" and handoff["handoff_state"] == "AWAITING_ACCEPTANCE"
    assert count(engine, HumanTaskRecord, factory) == 1
    with Session(engine) as db:
        case = db.get(CaseRecord, case_id)
        assert case.state not in {"RESOLVED", "HANDED_OFF"} and case.closure is None


@pytest.mark.parametrize("kind", ["inactive", "revoked", "foreign_grant"])
def test_permissions_are_rechecked_before_effects_and_recovery(tools_case, kind):
    context, _ = tools_case
    source, _, _, actor, _, _ = context
    engine, factory = source[3], source[2].factory_id
    op = operation(tools_case, "solve_scenario", {"allow_overtime": False, "time_limit": 2})
    with engine.begin() as db:
        if kind == "inactive":
            db.execute(update(User).where(User.user_id == actor.user_id).values(active=False))
        elif kind == "revoked":
            db.execute(delete(Membership).where(Membership.user_id == actor.user_id))
        else:
            actor = Principal(
                user_id=actor.user_id,
                username=actor.username,
                grants=(Grant(factory_id="other-factory", role="planner"),),
            )
    assert execute_operation(engine, actor, op)["status"] == "REJECTED"
    assert recover_operation(engine, actor, op)["status"] == "REJECTED"
    assert count(engine, SolveJob, factory) == 1


@pytest.mark.parametrize(
    "action,params",
    [
        ("shell", {"command": "whoami"}),
        ("solve_scenario", {"allow_overtime": False, "time_limit": 2, "confirmed": True}),
        (
            "request_information",
            {
                "question": "Reply",
                "role": "manager",
                "subject_id": "https://example.com/private",
                "fields": ["comment"],
                "deadline_minutes": 2,
            },
        ),
    ],
)
def test_invalid_action_has_no_business_effects(tools_case, action, params):
    context, _ = tools_case
    source = context[0]
    engine, factory = source[3], source[2].factory_id
    result = run(tools_case, action, params)
    assert result["status"] == "REJECTED" and result["code"] == "INVALID_TOOL_INPUT"
    assert count(engine, SolveJob, factory) == 1
    assert count(engine, HumanTaskRecord, factory) == count(engine, Publication, factory) == 0


def test_tampered_operation_and_cross_factory_candidate_are_rejected(tools_case):
    context, _ = tools_case
    source, _, _, actor, candidate_id, _ = context
    engine = source[3]
    op = operation(tools_case, "solve_scenario", {"allow_overtime": False, "time_limit": 2})
    op.parameters = {"allow_overtime": True, "time_limit": 2}
    assert execute_operation(engine, actor, op)["code"] == "INVALID_OPERATION"
    foreign_factory = "foreign-" + uuid4().hex
    with Session(engine) as db, db.begin():
        original = db.get(CandidateRecord, candidate_id)
        facts = deepcopy(db.get(SnapshotRecord, original.snapshot_id).document)
        facts.pop("content_hash")
        facts["factory_id"] = facts["profile"]["factory_id"] = foreign_factory
        facts["snapshot_id"] = uuid4().hex
        foreign_snapshot = Snapshot.model_validate(facts)
        candidate = deepcopy(original.document)
        candidate.pop("content_hash")
        candidate["candidate_id"] = uuid4().hex
        candidate["factory_id"] = foreign_factory
        candidate["binding"]["snapshot_hash"] = candidate["checker"]["snapshot_hash"] = (
            foreign_snapshot.content_hash
        )
        foreign_candidate = Candidate.model_validate(candidate)
        candidate_id = foreign_candidate.candidate_id
        db.add(
            SnapshotRecord(
                snapshot_id=foreign_snapshot.snapshot_id,
                factory_id=foreign_factory,
                content_hash=foreign_snapshot.content_hash,
                document=foreign_snapshot.model_dump(mode="json"),
                created_at=datetime.now(UTC),
            )
        )
        db.flush()
        db.add(
            CandidateRecord(
                candidate_id=candidate_id,
                factory_id=foreign_factory,
                snapshot_id=foreign_snapshot.snapshot_id,
                content_hash=foreign_candidate.content_hash,
                document=foreign_candidate.model_dump(mode="json"),
                created_at=datetime.now(UTC),
            )
        )
    try:
        compared = run(tools_case, "compare_candidates", {"candidate_ids": [candidate_id]})
        assert compared["code"] == "CANDIDATE_NOT_FOUND" and "candidates" not in compared
        requested = run(tools_case, "request_approval", {"candidate_id": candidate_id})
        assert requested["code"] == "CANDIDATE_NOT_FOUND"
    finally:
        owner = connect(os.environ["TEST_MIGRATION_DATABASE_URL"])
        assert owner.url.database == "byof_test"
        with owner.begin() as db:
            db.execute(delete(CandidateRecord).where(CandidateRecord.factory_id == foreign_factory))
            db.execute(delete(SnapshotRecord).where(SnapshotRecord.factory_id == foreign_factory))
        owner.dispose()


def test_finish_requires_plan_running_freshness_and_no_open_tasks(tools_case):
    context, case_id = tools_case
    source, reader, writer, actor, _, _ = context
    engine, factory = source[3], source[2].factory_id
    _, release = approve_and_commit(context)
    params = {
        "evidence_release_id": release.release_id,
        "risk_summary": "The plan is active and production keeps running.",
    }
    assert run(tools_case, "finish", params)["code"] == "EXECUTION_NOT_COMPLETED"
    assert deliver_one(engine, reader, writer)
    synchronize(engine, reader, factory)
    assert run(tools_case, "finish", params)["code"] == "EXECUTION_NOT_COMPLETED"
    assert control(source, "start-execution", "clock.step", {"minutes": 60}).status_code == 200
    actual = synchronize(engine, reader, factory)
    assert len(actual.actuals) < 8 or any(a.state != "COMPLETED" for a in actual.actuals)
    result = run(tools_case, "finish", params)
    assert result["status"] == "RESOLVED", result
    assert result["closing_evidence"]["snapshot_hash"] == actual.content_hash
    assert result["closing_evidence"]["execution_state"] == "IN_PROGRESS"
    # Runtime owns closure; the tool only returns verified evidence.
    with Session(engine) as db:
        assert db.get(CaseRecord, case_id).closure is None
    with Session(engine) as db, db.begin():
        state = db.get(FactoryState, factory)
        newer = actual.model_dump(mode="json", exclude={"content_hash"})
        newer["snapshot_id"] = uuid4().hex
        newer["scope_version"] += 1
        newer["planning_revision"] += 1
        newer["source"]["source_revision"] = str(int(newer["source"]["source_revision"]) + 1)
        newer["orders"].append({**newer["orders"][0], "order_id": "new-order-after-approval"})
        expanded = Snapshot.model_validate(newer)
        db.add(
            SnapshotRecord(
                snapshot_id=expanded.snapshot_id,
                factory_id=factory,
                content_hash=expanded.content_hash,
                document=expanded.model_dump(mode="json"),
                created_at=datetime.now(UTC),
            )
        )
        state.snapshot_id, state.source_revision = (
            expanded.snapshot_id,
            expanded.source.source_revision,
        )
    # The change came after this plan went live: it waits among the new changes.
    later = run(tools_case, "finish", params)
    assert later["status"] == "RESOLVED", later
    assert "Field changes" in later["summary"]
    # A later plan decided in another conversation took over; this conversation may close.
    with Session(engine) as db, db.begin():
        db.add(
            CandidateRecord(
                candidate_id="plan-from-another-conversation",
                factory_id=factory,
                snapshot_id=actual.snapshot_id,
                content_hash="e" * 64,
                document={},
                created_at=datetime.now(UTC),
            )
        )
        taken = expanded.model_dump(mode="json", exclude={"content_hash"})
        taken["snapshot_id"] = uuid4().hex
        taken["active_plan_version"] = "plan-from-another-conversation"
        taken["active_plan_hash"] = "e" * 64
        moved = Snapshot.model_validate(taken)
        db.add(
            SnapshotRecord(
                snapshot_id=moved.snapshot_id,
                factory_id=factory,
                content_hash=moved.content_hash,
                document=moved.model_dump(mode="json"),
                created_at=datetime.now(UTC),
            )
        )
        db.get(FactoryState, factory).snapshot_id = moved.snapshot_id
    taken_over = run(tools_case, "finish", params)
    assert taken_over["status"] == "RESOLVED", taken_over
    assert "other conversations" in taken_over["summary"]
    # Own plan still in effect but the change was taken up by a newer conversation.
    with Session(engine) as db, db.begin():
        db.get(FactoryState, factory).snapshot_id = expanded.snapshot_id
    create_case(engine, actor, factory, "newer-change", "Check the new order", start_new=True)
    handled = run(tools_case, "finish", params)
    assert handled["status"] == "RESOLVED", handled
    assert "other conversations" in handled["summary"]
    with engine.begin() as db:
        db.execute(
            update(FactoryState)
            .where(FactoryState.factory_id == factory)
            .values(snapshot_id=actual.snapshot_id, source_revision=actual.source.source_revision)
        )
    run(
        tools_case,
        "handoff",
        {"reason": "An owner still needs to confirm the risk handling.", "role": "manager"},
    )
    assert run(tools_case, "finish", params)["code"] == "OPEN_HUMAN_TASKS"
    with engine.begin() as db:
        db.execute(
            update(FactoryState)
            .where(FactoryState.factory_id == factory)
            .values(last_synced_at=datetime.now(UTC) - timedelta(seconds=31))
        )
    assert run(tools_case, "finish", params)["code"] == "FRESH_FACTS_REQUIRED"


def test_finish_rejects_other_case_release_and_changed_run(tools_case):
    context, case_id = tools_case
    source, _, _, _, _, _ = context
    engine, factory = source[3], source[2].factory_id
    _, release = approve_and_commit(context)
    params = {"evidence_release_id": release.release_id, "risk_summary": "Requesting closure."}
    with Session(engine) as db, db.begin():
        case = db.get(CaseRecord, case_id)
        case.context = {**case.context, "candidate_ids": []}
    assert run(tools_case, "finish", params)["code"] == "RELEASE_NOT_IN_CASE"
    with engine.begin() as db:
        db.execute(
            update(FactoryState)
            .where(FactoryState.factory_id == factory)
            .values(run_id="another-run")
        )
    assert run(tools_case, "finish", params)["code"] == "SOURCE_RUN_CHANGED"


@pytest.mark.parametrize("action", ["solve_scenario", "request_information"])
@pytest.mark.parametrize("revocation", ["membership", "inactive"])
def test_revocation_between_tool_precheck_and_effect_transaction_has_no_effect(
    tools_case, monkeypatch, action, revocation
):
    context, _ = tools_case
    source, _, _, actor, _, _ = context
    engine, factory = source[3], source[2].factory_id
    parameters = (
        {"allow_overtime": False, "time_limit": 2}
        if action == "solve_scenario"
        else {
            "question": "When will it recover?",
            "role": "maintainer",
            "subject_id": source[2].resources[0].resource_id,
            "fields": ["repair_eta"],
            "deadline_minutes": 10,
        }
    )
    name = "request_solve" if action == "solve_scenario" else "create_task"
    original = getattr(case_tools, name)
    reached = []

    def revoke_before_write(*args, **kwargs):
        reached.append(True)
        with engine.begin() as db:
            if revocation == "membership":
                db.execute(delete(Membership).where(Membership.user_id == actor.user_id))
            else:
                db.execute(update(User).where(User.user_id == actor.user_id).values(active=False))
        return original(*args, **kwargs)

    monkeypatch.setattr(case_tools, name, revoke_before_write)
    result = run(tools_case, action, parameters)
    assert reached == [True] and result["code"] == "AUTHORIZATION_REVOKED"
    assert count(engine, SolveJob, factory) == 1
    assert count(engine, HumanTaskRecord, factory) == count(engine, TaskAction, factory) == 0


def test_implicit_task_actor_is_case_owner_with_locked_authorization_even_on_retry(tools_case):
    context, case_id = tools_case
    source, _, _, actor, _, _ = context
    engine, factory = source[3], source[2].factory_id
    params = {
        "case_id": case_id,
        "factory_id": factory,
        "operation_id": uuid4().hex,
        "question": "Please check the information.",
        "role": "planner",
        "subject_id": case_id,
        "fields": ["comment"],
        "deadline_minutes": 10,
    }
    original = create_task(engine, **params)
    with Session(engine) as db:
        creation = db.scalar(select(TaskAction).where(TaskAction.task_id == original["task_id"]))
        assert creation.actor_id == actor.user_id
    with engine.begin() as db:
        db.execute(delete(Membership).where(Membership.user_id == actor.user_id))
    for request_id in (params["operation_id"], uuid4().hex):
        with pytest.raises(AccessError) as denied:
            create_task(engine, **{**params, "operation_id": request_id})
        assert denied.value.code == "AUTHORIZATION_REVOKED"
    assert count(engine, HumanTaskRecord, factory) == 1 and count(engine, TaskAction, factory) == 1


def test_solver_idempotency_does_not_bypass_current_authorization(tools_case):
    context, _ = tools_case
    source, _, _, actor, _, _ = context
    engine, factory = source[3], source[2].factory_id
    with engine.begin() as db:
        db.execute(delete(Membership).where(Membership.user_id == actor.user_id))
    with pytest.raises(AccessError) as denied:
        request_solve(
            engine, actor, factory, request_id="solve", allow_overtime=False, time_limit=2
        )
    assert denied.value.code == "AUTHORIZATION_REVOKED"
    assert count(engine, SolveJob, factory) == 1
