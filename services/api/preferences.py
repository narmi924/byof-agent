"""Explicit user actions confirm preferences; GET and model proposals never activate them."""

from fastapi import APIRouter, Request

from packages.planning import preferences as service
from services.api.access import principal

router = APIRouter(prefix="/api/factories/{factory_id}/preferences")


@router.get("")
def preferences(factory_id: str, request: Request):
    return service.get_preferences(request.app.state.engine, principal(request), factory_id)


@router.post("/proposals")
def propose(factory_id: str, body: service.ProposalInput, request: Request):
    return service.propose(
        request.app.state.engine, principal(request, mutation=True), factory_id, body
    )


@router.post("/proposals/{proposal_id}/confirmations")
def confirm(factory_id: str, proposal_id: str, body: service.ConfirmationInput, request: Request):
    return service.confirm(
        request.app.state.engine, principal(request, mutation=True), factory_id, proposal_id, body
    )


@router.post("/proposals/{proposal_id}/rejections")
def reject(factory_id: str, proposal_id: str, body: service.RejectionInput, request: Request):
    return service.reject_proposal(
        request.app.state.engine, principal(request, mutation=True), factory_id, proposal_id, body
    )


@router.post("/coordination")
def coordinate(factory_id: str, body: service.CoordinationInput, request: Request):
    return service.coordinate(
        request.app.state.engine, principal(request, mutation=True), factory_id, body
    )


@router.post("/{scope_type}/{scope_id}/deactivations")
def deactivate(
    factory_id: str,
    scope_type: str,
    scope_id: str,
    body: service.DeactivationInput,
    request: Request,
):
    return service.deactivate(
        request.app.state.engine,
        principal(request, mutation=True),
        factory_id,
        scope_type,
        scope_id,
        body,
    )
