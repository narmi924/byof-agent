"""Factory-scoped case and authenticated human-task routes; GET never changes handling state."""

from fastapi import APIRouter, Request
from pydantic import Field, StrictBool, StrictStr
from sqlalchemy.orm import Session

from packages.agent import cases as service
from packages.agent import human_tasks
from packages.auth import AccessError
from packages.domain.models import Contract, Identifier, Snapshot, Timestamp
from packages.planning.service import require_live, synchronize
from packages.planning.store import FactoryState, SnapshotRecord
from services.api.access import principal
from services.api.planning import source

router = APIRouter(prefix="/api")


class MessageInput(Contract):
    request_id: Identifier
    message: StrictStr = Field(min_length=1, max_length=8000)
    suggestion_id: Identifier | None = None


class CreateInput(MessageInput):
    start_new: StrictBool = False


class StopInput(Contract):
    request_id: Identifier
    expected_target: Identifier


def _live(request: Request, factory_id: str) -> None:
    with Session(request.app.state.engine) as db:
        state = db.get(FactoryState, factory_id)
        snapshot = db.get(SnapshotRecord, state.snapshot_id) if state else None
        if snapshot is None:
            raise AccessError("SNAPSHOT_REQUIRED", "Sync the factory facts first.", 409)
        require_live(Snapshot.model_validate(snapshot.document))


@router.get("/factories/{factory_id}/cases")
def cases(factory_id: str, request: Request):
    actor = principal(request)
    actor.require(factory_id, service.READ_ROLES)
    return {"cases": service.list_cases(request.app.state.engine, actor, factory_id)}


@router.get("/factories/{factory_id}/risk-suggestions")
def risk_suggestions(factory_id: str, request: Request):
    actor = principal(request)
    actor.require(factory_id, {"manager"})
    return service.list_risk_suggestions(request.app.state.engine, actor, factory_id)


@router.post("/factories/{factory_id}/cases")
def create_case(factory_id: str, body: CreateInput, request: Request):
    actor = principal(request, mutation=True)
    actor.require(factory_id, {"planner"})
    recovered = service.recover_case_input(
        request.app.state.engine, actor, factory_id, **body.model_dump(exclude_none=True)
    )
    if recovered is not None:
        return recovered
    connector = source(request)
    try:
        synchronize(request.app.state.engine, connector, factory_id)
    finally:
        connector.close()
    return service.create_case(
        request.app.state.engine, actor, factory_id, **body.model_dump(exclude_none=True)
    )


@router.get("/factories/{factory_id}/cases/{case_id}")
def case(factory_id: str, case_id: str, request: Request):
    actor = principal(request)
    actor.require(factory_id, service.READ_ROLES)
    return service.get_case(request.app.state.engine, actor, factory_id, case_id)


@router.get("/factories/{factory_id}/cases/{case_id}/history")
def history(
    factory_id: str, case_id: str, request: Request, before_at: Timestamp, before_id: Identifier
):
    return service.case_history(
        request.app.state.engine, principal(request), factory_id, case_id, before_at, before_id
    )


@router.post("/factories/{factory_id}/cases/{case_id}/messages")
def message(factory_id: str, case_id: str, body: MessageInput, request: Request):
    actor = principal(request, mutation=True)
    actor.require(factory_id, {"planner"})
    recovered = service.recover_case_input(
        request.app.state.engine,
        actor,
        factory_id,
        case_id=case_id,
        **body.model_dump(exclude_none=True),
    )
    if recovered is not None:
        return recovered
    connector = source(request)
    try:
        synchronize(request.app.state.engine, connector, factory_id)
    finally:
        connector.close()
    return service.message_case(
        request.app.state.engine, actor, factory_id, case_id, **body.model_dump(exclude_none=True)
    )


@router.post("/factories/{factory_id}/cases/{case_id}/stop")
def stop(factory_id: str, case_id: str, body: StopInput, request: Request):
    actor = principal(request, mutation=True)
    actor.require(factory_id, {"planner"})
    return service.stop_case_turn(
        request.app.state.engine, actor, factory_id, case_id, **body.model_dump()
    )


@router.get("/factories/{factory_id}/human-tasks")
def tasks(factory_id: str, request: Request, case_id: str | None = None):
    actor = principal(request)
    actor.require(factory_id, human_tasks.ROLES)
    return {
        "tasks": human_tasks.list_tasks(
            request.app.state.engine, actor, factory_id, case_id=case_id
        )
    }


@router.get("/factories/{factory_id}/human-tasks/{task_id}")
def task(factory_id: str, task_id: str, request: Request):
    actor = principal(request)
    actor.require(factory_id, human_tasks.ROLES)
    return human_tasks.get_task(request.app.state.engine, actor, factory_id, task_id)


@router.post("/factories/{factory_id}/human-tasks/{task_id}/responses")
def response(factory_id: str, task_id: str, body: human_tasks.ResponseInput, request: Request):
    actor = principal(request, mutation=True)
    actor.require(factory_id, human_tasks.ROLES)
    _live(request, factory_id)
    return human_tasks.respond(
        request.app.state.engine, actor, factory_id, task_id, **body.model_dump()
    )


@router.post("/factories/{factory_id}/human-tasks/{task_id}/transfers")
def transfer(factory_id: str, task_id: str, body: human_tasks.TransferInput, request: Request):
    actor = principal(request, mutation=True)
    actor.require(factory_id, human_tasks.ROLES)
    _live(request, factory_id)
    return human_tasks.transfer(
        request.app.state.engine, actor, factory_id, task_id, **body.model_dump()
    )


@router.post("/factories/{factory_id}/human-tasks/{task_id}/cancellations")
def cancellation(factory_id: str, task_id: str, body: human_tasks.CancelInput, request: Request):
    actor = principal(request, mutation=True)
    actor.require(factory_id, human_tasks.ROLES)
    _live(request, factory_id)
    return human_tasks.cancel(
        request.app.state.engine, actor, factory_id, task_id, **body.model_dump()
    )


@router.post("/factories/{factory_id}/human-tasks/{task_id}/handoffs")
def handoff(factory_id: str, task_id: str, body: human_tasks.HandoffInput, request: Request):
    actor = principal(request, mutation=True)
    actor.require(factory_id, {"planner", "manager"})
    _live(request, factory_id)
    return human_tasks.accept_handoff(
        request.app.state.engine, actor, factory_id, task_id, **body.model_dump()
    )
