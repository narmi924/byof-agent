"""Real login/CSRF/role authorization for contact configuration and read-only notification links."""

from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete
from sqlalchemy.orm import Session
from test_human_tasks_postgres import human_context as human_context
from test_human_tasks_postgres import task
from test_mail import settings
from test_notifications_postgres import Sender, configure, rows
from test_notifications_postgres import context as context

from packages import auth
from packages.agent.human_tasks import get_task
from packages.integrations.contacts import list_contacts
from packages.integrations.notifications import deliver_notification
from packages.persistence import LoginSession, User
from services.api.main import create_app


@pytest.fixture
def api(context):
    ctx = context
    password = "test-only-mail-login"
    password_hash = auth.hasher.hash(password)
    with Session(ctx.engine) as db, db.begin():
        for actor in ctx.actors.values():
            db.get(User, actor.user_id).password_hash = password_hash
    app = create_app(
        settings(
            database_url=SecretStr(ctx.engine.url.render_as_string(hide_password=False)),
            legacy_password_login_enabled=True,
        )
    )
    with ExitStack() as stack:
        clients = {}

        def login(role):
            if role in clients:
                return clients[role]
            client = stack.enter_context(TestClient(app))
            headers = {"Origin": "http://127.0.0.1:5173"}
            result = client.post(
                "/api/login",
                json={"username": ctx.actors[role].username, "password": password},
                headers=headers,
            )
            assert result.status_code == 200
            headers["X-CSRF-Token"] = result.json()["csrf_token"]
            clients[role] = client, headers
            return client, headers

        try:
            yield ctx, login, app
        finally:
            with ctx.engine.begin() as db:
                db.execute(
                    delete(LoginSession).where(
                        LoginSession.user_id.in_([a.user_id for a in ctx.actors.values()])
                    )
                )


def payload(ctx, **changes):
    return {
        "request_id": "configure-http",
        "role": "maintainer",
        "user_id": ctx.actors["maintainer"].user_id,
        "email": "maintainer@test.invalid",
        "enabled": True,
        "expected_version": 0,
        **changes,
    }


def test_contact_post_requires_login_origin_csrf_and_admin(api):
    ctx, login, app = api
    url = f"/api/admin/factories/{ctx.factory}/notification-contacts"
    with TestClient(app) as anonymous:
        assert anonymous.get(url).status_code == 401
    for role in ("planner", "maintainer", "manager", "outsider"):
        client, headers = login(role)
        assert client.get(url).status_code == 403
        assert client.post(url, json=payload(ctx), headers=headers).status_code == 403
    admin, headers = login("admin")
    assert (
        admin.post(url, json=payload(ctx), headers={"Origin": headers["Origin"]}).status_code == 403
    )
    assert (
        admin.post(
            url, json=payload(ctx), headers={**headers, "Origin": "https://untrusted.invalid"}
        ).status_code
        == 403
    )
    assert list_contacts(ctx.engine, ctx.actors["admin"], ctx.factory, settings())["contacts"] == []
    response = admin.post(url, json=payload(ctx), headers=headers)
    assert response.status_code == 200 and response.json()["version"] == 1
    assert admin.post(url, json=payload(ctx), headers=headers).json() == response.json()
    conflict = admin.post(url, json=payload(ctx, request_id="second"), headers=headers)
    assert conflict.status_code == 409 and conflict.json()["code"] == "CONTACT_VERSION_CHANGED"


def test_contact_validation_and_factory_scope_are_enforced(api):
    ctx, login, _ = api
    admin, headers = login("admin")
    url = f"/api/admin/factories/{ctx.factory}/notification-contacts"
    for changes in (
        {"confirmed": True},
        {"email": "a@test.invalid\nBcc:evil@test.invalid"},
        {"expected_version": True},
        {"role": "sim_admin"},
    ):
        result = admin.post(url, json=payload(ctx, **changes), headers=headers)
        assert result.status_code == 422 and "evil" not in result.text
    assert (
        admin.post(
            url.replace(ctx.factory, ctx.factory + "-other"), json=payload(ctx), headers=headers
        ).status_code
        == 403
    )
    result = admin.post(
        url, json=payload(ctx, user_id=ctx.actors["outsider"].user_id), headers=headers
    )
    assert result.status_code == 409 and result.json()["code"] == "INVALID_CONTACT_USER"


def test_csrf_initialization_in_other_tabs_preserves_existing_mutations(api):
    ctx, login, app = api
    first, headers = login("admin")
    with TestClient(app) as second:
        second.cookies.update(first.cookies)
        for _ in range(3):
            renewed = second.post("/api/csrf", json={}, headers={"Origin": headers["Origin"]})
            assert renewed.status_code == 200
            assert renewed.json()["csrf_token"] == headers["X-CSRF-Token"]
        url = f"/api/admin/factories/{ctx.factory}/notification-contacts"
        assert first.post(url, json=payload(ctx), headers=headers).status_code == 200
        assert second.post(url, json=payload(ctx), headers=headers).status_code == 200
        assert (
            first.post(
                url, json=payload(ctx), headers={**headers, "X-CSRF-Token": "forged"}
            ).status_code
            == 403
        )
        assert (
            second.post(
                "/api/csrf", json={}, headers={"Origin": "https://attacker.invalid"}
            ).status_code
            == 403
        )
        assert first.post("/api/logout", json={}, headers=headers).status_code == 200
        assert (
            second.post("/api/csrf", json={}, headers={"Origin": headers["Origin"]}).status_code
            == 401
        )


def test_notification_get_scans_are_read_only_and_never_expose_recipient(api):
    ctx, login, app = api
    record = task(ctx)
    configure(ctx)
    sender = Sender()
    assert deliver_notification(ctx.engine, settings(), sender)
    base = f"/api/factories/{ctx.factory}"
    url = f"{base}/notifications?task_id={record['task_id']}"
    with TestClient(app) as anonymous:
        assert anonymous.get(url).status_code == 401
    for role in ("warehouse", "admin", "outsider"):
        client, _ = login(role)
        assert client.get(url).status_code == 403
    maintainer, _ = login("maintainer")
    before = get_task(ctx.engine, ctx.actors["maintainer"], ctx.factory, record["task_id"])
    for _ in range(3):
        response = maintainer.get(url)
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        note = response.json()["notifications"][0]
        assert note["send_state"] == "PROVIDER_ACCEPTED" and note["delivery_state"] == "UNAVAILABLE"
        assert "@test.invalid" not in response.text and "recipient_id" not in response.text
        assert maintainer.get(f"{base}/human-tasks/{record['task_id']}").json() == before
    assert before == get_task(ctx.engine, ctx.actors["maintainer"], ctx.factory, record["task_id"])
    assert len(rows(ctx)) == len(sender.messages) == 1 and before["state"] == "OPEN"
    assert maintainer.get(url.replace(ctx.factory, ctx.factory + "-other")).status_code == 403
