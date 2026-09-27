"""The checked human-review POST requires actual login, scope and explicit intent."""

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from test_approval_review_postgres import rows, tick
from test_approval_review_postgres import unreviewed as unreviewed
from test_dynamic_factory_postgres import dynamic_source as dynamic_source
from test_publication_postgres import publishing as publishing

from packages import auth
from packages.persistence import LoginSession, User
from packages.planning.store import ApprovalRecord
from packages.settings import Settings
from services.api.main import create_app

pytestmark = pytest.mark.parametrize(
    "dynamic_source", [{"progress_revalidation": True}], indirect=True
)


@pytest.fixture
def review_api(unreviewed):
    ctx = unreviewed
    tick(ctx)
    password = "isolated-human-review-password"
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
        response = client.post(
            "/api/login",
            headers=headers,
            json={"username": ctx.actor.username, "password": password},
        )
        assert response.status_code == 200
        headers["X-CSRF-Token"] = response.json()["csrf_token"]
        try:
            yield ctx, client, headers, app
        finally:
            with ctx.engine.begin() as db:
                db.execute(delete(LoginSession).where(LoginSession.user_id == ctx.actor.user_id))


def test_review_scan_bad_json_or_other_role_never_grants_approval(review_api):
    ctx, client, headers, app = review_api
    url = f"/api/factories/{ctx.factory}/candidates/{ctx.candidate.candidate_id}/progress-approvals"
    body = {
        "request_id": "api-checked-review",
        "candidate_hash": ctx.candidate.content_hash,
        "action_scope": "publish_plan",
        "decision": "APPROVED",
    }
    with TestClient(app) as anonymous:
        assert anonymous.post(url, json=body, headers=headers).status_code == 401
    assert client.get(url).status_code == 405
    assert client.post(url, json=body, headers={"Origin": headers["Origin"]}).status_code == 403
    assert (
        client.post(
            url, json=body, headers={**headers, "Origin": "https://untrusted.invalid"}
        ).status_code
        == 403
    )
    for extra in ({"confirmed": True}, {"decision": "REJECTED"}, {"action_scope": "all"}):
        assert client.post(url, json={**body, **extra}, headers=headers).status_code == 422
    assert (
        client.post(
            url, json={**body, "action_scope": "allow_overtime"}, headers=headers
        ).status_code
        == 403
    )
    assert (
        client.post(
            url.replace(ctx.factory, "other-factory"), json=body, headers=headers
        ).status_code
        == 403
    )
    assert rows(ctx) == []
    with Session(ctx.engine) as db:
        assert (
            db.scalar(
                select(ApprovalRecord).where(
                    ApprovalRecord.candidate_id == ctx.candidate.candidate_id
                )
            )
            is None
        )


def test_explicit_review_records_new_approval_and_current_evidence_once(review_api):
    ctx, client, headers, _ = review_api
    url = f"/api/factories/{ctx.factory}/candidates/{ctx.candidate.candidate_id}/progress-approvals"
    body = {
        "request_id": "api-checked-review",
        "candidate_hash": ctx.candidate.content_hash,
        "action_scope": "publish_plan",
        "decision": "APPROVED",
    }
    first = client.post(url, json=body, headers=headers)
    assert first.status_code == 200
    assert client.post(url, json=body, headers=headers).json() == first.json()
    view = client.get(f"/api/factories/{ctx.factory}/workspace").json()
    evidence = view["approval_reviews"]
    assert len(evidence) == 1
    assert evidence[0]["approval_id"] == first.json()["approval_id"]
    assert evidence[0]["new_snapshot_hash"] == view["snapshot"]["content_hash"]
    assert evidence[0]["original_binding"] == first.json()["binding"]
    assert evidence[0]["approver_id"] == ctx.actor.user_id
    assert view["validation_certificates"] == []
    assert view["publications"][0]["release"]["candidate_hash"] != ctx.candidate.content_hash
    strict = url.replace("progress-approvals", "approvals")
    assert client.post(strict, json=body, headers=headers).status_code == 409
    assert len(rows(ctx)) == 1
