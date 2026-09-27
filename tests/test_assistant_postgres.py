"""Single-reviewer commands against isolated PostgreSQL and the real simulator HTTP API."""

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session
from test_case_runtime_postgres import case_context as case_context
from test_dynamic_factory_postgres import control, snapshot
from test_dynamic_factory_postgres import dynamic_source as dynamic_source
from test_publication_postgres import publishing as publishing

from packages.agent.assistant import AssistantRequest, enqueue, learning, process_one
from packages.agent.assistant_store import AssistantAction
from packages.agent.case_runtime import process_case
from packages.agent.cases import (
    create_case,
    get_case,
    ingest_sources,
    list_risk_suggestions,
    message_case,
    wake_completed_jobs,
)
from packages.agent.cases_store import CaseInput, CaseRecord
from packages.agent.recovery_paths import recovery_paths
from packages.auth import AccessError, Grant, Principal
from packages.domain.execution import ReplayStart, SimulatorCommand
from packages.integrations.factory_http import FactoryControls
from packages.persistence import Membership, User, connect
from packages.planning.publication import Publication, deliver_one
from packages.planning.service import request_solve, synchronize
from packages.planning.store import CandidateRecord, SolveJob
from services.factory_sim.service import run_due_tick
from services.factory_sim.storage import SourceAction, World
from services.solver_worker.main import run_once


@pytest.fixture
def reviewer(case_context):
    context, case = case_context
    source, reader, writer, actor, candidate_id, digest = context
    engine, factory = source[3], source[2].factory_id
    roles = ("sim_admin", "manager", "maintainer", "warehouse", "team_lead")
    with Session(engine) as db, db.begin():
        for role in roles:
            db.add(Membership(user_id=actor.user_id, factory_id=factory, role=role))
    actor = actor.model_copy(
        update={"grants": actor.grants + tuple(Grant(factory_id=factory, role=r) for r in roles)}
    )
    controls = FactoryControls(str(source[0].base_url), source[1]["controller"])
    try:
        yield source, reader, writer, actor, candidate_id, digest, controls, case
    finally:
        controls.close()
        with Session(engine) as db, db.begin():
            db.execute(delete(AssistantAction).where(AssistantAction.factory_id == factory))


def request(ctx, kind, payload=None, request_id=None):
    source, _, _, actor, *_ = ctx
    body = AssistantRequest(
        request_id=request_id or uuid4().hex,
        run_id=source[2].run_id,
        kind=kind,
        payload=payload or {},
    )
    return enqueue(source[3], actor, source[2].factory_id, body)


def approval(ctx):
    return request(
        ctx, "approve", {"candidate_id": ctx[4], "candidate_hash": ctx[5], "priority": "overtime"}
    )


def run(ctx):
    return process_one(ctx[0][3], ctx[1], ctx[6])


def ready(ctx):
    with Session(ctx[0][3]) as db, db.begin():
        db.execute(
            update(AssistantAction)
            .where(AssistantAction.factory_id == ctx[0][2].factory_id)
            .values(next_attempt_at=datetime.now(UTC))
        )


def test_displayed_computation_retry_can_be_submitted_and_processed(reviewer):
    ctx = reviewer
    source, _, _, actor, *_ = ctx
    factory, engine = source[2].factory_id, source[3]
    job = request_solve(
        engine,
        actor,
        factory,
        request_id="retry-evidence",
        allow_overtime=False,
        time_limit=30,
        case_id=ctx[7]["case_id"],
    )
    with Session(engine) as db, db.begin():
        saved = db.get(SolveJob, job.job_id)
        saved.state, saved.error_code = "FAILED", "WORKER_FAILURE"
    queued = request(
        ctx,
        "recover",
        {
            "case_id": ctx[7]["case_id"],
            "path_id": "recovery-retry_computation",
        },
    )
    assert run(ctx)
    with Session(engine) as db:
        saved = db.get(AssistantAction, queued["action_id"])
        assert saved.state == "DONE", saved.error_code
        assert saved.result["path"]["kind"] == "retry_computation"


def test_case_action_history_keeps_legacy_approval_before_global_limit(reviewer, monkeypatch):
    from types import SimpleNamespace

    from services.api import assistant as api

    source, _, _, actor, candidate_id, _, _, case = reviewer
    engine, factory = source[3], source[2].factory_id
    chosen = approval(reviewer)
    now = datetime.now(UTC)
    with Session(engine) as db, db.begin():
        saved_case = db.get(CaseRecord, case["case_id"])
        saved_case.context = {**saved_case.context, "candidate_ids": [candidate_id]}
        for index in range(65):
            identity = "unrelated-action-" + str(index)
            db.add(
                AssistantAction(
                    action_id=identity,
                    factory_id=factory,
                    user_id=actor.user_id,
                    request_id=identity,
                    run_id=source[2].run_id,
                    kind="start",
                    payload={},
                    result={"case_id": "unrelated-case"},
                    state="DONE",
                    created_at=now,
                    next_attempt_at=now,
                )
            )
    monkeypatch.setattr(api, "principal", lambda _: actor)
    request_context = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=engine)))
    assert chosen["action_id"] not in {
        row["action_id"] for row in api.state(factory, request_context)["actions"]
    }
    scoped = api.state(factory, request_context, case_id=case["case_id"])
    assert [row["action_id"] for row in scoped["actions"]] == [chosen["action_id"]]


def test_approval_survives_page_closure_and_starts_only_after_source_acceptance(reviewer):
    ctx = reviewer
    queued = approval(ctx)
    assert run(ctx)
    with Session(ctx[0][3]) as db:
        row = db.get(AssistantAction, queued["action_id"])
        assert row.state == "QUEUED" and row.result["release_id"]
    assert snapshot(ctx[0]).active_plan_version is None
    assert deliver_one(ctx[0][3], ctx[1], ctx[2])
    ready(ctx)
    assert run(ctx)
    with Session(ctx[0][3]) as db:
        assert db.get(AssistantAction, queued["action_id"]).state == "DONE"
        assert (
            db.scalar(
                select(func.count())
                .select_from(Publication)
                .where(Publication.factory_id == ctx[0][2].factory_id)
            )
            == 1
        )
    with Session(ctx[0][4]) as db:
        assert db.get(World, ctx[0][2].factory_id).mode == "RUNNING"
    assert not run(ctx)


def test_manager_approval_starts_first_plan_without_simulator_permission(reviewer):
    ctx = reviewer
    engine, factory = ctx[0][3], ctx[0][2].factory_id
    with Session(engine) as db, db.begin():
        db.execute(
            delete(Membership).where(
                Membership.user_id == ctx[3].user_id,
                Membership.factory_id == factory,
                Membership.role == "sim_admin",
            )
        )
    manager = ctx[3].model_copy(
        update={"grants": tuple(g for g in ctx[3].grants if g.role != "sim_admin")}
    )
    ctx = (*ctx[:3], manager, *ctx[4:])
    with pytest.raises(AccessError):
        request(ctx, "outage", {"resource_id": "KIT-01", "minutes": 30})
    queued = approval(ctx)
    assert run(ctx)
    assert deliver_one(engine, ctx[1], ctx[2])
    ready(ctx)
    assert run(ctx)
    with Session(engine) as db:
        assert db.get(AssistantAction, queued["action_id"]).state == "DONE"
    with Session(ctx[0][4]) as db:
        world = db.get(World, factory)
        assert world.mode == "RUNNING" and world.interval_ms == 60000


def test_factory_event_suggests_recent_manager_without_opening_another_case(reviewer):
    ctx = reviewer
    engine, factory = ctx[0][3], ctx[0][2].factory_id
    ingest_sources(engine)
    with Session(engine) as db, db.begin():
        prior = db.get(CaseRecord, ctx[7]["case_id"], with_for_update=True)
        prior.state = "RESOLVED"
        prior.updated_at = datetime.now(UTC)
    old_id = "legacy-planner-" + uuid4().hex
    with Session(engine) as db, db.begin():
        db.add(User(user_id=old_id, username=old_id, password_hash="not-a-login", active=True))
        db.flush()
        db.add(Membership(user_id=old_id, factory_id=factory, role="planner"))
    old_actor = Principal(
        user_id=old_id,
        username=old_id,
        grants=(Grant(factory_id=factory, role="planner"),),
    )
    old_case = create_case(
        engine, old_actor, factory, "old-case-request", "Open case of the previous planner"
    )
    try:
        before = snapshot(ctx[0])
        ctx[6].command(
            factory,
            SimulatorCommand(
                request_id="maintainer-timed-outage",
                run_id=before.run_id,
                kind="resource.outage",
                payload={"resource_id": before.resources[0].resource_id, "minutes": 30},
            ),
        )
        synchronize(engine, ctx[1], factory)
        assert ingest_sources(engine) == 0
        suggestions = list_risk_suggestions(engine, ctx[3], factory)["suggestions"]
        assert any("machine" in item["title"] for item in suggestions)
        with Session(engine) as db:
            next_case = db.scalar(
                select(CaseRecord).where(
                    CaseRecord.factory_id == factory,
                    CaseRecord.owner_id == ctx[3].user_id,
                    CaseRecord.case_id != ctx[7]["case_id"],
                )
            )
            assert next_case is None
            assert (
                db.scalar(
                    select(CaseInput).where(
                        CaseInput.case_id == old_case["case_id"],
                        CaseInput.kind == "SOURCE",
                    )
                )
                is None
            )
    finally:
        owner = connect(os.environ["TEST_MIGRATION_DATABASE_URL"])
        assert owner.url.database == "byof_test"
        try:
            with Session(owner) as db, db.begin():
                db.execute(delete(CaseInput).where(CaseInput.case_id == old_case["case_id"]))
                db.execute(delete(CaseRecord).where(CaseRecord.case_id == old_case["case_id"]))
                db.execute(delete(Membership).where(Membership.user_id == old_id))
                db.execute(delete(User).where(User.user_id == old_id))
        finally:
            owner.dispose()


@pytest.mark.parametrize("dynamic_source", [{"progress_revalidation": True}], indirect=True)
@pytest.mark.parametrize("fault_mode", ["idle", "running"])
def test_review_after_outage_keeps_clock_running_and_publishes_verified_plan(reviewer, fault_mode):
    ctx = reviewer
    approval(ctx)
    assert run(ctx)
    assert deliver_one(ctx[0][3], ctx[1], ctx[2])
    ready(ctx)
    assert run(ctx)
    factory, run_id = ctx[0][2].factory_id, ctx[0][2].run_id
    # Deterministic pacing in the test only; the approval code must never send clock.pause.
    ctx[6].command(
        factory,
        SimulatorCommand(
            request_id="pace", run_id=run_id, kind="clock.run", payload={"interval_ms": 60000}
        ),
    )
    with Session(ctx[0][3]) as db:
        original = db.get(CandidateRecord, ctx[4]).document
        later_resource = max(original["assignments"], key=lambda a: a["start_at"])["resource_id"]
    if fault_mode == "running":
        for _ in range(30):
            with Session(ctx[0][4]) as db, db.begin():
                db.get(World, factory).next_tick_at = datetime.now(UTC)
            assert run_due_tick(ctx[0][4])
            if any(a.state == "IN_PROGRESS" for a in snapshot(ctx[0]).actuals):
                break
        later_resource = next(
            a.resource_id for a in snapshot(ctx[0]).actuals if a.state == "IN_PROGRESS"
        )
    request(ctx, "outage", {"resource_id": later_resource, "minutes": 20})
    assert run(ctx)
    current = synchronize(ctx[0][3], ctx[1], factory)
    if fault_mode == "running":
        # The interrupted WIP is now supported, so a formal solve must remain
        # independently checkable instead of returning the old frozen-plan failure.
        preview = request_solve(
            ctx[0][3],
            ctx[3],
            factory,
            request_id="running-wip-preview",
            allow_overtime=False,
            time_limit=2,
            case_id=ctx[7]["case_id"],
        )
        assert run_once(ctx[0][3])
        with Session(ctx[0][3]) as db:
            old = db.get(SolveJob, preview.job_id)
            assert old.state == "SUCCEEDED"
            preliminary = db.get(CandidateRecord, old.candidate_id).document
            assert preliminary["has_solution"]
            assert preliminary["checker"]["status"] == "PASS"
    message_case(
        ctx[0][3],
        ctx[3],
        factory,
        ctx[7]["case_id"],
        "recovery-request",
        "Generate a breakdown recovery plan and reserve 15 minutes of review time.",
    )

    class RecoveryModel:
        calls = 0

        def complete(self, prompt):
            self.calls += 1
            assert self.calls == 1, (
                "A queued solve must wait for its result without another model call"
            )
            return json.dumps(
                {
                    "action": "solve_scenario",
                    "parameters": {
                        "allow_overtime": False,
                        "time_limit": 3,
                        "new_actions_not_before": (
                            current.snapshot_clock + timedelta(minutes=15)
                        ).isoformat(),
                    },
                    "reason_summary": "Compute the recovery plan from the confirmed stop; unaffected operations keep the original plan.",
                }
            )

    recovery_model = RecoveryModel()
    assert process_case(ctx[0][3], ctx[1], recovery_model)
    case_detail = get_case(ctx[0][3], ctx[3], factory, ctx[7]["case_id"])
    assert case_detail["error_code"] is None and recovery_model.calls == 1
    job_id = case_detail["operations"][-1]["result"]["job_id"]
    assert run_once(ctx[0][3])
    with Session(ctx[0][3]) as db:
        solved = db.get(SolveJob, job_id)
        assert solved.state == "SUCCEEDED"
        candidate = db.get(CandidateRecord, solved.candidate_id)
        candidate_id, digest = candidate.candidate_id, candidate.content_hash
        assert (
            candidate.document["has_solution"] and candidate.document["checker"]["status"] == "PASS"
        )
    # Production advances during the review.
    with Session(ctx[0][4]) as db, db.begin():
        db.get(World, factory).next_tick_at = datetime.now(UTC)
    assert run_due_tick(ctx[0][4])
    queued = request(ctx, "approve", {"candidate_id": candidate_id, "candidate_hash": digest})
    assert run(ctx)
    with Session(ctx[0][3]) as db:
        action = db.get(AssistantAction, queued["action_id"])
        assert action.state == "DONE", action.result
    assert deliver_one(ctx[0][3], ctx[1], ctx[2])
    with Session(ctx[0][4]) as db:
        world = db.get(World, factory)
        assert world.mode == "RUNNING" and world.business_clock > current.snapshot_clock
        assert world.active_candidate["content_hash"] == digest
    if fault_mode == "running":
        for _ in range(24):
            with Session(ctx[0][4]) as db, db.begin():
                db.get(World, factory).next_tick_at = datetime.now(UTC)
            assert run_due_tick(ctx[0][4])
        interrupted = next(a for a in current.actuals if a.state == "BLOCKED")
        resumed = next(
            a for a in snapshot(ctx[0]).actuals if a.operation_id == interrupted.operation_id
        )
        assert resumed.state in {"IN_PROGRESS", "COMPLETED"}, resumed
        assert resumed.remaining_minutes < interrupted.remaining_minutes


def test_invalid_card_payload_is_rejected_before_enqueue(reviewer):
    with pytest.raises(AccessError) as failure:
        request(reviewer, "outage", {"resource_id": "bad", "minutes": -5, "confirmed": True})
    assert failure.value.status == 422


def test_manager_recovery_choice_is_source_checked_and_reaches_the_same_case(reviewer):
    ctx = reviewer
    engine, factory = ctx[0][3], ctx[0][2].factory_id
    before = snapshot(ctx[0])
    receipt = next(row for row in before.receipts if row.status == "CONFIRMED")
    ctx[6].command(
        factory,
        SimulatorCommand(
            request_id="recovery-supply-loss",
            run_id=before.run_id,
            kind="receipt.cancel",
            payload={"receipt_id": receipt.receipt_id},
        ),
    )
    stock = next(row for row in before.inventory if row.material_id == receipt.material_id)
    ctx[6].command(
        factory,
        SimulatorCommand(
            request_id="recovery-stock-loss",
            run_id=before.run_id,
            kind="inventory.reconcile",
            payload={
                "material_id": stock.material_id,
                "expected_version": stock.version,
                "counted_on_hand": stock.reserved,
                "reason": "COUNT_CORRECTION",
            },
        ),
    )
    current = synchronize(engine, ctx[1], factory)
    path = next(p for p in recovery_paths(current) if p["kind"] == "material_supply")
    payload = {"case_id": ctx[7]["case_id"], "path_id": path["path_id"]}
    planner_only = ctx[3].model_copy(
        update={"grants": tuple(grant for grant in ctx[3].grants if grant.role == "planner")}
    )
    with pytest.raises(AccessError):
        enqueue(
            engine,
            planner_only,
            factory,
            AssistantRequest(
                request_id="unauthorized-recovery",
                run_id=current.run_id,
                kind="recover",
                payload=payload,
            ),
        )
    queued = request(ctx, "recover", payload)
    from packages.agent.assistant import track_recovery_paths

    with Session(engine) as db:
        tracked = track_recovery_paths(db, current, [path])
        assert tracked[0]["tracking_case_id"] == ctx[7]["case_id"]
    with pytest.raises(AccessError, match="already being followed"):
        request(ctx, "recover", payload)
    assert run(ctx)
    with Session(engine) as db:
        row = db.get(AssistantAction, queued["action_id"])
        assert row.state == "DONE"
        assert row.result["path"]["path_id"] == path["path_id"]
        message = db.scalar(
            select(CaseInput).where(CaseInput.input_key == "user:recovery:" + queued["action_id"])
        )
        assert message is not None and message.case_id == ctx[7]["case_id"]
    assert snapshot(ctx[0]).source.source_revision == current.source.source_revision
    # Selections created before stable path IDs were introduced remain deduplicated.
    with Session(engine) as db, db.begin():
        row = db.get(AssistantAction, queued["action_id"])
        row.payload = {**row.payload, "path_id": "legacy-content-hash"}
        row.result = {
            **row.result,
            "path": {**row.result["path"], "path_id": "legacy-content-hash"},
        }
    with pytest.raises(AccessError, match="already being followed"):
        request(ctx, "recover", payload)


def test_start_day_agent_solves_requests_review_and_human_executes(reviewer):
    ctx = reviewer
    request(ctx, "start")
    assert run(ctx)
    clock = snapshot(ctx[0]).snapshot_clock

    class Model:
        def __init__(self, actions):
            self.actions = iter(actions)

        def complete(self, prompt):
            action, parameters = next(self.actions)
            return json.dumps(
                {
                    "action": action,
                    "parameters": parameters,
                    "reason_summary": "Schedule today's production from the latest shop floor.",
                },
                ensure_ascii=False,
            )

    assert process_case(
        ctx[0][3],
        ctx[1],
        Model(
            [
                (
                    "solve_scenario",
                    {
                        "allow_overtime": False,
                        "time_limit": 2,
                        "new_actions_not_before": (clock + timedelta(minutes=15)).isoformat(),
                    },
                ),
                ("wait", {"reason": "Waiting for the schedule calculation", "recheck_minutes": 1}),
            ]
        ),
    )
    assert run_once(ctx[0][3])
    assert wake_completed_jobs(ctx[0][3])
    with Session(ctx[0][3]) as db:
        job = db.scalar(select(SolveJob).where(SolveJob.case_id == ctx[7]["case_id"]))
        record = db.get(CandidateRecord, job.candidate_id)
        candidate_id, digest = record.candidate_id, record.content_hash
    assert process_case(
        ctx[0][3],
        ctx[1],
        Model(
            [
                ("request_approval", {"candidate_id": candidate_id}),
                (
                    "reply",
                    {"message": "Today's plan is ready; preview it and approve.", "choices": []},
                ),
            ]
        ),
    )
    detail = get_case(ctx[0][3], ctx[3], ctx[0][2].factory_id, ctx[7]["case_id"])
    review = next(o for o in detail["operations"] if o["action"] == "request_approval")
    assert review["result"]["approval_state"] == "NOT_GRANTED"
    queued = request(ctx, "approve", {"candidate_id": candidate_id, "candidate_hash": digest})
    assert run(ctx)
    assert deliver_one(ctx[0][3], ctx[1], ctx[2])
    ready(ctx)
    assert run(ctx)
    with Session(ctx[0][3]) as db:
        assert db.get(AssistantAction, queued["action_id"]).state == "DONE"
    assert snapshot(ctx[0]).active_plan_hash == digest


def test_selected_recovery_survives_partial_supply_and_wakes_same_case(reviewer):
    from packages.agent.recovery_followup import recovery_view

    ctx = reviewer
    original = snapshot(ctx[0])
    test_manager_recovery_choice_is_source_checked_and_reaches_the_same_case(ctx)
    engine, factory = ctx[0][3], original.factory_id
    receipt = next(r for r in original.receipts if r.status == "CONFIRMED")
    case_id = ctx[7]["case_id"]

    class Model:
        def complete(self, prompt):
            return json.dumps(
                {
                    "action": "reply",
                    "parameters": {
                        "message": "Waiting for the authorized resupply facts.",
                        "choices": [],
                    },
                    "reason_summary": "Material is still short.",
                }
            )

    assert process_case(engine, ctx[1], Model())
    assert ingest_sources(engine) == 0
    for index, quantity in enumerate((1, 10000)):
        before = snapshot(ctx[0])
        stock = next(s for s in before.inventory if s.material_id == receipt.material_id)
        ctx[6].command(
            factory,
            SimulatorCommand(
                request_id=f"recovery-replenish-{index}",
                run_id=before.run_id,
                kind="inventory.reconcile",
                payload={
                    "material_id": stock.material_id,
                    "expected_version": stock.version,
                    "counted_on_hand": stock.reserved + quantity,
                    "reason": "COUNT_CORRECTION",
                },
            ),
        )
        current = synchronize(engine, ctx[1], factory)
        assert ingest_sources(engine) == 1
        assert ingest_sources(engine) == 0
        with Session(engine) as db:
            row = db.scalar(
                select(AssistantAction).where(
                    AssistantAction.kind == "recover", AssistantAction.factory_id == factory
                )
            )
            paths = {p["kind"]: p for p in recovery_paths(current)}
            view = recovery_view(db, row, current, paths)
            assert view["case_id"] == case_id
            assert view["path"]["path_id"] == "recovery-material_supply"
            assert view["condition_remaining"] is (index == 0)
            pending = list(
                db.scalars(
                    select(CaseInput).where(
                        CaseInput.case_id == case_id,
                        CaseInput.kind == "SOURCE",
                        CaseInput.turn_id.is_(None),
                    )
                )
            )
            assert len(pending) == 1
        assert process_case(engine, ctx[1], Model())
    # Clock-only synchronization does not repeatedly spend model budget.
    synchronize(engine, ctx[1], factory)
    assert ingest_sources(engine) == 0


def test_command_is_idempotent_and_cannot_change_payload_or_borrow_simulator_role(reviewer):
    ctx = reviewer
    first = request(
        ctx, "outage", {"resource_id": ctx[0][2].resources[0].resource_id, "minutes": 30}, "same"
    )
    assert request(ctx, "outage", first["payload"], "same")["action_id"] == first["action_id"]
    with pytest.raises(AccessError, match="original request"):
        request(ctx, "outage", {**first["payload"], "minutes": 40}, "same")
    planner = ctx[3].model_copy(
        update={"grants": tuple(g for g in ctx[3].grants if g.role == "planner")}
    )
    with pytest.raises(AccessError):
        enqueue(
            ctx[0][3],
            planner,
            ctx[0][2].factory_id,
            AssistantRequest(
                request_id="no-authority",
                run_id=ctx[0][2].run_id,
                kind="outage",
                payload=first["payload"],
            ),
        )
    assert run(ctx)
    after = snapshot(ctx[0])
    assert after.resources[0].unavailable[-1].end_at - after.resources[0].unavailable[
        -1
    ].start_at == timedelta(minutes=30)


def test_revoked_role_prevents_queued_effect(reviewer):
    ctx = reviewer
    queued = request(
        ctx, "outage", {"resource_id": ctx[0][2].resources[0].resource_id, "minutes": 30}
    )
    with Session(ctx[0][3]) as db, db.begin():
        db.execute(
            delete(Membership).where(
                Membership.user_id == ctx[3].user_id, Membership.role == "sim_admin"
            )
        )
    assert run(ctx)
    assert not snapshot(ctx[0]).resources[0].unavailable
    with Session(ctx[0][3]) as db:
        assert db.get(AssistantAction, queued["action_id"]).state == "FAILED"


def test_timed_outage_preserves_measured_work_and_does_not_pause_clock(reviewer):
    ctx = reviewer
    approval(ctx)
    run(ctx)
    assert deliver_one(ctx[0][3], ctx[1], ctx[2])
    # Before automatic clock start, explicitly step the real simulator to create WIP.
    assert control(ctx[0], "step", "clock.step", {"minutes": 1}).status_code == 200
    before = snapshot(ctx[0])
    actual = next(a for a in before.actuals if a.state in {"SETUP", "IN_PROGRESS"})
    ctx[6].command(
        before.factory_id,
        SimulatorCommand(
            request_id="timed",
            run_id=before.run_id,
            kind="resource.outage",
            payload={"resource_id": actual.resource_id, "minutes": 20},
        ),
    )
    after = snapshot(ctx[0])
    interrupted = next(a for a in after.actuals if a.operation_id == actual.operation_id)
    assert interrupted.state == "BLOCKED"
    assert (interrupted.remaining_minutes, interrupted.remaining_setup_minutes) == (
        actual.remaining_minutes,
        actual.remaining_setup_minutes,
    )
    assert after.snapshot_clock == before.snapshot_clock


def test_random_events_are_opt_in_and_persist_in_source_ledger(reviewer):
    ctx = reviewer
    factory = ctx[0][2].factory_id
    request(ctx, "scenario", {"enabled": True, "seed": 17, "every_minutes": 30})
    assert run(ctx)
    with Session(ctx[0][4]) as db, db.begin():
        world = db.get(World, factory, with_for_update=True)
        world.mode, world.next_tick_at = "RUNNING", datetime.now(UTC)
        world.scenario_state = {**world.scenario_state, "next_at": world.business_clock.isoformat()}
    assert run_due_tick(ctx[0][4])
    with Session(ctx[0][4]) as db:
        assert db.get(World, factory).scenario_state["counter"] == 1
        event = db.scalar(
            select(SourceAction).where(
                SourceAction.factory_id == factory, SourceAction.kind == "resource.outage"
            )
        )
        assert event.result["random"] is True
    synchronize(ctx[0][3], ctx[1], factory)
    assert ingest_sources(ctx[0][3]) == 0
    request(ctx, "scenario", {"enabled": False})
    assert run(ctx)
    with Session(ctx[0][4]) as db:
        assert db.get(World, factory).scenario_state["enabled"] is False
    replay = ctx[6].start_replay(
        factory, ReplayStart(request_id="replay-random", expected_run_id=ctx[0][2].run_id)
    )
    ctx[6].command(
        factory,
        SimulatorCommand(
            request_id="replay-step",
            run_id=replay["run_id"],
            kind="clock.step",
            payload={"minutes": 60},
        ),
    )
    with Session(ctx[0][4]) as db:
        world = db.get(World, factory)
        assert world.replay_state["done"] and world.replay_state["error_code"] is None
        assert world.scenario_state is None


def test_learning_needs_evidence_and_can_be_corrected_and_reset(reviewer):
    ctx = reviewer
    engine, factory = ctx[0][3], ctx[0][2].factory_id
    for index in range(3):
        with Session(engine) as db, db.begin():
            now = datetime.now(UTC)
            db.add(
                AssistantAction(
                    action_id=uuid4().hex,
                    factory_id=factory,
                    user_id=ctx[3].user_id,
                    request_id=f"feedback-{index}",
                    run_id=ctx[0][2].run_id,
                    kind="approve",
                    payload={},
                    state="DONE",
                    result={"learning": {"overtime": 1.0}},
                    created_at=now,
                    next_attempt_at=now,
                    attempts=0,
                )
            )
        with Session(engine) as db:
            learned = learning(db, factory, ctx[3].user_id)
        assert learned["active"] is (index == 2)
    assert learned["weights"]["overtime"] > learned["weights"]["delivery"]
    request(ctx, "preference", {"priority": "stability"})
    run(ctx)
    with Session(engine) as db:
        assert learning(db, factory, ctx[3].user_id)["weights"]["stability"] == 0.7
    request(ctx, "reset")
    run(ctx)
    with Session(engine) as db:
        assert learning(db, factory, ctx[3].user_id)["samples"] == 0


@pytest.mark.parametrize(
    "action,parameters",
    [
        (
            "reply",
            {"message": "Choose your priority.", "choices": ["Less overtime", "Protect delivery"]},
        ),
        ("propose_simulation", {"resource_id": None, "minutes": 30}),
    ],
)
def test_structured_model_reply_waits_for_human_without_changing_source(
    reviewer, action, parameters
):
    ctx = reviewer
    before = snapshot(ctx[0])

    class Model:
        def complete(self, prompt):
            return json.dumps(
                {
                    "action": action,
                    "parameters": parameters,
                    "reason_summary": "Please check your requirements.",
                },
                ensure_ascii=False,
            )

    assert process_case(ctx[0][3], ctx[1], Model())
    detail = get_case(ctx[0][3], ctx[3], before.factory_id, ctx[7]["case_id"])
    assert detail["operations"][-1]["result"]["await_user"] is True
    assert snapshot(ctx[0]).content_hash == before.content_hash
    with Session(ctx[0][3]) as db:
        assert (
            db.scalar(
                select(CaseInput).where(
                    CaseInput.case_id == ctx[7]["case_id"], CaseInput.kind == "TIMER"
                )
            )
            is None
        )
    message_case(
        ctx[0][3], ctx[3], before.factory_id, ctx[7]["case_id"], "continue", "Protect delivery"
    )
    assert process_case(ctx[0][3], ctx[1], Model())


def test_case_workspace_keeps_older_plans_outside_global_recent_window(reviewer):
    from packages.domain.models import Candidate
    from packages.planning.service import workspace

    source, _, _, actor, candidate_id, _, _, case = reviewer
    engine, factory = source[3], source[2].factory_id
    with Session(engine) as db, db.begin():
        original = db.get(CandidateRecord, candidate_id)
        conversation = db.get(CaseRecord, case["case_id"])
        conversation.context = {**conversation.context, "candidate_ids": [candidate_id]}
        base = Candidate.model_validate(original.document)
        for index in range(32):
            clone = Candidate.model_validate(
                {
                    **base.model_dump(mode="json"),
                    "candidate_id": f"history-noise-{uuid4().hex}",
                    "content_hash": None,
                }
            )
            db.add(
                CandidateRecord(
                    candidate_id=clone.candidate_id,
                    factory_id=factory,
                    snapshot_id=original.snapshot_id,
                    document=clone.model_dump(mode="json"),
                    content_hash=clone.content_hash,
                    created_at=datetime.now(UTC) + timedelta(seconds=index),
                )
            )
    assert candidate_id not in {
        r["candidate"].candidate_id for r in workspace(engine, factory)["candidates"]
    }
    scoped = workspace(engine, factory, case_id=case["case_id"])
    assert [r["candidate"].candidate_id for r in scoped["candidates"]] == [candidate_id]
    assert (
        workspace(engine, factory, candidate_id=candidate_id)["candidates"][0][
            "candidate"
        ].candidate_id
        == candidate_id
    )
    with pytest.raises(AccessError):
        workspace(engine, factory, case_id="foreign-case")
