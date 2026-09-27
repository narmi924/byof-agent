"""Only a current factory administrator can configure an authenticated role's contact."""

import re
from datetime import UTC, datetime

from pydantic import Field, StrictBool, StrictStr, field_validator
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from packages.agent.cases import live_actor
from packages.agent.human_tasks import ROLES, TaskRole
from packages.auth import AccessError, Principal, lock_membership, lock_user
from packages.domain.models import Contract, Identifier, NonNegative, canonical_hash
from packages.integrations.notification_store import ContactAction, NotificationContact
from packages.persistence import Membership, User
from packages.planning.store import FactoryState
from packages.settings import Settings


class ContactInput(Contract):
    request_id: Identifier
    role: TaskRole
    user_id: Identifier
    email: StrictStr = Field(min_length=3, max_length=254)
    enabled: StrictBool
    expected_version: NonNegative

    @field_validator("email")
    @classmethod
    def single_mailbox(cls, value: str) -> str:
        # One explicit ASCII mailbox; no display names, headers or address lists.
        if not re.fullmatch(
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)+", value
        ):
            raise ValueError("A single mailbox address is required")
        local, domain = value.rsplit("@", 1)
        if len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local:
            raise ValueError("Invalid mailbox")
        return local + "@" + domain.lower()


def channel_state(settings: Settings) -> str:
    if settings.smtp_mode == "disabled" or not settings.smtp_host or not settings.smtp_from:
        return "NOT_ENABLED"
    if settings.smtp_mode == "capture":
        return "CAPTURE" if settings.environment in {"local", "test"} else "NOT_ENABLED"
    return (
        "TLS"
        if settings.allow_real_email
        and (
            settings.real_email_allowlist.get_secret_value().strip()
            or settings.test_email_recipient
        )
        else "NOT_ENABLED"
    )


def contact_view(db: Session, row: NotificationContact) -> dict:
    user = db.get(User, row.user_id)
    return {
        "role": row.role,
        "user_id": row.user_id,
        "username": user.username if user else "",
        "email": row.email,
        "version": row.version,
        "enabled": row.enabled,
    }


def list_contacts(engine: Engine, actor: Principal, factory_id: str, settings: Settings) -> dict:
    with Session(engine) as db:
        live_actor(db, actor, factory_id, {"admin"})
        contacts = [
            contact_view(db, row)
            for row in db.scalars(
                select(NotificationContact)
                .where(NotificationContact.factory_id == factory_id)
                .order_by(NotificationContact.role)
            )
        ]
        users: dict[str, dict] = {}
        for user, member in db.execute(
            select(User, Membership)
            .join(Membership)
            .where(
                User.active.is_(True),
                Membership.factory_id == factory_id,
                Membership.role.in_(ROLES),
            )
            .order_by(User.username, Membership.role)
        ):
            users.setdefault(
                user.user_id, {"user_id": user.user_id, "username": user.username, "roles": []}
            )["roles"].append(member.role)
        return {
            "contacts": contacts,
            "eligible_users": list(users.values()),
            "channel_state": channel_state(settings),
        }


def configure_contact(
    engine: Engine, actor: Principal, factory_id: str, body: ContactInput
) -> dict:
    with Session(engine) as db, db.begin():
        state = db.get(FactoryState, factory_id, with_for_update=True)
        if state is None:
            raise AccessError("SNAPSHOT_REQUIRED", "Connect and sync the factory first.", 409)
        # The same factory lock serializes contact changes with task send authorization.
        live_actor(db, actor, factory_id, {"admin"}, lock=True)
        digest = canonical_hash(body)
        prior = db.get(ContactAction, (factory_id, body.request_id))
        if prior:
            if prior.actor_id != actor.user_id or prior.payload_hash != digest:
                raise AccessError(
                    "IDEMPOTENCY_CONFLICT",
                    "The action ID was already used for other contact content.",
                    409,
                )
            return prior.result
        user = lock_user(db, body.user_id)
        member = lock_membership(db, body.user_id, factory_id, body.role)
        if user is None or not user.active or member is None:
            raise AccessError(
                "INVALID_CONTACT_USER",
                "The contact has no valid role required by this factory.",
                409,
            )
        row = db.get(NotificationContact, (factory_id, body.role), with_for_update=True)
        if (row.version if row else 0) != body.expected_version:
            raise AccessError(
                "CONTACT_VERSION_CHANGED",
                "The contact configuration has changed; refresh before saving.",
                409,
            )
        if row is None:
            row = NotificationContact(factory_id=factory_id, role=body.role, version=0)
            db.add(row)
        row.user_id, row.email, row.enabled = body.user_id, body.email, body.enabled
        row.version, row.updated_at = row.version + 1, datetime.now(UTC)
        result = contact_view(db, row)
        db.add(
            ContactAction(
                factory_id=factory_id,
                request_id=body.request_id,
                actor_id=actor.user_id,
                payload_hash=digest,
                result=result,
                created_at=datetime.now(UTC),
            )
        )
        return result
