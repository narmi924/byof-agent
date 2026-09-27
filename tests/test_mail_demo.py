"""Multi-recipient SMTP policy and LAN links; all SMTP connections are controlled."""

import smtplib
from dataclasses import replace
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from unittest.mock import Mock

import pytest
from test_mail import SMTPStub, message, tls_settings

from packages.integrations import mail
from packages.integrations.mail import SMTPTransport


def config(**changes):
    return type(tls_settings())(_env_file=None, **{**tls_settings().model_dump(), **changes})


@pytest.mark.parametrize("recipient", ["maintainer@example.com", "warehouse@example.com"])
def test_list_replaces_legacy_gate_without_redirecting_any_role(monkeypatch, recipient):
    stub = SMTPStub()
    stub.rcpt = Mock(return_value=(250, b"OK"))
    monkeypatch.setattr(mail.smtplib, "SMTP", Mock(return_value=stub))
    settings = config(
        real_email_allowlist=" maintainer@example.com,warehouse@example.com ",
        test_email_recipient="smoke@example.com",
    )
    assert (
        SMTPTransport(settings).send(message(recipient=recipient), lambda: True).state
        == "PROVIDER_ACCEPTED"
    )
    stub.rcpt.assert_called_once_with(recipient)


@pytest.mark.parametrize(
    "changes,recipient,code",
    [
        (
            {"allow_real_email": False, "real_email_allowlist": "owner@example.invalid"},
            "owner@example.invalid",
            "REAL_EMAIL_NOT_AUTHORIZED",
        ),
        (
            {"real_email_allowlist": "warehouse@example.com"},
            "owner@example.invalid",
            "RECIPIENT_NOT_ALLOWED",
        ),
        (
            {"real_email_allowlist": "owner@example.invalid,"},
            "owner@example.invalid",
            "INVALID_REAL_EMAIL_ALLOWLIST",
        ),
        (
            {"real_email_allowlist": "owner@example.invalid\nBcc: bad@example.com"},
            "owner@example.invalid",
            "INVALID_REAL_EMAIL_ALLOWLIST",
        ),
        ({"smtp_mode": "tls", "smtp_port": 587}, "owner@example.invalid", "SMTP_TLS_PORT_MISMATCH"),
        (
            {"smtp_mode": "starttls", "smtp_port": 465},
            "owner@example.invalid",
            "SMTP_TLS_PORT_MISMATCH",
        ),
    ],
)
def test_policy_and_tls_configuration_reject_before_network(monkeypatch, changes, recipient, code):
    network = Mock(side_effect=AssertionError("No unauthorized network"))
    monkeypatch.setattr(mail.smtplib, "SMTP", network)
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", network)
    result = SMTPTransport(config(**changes)).send(message(recipient=recipient), lambda: True)
    assert result.state == "FAILED" and result.code == code
    network.assert_not_called()


@pytest.mark.parametrize(
    "environment,origin,allowed",
    [
        ("local", "http://192.168.1.50:18080", True),
        ("test", "http://10.0.0.50:18080", True),
        ("local", "http://172.16.1.50:18080", True),
        ("local", "http://8.8.8.8:18080", False),
        ("local", "http://api:8000", False),
        ("local", "http://169.254.169.254", False),
        ("production", "http://192.168.1.50:18080", False),
        ("production", "https://workbench.example.com", True),
    ],
)
def test_public_origin_supports_explicit_local_lan_only(monkeypatch, environment, origin, allowed):
    stub = SMTPStub()
    constructor = Mock(return_value=stub)
    monkeypatch.setattr(mail.smtplib, "SMTP", constructor)
    request = message(recipient="owner@example.invalid")
    request = replace(request, task_url=request.task_url.replace("http://127.0.0.1:5173", origin))
    result = SMTPTransport(config(environment=environment, public_origin=origin)).send(
        request, lambda: True
    )
    assert (result.state == "PROVIDER_ACCEPTED") is allowed
    assert constructor.call_count == int(allowed)


def test_business_template_uses_structured_context_and_deadline(monkeypatch):
    stub = SMTPStub()
    stub.data = Mock(return_value=(250, b"OK"))
    monkeypatch.setattr(mail.smtplib, "SMTP", Mock(return_value=stub))
    request = message(
        recipient="owner@example.invalid",
        factory_label="Example factory",
        subject_label="Machine M-03",
        role="maintainer",
        due_at=datetime(2026, 9, 21, 10, tzinfo=UTC),
    )
    assert SMTPTransport(config()).send(request, lambda: True).state == "PROVIDER_ACCEPTED"
    received = BytesParser(policy=policy.default).parsebytes(stub.data.call_args.args[0])
    body = received.get_content()
    assert all(
        value in body
        for value in (
            "Example factory",
            "Machine M-03",
            "Maintenance",
            "Expected recovery time",
            "2026-09-21T10:00+00:00",
            request.task_url,
        )
    )
    assert "Task ID" not in body and "private-test-marker" not in body


@pytest.mark.parametrize("kind", ["APPROVAL", "HANDOFF"])
def test_review_and_handoff_do_not_require_information_fields(monkeypatch, kind):
    stub = SMTPStub()
    monkeypatch.setattr(mail.smtplib, "SMTP", Mock(return_value=stub))
    assert (
        SMTPTransport(config())
        .send(message(recipient="owner@example.invalid", task_type=kind, fields=()), lambda: True)
        .state
        == "PROVIDER_ACCEPTED"
    )


def test_authentication_failure_is_safe_and_never_sends_data(monkeypatch, capsys):
    stub = SMTPStub()
    stub.login = Mock(side_effect=smtplib.SMTPAuthenticationError(535, b"private-test-marker"))
    monkeypatch.setattr(mail.smtplib, "SMTP", Mock(return_value=stub))
    result = SMTPTransport(config()).send(message(recipient="owner@example.invalid"), lambda: True)
    assert result == mail.SendResult("FAILED", "SMTP_AUTHENTICATION_FAILED")
    assert "data" not in stub.calls and "private-test-marker" not in repr(result) + repr(
        capsys.readouterr()
    )
