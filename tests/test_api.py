from fastapi.testclient import TestClient

from packages.auth import AccessError, Grant, Principal
from packages.settings import Settings
from services.api.main import create_app


def offline_client() -> TestClient:
    return TestClient(create_app(Settings(_env_file=None, database_url="")))


def test_health_distinguishes_live_and_ready():
    with offline_client() as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        assert client.get("/health/ready").status_code == 503
        result = client.get("/api/system/status")
        assert result.json()["state"] == "setup_required"
        assert result.json()["version"] == "1.0"
        assert result.headers["cache-control"] == "no-store"
        assert "password" not in result.text


def test_anonymous_session_and_get_links_never_approve():
    with offline_client() as client:
        assert client.get("/api/session").json()["code"] == "UNAUTHENTICATED"
        assert client.get("/api/login").status_code == 405
        assert client.get("/api/logout").status_code == 405
        assert client.get("/api/releases").status_code == 404


def test_invalid_login_payload_does_not_echo_credentials():
    with offline_client() as client:
        response = client.post(
            "/api/login",
            json={"username": "a", "password": "sensitive-test-value", "confirmed": True},
        )
        assert response.status_code == 422
        assert "sensitive-test-value" not in response.text
        assert response.json()["code"] == "INVALID_INPUT"


def test_login_requires_trusted_origin_before_database():
    with offline_client() as client:
        response = client.post("/api/login", json={"username": "a", "password": "b"})
        assert response.status_code == 403
        assert response.json()["code"] == "INVALID_ORIGIN"


def test_role_and_factory_are_independent_permissions():
    principal = Principal(
        user_id="u", username="planner", grants=(Grant(factory_id="a", role="planner"),)
    )
    principal.require("a", {"planner"})
    for factory, roles in [("b", {"planner"}), ("a", {"manager"}), ("a", {"sim_admin"})]:
        try:
            principal.require(factory, roles)
        except AccessError as exc:
            assert exc.code == "FORBIDDEN"
        else:
            raise AssertionError("Role/factory isolation bypass")


def test_business_studies_have_no_source_write_or_conversation_bypass_route():
    with offline_client() as client:
        for method, path in (
            ("get", "/api/admin/factories/example/business-studies"),
            ("post", "/api/admin/factories/example/business-accept"),
            ("post", "/api/factories/example/assistant/business-studies"),
        ):
            assert getattr(client, method)(path).status_code == 404
