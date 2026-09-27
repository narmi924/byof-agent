"""Create the two selectable demo identities without user passwords."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from packages.demo_identities import BUSINESS_FACTORIES, FACTORY_ID, NO_PASSWORD, PERSONAS
from packages.persistence import LoginSession, Membership, User, connect
from packages.settings import Settings

LEGACY_ROLES = ("planner", "manager", "maintainer", "warehouse", "team_lead", "admin", "sim_admin")
LEGACY_REVIEWER = ("planner", "manager", "sim_admin", "maintainer", "warehouse", "team_lead")


def retire_legacy_accounts(db: Session) -> None:
    for username in (*LEGACY_ROLES, "reviewer"):
        user = db.scalar(select(User).where(User.username == username).with_for_update())
        if user is None:
            continue
        memberships = list(db.scalars(select(Membership).where(Membership.user_id == user.user_id)))
        expected = (
            {("skf-workshop", role) for role in LEGACY_REVIEWER}
            if username == "reviewer"
            else {(factory_id, username) for factory_id in ("skf-reference", "skf-workshop")}
        )
        actual = {(membership.factory_id, membership.role) for membership in memberships}
        if actual and actual != expected:
            raise ValueError(
                f"Legacy account {username} has changed grants; inspect it before setup"
            )
        db.execute(delete(LoginSession).where(LoginSession.user_id == user.user_id))
        db.execute(delete(Membership).where(Membership.user_id == user.user_id))
        user.active = False


def provision(engine: Engine) -> dict[str, str]:
    outcomes: dict[str, str] = {}
    with Session(engine) as db, db.begin():
        for role, (username, grants) in PERSONAS.items():
            user = db.scalar(select(User).where(User.username == username).with_for_update())
            created = user is None
            if user is None:
                user = User(
                    user_id=str(uuid4()),
                    username=username,
                    password_hash=NO_PASSWORD,
                    active=True,
                )
                db.add(user)
                db.flush()
                outcomes[role] = "CREATED"
            else:
                if not user.active:
                    raise ValueError(f"Existing {username} is inactive")
                outcomes[role] = "EXISTING_PRESERVED"
            existing = {
                (membership.factory_id, membership.role)
                for membership in db.scalars(
                    select(Membership).where(Membership.user_id == user.user_id)
                )
            }
            expected = {(FACTORY_ID, grant) for grant in grants}
            allowed = expected | {
                (factory, grant) for factory in BUSINESS_FACTORIES for grant in grants
            }
            if existing - allowed:
                raise ValueError(f"Existing {username} has unexpected grants")
            if not created and not expected <= existing:
                raise ValueError(f"Existing {username} has incomplete workshop grants")
            for factory in BUSINESS_FACTORIES:
                present = {(f, r) for f, r in existing if f == factory}
                if present and present != {(factory, grant) for grant in grants}:
                    raise ValueError(f"Existing {username} has incomplete scenario grants")
            legacy = existing - expected
            if legacy:
                db.execute(
                    delete(Membership).where(
                        Membership.user_id == user.user_id,
                        Membership.factory_id.in_(BUSINESS_FACTORIES),
                    )
                )
                outcomes[role] = "SCENARIO_ACCESS_REVOKED"
            if legacy or user.password_hash != NO_PASSWORD:
                # An old tab must select its role again after authority changes.
                db.execute(delete(LoginSession).where(LoginSession.user_id == user.user_id))
            user.password_hash = NO_PASSWORD
            for factory_id, grant in expected - existing:
                db.add(Membership(user_id=user.user_id, factory_id=factory_id, role=grant))
        retire_legacy_accounts(db)
    return outcomes


def main() -> None:
    settings = Settings()
    if settings.environment != "local":
        raise ValueError("Demo identity setup is limited to local workspaces")
    engine = connect(settings.migration_database_url.get_secret_value())
    try:
        print(provision(engine))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
