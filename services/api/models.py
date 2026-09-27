"""Authenticated model choices contain public IDs only."""

from fastapi import APIRouter, Request
from pydantic import Field, StrictInt
from sqlalchemy.orm import Session

from packages.auth import AccessError
from packages.domain.models import Contract, Identifier
from packages.providers.catalog import ModelCatalog
from packages.providers.selection import choose, view
from services.api.access import principal

router = APIRouter(prefix="/api/model-selection")


class SelectionInput(Contract):
    model_id: Identifier
    expected_version: StrictInt = Field(ge=0)
    request_id: Identifier


@router.get("")
def read(request: Request):
    actor = principal(request)
    if not any(g.role in {"planner", "manager"} for g in actor.grants):
        raise AccessError("FORBIDDEN", "This account may not choose the Production Agent model.")
    with Session(request.app.state.engine) as db:
        return view(db, actor.user_id, ModelCatalog(request.app.state.settings))


@router.post("")
def select_model(body: SelectionInput, request: Request):
    return choose(
        request.app.state.engine,
        principal(request, mutation=True),
        ModelCatalog(request.app.state.settings),
        **body.model_dump(),
    )
