"""Controlled SMTP with real PostgreSQL, source HTTP, login, task reply and Agent resume."""

import os
from email import policy
from email.parser import BytesParser
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from sqlalchemy import delete
from test_case_runtime_postgres import FeedbackModel
from test_case_runtime_postgres import case_context as case_context
from test_cases_api_postgres import case_api as case_api
from test_dynamic_factory_postgres import dynamic_source as dynamic_source
from test_mail import SMTPStub
from test_mail_demo import config as mail_settings
from test_publication_postgres import publishing as publishing

from packages.agent import case_runtime
from packages.agent.cases import get_case
from packages.integrations import mail
from packages.integrations.notification_store import (
    ContactAction,
    Notification,
    NotificationContact,
)
from packages.integrations.notifications import deliver_notification
from packages.persistence import connect


def test_agent_task_smtp_link_login_response_resumes_same_case(case_api, monkeypatch):
    ctx = case_api
    source, reader, _, actor, *_ = ctx.context
    ctx.app.state.settings.public_origin = "http://192.168.1.50:18080"
    model = FeedbackModel(source[2].resources[0].resource_id)
    owner = connect(os.environ["TEST_MIGRATION_DATABASE_URL"])
    assert owner.url.database == "byof_test"
    try:
        admin, admin_headers = ctx.login("admin")
        result = admin.post(
            f"/api/admin/factories/{ctx.factory}/notification-contacts",
            json={
                "request_id": "mail-flow-contact",
                "role": "maintainer",
                "user_id": ctx.accounts["maintainer"],
                "email": "maintenance@example.com",
                "enabled": True,
                "expected_version": 0,
            },
            headers=admin_headers,
        )
        assert result.status_code == 200
        assert case_runtime.process_case(ctx.engine, reader, model)
        stub = SMTPStub()
        stub.rcpt = Mock(return_value=(250, b"OK"))
        stub.data = Mock(return_value=(250, b"OK"))
        monkeypatch.setattr(mail.smtplib, "SMTP", Mock(return_value=stub))
        assert deliver_notification(
            ctx.engine,
            mail_settings(
                real_email_allowlist="maintenance@example.com",
                public_origin=ctx.app.state.settings.public_origin,
            ),
        )
        stub.rcpt.assert_called_once_with("maintenance@example.com")
        received = BytesParser(policy=policy.default).parsebytes(stub.data.call_args.args[0])
        link = next(
            line for line in received.get_content().splitlines() if line.startswith("http://")
        )
        target = urlsplit(link)
        identifiers = parse_qs(target.query)
        assert target.netloc == "192.168.1.50:18080"
        assert set(identifiers) == {"factory_id", "case_id", "task_id"}
        task_id = identifiers["task_id"][0]
        assert identifiers["factory_id"] == [ctx.factory] and identifiers["case_id"] == [
            ctx.case["case_id"]
        ]
        task_path = ctx.base + f"/human-tasks/{task_id}"
        with TestClient(ctx.app) as anonymous:
            assert anonymous.get(task_path).status_code == 401
        for role in ("warehouse", "outsider"):
            other, _ = ctx.login(role)
            assert other.get(task_path).status_code == 403
        maintainer, headers = ctx.login("maintainer")
        task = maintainer.get(task_path).json()
        assert task["state"] == "OPEN" and task["send_state"] == "PROVIDER_ACCEPTED"
        assert "maintenance@example.com" not in str(task)
        response = maintainer.post(
            task_path + "/responses",
            json={
                "request_id": "mail-flow-response",
                "expected_task_version": task["version"],
                "answer": {
                    "repair_eta": "2030-09-16T11:30:00+08:00",
                    "comment": "Checked by maintenance",
                },
            },
            headers=headers,
        )
        assert response.status_code == 200
        calls_before = len(model.contexts)
        assert case_runtime.process_case(ctx.engine, reader, model)
        detail = get_case(ctx.engine, actor, ctx.factory, ctx.case["case_id"])
        assert detail["case_id"] == ctx.case["case_id"] and len(model.contexts) > calls_before
        assert any(
            t["task_id"] == task_id and t["state"] == "RESPONDED"
            for t in model.contexts[-1]["current_human_tasks"]
        )
        assert not deliver_notification(
            ctx.engine, mail_settings(real_email_allowlist="maintenance@example.com")
        )
        assert stub.data.call_count == 1
    finally:
        with owner.begin() as db:
            for record in (Notification, ContactAction, NotificationContact):
                db.execute(delete(record).where(record.factory_id == ctx.factory))
        owner.dispose()
