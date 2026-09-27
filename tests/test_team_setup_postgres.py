"""Factory isolation and password-free demo sessions on isolated PostgreSQL."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import MetaData, Table, delete, insert, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from packages import auth
from packages.agent.cases_store import CaseRecord
from packages.agent.checkpoints import checkpoint_thread_id
from packages.demo_identities import (
    BUSINESS_FACTORIES,
    FACTORY_ID,
    NO_PASSWORD,
    PERSONAS,
    valid_persona_grants,
)
from packages.domain.execution import SimulatorCommand
from packages.domain.models import Snapshot, batch_operations
from packages.persistence import LoginSession, Membership, User, connect
from packages.settings import Settings
from scripts import setup_team
from scripts.rebuild_workshop import rebuild
from scripts.setup_business_scenarios import ensure_factories as ensure_business_factories
from scripts.setup_business_scenarios import grant_personas
from scripts.setup_workshop_personas import provision
from services.api.main import create_app
from services.factory_sim.service import command
from services.factory_sim.storage import SourceAction, SourceChange, SourceRun, World


@pytest.fixture
def team_database():
    values = [
        os.getenv(key)
        for key in ("TEST_DATABASE_URL", "TEST_FACTORY_DATABASE_URL", "TEST_MIGRATION_DATABASE_URL")
    ]
    if not all(values):
        pytest.skip("Explicit isolated PostgreSQL test URLs required")
    urls = [make_url(value) for value in values]
    if (
        any(url.database != "byof_test" or url.query for url in urls)
        or any(url.drivername != "postgresql+psycopg" for url in urls)
        or len({(url.host, url.port or 5432) for url in urls}) != 1
        or [url.username for url in urls] != ["byof_app", "factory_sim_app", "byof_owner"]
    ):
        pytest.fail("Team setup tests require separate roles in the isolated byof_test database")
    app, source, owner = [connect(value) for value in values]
    names = tuple(username for username, _ in PERSONAS.values())
    factories = (*setup_team.TEAM_FACTORIES, *BUSINESS_FACTORIES)
    owned = False
    try:
        with Session(owner) as db:
            if (
                db.scalar(select(User).where(User.username.in_(names))) is not None
                or db.scalar(select(World).where(World.factory_id.in_(factories))) is not None
            ):
                pytest.fail("Fixed demo test identities are occupied; existing data was preserved")
        owned = True
        yield app, source, owner
    finally:
        if owned:
            with Session(owner) as db, db.begin():
                user_ids = list(db.scalars(select(User.user_id).where(User.username.in_(names))))
                for model in (LoginSession, Membership, User):
                    db.execute(delete(model).where(model.user_id.in_(user_ids)))
                for model in (SourceAction, SourceChange, SourceRun, World):
                    db.execute(delete(model).where(model.factory_id.in_(factories)))
        for engine in (app, source, owner):
            engine.dispose()


def snapshot(engine, factory_id):
    with Session(engine) as db:
        world = db.get(World, factory_id)
        assert world is not None
        return Snapshot.model_validate(world.document)


def test_full_workshop_is_mutable_without_changing_reference(team_database):
    _, source, owner = team_database
    assert setup_team.ensure_factories(source) == {
        factory: "CREATED" for factory in setup_team.TEAM_FACTORIES
    }
    reference = snapshot(source, "skf-reference")
    workshop = snapshot(source, FACTORY_ID)
    assert workshop.run_id != reference.run_id
    assert len(batch_operations(workshop)[1]) == len(batch_operations(reference)[1]) == 864
    assert len(workshop.orders) == len(reference.orders) == 6

    stock = next(item for item in workshop.inventory if item.material_id == "IR-6202")
    command(
        source,
        FACTORY_ID,
        SimulatorCommand(
            request_id=str(uuid4()),
            run_id=workshop.run_id,
            kind="inventory.reconcile",
            payload={
                "material_id": stock.material_id,
                "expected_version": stock.version,
                "counted_on_hand": stock.on_hand - 50,
                "reason": "COUNT_CORRECTION",
            },
        ),
    )
    assert snapshot(source, FACTORY_ID).inventory != workshop.inventory
    assert snapshot(source, "skf-reference") == reference
    assert setup_team.ensure_factories(source) == {
        factory: "EXISTING_PRESERVED" for factory in setup_team.TEAM_FACTORIES
    }
    assert snapshot(source, "skf-reference") == reference
    old_case = "obsolete-workshop-case"
    checkpoints = Table("checkpoints", MetaData(), schema="byof", autoload_with=owner)
    thread_id = checkpoint_thread_id(FACTORY_ID, old_case)
    with Session(owner) as db, db.begin():
        db.add(
            CaseRecord(
                case_id=old_case,
                factory_id=FACTORY_ID,
                run_id=workshop.run_id,
                owner_id="obsolete-manager",
                state="WAITING",
                title="Old demo record",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        db.connection().execute(
            insert(checkpoints).values(
                thread_id=thread_id,
                checkpoint_ns="",
                checkpoint_id="obsolete-checkpoint",
                checkpoint={},
                metadata={},
            )
        )
    renewed, removed = rebuild(owner)
    assert removed > 0
    assert renewed.run_id != workshop.run_id
    assert snapshot(source, FACTORY_ID) == renewed
    assert snapshot(source, "skf-reference") == reference
    with Session(owner) as db:
        assert db.get(CaseRecord, old_case) is None
        assert (
            db.scalar(select(checkpoints.c.thread_id).where(checkpoints.c.thread_id == thread_id))
            is None
        )


def test_legacy_workshop_requires_explicit_rebuild_for_safety_stock(team_database):
    from packages.domain.skf import load_skf_snapshot
    from services.factory_sim.today_run import start_today_run

    _, source, owner = team_database
    setup_team.ensure_factories(source)
    reference = snapshot(source, "skf-reference")
    current = snapshot(source, FACTORY_ID)
    raw = current.model_dump(mode="json", exclude={"content_hash"})
    raw["profile"]["version"] = "V1.6.workshop-full-1"
    raw["inventory"] = [row.model_dump(mode="json") for row in load_skf_snapshot().inventory]
    legacy = Snapshot.model_validate(raw)
    with Session(source) as db, db.begin():
        db.get(World, FACTORY_ID).document = legacy.model_dump(mode="json")

    assert setup_team.ensure_factories(source)[FACTORY_ID] == "EXISTING_PRESERVED"
    assert snapshot(source, FACTORY_ID) == legacy
    # Starting a dated run carries the old initial stock; it is not an upgrade.
    start_today_run(source, FACTORY_ID, "legacy-today", legacy.run_id)
    dated = snapshot(source, FACTORY_ID)
    assert dated.inventory == legacy.inventory
    assert dated.profile.version == "V1.6.workshop-full-1"

    renewed, _ = rebuild(owner)
    assert renewed.profile.version == "V1.6.workshop-full-2"
    assert renewed.inventory == setup_team.workshop_snapshot().inventory
    assert snapshot(source, "skf-reference") == reference
    with Session(source) as db:
        world = db.get(World, FACTORY_ID)
        assert world.mode == "PAUSED" and world.active_candidate is None
    assert not renewed.actuals and renewed.active_plan_version is None
    setup_team.ensure_factories(source)
    assert snapshot(source, FACTORY_ID) == renewed


def test_role_selection_issues_only_scoped_sessions_without_passwords(team_database):
    app, _, owner = team_database
    assert provision(owner) == {"manager": "CREATED", "maintainer": "CREATED"}
    with Session(owner) as db:
        users = list(
            db.scalars(
                select(User).where(User.username.in_(username for username, _ in PERSONAS.values()))
            )
        )
        assert len(users) == 2
        assert all(user.password_hash == NO_PASSWORD for user in users)

    for role, (username, granted) in PERSONAS.items():
        token, csrf = auth.select_demo_role(app, role)
        actor = auth.authenticate(app, token, csrf)
        assert actor.username == username
        assert {(grant.factory_id, grant.role) for grant in actor.grants} == {
            (FACTORY_ID, item) for item in granted
        }
        with pytest.raises(auth.AccessError) as other_factory:
            actor.require("skf-reference", set(granted))
        assert other_factory.value.code == "FORBIDDEN"
        with pytest.raises(auth.AccessError) as password_login:
            auth.login(app, username, "any-password")
        assert password_login.value.code == "INVALID_CREDENTIALS"
    assert provision(owner) == {
        "manager": "EXISTING_PRESERVED",
        "maintainer": "EXISTING_PRESERVED",
    }
    with pytest.raises(auth.AccessError) as unknown:
        auth.select_demo_role(app, "admin")
    assert unknown.value.code == "UNKNOWN_DEMO_ROLE"


def test_legacy_persona_setup_entry_point_only_provisions_shared_workshop(team_database):
    app, _, owner = team_database
    provision(owner)
    grant_personas(owner)
    grant_personas(owner)
    provision(owner)
    for role, (_, granted) in PERSONAS.items():
        token, csrf = auth.select_demo_role(app, role)
        actor = auth.authenticate(app, token, csrf)
        assert {(g.factory_id, g.role) for g in actor.grants} == {
            (FACTORY_ID, grant) for grant in granted
        }
        for factory in BUSINESS_FACTORIES:
            with pytest.raises(auth.AccessError):
                actor.require(factory, set(granted))


def test_upgrade_revokes_complete_legacy_fixture_grants_and_sessions_preserving_data(team_database):
    app, source, owner = team_database
    provision(owner)
    ensure_business_factories(source)
    world = snapshot(source, BUSINESS_FACTORIES[0])
    command(
        source,
        world.factory_id,
        SimulatorCommand(
            request_id="preserved-fixture-history",
            run_id=world.run_id,
            kind="worker.absent",
            payload={"worker_id": world.workers[0].worker_id},
        ),
    )
    worlds = {factory: snapshot(source, factory) for factory in BUSINESS_FACTORIES}
    sessions = {role: auth.select_demo_role(app, role) for role in PERSONAS}
    with Session(owner) as db, db.begin():
        before_users = {
            user.username: user.user_id
            for user in db.scalars(select(User))
            if user.username in {entry[0] for entry in PERSONAS.values()}
        }
        actions = [
            (row.action_id, row.request, row.result)
            for row in db.scalars(
                select(SourceAction).where(SourceAction.factory_id == world.factory_id)
            )
        ]
        changes = [
            row.document
            for row in db.scalars(
                select(SourceChange).where(SourceChange.factory_id == world.factory_id)
            )
        ]
        for username, roles in PERSONAS.values():
            user_id = before_users[username]
            for factory in BUSINESS_FACTORIES:
                for role in roles:
                    db.add(Membership(user_id=user_id, factory_id=factory, role=role))
    with pytest.raises(auth.AccessError, match="The demo identity permissions are incorrect"):
        auth.select_demo_role(app, "manager")
    assert provision(owner) == {
        "manager": "SCENARIO_ACCESS_REVOKED",
        "maintainer": "SCENARIO_ACCESS_REVOKED",
    }
    for role, (username, grants) in PERSONAS.items():
        with pytest.raises(auth.AccessError) as expired:
            auth.authenticate(app, *sessions[role])
        assert expired.value.code == "SESSION_EXPIRED"
        token, csrf = auth.select_demo_role(app, role)
        actor = auth.authenticate(app, token, csrf)
        assert actor.user_id == before_users[username]
        assert {(g.factory_id, g.role) for g in actor.grants} == {
            (FACTORY_ID, grant) for grant in grants
        }
        for factory in BUSINESS_FACTORIES:
            with pytest.raises(auth.AccessError):
                actor.require(factory, set(grants))
    assert {factory: snapshot(source, factory) for factory in BUSINESS_FACTORIES} == worlds
    with Session(owner) as db:
        assert [
            (row.action_id, row.request, row.result)
            for row in db.scalars(
                select(SourceAction).where(SourceAction.factory_id == world.factory_id)
            )
        ] == actions
        assert [
            row.document
            for row in db.scalars(
                select(SourceChange).where(SourceChange.factory_id == world.factory_id)
            )
        ] == changes


@pytest.mark.parametrize(
    "invalid", ["missing-workshop", "partial-scenario", "wrong-role", "unknown"]
)
def test_setup_rejects_unrecognized_grants_without_partial_repair(team_database, invalid):
    app, _, owner = team_database
    provision(owner)
    token, csrf = auth.select_demo_role(app, "manager")
    with Session(owner) as db, db.begin():
        users = {
            role: db.scalar(select(User).where(User.username == username))
            for role, (username, _) in PERSONAS.items()
        }
        for role in PERSONAS["manager"][1]:
            db.add(
                Membership(
                    user_id=users["manager"].user_id, factory_id=BUSINESS_FACTORIES[0], role=role
                )
            )
        maintainer = users["maintainer"]
        if invalid == "missing-workshop":
            db.execute(
                delete(Membership).where(
                    Membership.user_id == maintainer.user_id,
                    Membership.factory_id == FACTORY_ID,
                    Membership.role == "sim_admin",
                )
            )
        else:
            db.add(
                Membership(
                    user_id=maintainer.user_id,
                    factory_id="unknown" if invalid == "unknown" else BUSINESS_FACTORIES[0],
                    role="manager" if invalid == "wrong-role" else "maintainer",
                )
            )
    with Session(owner) as db:
        before = {(r.user_id, r.factory_id, r.role) for r in db.scalars(select(Membership))}
    with pytest.raises(ValueError, match="grants"):
        provision(owner)
    with Session(owner) as db:
        assert {(r.user_id, r.factory_id, r.role) for r in db.scalars(select(Membership))} == before
        assert db.get(LoginSession, auth.digest(token)) is not None
    assert auth.authenticate(app, token, csrf).username == PERSONAS["manager"][0]


def test_persona_grant_validation_accepts_only_complete_workshop_roles():
    for _, roles in PERSONAS.values():
        expected = {(FACTORY_ID, role) for role in roles}
        assert valid_persona_grants(expected, roles)
        assert not valid_persona_grants(set(), roles)
        assert not valid_persona_grants({next(iter(expected))}, roles)
        assert not valid_persona_grants(
            expected | {(BUSINESS_FACTORIES[0], role) for role in roles}, roles
        )


@pytest.mark.parametrize("factory,role", [("business-urgent", "sim_admin"), ("unknown", "planner")])
def test_persona_login_rejects_wrong_roles_and_unlisted_factories(team_database, factory, role):
    app, _, owner = team_database
    provision(owner)
    with Session(owner) as db, db.begin():
        user = db.scalar(select(User).where(User.username == PERSONAS["manager"][0]))
        db.add(Membership(user_id=user.user_id, factory_id=factory, role=role))
    with pytest.raises(auth.AccessError, match="The demo identity permissions are incorrect"):
        auth.select_demo_role(app, "manager")


def test_extra_grant_cannot_be_selected_as_demo_role(team_database):
    app, _, owner = team_database
    provision(owner)
    with Session(owner) as db, db.begin():
        manager = db.scalar(select(User).where(User.username == PERSONAS["manager"][0]))
        assert manager is not None
        db.add(Membership(user_id=manager.user_id, factory_id="skf-reference", role="admin"))
    with pytest.raises(auth.AccessError) as error:
        auth.select_demo_role(app, "manager")
    assert error.value.code == "DEMO_ROLE_UNAVAILABLE"


def test_setup_retires_old_password_persona_and_existing_session(team_database):
    app, _, owner = team_database
    legacy_id = str(uuid4())
    with Session(owner) as db, db.begin():
        db.add(
            User(
                user_id=legacy_id,
                username="planner",
                password_hash=auth.hasher.hash("obsolete-demo-password"),
                active=True,
            )
        )
        db.flush()
        for factory_id in setup_team.TEAM_FACTORIES:
            db.add(Membership(user_id=legacy_id, factory_id=factory_id, role="planner"))
        db.add(
            LoginSession(
                token_hash=auth.digest("obsolete-session"),
                user_id=legacy_id,
                csrf_hash=auth.digest("obsolete-csrf"),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
    try:
        provision(owner)
        with Session(app) as db:
            assert not db.get(User, legacy_id).active
            assert list(db.scalars(select(Membership).where(Membership.user_id == legacy_id))) == []
            assert (
                list(db.scalars(select(LoginSession).where(LoginSession.user_id == legacy_id)))
                == []
            )
        with pytest.raises(auth.AccessError):
            auth.login(app, "planner", "obsolete-demo-password")
    finally:
        with Session(owner) as db, db.begin():
            db.execute(delete(User).where(User.user_id == legacy_id))


def test_role_endpoint_issues_cookie_and_preserves_scoped_authority(team_database):
    app_engine, _, owner = team_database
    provision(owner)
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=SecretStr(app_engine.url.render_as_string(hide_password=False)),
    )
    with TestClient(create_app(settings)) as client:
        assert (
            client.post(
                "/api/login",
                json={"username": "workshop-manager", "password": "unused"},
                headers={"Origin": settings.public_origin},
            ).status_code
            == 404
        )
        assert client.post("/api/role-session", json={"role": "manager"}).status_code == 403
        assert (
            client.post(
                "/api/role-session",
                json={"role": "admin"},
                headers={"Origin": settings.public_origin},
            ).status_code
            == 422
        )
        selected = client.post(
            "/api/role-session",
            json={"role": "manager"},
            headers={"Origin": settings.public_origin},
        )
        assert selected.status_code == 200
        assert "HttpOnly" in selected.headers["set-cookie"]
        assert selected.json()["csrf_token"]
        identity = client.get("/api/session").json()
        assert identity["username"] == "workshop-manager"
        assert {grant["factory_id"] for grant in identity["grants"]} == {FACTORY_ID}
        assert client.get("/api/factories").json()["factories"][0]["factory_id"] == FACTORY_ID


def test_route_sessions_remain_independent_in_one_browser(team_database):
    app_engine, _, owner = team_database
    provision(owner)
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=SecretStr(app_engine.url.render_as_string(hide_password=False)),
    )
    with TestClient(create_app(settings)) as client:
        identities = {}
        tokens = {}
        for surface, role in (("agent", "manager"), ("simulator", "maintainer")):
            headers = {"Origin": settings.public_origin, "X-BYOF-Surface": surface}
            response = client.post("/api/role-session", json={"role": role}, headers=headers)
            assert response.status_code == 200
            assert f"byof_{surface}_session=" in response.headers["set-cookie"]
            tokens[surface] = response.json()["csrf_token"]
            identities[surface] = client.get("/api/session", headers=headers).json()
        assert identities["agent"]["user_id"] != identities["simulator"]["user_id"]
        for surface in identities:
            headers = {"X-BYOF-Surface": surface}
            assert client.get("/api/session", headers=headers).json() == identities[surface]
            roles = client.get("/api/factories", headers=headers).json()["factories"][0]["roles"]
            assert ("manager" in roles) == (surface == "agent")
        assert client.get("/api/session").status_code == 401
        assert client.get("/api/session", headers={"X-BYOF-Surface": "unknown"}).status_code == 400
        headers = {"Origin": settings.public_origin, "X-BYOF-Surface": "agent"}
        assert (
            client.post(
                "/api/role-session", json={"role": "maintainer"}, headers=headers
            ).status_code
            == 422
        )
        headers["X-CSRF-Token"] = tokens["simulator"]
        assert client.post("/api/logout", headers=headers).status_code == 403
        headers["X-CSRF-Token"] = tokens["agent"]
        assert client.post("/api/logout", headers=headers).status_code == 200
        assert client.get("/api/session", headers={"X-BYOF-Surface": "agent"}).status_code == 401
        assert (
            client.get("/api/session", headers={"X-BYOF-Surface": "simulator"}).status_code == 200
        )
