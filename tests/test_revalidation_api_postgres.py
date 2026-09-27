"""Login, CSRF, scan-safe certificate requests and conditional publication over real services."""

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from test_dynamic_factory_postgres import dynamic_source as dynamic_source
from test_publication_postgres import publishing as publishing
from test_revalidation_postgres import progress as progress

from packages import auth
from packages.persistence import LoginSession, User
from packages.planning.publication import deliver_one
from packages.planning.revalidation_store import ValidationRecord
from packages.settings import Settings
from services.api.main import create_app

pytestmark = pytest.mark.parametrize(
    "dynamic_source", [{"progress_revalidation": True}], indirect=True
)


@pytest.fixture
def api(progress):
    ctx = progress
    password = "local-revalidation-test-password"
    with Session(ctx.engine) as db, db.begin():
        db.get(User, ctx.actor.user_id).password_hash = auth.hasher.hash(password)
    app = create_app(
        Settings(
            _env_file=None,
            environment="test",
            legacy_password_login_enabled=True,
            database_url=SecretStr(ctx.engine.url.render_as_string(hide_password=False)),
            factory_api_url=str(ctx.source[0].base_url),
            factory_api_token=SecretStr(ctx.source[1]["reader"]),
            llm_gateway_api_key=SecretStr(""),
            smtp_mode="disabled",
        )
    )
    with TestClient(app) as client:
        headers = {"Origin": "http://127.0.0.1:5173"}
        login = client.post(
            "/api/login",
            headers=headers,
            json={"username": ctx.actor.username, "password": password},
        )
        assert login.status_code == 200
        headers["X-CSRF-Token"] = login.json()["csrf_token"]
        try:
            yield ctx, client, headers, app
        finally:
            with ctx.engine.begin() as db:
                db.execute(delete(LoginSession).where(LoginSession.user_id == ctx.actor.user_id))


def test_scanning_cannot_create_certificate_and_post_requires_login_csrf_and_exact_input(api):
    ctx, client, headers, app = api
    url = f"/api/factories/{ctx.factory}/candidates/{ctx.candidate.candidate_id}/validations"
    body = {"request_id": "http-validation", "candidate_hash": ctx.candidate.content_hash}
    with TestClient(app) as anonymous:
        assert anonymous.post(url, json=body, headers=headers).status_code == 401
    assert client.get(url).status_code == 405
    assert client.post(url, json=body, headers={"Origin": headers["Origin"]}).status_code == 403
    assert client.post(url, json={**body, "confirmed": True}, headers=headers).status_code == 422
    assert (
        client.post(
            url, json=body, headers={**headers, "Origin": "https://untrusted.invalid"}
        ).status_code
        == 403
    )
    with Session(ctx.engine) as db:
        assert (
            db.scalar(select(ValidationRecord).where(ValidationRecord.factory_id == ctx.factory))
            is None
        )
    workspace_url = f"/api/factories/{ctx.factory}/workspace"
    first = client.get(workspace_url)
    assert first.status_code == 200 and first.json()["validation_certificates"] == []
    second = client.get(workspace_url).json()
    assert {key: value for key, value in second.items() if key != "server_time"} == {
        key: value for key, value in first.json().items() if key != "server_time"
    }


def test_http_certificate_and_publication_bind_original_approval_and_current_snapshot(api):
    ctx, client, headers, _ = api
    prefix = f"/api/factories/{ctx.factory}/candidates/{ctx.candidate.candidate_id}"
    body = {"request_id": "http-validation", "candidate_hash": ctx.candidate.content_hash}
    response = client.post(prefix + "/validations", json=body, headers=headers)
    assert response.status_code == 200
    cert = response.json()
    assert client.post(prefix + "/validations", json=body, headers=headers).json() == cert
    assert cert["approval_ids"] == [ctx.approval.approval_id]
    assert cert["old_snapshot_hash"] == ctx.original.content_hash
    assert cert["new_snapshot_hash"] == ctx.current.content_hash
    view = client.get(f"/api/factories/{ctx.factory}/workspace").json()
    assert view["validation_certificates"] == [cert]
    assert (
        next(
            row
            for row in view["candidates"]
            if row["candidate"]["candidate_id"] == ctx.candidate.candidate_id
        )["run_id"]
        == ctx.current.run_id
    )
    pub_body = {
        "request_id": "http-publish",
        "candidate_hash": ctx.candidate.content_hash,
        "certificate_id": cert["certificate_id"],
    }
    result = client.post(prefix + "/publications", json=pub_body, headers=headers)
    assert result.status_code == 200 and result.json()["source_state"] == "PENDING_SOURCE"
    assert deliver_one(ctx.engine, ctx.reader, ctx.writer)
    release = client.get(f"/api/factories/{ctx.factory}/workspace").json()["publications"][0][
        "release"
    ]
    assert (
        release["source_state"] == "ACTIVE"
        and release["candidate_hash"] == ctx.candidate.content_hash
    )
    assert (
        client.post(
            prefix + "/publications",
            json={**pub_body, "certificate_id": "different-certificate"},
            headers=headers,
        ).status_code
        == 409
    )
