"""One persistent Case across a running factory, human input, real solving and execution.

This integration case intentionally uses a fixed five real seconds per business minute.
Its clock worker stays alive through solving, human review and conditional publication.
Run alone with the explicit PostgreSQL test URLs; shared worker queues are not parallel-safe.
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import test_dynamic_factory_postgres as source_fixture
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from test_case_runtime_postgres import case_context as case_context
from test_cases_api_postgres import case_api as case_api
from test_dynamic_delay_flow import assert_inventory_ledger, physical_work
from test_dynamic_factory_postgres import control, snapshot
from test_dynamic_factory_postgres import dynamic_source as dynamic_source
from test_publication_postgres import publishing as publishing

from packages.agent import case_runtime
from packages.agent.cases import get_case, ingest_sources, wake_completed_jobs
from packages.agent.cases_store import CaseInput, CaseRecord, CaseTurn
from packages.agent.human_tasks import HumanTaskRecord, TaskReminder
from packages.domain.models import Candidate, Snapshot, batch_operations
from packages.domain.skf import load_skf_snapshot
from packages.integrations.notification_store import Notification
from packages.persistence import connect
from packages.planning.checker import check_candidate, check_revalidated_plan
from packages.planning.preference_store import (
    ObjectiveRecord,
    PreferenceAction,
    PreferenceCoordination,
    PreferenceHead,
    PreferenceProposal,
    PreferenceRevision,
    PreferenceState,
)
from packages.planning.preferences import load_objective
from packages.planning.publication import Publication, deliver_one
from packages.planning.revalidation import check_progress
from packages.planning.revalidation_store import ValidationRecord
from packages.planning.review_store import ApprovalReviewRecord
from packages.planning.service import active_baseline, synchronize
from packages.planning.store import ApprovalRecord, CandidateRecord, SnapshotRecord, SolveJob
from services.factory_sim.service import run_due_tick
from services.factory_sim.storage import SourceAction
from services.solver_worker.main import run_once

CLOCK_INTERVAL_MS = 5000
SOLVE_SECONDS = 1
MAX_FLOW_SECONDS = 1800


@pytest.fixture(autouse=True)
def legal_three_batch_input(monkeypatch):
    def development_input(*, development):
        assert development
        original = load_skf_snapshot(development=True)
        raw = original.model_dump(mode="python", exclude={"content_hash"})
        raw["orders"][0]["quantity"] *= 3
        legal = Snapshot.model_validate(raw)
        assert legal.profile == original.profile
        assert legal.resources == original.resources and legal.workers == original.workers
        assert legal.inventory == original.inventory and legal.horizon == original.horizon
        return legal

    monkeypatch.setattr(source_fixture, "load_skf_snapshot", development_input)


@pytest.fixture
def main_flow(case_api):
    ctx = case_api
    try:
        yield ctx
    finally:
        try:
            _save_failure_facts(ctx, "main-flow teardown")
        except Exception as exc:
            print(f"main-flow evidence unavailable: {type(exc).__name__}", flush=True)
        owner = connect(os.environ["TEST_MIGRATION_DATABASE_URL"])
        assert owner.url.database == "byof_test"
        try:
            with owner.begin() as db:
                for table in (
                    Notification,
                    ValidationRecord,
                    ApprovalReviewRecord,
                    ObjectiveRecord,
                    PreferenceAction,
                    PreferenceCoordination,
                    PreferenceHead,
                    PreferenceRevision,
                    PreferenceProposal,
                    PreferenceState,
                ):
                    db.execute(delete(table).where(table.factory_id == ctx.factory))
        finally:
            owner.dispose()


class RunningFactory:
    def __init__(self, source):
        self.source = source
        self.stopping = threading.Event()
        self.progress = threading.Event()
        self.errors = []
        self.ticks = 0
        self.started_at = time.monotonic()
        self.thread = threading.Thread(target=self._run, name="main-case-clock", daemon=True)

    def _run(self):
        try:
            while not self.stopping.is_set():
                if run_due_tick(self.source[4]):
                    self.ticks += 1
                    self.progress.set()
                self.stopping.wait(0.02)
        except BaseException as exc:
            self.errors.append(exc)
            self.progress.set()

    def __enter__(self):
        _control(self.source, "clock.run", {"interval_ms": CLOCK_INTERVAL_MS})
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stopping.set()
        self.thread.join(timeout=10)
        assert not self.thread.is_alive(), "Source worker did not stop during test teardown"

    def live(self):
        assert not self.errors, self.errors
        assert self.thread.is_alive(), "The clock worker must remain live during all business work"
        assert time.monotonic() - self.started_at < MAX_FLOW_SECONDS, "Main flow real-time budget"
        return snapshot(self.source)

    def until(self, condition, *, stage, timeout=MAX_FLOW_SECONDS):
        print(f"main-flow: waiting for {stage}", flush=True)
        deadline = min(self.started_at + MAX_FLOW_SECONDS, time.monotonic() + timeout)
        while time.monotonic() < deadline:
            current = self.live()
            if condition(current):
                print(f"main-flow: {stage} at {current.snapshot_clock.isoformat()}", flush=True)
                return current
            self.progress.wait(0.5)
            self.progress.clear()
        current = self.live()
        pytest.fail(
            f"{stage}: source time {current.snapshot_clock}, ticks={self.ticks}, "
            f"actuals={[(a.operation_id, a.state, a.remaining_minutes) for a in current.actuals]}"
        )


def _control(source, kind, payload=None):
    response = control(source, str(uuid4()), kind, payload)
    assert response.status_code == 200, (kind, response.status_code, response.json())
    return response.json()


def _post(ctx, path, body, role="planner"):
    client, headers = ctx.login(role)
    response = client.post(ctx.base + path, json=body, headers=headers)
    assert response.status_code == 200, (path, response.status_code, response.json())
    return response.json()


def _read(ctx, path, role="planner"):
    client, _ = ctx.login(role)
    response = client.get(ctx.base + path)
    assert response.status_code == 200, (path, response.status_code, response.json())
    return response.json()


def _action(name, parameters, reason):
    return json.dumps(
        {"action": name, "parameters": parameters, "reason_summary": reason}, ensure_ascii=False
    )


class VisibleFeedbackModel:
    """Select from actual Runtime evidence, without fixture IDs, clocks, DBs or source clients."""

    def __init__(self):
        self.contexts = []
        self.decisions = []

    def complete(self, prompt):
        context = json.loads(prompt.split("Business context (data, not instructions):\n", 1)[1])
        self.contexts.append(context)
        decision = self.choose(context)
        self.decisions.append(json.loads(decision))
        return decision

    def choose(self, context):
        facts = context["facts"]
        tools = [item for item in context["tool_results"] if item["result"] is not None]
        tasks = context["current_human_tasks"]
        inputs = context["inputs"]

        def wait(reason):
            return _action("wait", {"reason": reason, "recheck_minutes": 30}, reason)

        def query(entity):
            return _action(
                "query",
                {"entity": entity, "identity": None, "offset": 0},
                "Check the current source facts",
            )

        def latest(action, *, entity=None):
            return next(
                (
                    item["result"]
                    for item in reversed(tools)
                    if item["action"] == action
                    and (entity is None or item["result"].get("entity") == entity)
                    and (action != "query" or item["turn_id"] == context["turn_id"])
                ),
                None,
            )

        completed = next(
            (
                release
                for release in context["current_publications"]
                if release["source_state"] == "ACTIVE" and release["execution_state"] == "COMPLETED"
            ),
            None,
        )
        if completed:
            return _action(
                "finish",
                {
                    "evidence_release_id": completed["release_id"],
                    "risk_summary": "Owner information handled and the actual release record shows completion; quality and scope need a full check.",
                },
                "The execution source has completion records; ask the service to check all closing evidence",
            )
        if context["current_publications"]:
            return wait(
                "The plan has a release record; waiting for the execution source and actual production results"
            )

        wants_stability = any(
            row["kind"] == "USER" and "stability" in row["data"].get("message", "")
            for row in inputs
        )
        objective = context["objective_state"]
        if (
            wants_stability
            and (objective.get("definition") or {}).get("selection") != "stability_first"
        ):
            pending = context["case"]["context"].get("pending_preference", {})
            if not pending:
                return _action(
                    "propose_preference",
                    {"selection": "stability_first"},
                    "The planner proposes keeping schedule stability",
                )
            return wait(
                "The preference is only a proposal; waiting for the planner to confirm the tardiness and overtime bounds"
            )

        unknowns = context["case"]["context"].get("unknowns", [])
        information_tasks = [task for task in tasks if task["owner_role"] == "maintainer"]
        if unknowns and information_tasks:
            if any(task["state"] == "RESPONDED" for task in information_tasks):
                return wait(
                    "A named reply is only information; waiting for the execution source to confirm remaining hours and machine state"
                )
            return wait(
                "The maintenance owner has not replied; keep the case waiting and do not guess the remaining work"
            )

        candidate_ids = context["case"]["context"].get("candidate_ids", [])
        if candidate_ids:
            comparison = latest("compare_candidates")
            if comparison is None:
                return _action(
                    "compare_candidates",
                    {"candidate_ids": candidate_ids},
                    "Check the actual plan metrics and the full check",
                )
            if comparison.get("status") != "OK":
                return wait(
                    "The compare tool did not confirm the plan is currently usable; the facts need checking again"
                )
            candidate = next(
                (
                    row
                    for row in comparison["candidates"]
                    if row["has_solution"]
                    and row["current"]
                    and row["current_checker"]["status"] == "PASS"
                ),
                None,
            )
            if candidate is None:
                return wait("The actual solve and check have no plan to approve yet")
            if latest("request_approval") is None and not context["approvals"]:
                return _action(
                    "request_approval",
                    {"candidate_id": candidate["candidate_id"]},
                    "The plan passed the check and needs an explicit planner decision",
                )
            return wait(
                "Waiting for manual approval, conditional release and execution feedback; a notification cannot close the case"
            )

        actuals = latest("query", entity="actuals")
        if not actuals:
            return query("actuals")
        if not unknowns and any(
            row["state"] == "BLOCKED" and row["remaining_minutes"] is None
            for row in actuals["items"]
        ):
            return wait(
                "The latest query still has unknown remaining work; solve after the source confirms"
            )
        if unknowns:
            resources = latest("query", entity="resources")
            if not resources:
                return query("resources")
            blocked = next(
                row
                for row in actuals["items"]
                if row["state"] == "BLOCKED" and row["remaining_minutes"] is None
            )
            assert any(
                row["resource_id"] == blocked["resource_id"] and row["status"] == "DOWN"
                for row in resources["items"]
            )
            return _action(
                "request_information",
                {
                    "question": "Please confirm the expected repair time, remaining production minutes and remaining changeover minutes.",
                    "role": "maintainer",
                    "subject_id": blocked["operation_id"],
                    "fields": [
                        "repair_eta",
                        "remaining_minutes",
                        "remaining_setup_minutes",
                        "comment",
                    ],
                    "deadline_minutes": 60,
                },
                "The execution source shows the machine down with unknown remaining work; the maintenance owner must confirm",
            )
        if facts["orders"] > 1 and information_tasks:
            resources = latest("query", entity="resources")
            if not resources:
                return query("resources")
            if any(row["status"] != "AVAILABLE" for row in resources["items"]):
                return wait(
                    "The execution source still has unavailable resources; waiting for a recovery record"
                )
            if latest("solve_scenario") is not None:
                return wait("The solve is queued; waiting for the actual plan result")
            return _action(
                "solve_scenario",
                {
                    "allow_overtime": False,
                    "time_limit": SOLVE_SECONDS,
                    "new_actions_not_before": (
                        datetime.fromisoformat(facts["business_clock"]) + timedelta(minutes=15)
                    ).isoformat(),
                },
                "The execution source recovered and the preference is confirmed; reserve fifteen business minutes for manual review and conditional release",
            )
        return wait(
            "Normal production continues; waiting for actual disruptions or new order requests"
        )


def _turn(ctx, model, stage):
    print(f"main-flow: agent turn for {stage}", flush=True)
    engine, reader, actor = ctx.engine, ctx.context[1], ctx.context[3]
    synchronize(engine, reader, ctx.factory)
    ingest_sources(engine)
    wake_completed_jobs(engine)
    assert case_runtime.process_case(engine, reader, model), f"{stage}: no actionable Case input"
    detail = get_case(engine, actor, ctx.factory, ctx.case["case_id"])
    assert detail["case_id"] == ctx.case["case_id"]
    assert detail["error_code"] is None, (stage, detail["error_code"])
    rejected = [
        op for op in detail["operations"] if (op["result"] or {}).get("status") == "REJECTED"
    ]
    if rejected:
        _save_failure_facts(ctx, stage)
    assert rejected == [], (stage, [(op["action"], op["result"]) for op in rejected])
    return detail


def _save_failure_facts(ctx, stage):
    """Retain reproducible source evidence without prompts, replies or credentials."""
    from packages.auth import AccessError
    from packages.integrations.sync import SourceBatch
    from packages.planning.store import FactoryState

    with Session(ctx.engine) as db:
        state = db.get(FactoryState, ctx.factory)
        current = Snapshot.model_validate(db.get(SnapshotRecord, state.snapshot_id).document)
        baseline = active_baseline(db, current)
        records = list(
            db.scalars(select(CandidateRecord).where(CandidateRecord.factory_id == ctx.factory))
        )
        report = {
            "stage": stage,
            "case_id": ctx.case["case_id"],
            "factory_id": ctx.factory,
            "current": current.model_dump(mode="json"),
            "baseline": baseline.model_dump(mode="json") if baseline else None,
            "candidates": [],
            "source_batches": [
                row.document
                for row in db.scalars(
                    select(SourceBatch)
                    .where(
                        SourceBatch.factory_id == ctx.factory, SourceBatch.run_id == current.run_id
                    )
                    .order_by(SourceBatch.revision)
                )
            ],
            "source_current": snapshot(ctx.source).model_dump(mode="json"),
            "publications": [
                {
                    "release": row.document,
                    "state": row.state,
                    "error_code": row.error_code,
                    "source_receipt": row.source_receipt,
                }
                for row in db.scalars(
                    select(Publication).where(Publication.factory_id == ctx.factory)
                )
            ],
        }
        case = db.get(CaseRecord, ctx.case["case_id"])
        report["case_state"] = case.state if case else None
        report["case_error"] = case.error_code if case else None
        for record in records:
            candidate = Candidate.model_validate(record.document)
            original = Snapshot.model_validate(db.get(SnapshotRecord, record.snapshot_id).document)
            item = {
                "candidate": candidate.model_dump(mode="json"),
                "original": original.model_dump(mode="json"),
            }
            try:
                proof = check_progress(db, current, candidate)
                item["progress"] = proof.checked.report.model_dump(mode="json")
            except AccessError as exc:
                item["progress_error"] = exc.code
            if baseline is not None:
                item["remaining_check"] = check_revalidated_plan(
                    original,
                    current,
                    candidate,
                    baseline=baseline,
                    objective=load_objective(db, ctx.factory, candidate.binding.objective_version),
                ).report.model_dump(mode="json")
            report["candidates"].append(item)
    directory = Path(__file__).resolve().parents[1] / ".runtime"
    directory.mkdir(exist_ok=True)
    path = directory / f"p3-main-flow-{ctx.case['case_id']}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"main-flow failure facts: {path}", flush=True)


def _release(ctx, candidate_id, candidate_hash, *, progress=False):
    approval_path = "progress-approvals" if progress else "approvals"
    approved = _post(
        ctx,
        f"/candidates/{candidate_id}/{approval_path}",
        {
            "request_id": str(uuid4()),
            "candidate_hash": candidate_hash,
            "action_scope": "publish_plan",
            "decision": "APPROVED",
        },
    )
    assert approved["approver_id"] == ctx.accounts["planner"]
    assert approved["candidate_hash"] == candidate_hash
    if not hasattr(ctx, "release_attempt_ids"):
        ctx.release_attempt_ids = []
    client, headers = ctx.login("planner")
    for attempt in range(3 if progress else 1):
        certificate = (
            _post(
                ctx,
                f"/candidates/{candidate_id}/validations",
                {"request_id": str(uuid4()), "candidate_hash": candidate_hash},
            )
            if progress
            else None
        )
        response = client.post(
            ctx.base + f"/candidates/{candidate_id}/publications",
            json={
                "request_id": str(uuid4()),
                "candidate_hash": candidate_hash,
                **({"certificate_id": certificate["certificate_id"]} if certificate else {}),
            },
            headers=headers,
        )
        if (
            progress
            and response.status_code == 409
            and response.json().get("code") == "VALIDATION_BINDING_CHANGED"
        ):
            print(
                f"main-flow: fact version advanced before local commit, revalidate {attempt + 1}",
                flush=True,
            )
            continue
        assert response.status_code == 200, (response.status_code, response.json())
        released = response.json()
        ctx.release_attempt_ids.append(released["release_id"])
        assert released["local_state"] == "LOCAL_COMMITTED"
        assert released["source_state"] == "PENDING_SOURCE"
        assert released["execution_state"] == "NOT_STARTED"
        assert deliver_one(ctx.engine, ctx.context[1], ctx.context[2])
        records = _read(ctx, "/workspace")["publications"]
        result = next(
            row for row in records if row["release"]["release_id"] == released["release_id"]
        )
        accepted = result["release"]
        if accepted["source_state"] == "ACTIVE":
            break
        # Only a definite rejection permits a new operation, with fresh full evidence.
        assert progress and accepted["source_state"] == "REJECTED", result
        assert result["error_code"] == "SOURCE_CONDITIONS_CHANGED", result
        assert accepted["source_receipt_id"] is not None
        print(f"main-flow: source rejected advanced version, revalidate {attempt + 1}", flush=True)
    else:
        pytest.fail("Bounded publication recovery exhausted while factory continued running")
    assert accepted["source_receipt_id"] is not None
    assert approved["approval_id"] in accepted["approval_ids"]
    assert snapshot(ctx.source).active_plan_hash == candidate_hash
    return accepted


@pytest.mark.parametrize("dynamic_source", [{"progress_revalidation": True}], indirect=True)
def test_same_case_main_flow_with_continuous_factory_and_real_human_actions(main_flow):
    ctx = main_flow
    source, reader, _, actor, initial_id, initial_hash = ctx.context
    initial = snapshot(source)
    assert initial.profile.version == "progress-fixture/1"
    assert initial.profile.policy.progress_revalidation_enabled
    assert initial.profile.policy.freeze_window_min == 60
    assert len(batch_operations(initial)[1]) == 24
    model = VisibleFeedbackModel()
    initial_release = _release(ctx, initial_id, initial_hash)
    with Session(ctx.engine) as db:
        baseline = Candidate.model_validate(db.get(CandidateRecord, initial_id).document)

    with RunningFactory(source) as clock:
        # Normal accepted production establishes real reservations and execution history.
        clock.until(lambda current: bool(current.actuals), stage="initial accepted production")
        normal = _turn(ctx, model, "normal production")
        assert normal["state"] == "WAITING" and normal["closure"] is None
        before_fault = clock.until(
            lambda current: (
                current.snapshot_clock >= initial.snapshot_clock + timedelta(minutes=13)
            ),
            stage="parallel production before outage",
        )
        interrupted = next(
            a for a in before_fault.actuals if a.state == "IN_PROGRESS" and a.consumed
        )
        independent = next(
            a
            for a in before_fault.actuals
            if a.state == "IN_PROGRESS"
            and a.resource_id != interrupted.resource_id
            and a.worker_id != interrupted.worker_id
        )
        _control(source, "resource.down", {"resource_id": interrupted.resource_id})
        blocked = next(
            a for a in snapshot(source).actuals if a.operation_id == interrupted.operation_id
        )
        assert blocked.state == "BLOCKED" and blocked.remaining_minutes is None
        assert blocked.consumed == interrupted.consumed and blocked.segments == interrupted.segments

        fault = _turn(ctx, model, "unknown remaining work")
        assert fault["state"] == "WAITING" and fault["closure"] is None
        task = next(
            row for row in _read(ctx, "/human-tasks")["tasks"] if row["task_type"] == "INFORMATION"
        )
        assert task["state"] == "OPEN" and task["owner_role"] == "maintainer"
        assert task["subject_id"] == interrupted.operation_id
        assert {op["action"] for op in fault["operations"]} >= {
            "query",
            "request_information",
            "wait",
        }

        calls_before_wait = len(model.contexts)
        progressed = clock.until(
            lambda current: (
                current.snapshot_clock >= before_fault.snapshot_clock + timedelta(minutes=3)
            ),
            stage="independent work during human wait",
        )
        another = next(a for a in progressed.actuals if a.operation_id == independent.operation_id)
        assert physical_work(another) > physical_work(independent)
        stopped = next(a for a in progressed.actuals if a.operation_id == interrupted.operation_id)
        assert stopped.state == "BLOCKED" and stopped.remaining_minutes is None
        assert stopped.segments == blocked.segments and stopped.consumed == blocked.consumed
        assert_inventory_ledger(initial, progressed)
        assert len(model.contexts) == calls_before_wait
        assert _read(ctx, f"/cases/{ctx.case['case_id']}")["closure"] is None

        # The new order and preference instruction join the same persistent Case.
        urgent = initial.orders[0].model_dump(mode="json")
        urgent.update(
            order_id="expedite-" + uuid4().hex,
            quantity=50,
            priority_weight=5,
            due_at=(initial.snapshot_clock + timedelta(hours=8)).isoformat(),
        )
        _control(source, "order.add", urgent)
        _post(
            ctx,
            f"/cases/{ctx.case['case_id']}/messages",
            {
                "request_id": str(uuid4()),
                "message": "A rush order came in; keep schedule stability first. I will explicitly confirm the tardiness and overtime bounds.",
            },
        )
        changed = _turn(ctx, model, "urgent order and preference proposal")
        proposals = _read(ctx, "/preferences")
        agent_proposal = next(
            row for row in proposals["agent_proposals"] if row["case_id"] == ctx.case["case_id"]
        )
        assert agent_proposal["selection"] == "stability_first"
        assert proposals["effective"]["objective_version"] == "delivery-v1"
        assert changed["closure"] is None and len(_read(ctx, "/cases")["cases"]) == 1
        proposed = _post(
            ctx,
            "/preferences/proposals",
            {
                "request_id": str(uuid4()),
                "scope_type": "CASE",
                "scope_id": ctx.case["case_id"],
                "definition": {
                    "selection": "stability_first",
                    "max_weighted_tardiness": 1000,
                    "max_incremental_overtime_minutes": 0,
                },
                "expected_version": 0,
                "reason": "Keep unrelated production and limit tardiness and added overtime.",
                "source_proposal_id": agent_proposal["proposal_id"],
            },
        )
        assert _read(ctx, "/preferences")["effective"]["objective_version"] == "delivery-v1"
        confirmed = _post(
            ctx,
            f"/preferences/proposals/{proposed['proposal_id']}/confirmations",
            {
                "request_id": str(uuid4()),
                "expected_state_version": proposals["state_version"],
            },
        )
        preference = _read(ctx, "/preferences")["effective"]
        assert preference["status"] == "READY"
        assert preference["definition"]["selection"] == "stability_first"
        assert preference["sources"][0]["confirmed_by"] == ctx.accounts["planner"]
        assert preference["sources"][0]["preference_id"] == confirmed["preference_id"]

        # A named user's reply is information; the private control operation confirms source facts.
        response = _post(
            ctx,
            f"/human-tasks/{task['task_id']}/responses",
            {
                "request_id": str(uuid4()),
                "expected_task_version": task["version"],
                "answer": {
                    "repair_eta": (
                        snapshot(source).snapshot_clock + timedelta(minutes=10)
                    ).isoformat(),
                    "remaining_minutes": interrupted.remaining_minutes,
                    "remaining_setup_minutes": interrupted.remaining_setup_minutes,
                    "comment": "Checked the work records at the stop; remaining work uses the shop floor confirmed values.",
                },
            },
            role="maintainer",
        )
        assert response["state"] == "RESPONDED"
        assert response["response"]["actor_id"] == ctx.accounts["maintainer"]
        assert response["response"]["source"] == "authenticated_human_information"
        unconfirmed = next(
            a for a in snapshot(source).actuals if a.operation_id == interrupted.operation_id
        )
        assert unconfirmed.remaining_minutes is None and unconfirmed.consumed == blocked.consumed
        waiting = _turn(ctx, model, "reply cannot alter enterprise facts")
        assert waiting["state"] == "WAITING" and waiting["closure"] is None
        assert all(op["action"] != "solve_scenario" for op in waiting["operations"])

        # Wait out actual frozen starts; their original time window and the source clock stay intact.
        recovery_after = max(row.start_at for row in baseline.assignments) + timedelta(minutes=1)
        clock.until(
            lambda current: current.snapshot_clock >= recovery_after,
            stage="outage beyond frozen starts",
        )
        _control(
            source,
            "execution.confirm_remaining",
            {
                "operation_id": interrupted.operation_id,
                "remaining_minutes": interrupted.remaining_minutes,
                "remaining_setup_minutes": interrupted.remaining_setup_minutes,
            },
        )
        _control(source, "resource.restore", {"resource_id": interrupted.resource_id})
        repaired = snapshot(source)
        assert repaired.profile.policy.freeze_window_min == 60
        repaired = clock.until(
            lambda current: current.snapshot_clock > repaired.snapshot_clock,
            stage="real tick after repair before new solve",
        )
        recovering = next(a for a in repaired.actuals if a.operation_id == interrupted.operation_id)
        assert recovering.state == "IN_PROGRESS" and recovering.remaining_minutes is not None
        assert recovering.consumed == blocked.consumed
        planned = _turn(ctx, model, "fresh source recovery and actual solver request")
        solve_op = next(
            op for op in reversed(planned["operations"]) if op["action"] == "solve_scenario"
        )
        assert solve_op["result"]["status"] == "PENDING"
        assert run_once(ctx.engine)
        with Session(ctx.engine) as db:
            job = db.get(SolveJob, solve_op["result"]["job_id"])
            assert job.state == "SUCCEEDED", (job.state, job.error_code)
            saved = db.get(CandidateRecord, job.candidate_id)
            candidate = Candidate.model_validate(saved.document)
            basis = Snapshot.model_validate(db.get(SnapshotRecord, saved.snapshot_id).document)
            objective = load_objective(db, ctx.factory, candidate.binding.objective_version)
            assert (
                check_candidate(
                    basis, candidate, baseline=active_baseline(db, basis), objective=objective
                ).status
                == "PASS"
            )
        assert candidate.has_solution and len(candidate.assignments) == 32
        assert candidate.schema_version == "byof.candidate/3"
        assert candidate.new_actions_not_before == basis.snapshot_clock + timedelta(minutes=15)
        assert candidate.accept_before >= candidate.new_actions_not_before
        assert candidate.binding.objective_version == preference["objective_version"]
        clock.until(
            lambda current: current.snapshot_clock >= basis.snapshot_clock + timedelta(minutes=2),
            stage="actual production during candidate review",
        )
        compared = _turn(ctx, model, "solver feedback, comparison and review request")
        review = next(
            row for row in _read(ctx, "/human-tasks")["tasks"] if row["task_type"] == "APPROVAL"
        )
        assert (
            review["state"] == "OPEN" and review["review"]["candidate_id"] == candidate.candidate_id
        )
        assert any(
            op["action"] == "compare_candidates" and op["result"]["status"] == "OK"
            for op in compared["operations"]
        )
        assert _read(ctx, f"/cases/{ctx.case['case_id']}")["closure"] is None
        clock.live()
        accepted = _release(ctx, candidate.candidate_id, candidate.content_hash, progress=True)
        assert accepted["release_id"] != initial_release["release_id"]
        releases = _read(ctx, "/workspace")["publications"]
        assert {r["release"]["release_id"] for r in releases} == set(ctx.release_attempt_ids)
        assert sum(r["release"]["source_state"] == "ACTIVE" for r in releases) == 2
        assert all(
            r["release"]["source_state"] == "ACTIVE"
            or (
                r["release"]["source_state"] == "REJECTED"
                and r["error_code"] == "SOURCE_CONDITIONS_CHANGED"
            )
            for r in releases
        )
        reviewed = _read(ctx, f"/human-tasks/{review['task_id']}")
        assert reviewed["state"] == "REVIEWED" and reviewed["review"]["outcome"] == "APPROVED"
        following = _turn(ctx, model, "accepted plan still requires execution follow-up")
        assert following["state"] == "WAITING" and following["closure"] is None

        completed = clock.until(
            lambda current: (
                len(current.actuals) == 32 and all(a.state == "COMPLETED" for a in current.actuals)
            ),
            stage="new accepted plan actual completion",
        )
        assert all(a.quality_state == "PASSED" for a in completed.actuals)
        assert all(order.status == "COMPLETED" for order in completed.orders)
        assert_inventory_ledger(initial, completed)
        final = _turn(ctx, model, "source completion and closure evidence")
        assert final["state"] == "RESOLVED", final["operations"][-2:]
        evidence = final["closure"]["closing_evidence"]
        assert evidence["release_id"] == accepted["release_id"]
        assert evidence["source_receipt_id"] == accepted["source_receipt_id"]
        assert evidence["execution_state"] == "COMPLETED"
        assert evidence["active_plan_version"] == completed.active_plan_version
        assert evidence["scope_version"] == completed.scope_version
        assert evidence["open_human_tasks"] == 0 and evidence["risk_summary"]
        clock.live()

        with Session(ctx.engine) as db:
            assert (
                len(
                    list(db.scalars(select(CaseRecord).where(CaseRecord.factory_id == ctx.factory)))
                )
                == 1
            )
            turns = list(
                db.scalars(select(CaseTurn).where(CaseTurn.case_id == ctx.case["case_id"]))
            )
            assert (
                sum(turn.model_requests for turn in turns)
                == len(model.contexts)
                <= case_runtime.MODEL_REQUESTS_PER_CASE
            )
            assert all(
                turn.model_requests <= case_runtime.MODEL_REQUESTS_PER_TURN for turn in turns
            )
            assert all(turn.solver_requests <= case_runtime.MAX_SOLVES_PER_TURN for turn in turns)
            assert (
                len(
                    list(
                        db.scalars(select(SolveJob).where(SolveJob.case_id == ctx.case["case_id"]))
                    )
                )
                == 1
            )
            assert not list(
                db.scalars(
                    select(HumanTaskRecord).where(
                        HumanTaskRecord.case_id == ctx.case["case_id"],
                        HumanTaskRecord.state.in_(["OPEN", "ESCALATED"]),
                    )
                )
            )
            assert not list(
                db.scalars(
                    select(TaskReminder).where(
                        TaskReminder.case_id == ctx.case["case_id"], TaskReminder.state == "QUEUED"
                    )
                )
            )
            assert (
                len(
                    list(
                        db.scalars(
                            select(ApprovalRecord).where(ApprovalRecord.factory_id == ctx.factory)
                        )
                    )
                )
                == 2
            )
            assert len(
                list(db.scalars(select(Publication).where(Publication.factory_id == ctx.factory)))
            ) == len(ctx.release_attempt_ids)
            assert any(
                row.kind == "human_task.responded"
                for row in db.scalars(
                    select(CaseInput).where(CaseInput.case_id == ctx.case["case_id"])
                )
            )
        with Session(source[4]) as db:
            actions = list(
                db.scalars(select(SourceAction).where(SourceAction.factory_id == ctx.factory))
            )
            assert sum(action.kind == "plan.submit" for action in actions) == len(
                ctx.release_attempt_ids
            )
            assert sum(action.kind == "clock.run" for action in actions) == 1
            assert all(action.kind not in {"clock.pause", "clock.step"} for action in actions)
        prompt_data = json.dumps(model.contexts, ensure_ascii=False)
        assert all(token not in prompt_data for token in source[1].values())
        assert all(
            key not in prompt_data
            for key in (
                '"future_events"',
                '"replay_state"',
                '"control_token"',
                '"expected_actions"',
            )
        )
        assert {decision["action"] for decision in model.decisions} >= {
            "query",
            "request_information",
            "wait",
            "propose_preference",
            "solve_scenario",
            "compare_candidates",
            "request_approval",
            "finish",
        }
