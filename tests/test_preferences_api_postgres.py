"""Authenticated HTTP preference changes and read-only links over real PostgreSQL."""

from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session
from test_human_tasks_postgres import human_context as human_context
from test_notifications_api_postgres import api as api
from test_preferences_postgres import context as context
from test_preferences_postgres import definition, grant, make_conflict

from packages.persistence import Membership
from packages.planning.preferences import get_preferences


def proposal(ctx, **changes):
    return {
        "request_id": "propose-http",
        "scope_type": "FACTORY",
        "scope_id": ctx.factory,
        "definition": definition().model_dump(mode="json"),
        "expected_version": 0,
        "reason": "Due date and overtime bounds checked",
        **changes,
    }


def test_preference_http_requires_login_csrf_origin_and_current_scope_role(api):
    ctx, login, app = api
    url = f"/api/factories/{ctx.factory}/preferences"
    with TestClient(app) as anonymous:
        assert anonymous.get(url).status_code == 401
    for role in ("planner", "maintainer", "manager", "outsider"):
        client, headers = login(role)
        assert (
            client.post(url + "/proposals", json=proposal(ctx), headers=headers).status_code == 403
        )
    admin, headers = login("admin")
    assert (
        admin.post(
            url + "/proposals", json=proposal(ctx), headers={"Origin": headers["Origin"]}
        ).status_code
        == 403
    )
    assert (
        admin.post(
            url + "/proposals",
            json=proposal(ctx),
            headers={**headers, "Origin": "https://untrusted.invalid"},
        ).status_code
        == 403
    )
    assert get_preferences(ctx.engine, ctx.actors["admin"], ctx.factory)["proposals"] == []
    assert admin.post(url + "/proposals", json=proposal(ctx), headers=headers).status_code == 200
    with Session(ctx.engine) as db, db.begin():
        db.execute(delete(Membership).where(Membership.user_id == ctx.actors["admin"].user_id))
    assert admin.get(url).status_code == 403


def test_scanned_links_and_model_confirmation_cannot_activate_preference(api):
    ctx, login, _ = api
    admin, headers = login("admin")
    url = f"/api/factories/{ctx.factory}/preferences"
    created = admin.post(url + "/proposals", json=proposal(ctx), headers=headers).json()
    confirm_url = url + f"/proposals/{created['proposal_id']}/confirmations"
    initial = admin.get(url).json()
    assert initial == admin.get(url).json()
    assert initial["effective"]["objective_version"] == "delivery-v1"
    assert initial["state_version"] == 0 and initial["proposals"][0]["state"] == "PENDING"
    assert admin.get(confirm_url).status_code == 405
    body = {"request_id": "confirm-http", "expected_state_version": 0}
    assert (
        admin.post(confirm_url, json={**body, "confirmed": True}, headers=headers).status_code
        == 422
    )
    assert admin.post(confirm_url, content="{bad", headers=headers).status_code == 422
    assert admin.get(url).json() == initial
    confirmed = admin.post(confirm_url, json=body, headers=headers)
    assert confirmed.status_code == 200
    assert admin.post(confirm_url, json=body, headers=headers).json() == confirmed.json()
    effective = admin.get(url).json()["effective"]
    assert effective["sources"][0]["confirmed_by"] == ctx.actors["admin"].user_id
    assert effective["resolution_version"] == 1 and effective["coordination"] is None
    actual = admin.get(f"/api/factories/{ctx.factory}/workspace").json()
    assert actual["snapshot"]["content_hash"] == ctx.snapshot.content_hash
    assert actual["objective_state"] == effective


def test_missing_preference_bounds_and_cross_factory_sources_are_rejected(api):
    ctx, login, _ = api
    planner, headers = login("planner")
    url = f"/api/factories/{ctx.factory}/preferences"
    assert (
        planner.post(
            url + "/proposals",
            headers=headers,
            json=proposal(
                ctx,
                scope_type="CASE",
                scope_id=ctx.case_id,
                definition={"selection": "stability_first"},
            ),
        ).status_code
        == 422
    )
    assert (
        planner.post(
            url + "/proposals",
            headers=headers,
            json=proposal(
                ctx,
                scope_type="CASE",
                scope_id=ctx.case_id,
                source_proposal_id="invented-model-confirmation",
            ),
        ).status_code
        == 409
    )
    outsider, outsider_headers = login("outsider")
    assert outsider.get(url).status_code == 403
    assert (
        outsider.post(
            url + "/proposals",
            headers=outsider_headers,
            json=proposal(ctx, scope_type="CASE", scope_id=ctx.case_id),
        ).status_code
        == 403
    )
    assert planner.get(url).json()["proposals"] == []


def test_http_coordination_identifies_actual_confirmer_and_preserves_versions(api):
    ctx, login, _ = api
    conflict = make_conflict(ctx)
    grant(ctx, "admin", "planner")
    admin, headers = login("admin")
    url = f"/api/factories/{ctx.factory}/preferences"
    body = {
        "request_id": "coordinate-http",
        "expected_state_version": 2,
        "context_hash": conflict["context_hash"],
        "definition": definition().model_dump(mode="json"),
        "reason": "Shared resources coordinated by delivery first",
    }
    result = admin.post(url + "/coordination", headers=headers, json=body)
    assert result.status_code == 200
    final = result.json()
    assert final["coordination"]["confirmed_by"] == ctx.actors["admin"].user_id
    assert final["coordination"]["context_hash"] == conflict["context_hash"]
    assert {s["confirmed_by"] for s in final["sources"]} == {ctx.actors["planner"].user_id}
    assert final["definition"]["selection"] == "delivery_first" and final["resolution_version"] == 3
    assert admin.get(url).json()["effective"] == final
    assert admin.post(url + "/coordination", headers=headers, json=body).json() == final
