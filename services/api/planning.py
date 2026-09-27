"""Authenticated workbench API; mutation authority is independent of model output."""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import Field, StrictBool
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.agent.work import AgentRun, enqueue, view
from packages.domain.execution import ReplayStart, SimulatorCommand, TodayRunStart
from packages.domain.models import Contract, Digest, Identifier, Timestamp
from packages.integrations.factory_http import FactoryControls, FactoryHTTP
from packages.planning import service
from packages.planning.publication import commit_publication, publications
from packages.planning.store import FactoryState
from services.api.access import principal

router = APIRouter(prefix="/api")
READ_ROLES = {"planner", "manager", "maintainer", "warehouse", "team_lead", "admin"}
WORKSPACE_ROLES = READ_ROLES | {"sim_admin"}


class SolveInput(Contract):
    request_id: Identifier
    allow_overtime: StrictBool = False
    time_limit: int = Field(default=30, strict=True, ge=1, le=120)
    new_actions_not_before: Timestamp | None = None


class ApprovalInput(Contract):
    request_id: Identifier
    candidate_hash: Digest
    action_scope: Literal["publish_plan", "allow_overtime"]
    decision: Literal["APPROVED", "REJECTED"]


class AgentInput(Contract):
    request_id: Identifier
    message: str = Field(min_length=1, max_length=8000)


class PublicationInput(Contract):
    request_id: Identifier
    candidate_hash: Digest
    certificate_id: Identifier | None = None


class ValidationInput(Contract):
    request_id: Identifier
    candidate_hash: Digest


class ProgressApprovalInput(ApprovalInput):
    decision: Literal["APPROVED"]


def source(request: Request) -> FactoryHTTP:
    config = request.app.state.settings
    return FactoryHTTP(config.factory_api_url, config.factory_api_token.get_secret_value())


@router.get("/factories")
def factories(request: Request):
    actor = principal(request)
    result = []
    with Session(request.app.state.engine) as db:
        for factory_id in sorted({g.factory_id for g in actor.grants if g.role in WORKSPACE_ROLES}):
            state = db.get(FactoryState, factory_id)
            result.append(
                {
                    "factory_id": factory_id,
                    "roles": sorted(g.role for g in actor.grants if g.factory_id == factory_id),
                    "last_synced_at": state.last_synced_at if state else None,
                    "snapshot_id": state.snapshot_id if state else None,
                }
            )
    return {"factories": result}


@router.get("/factories/{factory_id}/workspace")
def workspace(
    factory_id: str, request: Request, case_id: str | None = None, candidate_id: str | None = None
):
    principal(request).require(factory_id, WORKSPACE_ROLES)
    result = service.workspace(
        request.app.state.engine, factory_id, case_id=case_id, candidate_id=candidate_id
    )
    return {
        **result,
        "publications": publications(
            request.app.state.engine,
            factory_id,
            candidate_ids=[row["candidate"].candidate_id for row in result["candidates"]]
            if case_id or candidate_id
            else None,
        ),
        "server_time": datetime.now(UTC),
    }


@router.get("/factories/{factory_id}/candidates/{candidate_id}/export")
def export_plan(factory_id: str, candidate_id: str, candidate_hash: Digest, request: Request):
    from packages.planning.exports import export_candidate

    document = export_candidate(
        request.app.state.engine,
        principal(request),
        factory_id,
        candidate_id,
        candidate_hash=candidate_hash,
    )
    return JSONResponse(
        document, headers={"Content-Disposition": 'attachment; filename="byof-plan.json"'}
    )


@router.post("/factories/{factory_id}/candidates/{candidate_id}/publications")
def publication(factory_id: str, candidate_id: str, body: PublicationInput, request: Request):
    actor = principal(request, mutation=True)
    actor.require(factory_id, {"planner"})
    connector = source(request)
    try:
        service.synchronize(request.app.state.engine, connector, factory_id)
    finally:
        connector.close()
    return commit_publication(
        request.app.state.engine, actor, factory_id, candidate_id, **body.model_dump()
    )


@router.post("/factories/{factory_id}/candidates/{candidate_id}/validations")
def validate_remaining(factory_id: str, candidate_id: str, body: ValidationInput, request: Request):
    from packages.planning.revalidation import issue_certificate

    actor = principal(request, mutation=True)
    actor.require(factory_id, {"planner"})
    connector = source(request)
    try:
        service.synchronize(request.app.state.engine, connector, factory_id)
    finally:
        connector.close()
    return issue_certificate(
        request.app.state.engine, actor, factory_id, candidate_id, **body.model_dump()
    )


@router.post("/factories/{factory_id}/candidates/{candidate_id}/progress-approvals")
def approve_remaining(
    factory_id: str, candidate_id: str, body: ProgressApprovalInput, request: Request
):
    from packages.planning.reviews import approve_progress

    actor = principal(request, mutation=True)
    actor.require(factory_id, {"manager" if body.action_scope == "allow_overtime" else "planner"})
    connector = source(request)
    try:
        service.synchronize(request.app.state.engine, connector, factory_id)
    finally:
        connector.close()
    return approve_progress(
        request.app.state.engine, actor, factory_id, candidate_id, **body.model_dump()
    )


@router.post("/admin/factories/{factory_id}/simulator/commands")
def simulator_command(factory_id: str, body: SimulatorCommand, request: Request):
    principal(request, mutation=True).require(factory_id, {"sim_admin"})
    if body.kind == "business.accept":
        from packages.auth import AccessError

        raise AccessError(
            "BUSINESS_ACCEPT_UNSUPPORTED",
            "Business options cannot be written directly into the shop floor. Maintain the factory facts as they are, then the manager analyzes and approves the production plan.",
            409,
        )
    config = request.app.state.settings
    controls = FactoryControls(
        config.factory_api_url, config.factory_control_token.get_secret_value()
    )
    try:
        result = controls.command(factory_id, body)
    finally:
        controls.close()
    connector = source(request)
    try:
        service.synchronize(request.app.state.engine, connector, factory_id)
    finally:
        connector.close()
    return result


@router.get("/admin/factories/{factory_id}/simulator")
def simulator_status(factory_id: str, request: Request):
    from urllib.parse import quote

    principal(request).require(factory_id, {"sim_admin"})
    config = request.app.state.settings
    controls = FactoryControls(
        config.factory_api_url, config.factory_control_token.get_secret_value()
    )
    try:
        return controls._get("/simulator/v1/factories/" + quote(factory_id, safe=""))
    finally:
        controls.close()


@router.post("/admin/factories/{factory_id}/simulator/replays")
def simulator_replay(factory_id: str, body: ReplayStart, request: Request):
    actor = principal(request, mutation=True)
    actor.require(factory_id, {"sim_admin"})
    config = request.app.state.settings
    controls = FactoryControls(
        config.factory_api_url, config.factory_control_token.get_secret_value()
    )
    try:
        result = controls.start_replay(factory_id, body)
    finally:
        controls.close()
    connector = source(request)
    try:
        service.synchronize(
            request.app.state.engine,
            connector,
            factory_id,
            run_switch=(actor, body.request_id, result["run_id"]),
        )
    finally:
        connector.close()
    return result


@router.post("/admin/factories/{factory_id}/simulator/today-runs")
def simulator_today_run(factory_id: str, body: TodayRunStart, request: Request):
    actor = principal(request, mutation=True)
    actor.require(factory_id, {"sim_admin"})
    config = request.app.state.settings
    controls = FactoryControls(
        config.factory_api_url, config.factory_control_token.get_secret_value()
    )
    try:
        result = controls.start_today_run(factory_id, body)
    finally:
        controls.close()
    connector = source(request)
    try:
        service.synchronize(
            request.app.state.engine,
            connector,
            factory_id,
            run_switch=(actor, body.request_id, result["run_id"]),
        )
    finally:
        connector.close()
    return result


@router.post("/factories/{factory_id}/sync")
def sync(factory_id: str, request: Request):
    principal(request, mutation=True).require(factory_id, {"planner", "admin"})
    connector = source(request)
    try:
        snapshot = service.synchronize(request.app.state.engine, connector, factory_id)
    finally:
        connector.close()
    return {"snapshot_id": snapshot.snapshot_id, "content_hash": snapshot.content_hash}


@router.post("/factories/{factory_id}/solve")
def solve(factory_id: str, body: SolveInput, request: Request):
    actor = principal(request, mutation=True)
    job = service.request_solve(request.app.state.engine, actor, factory_id, **body.model_dump())
    return {
        "job_id": job.job_id,
        "state": job.state,
        "new_actions_not_before": job.new_actions_not_before,
    }


@router.post("/factories/{factory_id}/candidates/{candidate_id}/approvals")
def approval(factory_id: str, candidate_id: str, body: ApprovalInput, request: Request):
    actor = principal(request, mutation=True)
    actor.require(factory_id, {"manager"} if body.action_scope == "allow_overtime" else {"planner"})
    connector = source(request)
    try:
        service.synchronize(request.app.state.engine, connector, factory_id)
    finally:
        connector.close()
    return service.approve(
        request.app.state.engine, actor, factory_id, candidate_id, **body.model_dump()
    )


@router.get("/factories/{factory_id}/agent-runs")
def agent_runs(factory_id: str, request: Request):
    principal(request).require(factory_id, READ_ROLES)
    with Session(request.app.state.engine) as db:
        records = db.scalars(
            select(AgentRun)
            .where(AgentRun.factory_id == factory_id)
            .order_by(AgentRun.created_at.desc())
            .limit(30)
        ).all()
        return {"runs": [view(r) for r in records]}


@router.post("/factories/{factory_id}/agent-runs")
def agent_run(factory_id: str, body: AgentInput, request: Request):
    actor = principal(request, mutation=True)
    return view(enqueue(request.app.state.engine, actor, factory_id, body.request_id, body.message))
