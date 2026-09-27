"""Explicit PostgreSQL tests; absence is reported as skipped, never replaced by SQLite."""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from packages import auth
from packages.domain.skf import load_skf_snapshot
from packages.persistence import LoginSession, Membership, User, connect
from packages.settings import Settings
from services.api.main import create_app


@pytest.fixture
def engine():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required for actual PostgreSQL integration")
    instance = connect(url)
    yield instance
    instance.dispose()


def test_postgres_session_persistence_expiry_csrf_and_roles(engine):
    user_id = str(uuid.uuid4())
    with Session(engine) as db, db.begin():
        db.add(
            User(
                user_id=user_id,
                username=user_id,
                password_hash=auth.hasher.hash("test-pass"),
                active=True,
            )
        )
        db.flush()
        db.add(Membership(user_id=user_id, factory_id="identity-test", role="planner"))
    try:
        token, csrf = auth.login(engine, user_id, "test-pass")
        engine.dispose()
        principal = auth.authenticate(engine, token, csrf)
        principal.require("identity-test", {"planner"})
        with pytest.raises(auth.AccessError, match="may not"):
            principal.require("another-factory", {"planner"})
        with pytest.raises(auth.AccessError) as invalid:
            auth.authenticate(engine, token, "wrong-token")
        assert invalid.value.code == "INVALID_CSRF"
        with Session(engine) as db, db.begin():
            stored = db.scalar(select(LoginSession).where(LoginSession.user_id == user_id))
            assert stored.token_hash != token
            assert stored.csrf_hash != csrf
            db.execute(
                update(LoginSession)
                .where(LoginSession.user_id == user_id)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        with pytest.raises(auth.AccessError) as expired:
            auth.authenticate(engine, token, csrf)
        assert expired.value.code == "SESSION_EXPIRED"
    finally:
        with engine.begin() as connection:
            connection.execute(delete(LoginSession).where(LoginSession.user_id == user_id))
            connection.execute(delete(Membership).where(Membership.user_id == user_id))
            connection.execute(delete(User).where(User.user_id == user_id))


def test_postgres_application_cannot_access_simulator_schema(engine):
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT current_user")) == "byof_app"
        assert not connection.scalar(
            text("SELECT has_schema_privilege(current_user, 'factory_sim', 'USAGE')")
        )
        assert not connection.scalar(
            text("SELECT has_schema_privilege(current_user, 'byof', 'CREATE')")
        )
        with pytest.raises(ProgrammingError):
            connection.execute(text("CREATE TABLE factory_sim.forbidden_probe(id integer)"))


def test_http_login_cookie_csrf_logout_and_get_safety(engine):
    user_id = str(uuid.uuid4())
    with Session(engine) as db, db.begin():
        db.add(
            User(
                user_id=user_id,
                username=user_id,
                password_hash=auth.hasher.hash("test-pass"),
                active=True,
            )
        )
    config = Settings(
        _env_file=None,
        database_url=os.environ["TEST_DATABASE_URL"],
        legacy_password_login_enabled=True,
    )
    try:
        with TestClient(create_app(config)) as client:
            headers = {"origin": config.public_origin}
            result = client.post(
                "/api/login", json={"username": user_id, "password": "wrong"}, headers=headers
            )
            assert result.status_code == 401
            result = client.post(
                "/api/login", json={"username": user_id, "password": "test-pass"}, headers=headers
            )
            assert result.status_code == 200
            assert "HttpOnly" in result.headers["set-cookie"]
            assert "SameSite=strict" in result.headers["set-cookie"]
            assert client.get("/api/session").json()["user_id"] == user_id
            assert client.get("/api/logout").status_code == 405
            assert client.post("/api/logout", headers=headers).status_code == 403
            assert client.get("/api/session").status_code == 200
            headers["x-csrf-token"] = result.json()["csrf_token"]
            assert client.post("/api/logout", headers=headers).status_code == 200
            assert client.get("/api/session").status_code == 401
            with Session(engine) as db:
                assert (
                    db.scalar(select(LoginSession).where(LoginSession.user_id == user_id)) is None
                )
    finally:
        with engine.begin() as connection:
            connection.execute(delete(LoginSession).where(LoginSession.user_id == user_id))
            connection.execute(delete(User).where(User.user_id == user_id))


def test_postgres_rollback_and_invalid_role_have_no_partial_effect(engine):
    user_id = str(uuid.uuid4())
    with pytest.raises(IntegrityError):
        with Session(engine) as db, db.begin():
            db.add(User(user_id=user_id, username=user_id, password_hash="test-only", active=True))
            db.flush()
            db.add(Membership(user_id=user_id, factory_id="identity-test", role="superuser"))
            db.flush()
    with Session(engine) as db:
        assert db.get(User, user_id) is None
        assert db.scalar(select(Membership).where(Membership.user_id == user_id)) is None


def test_simulator_role_cannot_read_byof_identity():
    url = os.environ.get("TEST_FACTORY_DATABASE_URL")
    if not url:
        pytest.skip("TEST_FACTORY_DATABASE_URL required for reciprocal schema isolation")
    simulator = connect(url)
    try:
        with simulator.connect() as connection:
            assert connection.scalar(text("SELECT current_user")) == "factory_sim_app"
            assert not connection.scalar(
                text("SELECT has_schema_privilege(current_user, 'byof', 'USAGE')")
            )
            with pytest.raises(ProgrammingError):
                connection.execute(text("SELECT user_id FROM byof.users"))
    finally:
        simulator.dispose()


def test_profile_validation_checks_session_csrf_and_factory_role(engine):
    profile = load_skf_snapshot(development=True).profile
    config = Settings(
        _env_file=None,
        database_url=os.environ["TEST_DATABASE_URL"],
        legacy_password_login_enabled=True,
    )
    for factory_id, role, expected in [
        (profile.factory_id, "admin", 200),
        (profile.factory_id, "planner", 403),
        ("other-factory", "admin", 403),
    ]:
        user_id = str(uuid.uuid4())
        with Session(engine) as db, db.begin():
            db.add(
                User(
                    user_id=user_id,
                    username=user_id,
                    password_hash=auth.hasher.hash("test-pass"),
                    active=True,
                )
            )
            db.flush()
            db.add(Membership(user_id=user_id, factory_id=factory_id, role=role))
        try:
            with TestClient(create_app(config)) as client:
                payload = profile.model_dump(mode="json")
                assert (
                    client.post("/api/factory-profiles/validate", json=payload).status_code == 401
                )
                headers = {"origin": config.public_origin}
                login = client.post(
                    "/api/login",
                    json={"username": user_id, "password": "test-pass"},
                    headers=headers,
                )
                assert login.status_code == 200
                assert (
                    client.post(
                        "/api/factory-profiles/validate", json=payload, headers=headers
                    ).status_code
                    == 403
                )
                headers["x-csrf-token"] = login.json()["csrf_token"]
                result = client.post(
                    "/api/factory-profiles/validate", json=payload, headers=headers
                )
                assert result.status_code == expected
                if expected == 200:
                    assert result.json()["status"] == "STRUCTURALLY_VALID"
                    assert result.json()["activation_allowed"] is False
                    assert profile.activation_state == "DRAFT"
                payload["confirmed"] = True
                assert (
                    client.post(
                        "/api/factory-profiles/validate", json=payload, headers=headers
                    ).status_code
                    == 422
                )
        finally:
            with engine.begin() as connection:
                connection.execute(delete(LoginSession).where(LoginSession.user_id == user_id))
                connection.execute(delete(Membership).where(Membership.user_id == user_id))
                connection.execute(delete(User).where(User.user_id == user_id))
