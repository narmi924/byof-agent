"""Explicit SMTP submission results; provider acceptance is not delivery or human consent."""

from __future__ import annotations

import re
import smtplib
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.policy import SMTP
from ipaddress import IPv4Address, IPv4Network
from typing import Literal
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from packages.settings import Settings

_EMAIL = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}"
)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}")
_MESSAGE_ID = re.compile(r"<[A-Za-z0-9][A-Za-z0-9_.-]{0,159}@[A-Za-z0-9.-]+>")
_FIELDS = {
    "repair_eta": "Expected recovery time",
    "remaining_minutes": "Remaining production time (minutes)",
    "remaining_setup_minutes": "Remaining changeover time (minutes)",
    "receipt_eta": "Expected arrival time",
    "comment": "Handling note",
}
_ROLES = {
    "planner": "Planner",
    "manager": "Manager",
    "maintainer": "Maintenance",
    "warehouse": "Warehouse",
    "team_lead": "Team lead",
}


@dataclass(frozen=True)
class MailMessage:
    message_id: str
    recipient: str
    task_id: str
    task_url: str
    fields: tuple[str, ...]
    factory_label: str = "Production factory"
    subject_label: str = ""
    role: str = "maintainer"
    task_type: str = "INFORMATION"
    due_at: datetime | None = None


@dataclass(frozen=True)
class SmokeMessage:
    message_id: str
    recipient: str
    workbench_url: str


@dataclass(frozen=True)
class SendResult:
    state: Literal["PROVIDER_ACCEPTED", "FAILED", "UNKNOWN", "CANCELLED"]
    code: str | None = None


class MailConfigurationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _address(value: str) -> str:
    if type(value) is not str or len(value) > 254 or _EMAIL.fullmatch(value) is None:
        raise MailConfigurationError("INVALID_EMAIL_ADDRESS")
    local, domain = value.rsplit("@", 1)
    if len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local:
        raise MailConfigurationError("INVALID_EMAIL_ADDRESS")
    return local + "@" + domain.lower()


def authorize_recipient(settings: Settings, recipient: str) -> None:
    if not settings.allow_real_email:
        raise MailConfigurationError("REAL_EMAIL_NOT_AUTHORIZED")
    # An explicit list replaces the legacy one-address gate; it never redirects mail.
    configured = settings.real_email_allowlist.get_secret_value()
    if configured.strip():
        try:
            allowed = {_address(item.strip()) for item in configured.split(",")}
        except MailConfigurationError:
            raise MailConfigurationError("INVALID_REAL_EMAIL_ALLOWLIST") from None
    else:
        try:
            allowed = {_address(settings.test_email_recipient)}
        except MailConfigurationError:
            raise MailConfigurationError("REAL_EMAIL_NOT_AUTHORIZED") from None
    if _address(recipient) not in allowed:
        raise MailConfigurationError("RECIPIENT_NOT_ALLOWED")


def _local_http_host(host: str) -> bool:
    if host in {"127.0.0.1", "localhost", "::1"}:
        return True
    try:
        address = IPv4Address(host)
    except ValueError:
        return False
    return any(
        address in IPv4Network(network)
        for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    )


def _task_link(settings: Settings, message: MailMessage | SmokeMessage) -> str:
    link = message.workbench_url if isinstance(message, SmokeMessage) else message.task_url
    if type(link) is not str or len(link) > 2048:
        raise MailConfigurationError("INVALID_TASK_LINK")
    if any(ord(char) < 33 or char == "\\" for char in link + settings.public_origin):
        raise MailConfigurationError("INVALID_TASK_LINK")
    try:
        origin, target = urlsplit(settings.public_origin), urlsplit(link)
        if (
            origin.scheme not in {"http", "https"}
            or origin.hostname is None
            or origin.username is not None
            or origin.password is not None
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
            or target.username is not None
            or target.password is not None
            or target.fragment
            or (target.scheme, target.hostname, target.port)
            != (origin.scheme, origin.hostname, origin.port)
            or target.path != "/"
        ):
            raise MailConfigurationError("INVALID_TASK_LINK")
        if origin.scheme != "https" and not (
            settings.environment in {"local", "test"} and _local_http_host(origin.hostname)
        ):
            raise MailConfigurationError("INSECURE_TASK_ORIGIN")
        query = parse_qs(
            target.query, keep_blank_values=True, strict_parsing=True, max_num_fields=3
        )
    except ValueError:
        raise MailConfigurationError("INVALID_TASK_LINK") from None
    if isinstance(message, SmokeMessage):
        if query:
            raise MailConfigurationError("INVALID_TASK_LINK")
        return link
    if set(query) != {"task_id", "factory_id", "case_id"} or query.get("task_id") != [
        message.task_id
    ]:
        raise MailConfigurationError("INVALID_TASK_LINK")
    if (
        any(len(values) != 1 for values in query.values())
        or _ID.fullmatch(query["factory_id"][0]) is None
    ):
        raise MailConfigurationError("INVALID_TASK_LINK")
    try:
        for field in ("case_id", "task_id"):
            if str(UUID(query[field][0])) != query[field][0].lower():
                raise ValueError("Noncanonical UUID")
    except ValueError:
        raise MailConfigurationError("INVALID_TASK_LINK") from None
    return message.task_url


def _render(settings: Settings, message: MailMessage | SmokeMessage) -> tuple[str, str, bytes]:
    sender, recipient = _address(settings.smtp_from), _address(message.recipient)
    link = _task_link(settings, message)
    if type(message.message_id) is not str or _MESSAGE_ID.fullmatch(message.message_id) is None:
        raise MailConfigurationError("INVALID_MESSAGE_ID")
    mail = EmailMessage(policy=SMTP)
    mail["From"], mail["To"] = sender, recipient
    mail["Message-ID"] = message.message_id
    mail["Auto-Submitted"] = "auto-generated"
    if isinstance(message, SmokeMessage):
        mail["Subject"] = "[BYOF] Mail channel test"
        mail.set_content(
            f"This is a mail channel test started explicitly by an administrator.\nNo production case or manual task was created, and no business reply is needed.\n\nWorkbench:\n{link}\n\nBYOF production planning workbench\n",
            charset="utf-8",
            cte="quoted-printable",
        )
        return sender, recipient, mail.as_bytes()
    if type(message.task_id) is not str or _ID.fullmatch(message.task_id) is None:
        raise MailConfigurationError("INVALID_TASK_ID")
    if (
        type(message.fields) is not tuple
        or not (1 if message.task_type == "INFORMATION" else 0)
        <= len(message.fields)
        <= len(_FIELDS)
        or any(type(field) is not str or field not in _FIELDS for field in message.fields)
        or len(set(message.fields)) != len(message.fields)
    ):
        raise MailConfigurationError("INVALID_INFORMATION_FIELDS")
    if message.role not in _ROLES or message.task_type not in {
        "INFORMATION",
        "APPROVAL",
        "HANDOFF",
    }:
        raise MailConfigurationError("INVALID_TASK_CONTENT")
    for label in (message.factory_label, message.subject_label):
        if type(label) is not str or len(label) > 180 or any(ord(c) < 32 for c in label):
            raise MailConfigurationError("INVALID_TASK_CONTENT")
    role = _ROLES[message.role]
    mail["Subject"] = f"[BYOF] Open item for {role} — {message.factory_label}"
    fields = ", ".join(_FIELDS[field] for field in message.fields)
    action = {
        "INFORMATION": f"Please provide: {fields}.",
        "APPROVAL": "Check the plan in the workbench and make an explicit approval decision.",
        "HANDOFF": "Check the case in the workbench and take it over.",
    }[message.task_type]
    subject = f"Object: {message.subject_label}\n" if message.subject_label else ""
    deadline = ""
    if message.due_at is not None:
        if message.due_at.tzinfo is None or message.due_at.utcoffset() is None:
            raise MailConfigurationError("INVALID_TASK_CONTENT")
        deadline = f"Reply by (real time): {message.due_at.isoformat(timespec='minutes')}\n"
    mail.set_content(
        f"{message.factory_label}\n\nA production case needs the {role}.\n{subject}{action}\n{deadline}\n"
        f"Sign in to the workbench to handle it:\n{link}\n\nOpening the link does not approve a plan; approval needs an explicit action in the workbench.\n\nBYOF production planning workbench\n",
        charset="utf-8",
        cte="quoted-printable",
    )
    return sender, recipient, mail.as_bytes()


class SMTPTransport:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _configuration(self, message: MailMessage | SmokeMessage) -> tuple[str, str, bytes]:
        settings = self.settings
        if settings.smtp_mode == "disabled":
            raise MailConfigurationError("SMTP_DISABLED")
        if settings.smtp_mode not in {"capture", "starttls", "tls"}:
            raise MailConfigurationError("INVALID_SMTP_MODE")
        if (settings.smtp_mode == "tls" and settings.smtp_port == 587) or (
            settings.smtp_mode == "starttls" and settings.smtp_port == 465
        ):
            raise MailConfigurationError("SMTP_TLS_PORT_MISMATCH")
        if (
            type(settings.smtp_host) is not str
            or len(settings.smtp_host) > 253
            or re.fullmatch(r"[A-Za-z0-9]+(?:[A-Za-z0-9.-]*[A-Za-z0-9])?", settings.smtp_host)
            is None
            or any(
                not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                for label in settings.smtp_host.split(".")
            )
            or type(settings.smtp_port) is not int
            or not 1 <= settings.smtp_port <= 65535
        ):
            raise MailConfigurationError("INVALID_SMTP_ENDPOINT")
        if (
            type(settings.smtp_timeout_seconds) is not int
            or not 1 <= settings.smtp_timeout_seconds <= 30
        ):
            raise MailConfigurationError("INVALID_SMTP_TIMEOUT")
        sender, recipient, payload = _render(settings, message)
        if settings.smtp_mode == "capture":
            if settings.environment not in {"local", "test"} or settings.smtp_host != "127.0.0.1":
                raise MailConfigurationError("CAPTURE_REQUIRES_LOCAL_LOOPBACK")
            if (
                settings.smtp_username.get_secret_value()
                or settings.smtp_password.get_secret_value()
            ):
                raise MailConfigurationError("CAPTURE_CREDENTIALS_FORBIDDEN")
            if not sender.endswith(".invalid") or not recipient.endswith(".invalid"):
                raise MailConfigurationError("CAPTURE_RECIPIENT_FORBIDDEN")
        else:
            authorize_recipient(settings, recipient)
            if (
                not settings.smtp_username.get_secret_value()
                or not settings.smtp_password.get_secret_value()
            ):
                raise MailConfigurationError("SMTP_CREDENTIALS_REQUIRED")
        return sender, recipient, payload

    def validate(self, message: MailMessage | SmokeMessage) -> None:
        self._configuration(message)

    def send(
        self, message: MailMessage | SmokeMessage, before_data: Callable[[], bool]
    ) -> SendResult:
        try:
            sender, recipient, payload = self._configuration(message)
        except MailConfigurationError as exc:
            return SendResult("FAILED", exc.code)
        settings = self.settings
        deadline = time.monotonic() + settings.smtp_timeout_seconds
        client: smtplib.SMTP | None = None
        in_data = False

        def remaining() -> float:
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise TimeoutError("SMTP budget exhausted")
            if client is not None and client.sock is not None:
                client.sock.settimeout(seconds)
            return seconds

        try:
            if settings.smtp_mode == "tls":
                client = smtplib.SMTP_SSL(
                    settings.smtp_host,
                    settings.smtp_port,
                    local_hostname="byof.invalid",
                    timeout=remaining(),
                    context=ssl.create_default_context(),
                )
            else:
                client = smtplib.SMTP(
                    settings.smtp_host,
                    settings.smtp_port,
                    local_hostname="byof.invalid",
                    timeout=remaining(),
                )
            remaining()
            client.ehlo_or_helo_if_needed()
            if settings.smtp_mode == "starttls":
                remaining()
                client.starttls(context=ssl.create_default_context())
                remaining()
                client.ehlo_or_helo_if_needed()
            if settings.smtp_mode in {"starttls", "tls"}:
                remaining()
                client.login(
                    settings.smtp_username.get_secret_value(),
                    settings.smtp_password.get_secret_value(),
                )
            remaining()
            if client.mail(sender)[0] != 250:
                return SendResult("FAILED", "SMTP_SENDER_REJECTED")
            remaining()
            if client.rcpt(recipient)[0] not in {250, 251}:
                return SendResult("FAILED", "SMTP_RECIPIENT_REJECTED")
            try:
                current = before_data()
            except Exception:
                return SendResult("FAILED", "BEFORE_DATA_CHECK_FAILED")
            if current is not True:
                return SendResult("CANCELLED", "NOTIFICATION_NO_LONGER_CURRENT")
            remaining()
            in_data = True
            code, _ = client.data(payload)
            in_data = False
            if 400 <= code <= 599:
                return SendResult("FAILED", "SMTP_DATA_REJECTED")
            if code != 250:
                return SendResult("UNKNOWN", "SMTP_RESULT_UNKNOWN")
            return SendResult("PROVIDER_ACCEPTED")
        except smtplib.SMTPAuthenticationError:
            return SendResult("FAILED", "SMTP_AUTHENTICATION_FAILED")
        except smtplib.SMTPResponseException as exc:
            if in_data and not 400 <= exc.smtp_code <= 599:
                return SendResult("UNKNOWN", "SMTP_RESULT_UNKNOWN")
            return SendResult(
                "FAILED", "SMTP_DATA_REJECTED" if in_data else "SMTP_COMMAND_REJECTED"
            )
        except ssl.SSLError:
            return SendResult("UNKNOWN" if in_data else "FAILED", "SMTP_TLS_FAILED")
        except smtplib.SMTPNotSupportedError:
            return SendResult("FAILED", "SMTP_REQUIRED_FEATURE_UNAVAILABLE")
        except (OSError, smtplib.SMTPException):
            return SendResult(
                "UNKNOWN" if in_data else "FAILED",
                "SMTP_RESULT_UNKNOWN" if in_data else "SMTP_CONNECTION_FAILED",
            )
        finally:
            if client is not None:
                try:
                    remaining()
                    client.quit()
                except (OSError, smtplib.SMTPException):
                    pass
                finally:
                    try:
                        client.close()
                    except OSError:
                        pass
