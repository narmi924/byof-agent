"""Structured reviewer commands. Merely reading chat never grants a decision."""

from fastapi import APIRouter, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from packages.agent.assistant import (
    AssistantRequest,
    action_view,
    enqueue,
    learning,
    track_recovery_paths,
)
from packages.agent.assistant_store import AssistantAction
from packages.agent.cases_store import CaseRecord
from packages.agent.planning_context import latest_solver_state, material_shortfalls
from packages.agent.recovery_paths import recovery_paths
from packages.agent.treatment_execution import ExecutionDecision, decide_execution
from packages.auth import AccessError
from packages.domain.models import Snapshot
from packages.integrations.factory_http import FactoryControls
from packages.planning.business_service import study_view
from packages.planning.service import active_baseline
from packages.planning.store import FactoryState, SnapshotRecord, SolveJob
from services.api.access import principal

router = APIRouter(prefix="/api/factories/{factory_id}/assistant")


@router.get("")
def state(factory_id: str, request: Request, case_id: str | None = None):
    actor = principal(request)
    actor.require(factory_id, {"planner", "manager", "admin"})
    with Session(request.app.state.engine) as db:
        factory = db.get(FactoryState, factory_id)
        saved = db.get(SnapshotRecord, factory.snapshot_id) if factory else None
        snapshot = Snapshot.model_validate(saved.document) if saved else None
        studies = _studies(db, factory_id, case_id)
        case = db.get(CaseRecord, case_id) if case_id else None
        case_actions = (
            or_(
                AssistantAction.payload["case_id"].astext == case_id,
                AssistantAction.result["case_id"].astext == case_id,
                AssistantAction.payload["candidate_id"].astext.in_(
                    case.context.get("candidate_ids", [])
                ),
                AssistantAction.payload["candidate_id"].astext.in_(
                    select(SolveJob.candidate_id).where(
                        SolveJob.factory_id == factory_id, SolveJob.case_id == case_id
                    )
                ),
            )
            if case
            else None
        )
        rows = db.scalars(
            select(AssistantAction)
            .where(
                AssistantAction.factory_id == factory_id,
                AssistantAction.user_id == actor.user_id,
                *([case_actions] if case_actions is not None else []),
            )
            .order_by(AssistantAction.created_at.desc())
            .limit(200 if case_id is not None else 60)
        )
        solver_state = latest_solver_state(db, snapshot, case_id) if snapshot and case_id else None
        return {
            "actions": [action_view(row) for row in rows],
            "learning": learning(db, factory_id, actor.user_id),
            "business_studies": studies,
            "material_balance": {
                "snapshot_id": snapshot.snapshot_id if snapshot else None,
                "shortfalls": material_shortfalls(snapshot) if snapshot else [],
            },
            "recovery_paths": track_recovery_paths(
                db,
                snapshot,
                recovery_paths(
                    snapshot,
                    active_baseline(db, snapshot) if snapshot.active_plan_hash else None,
                    solver_state=solver_state,
                ),
            )
            if snapshot
            else [],
        }


@router.post("/actions")
def submit(factory_id: str, body: AssistantRequest, request: Request):
    return enqueue(request.app.state.engine, principal(request, mutation=True), factory_id, body)


@router.post("/executions/{action_id}/decision")
def execution_decision(factory_id: str, action_id: str, body: ExecutionDecision, request: Request):
    settings = request.app.state.settings
    controls = FactoryControls(
        settings.factory_api_url, settings.factory_control_token.get_secret_value()
    )
    try:
        return decide_execution(
            request.app.state.engine,
            controls,
            principal(request, mutation=True),
            factory_id,
            action_id,
            body,
        )
    finally:
        controls.close()


@router.get("/recovery-requests")
def recovery_requests(factory_id: str, request: Request):
    actor = principal(request)
    actor.require(factory_id, {"maintainer", "sim_admin"})
    with Session(request.app.state.engine) as db:
        state = db.get(FactoryState, factory_id)
        saved = db.get(SnapshotRecord, state.snapshot_id) if state else None
        if saved is None:
            return {"run_id": None, "requests": []}
        snapshot = Snapshot.model_validate(saved.document)
        baseline = active_baseline(db, snapshot) if snapshot.active_plan_hash else None
        from packages.agent.recovery_followup import recovery_view, selected_rows

        paths = {p["kind"]: p for p in recovery_paths(snapshot, baseline)}
        return {
            "run_id": snapshot.run_id,
            "requests": [
                recovery_view(db, row, snapshot, paths)
                for row in selected_rows(db, snapshot)
                if row.result and isinstance(row.result.get("path"), dict)
            ],
        }


def _studies(db: Session, factory_id: str, case_id: str | None = None) -> list[dict]:
    state = db.get(FactoryState, factory_id)
    saved = db.get(SnapshotRecord, state.snapshot_id) if state else None
    snapshot = Snapshot.model_validate(saved.document) if saved else None
    if snapshot is None:
        return []
    run_id = snapshot.run_id
    if case_id is not None:
        case = db.get(CaseRecord, case_id)
        if case is None or case.factory_id != factory_id:
            raise AccessError(
                "CASE_NOT_FOUND", "The current factory has no such conversation.", 404
            )
        # A conversation from an earlier run stays readable; its results are never current.
        run_id = case.run_id
    jobs = db.scalars(
        select(SolveJob)
        .join(SnapshotRecord, SolveJob.snapshot_id == SnapshotRecord.snapshot_id)
        .join(CaseRecord, SolveJob.case_id == CaseRecord.case_id)
        .where(
            SolveJob.factory_id == factory_id,
            SnapshotRecord.factory_id == factory_id,
            SnapshotRecord.document["run_id"].astext == run_id,
            SolveJob.business_request.is_not(None),
            SolveJob.case_id.is_not(None),
            CaseRecord.factory_id == factory_id,
            CaseRecord.run_id == run_id,
            *([SolveJob.case_id == case_id] if case_id is not None else []),
        )
        .order_by(SolveJob.created_at.desc())
        .limit(200 if case_id is not None else 10)
    )
    return [study_view(job, snapshot) for job in jobs]
