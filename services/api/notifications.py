"""Authenticated contact configuration and notification evidence; links only read tasks."""

from fastapi import APIRouter, Request

from packages.integrations.contacts import ContactInput, configure_contact, list_contacts
from packages.integrations.notifications import list_notifications
from services.api.access import principal

router = APIRouter(prefix="/api")


@router.get("/admin/factories/{factory_id}/notification-contacts")
def contacts(factory_id: str, request: Request):
    actor = principal(request)
    actor.require(factory_id, {"admin"})
    return list_contacts(request.app.state.engine, actor, factory_id, request.app.state.settings)


@router.post("/admin/factories/{factory_id}/notification-contacts")
def configure(factory_id: str, body: ContactInput, request: Request):
    actor = principal(request, mutation=True)
    actor.require(factory_id, {"admin"})
    return configure_contact(request.app.state.engine, actor, factory_id, body)


@router.get("/factories/{factory_id}/notifications")
def notifications(factory_id: str, task_id: str, request: Request):
    actor = principal(request)
    return list_notifications(request.app.state.engine, actor, factory_id, task_id)
