"""Real loopback SMTP and controlled TLS failures; never send external email."""

import smtplib
import ssl
import threading
from contextlib import contextmanager
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from packages.integrations import mail
from packages.integrations.mail import MailMessage, SendResult, SMTPTransport
from packages.settings import Settings
from scripts.capture_mail import MAILBOX_ROOT, CaptureHandler, CaptureServer


def settings(**overrides):
    return Settings(
        _env_file=None,
        **{
            "environment": "test",
            "smtp_mode": "capture",
            "smtp_host": "127.0.0.1",
            "smtp_port": 1025,
            "smtp_from": "byof@capture.invalid",
            "smtp_username": "",
            "smtp_password": "",
            "allow_real_email": False,
            "test_email_recipient": "",
            "public_origin": "http://127.0.0.1:5173",
            "smtp_timeout_seconds": 2,
            **overrides,
        },
    )


def message(**overrides):
    task_id, case_id = str(uuid4()), str(uuid4())
    return MailMessage(
        **{
            "message_id": f"<byof-{uuid4().hex}@notifications.invalid>",
            "recipient": "maintainer@test.invalid",
            "task_id": task_id,
            "task_url": f"http://127.0.0.1:5173/?factory_id=assembly-one&case_id={case_id}&task_id={task_id}",
            "fields": ("repair_eta", "remaining_minutes"),
            **overrides,
        }
    )


@contextmanager
def capture(handler=CaptureHandler, **limits):
    MAILBOX_ROOT.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="test-", dir=MAILBOX_ROOT) as folder:
        with CaptureServer(
            ("127.0.0.1", 0), mailbox=Path(folder), handler=handler, **limits
        ) as server:
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
            )
            thread.start()
            try:
                yield server, settings(smtp_port=server.server_address[1])
            finally:
                server.shutdown()
                thread.join(timeout=2)
                assert not thread.is_alive()


def test_socket_capture_contains_only_fixed_template_and_real_task_fields(capsys):
    with capture() as (server, configuration):
        request = message()
        checked = []
        result = SMTPTransport(configuration).send(request, lambda: checked.append(True) or True)
        assert result == SendResult("PROVIDER_ACCEPTED") and checked == [True]
        paths = list(server.mailbox.glob("*.eml"))
        assert len(paths) == 1
        received = BytesParser(policy=policy.default).parsebytes(paths[0].read_bytes())
        assert received["To"] == request.recipient and received["Message-ID"] == request.message_id
        assert received["Subject"] == "[BYOF] Open item for Maintenance — Production factory"
        assert received.get_content_type() == "text/plain"
        body = received.get_content()
        assert request.task_url in body and "Task ID:" not in body
        assert "Expected recovery time" in body and "Remaining production time (minutes)" in body
        assert "confirmed" not in body and "approved successfully" not in body
        assert "Bcc" not in received and "Reply-To" not in received
    assert capsys.readouterr() == ("", "")


def test_duplicate_message_id_is_not_claimed_as_smtp_idempotency():
    with capture() as (server, configuration):
        request = message()
        transport = SMTPTransport(configuration)
        assert transport.send(request, lambda: True).state == "PROVIDER_ACCEPTED"
        assert transport.send(request, lambda: True).state == "PROVIDER_ACCEPTED"
        assert len(list(server.mailbox.glob("*.eml"))) == 2


class LostDataReply(CaptureHandler):
    def _reply(self, response):
        if response == b"250 Captured":
            raise ConnectionAbortedError("Controlled loss after data was stored")
        super()._reply(response)


class LostQuitReply(CaptureHandler):
    def _reply(self, response):
        if response == b"221 Closing":
            raise ConnectionAbortedError("Controlled loss after provider acceptance")
        super()._reply(response)


class RejectedData(CaptureHandler):
    def _reply(self, response):
        if response.startswith(b"354"):
            super()._reply(b"554 Data refused")
            raise ConnectionAbortedError("Controlled explicit data refusal")
        super()._reply(response)


@pytest.mark.parametrize(
    "handler,expected,count",
    [
        (LostDataReply, "UNKNOWN", 1),
        (LostQuitReply, "PROVIDER_ACCEPTED", 1),
        (RejectedData, "FAILED", 0),
    ],
)
def test_real_socket_reply_loss_preserves_known_and_unknown_outcomes(handler, expected, count):
    with capture(handler) as (server, configuration):
        checked = []
        result = SMTPTransport(configuration).send(message(), lambda: checked.append(True) or True)
        assert result.state == expected and checked == [True]
        assert len(list(server.mailbox.glob("*.eml"))) == count


@pytest.mark.parametrize("answer", [False, None, 1])
def test_before_data_recheck_cancels_before_body_submission(answer):
    with capture() as (server, configuration):
        result = SMTPTransport(configuration).send(message(), lambda: answer)
        assert result == SendResult("CANCELLED", "NOTIFICATION_NO_LONGER_CURRENT")
        assert list(server.mailbox.glob("*.eml")) == []


def test_before_data_exception_has_no_data_effect_or_secret_output(capsys):
    def unavailable():
        raise RuntimeError("private-test-marker")

    with capture() as (server, configuration):
        result = SMTPTransport(configuration).send(message(), unavailable)
        assert result == SendResult("FAILED", "BEFORE_DATA_CHECK_FAILED")
        assert list(server.mailbox.glob("*.eml")) == []
    assert "private-test-marker" not in repr(result) + repr(capsys.readouterr())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: replace(m, recipient="user@test.invalid\r\nBcc: leak@example.com"),
        lambda m: replace(m, recipient="First <user@test.invalid>"),
        lambda m: replace(m, recipient="first@test.invalid,second@test.invalid"),
        lambda m: replace(m, task_id="task\r\nInjected: yes"),
        lambda m: replace(m, message_id="<valid@local.invalid>\r\nBcc: private@example.com"),
        lambda m: replace(m, fields=("repair_eta", "arbitrary_field")),
        lambda m: replace(m, fields=("comment", "comment")),
        lambda m: replace(m, task_url=m.task_url.replace("127.0.0.1", "attacker.invalid")),
        lambda m: replace(m, task_url=m.task_url + "&redirect=https://attacker.invalid"),
        lambda m: replace(m, task_url=m.task_url + "&task_id=" + m.task_id),
        lambda m: replace(m, task_url=m.task_url + "#approve"),
        lambda m: replace(m, task_url=m.task_url.replace("/?", "/approval?")),
        lambda m: replace(m, task_url=m.task_url.replace(m.task_id, str(uuid4()))),
        lambda m: replace(m, task_url=m.task_url.replace("http://", "http://user:password@")),
    ],
)
def test_untrusted_headers_fields_and_links_are_rejected_before_network(monkeypatch, mutate):
    network = Mock(side_effect=AssertionError("No network for rejected input"))
    monkeypatch.setattr(mail.smtplib, "SMTP", network)
    callback = Mock(return_value=True)
    result = SMTPTransport(settings()).send(mutate(message()), callback)
    assert result.state == "FAILED"
    network.assert_not_called()
    callback.assert_not_called()


@pytest.mark.parametrize(
    "configuration,recipient",
    [
        ({"smtp_mode": "disabled"}, "owner@test.invalid"),
        ({"environment": "production"}, "owner@test.invalid"),
        ({"smtp_host": "localhost"}, "owner@test.invalid"),
        ({"smtp_host": "127.0.0.2"}, "owner@test.invalid"),
        ({"smtp_username": "should-not-leave-capture"}, "owner@test.invalid"),
        ({"smtp_password": "private-test-marker"}, "owner@test.invalid"),
        ({"smtp_from": "byof@example.com"}, "owner@test.invalid"),
        ({}, "owner@example.com"),
        ({"smtp_from": "sender@capture.invalid\nBcc: leak@example.com"}, "owner@test.invalid"),
        ({"smtp_mode": "starttls", "allow_real_email": False}, "owner@example.com"),
        (
            {
                "smtp_mode": "starttls",
                "allow_real_email": True,
                "test_email_recipient": "other@example.com",
            },
            "owner@example.com",
        ),
        (
            {
                "smtp_mode": "tls",
                "allow_real_email": True,
                "test_email_recipient": "owner@example.com",
            },
            "owner@example.com",
        ),
    ],
)
def test_capture_and_real_modes_enforce_configuration_and_exact_recipient(
    monkeypatch, configuration, recipient
):
    smtp = Mock(side_effect=AssertionError("Unauthorized network"))
    monkeypatch.setattr(mail.smtplib, "SMTP", smtp)
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", smtp)
    result = SMTPTransport(settings(**configuration)).send(
        message(recipient=recipient), lambda: True
    )
    assert result.state == "FAILED"
    smtp.assert_not_called()
    assert "private-test-marker" not in repr(result)


class SMTPStub:
    def __init__(self, *, tls_error=None, data_code=250):
        self.calls = []
        self.sock = Mock()
        self.tls_error, self.data_code = tls_error, data_code

    def ehlo_or_helo_if_needed(self):
        self.calls.append("ehlo")

    def starttls(self, *, context):
        self.calls.append("starttls")
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        if self.tls_error:
            raise self.tls_error

    def login(self, username, password):
        self.calls.append("auth")

    def mail(self, sender):
        self.calls.append("mail")
        return 250, b"OK"

    def rcpt(self, recipient):
        self.calls.append("rcpt")
        return 250, b"OK"

    def data(self, payload):
        self.calls.append("data")
        return self.data_code, b"Controlled response"

    def quit(self):
        self.calls.append("quit")

    def close(self):
        self.calls.append("close")


def tls_settings(mode="starttls"):
    return settings(
        smtp_mode=mode,
        smtp_port=465 if mode == "tls" else 587,
        smtp_host="smtp.example.invalid",
        smtp_username="test-user",
        smtp_password="private-test-marker",
        smtp_from="byof@example.invalid",
        allow_real_email=True,
        test_email_recipient="owner@example.invalid",
    )


@pytest.mark.parametrize(
    "error",
    [
        ssl.SSLCertVerificationError("Controlled certificate refusal"),
        smtplib.SMTPNotSupportedError("No STARTTLS"),
    ],
)
def test_starttls_failure_never_falls_back_or_sends_credentials(monkeypatch, error):
    stub = SMTPStub(tls_error=error)
    constructor = Mock(return_value=stub)
    monkeypatch.setattr(mail.smtplib, "SMTP", constructor)
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", Mock(side_effect=AssertionError("No fallback")))
    result = SMTPTransport(tls_settings()).send(
        message(recipient="owner@example.invalid"), lambda: True
    )
    assert result.state == "FAILED" and constructor.call_count == 1
    assert "auth" not in stub.calls and "mail" not in stub.calls and "data" not in stub.calls


def test_starttls_success_rechecks_after_auth_and_rcpt_before_data(monkeypatch):
    stub = SMTPStub()
    monkeypatch.setattr(mail.smtplib, "SMTP", Mock(return_value=stub))

    def recheck():
        assert stub.calls == ["ehlo", "starttls", "ehlo", "auth", "mail", "rcpt"]
        stub.calls.append("checked")
        return True

    result = SMTPTransport(tls_settings()).send(message(recipient="owner@example.invalid"), recheck)
    assert result.state == "PROVIDER_ACCEPTED"
    assert stub.calls[-4:] == ["checked", "data", "quit", "close"]


def test_implicit_tls_always_uses_verified_context_and_never_plain_smtp(monkeypatch):
    stub = SMTPStub()
    ssl_constructor = Mock(return_value=stub)
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", ssl_constructor)
    monkeypatch.setattr(mail.smtplib, "SMTP", Mock(side_effect=AssertionError("No plain SMTP")))
    result = SMTPTransport(tls_settings("tls")).send(
        message(recipient="owner@example.invalid"), lambda: True
    )
    assert result.state == "PROVIDER_ACCEPTED"
    context = ssl_constructor.call_args.kwargs["context"]
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert "starttls" not in stub.calls and "auth" in stub.calls


@pytest.mark.parametrize(
    "code,expected", [(451, "FAILED"), (550, "FAILED"), (-1, "UNKNOWN"), (354, "UNKNOWN")]
)
def test_data_explicit_rejection_and_malformed_ack_are_distinct(monkeypatch, code, expected):
    stub = SMTPStub(data_code=code)
    monkeypatch.setattr(mail.smtplib, "SMTP", Mock(return_value=stub))
    result = SMTPTransport(settings()).send(message(), lambda: True)
    assert result.state == expected and stub.calls.count("data") == 1


def test_capture_enforces_capacity_size_and_refuses_real_recipient():
    with capture(max_messages=1) as (server, configuration):
        transport = SMTPTransport(configuration)
        assert transport.send(message(), lambda: True).state == "PROVIDER_ACCEPTED"
        assert transport.send(message(), lambda: True).state == "FAILED"
        assert len(list(server.mailbox.glob("*.eml"))) == 1
        with smtplib.SMTP("127.0.0.1", configuration.smtp_port, timeout=2) as client:
            client.ehlo()
            assert client.mail("sender@local.invalid")[0] == 250
            assert client.rcpt("person@example.com")[0] == 550
            assert client.docmd("AUTH", "PLAIN")[0] == 502
    with capture(max_bytes=1024) as (server, configuration):
        result = SMTPTransport(configuration).send(
            message(subject_label="Machine M-03 " * 40), lambda: True
        )
        assert result.state == "FAILED" and len(list(server.mailbox.glob("*.eml"))) == 0


def test_capture_rejects_nonloopback_or_outside_mailbox(tmp_path):
    with pytest.raises(ValueError):
        CaptureServer(("0.0.0.0", 0))
    with pytest.raises(ValueError):
        CaptureServer(("127.0.0.1", 0), mailbox=tmp_path)


@pytest.mark.parametrize("seconds", [0, 31, True, 1.5, float("inf")])
def test_settings_reject_unbounded_smtp_timeout(seconds):
    with pytest.raises(ValidationError):
        settings(smtp_timeout_seconds=seconds)


def test_expired_overall_budget_stops_before_data(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(mail.time, "monotonic", lambda: now[0])
    stub = SMTPStub()
    monkeypatch.setattr(mail.smtplib, "SMTP", Mock(return_value=stub))

    def check():
        now[0] = 3.0
        return True

    result = SMTPTransport(settings(smtp_timeout_seconds=2)).send(message(), check)
    assert result.state == "FAILED" and "data" not in stub.calls
    assert stub.calls[-1] == "close"


@pytest.mark.parametrize(
    "host", ["127.0.0.1\x00.other.invalid", "smtp..invalid", "smtp.invalid\r\nAUTH"]
)
def test_invalid_smtp_host_never_reaches_socket(monkeypatch, host):
    network = Mock(side_effect=AssertionError("Malformed endpoint cannot open a socket"))
    monkeypatch.setattr(mail.smtplib, "SMTP", network)
    result = SMTPTransport(settings(smtp_host=host)).send(message(), lambda: True)
    assert result == SendResult("FAILED", "INVALID_SMTP_ENDPOINT")
    network.assert_not_called()
