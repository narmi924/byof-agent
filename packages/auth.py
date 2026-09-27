"""Server-issued sessions, real-clock expiry and factory-scoped role checks."""

import hashlib
import hmac
import secrets
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from packages.demo_identities import NO_PASSWORD, PERSONAS, valid_persona_grants
from packages.persistence import LoginSession, Membership, User

Role = Literal["planner", "manager", "maintainer", "warehouse", "team_lead", "admin", "sim_admin"]
hasher = PasswordHasher()
_DUMMY_HASH = hasher.hash("no-user-" + secrets.token_hex(16))


class AccessError(Exception):
    def __init__(self, code: str, message: str, status: int = 403):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


class Grant(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    factory_id: str
    role: Role


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    user_id: str
    username: str
    grants: tuple[Grant, ...]

    def require(self, factory_id: str, roles: set[str]) -> None:
        if not any(g.factory_id == factory_id and g.role in roles for g in self.grants):
            raise AccessError("FORBIDDEN", "This account may not perform this action.")


def lock_user(db: Session, user_id: str) -> User | None:
    """Pin live authorization through commit without excluding other authorization readers.

    FOR SHARE blocks active/role revocation, unlike FOR KEY SHARE. Callers retain their
    business-row locks; these identity rows must not be updated after taking a read lock.
    """
    return db.get(User, user_id, with_for_update={"read": True}, populate_existing=True)


def lock_membership(db: Session, user_id: str, factory_id: str, role: str) -> Membership | None:
    return db.get(
        Membership,
        (user_id, factory_id, role),
        with_for_update={"read": True},
        populate_existing=True,
    )


def lock_memberships(
    db: Session,
    user_id: str,
    factory_id: str,
    roles: Collection[str] | None = None,
) -> tuple[Membership, ...]:
    statement = select(Membership).where(
        Membership.user_id == user_id, Membership.factory_id == factory_id
    )
    if roles is not None:
        statement = statement.where(Membership.role.in_(roles))
    return tuple(
        db.scalars(
            statement.order_by(Membership.role)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    )


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _session_csrf(token: str) -> str:
    return hmac.new(token.encode(), b"byof-session-csrf-v1", hashlib.sha256).hexdigest()


def _issue_session(db: Session, user: User) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = _session_csrf(token)
    db.add(
        LoginSession(
            token_hash=digest(token),
            user_id=user.user_id,
            csrf_hash=digest(csrf),
            expires_at=datetime.now(UTC) + timedelta(hours=8),
        )
    )
    return token, csrf


def select_demo_role(engine: Engine, role: str) -> tuple[str, str]:
    """Open a scoped demo session from a fixed server-side role allowlist."""
    if role not in PERSONAS:
        raise AccessError(
            "UNKNOWN_DEMO_ROLE", "Choose the manager or the disruption simulator.", 422
        )
    username, expected_roles = PERSONAS[role]
    with Session(engine) as db, db.begin():
        user = db.scalar(select(User).where(User.username == username))
        if user is None or not user.active or user.password_hash != NO_PASSWORD:
            raise AccessError(
                "DEMO_ROLE_UNAVAILABLE", "The demo identities are not set up yet.", 503
            )
        grants = {
            (grant.factory_id, grant.role)
            for grant in db.scalars(select(Membership).where(Membership.user_id == user.user_id))
        }
        if not valid_persona_grants(grants, expected_roles):
            raise AccessError(
                "DEMO_ROLE_UNAVAILABLE", "The demo identity permissions are incorrect.", 503
            )
        return _issue_session(db, user)


def login(engine: Engine, username: str, password: str) -> tuple[str, str]:
    with Session(engine) as db, db.begin():
        user = db.scalar(select(User).where(User.username == username))
        valid: bool
        try:
            valid = hasher.verify(user.password_hash if user else _DUMMY_HASH, password)
        except (VerificationError, InvalidHashError):
            valid = False
        if not valid or not user or not user.active:
            raise AccessError("INVALID_CREDENTIALS", "The user name or password is incorrect.", 401)
        return _issue_session(db, user)


def authenticate(engine: Engine, token: str | None, csrf: str | None = None) -> Principal:
    if not token or len(token) > 200:
        raise AccessError("UNAUTHENTICATED", "Sign in first.", 401)
    with Session(engine) as db:
        session = db.get(LoginSession, digest(token))
        if not session or session.expires_at <= datetime.now(UTC):
            raise AccessError("SESSION_EXPIRED", "Your sign-in has expired; sign in again.", 401)
        if csrf is not None and not hmac.compare_digest(session.csrf_hash, digest(csrf)):
            raise AccessError("INVALID_CSRF", "Page verification has expired; sign in again.")
        user = db.get(User, session.user_id)
        if not user or not user.active:
            raise AccessError("UNAUTHENTICATED", "Sign in first.", 401)
        grants = db.scalars(select(Membership).where(Membership.user_id == user.user_id)).all()
        return Principal(
            user_id=user.user_id,
            username=user.username,
            grants=tuple(
                Grant.model_validate({"factory_id": g.factory_id, "role": g.role}) for g in grants
            ),
        )


def logout(engine: Engine, token: str) -> None:
    with engine.begin() as connection:
        connection.execute(delete(LoginSession).where(LoginSession.token_hash == digest(token)))


def renew_csrf(engine: Engine, token: str) -> str:
    authenticate(engine, token)
    with Session(engine) as db, db.begin():
        record = db.get(LoginSession, digest(token), with_for_update=True)
        if record is None or record.expires_at <= datetime.now(UTC):
            raise AccessError("SESSION_EXPIRED", "Your sign-in has expired; sign in again.", 401)
        # Stable across tabs and workers; the HttpOnly session secret never leaves the cookie.
        csrf = _session_csrf(token)
        record.csrf_hash = digest(csrf)
        return csrf
