"""Durable notification intents with a commit before SMTP DATA and no unknown-result retry."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import uuid4

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from packages.agent import human_tasks
from packages.agent.cases_store import CaseRecord
from packages.agent.human_tasks import HumanTaskRecord, TaskReminder
from packages.auth import AccessError, Principal, lock_membership, lock_user
from packages.integrations.contacts import channel_state
from packages.integrations.mail import (
    MailConfigurationError,
    MailMessage,
    SendResult,
    SMTPTransport,
    authorize_recipient,
)
from packages.integrations.notification_store import Notification, NotificationContact
from packages.planning.store import FactoryState, SnapshotRecord
from packages.settings import Settings

PENDING = {"QUEUED", "CLAIMED", "NOT_CONFIGURED", "NOT_ENABLED"}
TERMINAL_SEND = {"PROVIDER_ACCEPTED", "FAILED", "UNKNOWN", "CANCELLED"}
LEASE_SECONDS = 45


def view(row: Notification) -> dict:
    return {
        "notification_id": row.notification_id,
        "task_id": row.task_id,
        "task_version": row.task_version,
        "kind": row.kind,
        "send_state": "QUEUED" if row.send_state == "CLAIMED" else row.send_state,
        "delivery_state": "UNAVAILABLE",
        "message_id": row.message_id,
        "attempts": row.attempts,
        "error_code": row.error_code,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def list_notifications(engine: Engine, actor: Principal, factory_id: str, task_id: str) -> dict:
    # Reuse task visibility, including its current assignee. No recipient data leaves this route.
    human_tasks.get_task(engine, actor, factory_id, task_id)
    with Session(engine) as db:
        return {
            "notifications": [
                view(row)
                for row in db.scalars(
                    select(Notification)
                    .where(Notification.factory_id == factory_id, Notification.task_id == task_id)
                    .order_by(Notification.created_at.desc())
                    .limit(100)
                )
            ]
        }


def _lock(db: Session, factory_id: str, case_id: str, task_id: str):
    state = db.get(FactoryState, factory_id, with_for_update=True)
    case = db.get(CaseRecord, case_id, with_for_update=True)
    task = db.get(HumanTaskRecord, task_id, with_for_update=True)
    return state, case, task


def _valid(db: Session, state, case, task, row: Notification | None = None) -> bool:
    if (
        case is None
        or task is None
        or case.factory_id != task.factory_id
        or task.case_id != case.case_id
        or task.state not in human_tasks.LIVE_STATES
        or case.state in human_tasks.TERMINAL_CASE_STATES
    ):
        return False
    try:
        human_tasks._current_run(db, state, case)
    except AccessError:
        return False
    if row:
        if (
            task.version != row.task_version
            or task.factory_id != row.factory_id
            or task.case_id != row.case_id
        ):
            return False
        if row.reminder_id:
            reminder = db.get(TaskReminder, row.reminder_id)
            if (
                reminder is None
                or reminder.state != "QUEUED"
                or reminder.task_id != task.task_id
                or reminder.task_version != task.version
            ):
                return False
    return True


def _new(db: Session, task: HumanTaskRecord, kind: str, key: str, reminder_id=None) -> None:
    if db.scalar(select(Notification.notification_id).where(Notification.dedupe_key == key)):
        return
    identifier, now = str(uuid4()), datetime.now(UTC)
    db.add(
        Notification(
            notification_id=identifier,
            factory_id=task.factory_id,
            case_id=task.case_id,
            task_id=task.task_id,
            task_version=task.version,
            kind=kind,
            dedupe_key=key,
            reminder_id=reminder_id,
            role="manager" if kind == "ESCALATION" else task.owner_role,
            message_id=f"<{identifier}@byof.invalid>",
            send_state="QUEUED",
            attempts=0,
            created_at=now,
            updated_at=now,
        )
    )


def enqueue_pending(engine: Engine, settings: Settings) -> int:
    if channel_state(settings) == "NOT_ENABLED":
        return 0
    with Session(engine) as db:
        # Exclude already recorded task versions before the limit, so old tasks cannot starve new ones.
        tasks = list(
            db.execute(
                select(HumanTaskRecord.factory_id, HumanTaskRecord.case_id, HumanTaskRecord.task_id)
                .where(
                    HumanTaskRecord.state.in_(human_tasks.LIVE_STATES),
                    ~exists(
                        select(Notification.notification_id).where(
                            Notification.task_id == HumanTaskRecord.task_id,
                            Notification.task_version == HumanTaskRecord.version,
                            Notification.kind != "REMINDER",
                        )
                    ),
                )
                .order_by(HumanTaskRecord.created_at)
                .limit(100)
            )
        )
        reminders = list(
            db.execute(
                select(
                    TaskReminder.factory_id,
                    TaskReminder.case_id,
                    TaskReminder.task_id,
                    TaskReminder.reminder_id,
                )
                .where(
                    TaskReminder.state == "QUEUED",
                    ~exists(
                        select(Notification.notification_id).where(
                            Notification.reminder_id == TaskReminder.reminder_id
                        )
                    ),
                )
                .order_by(TaskReminder.created_at)
                .limit(100)
            )
        )
    added = 0
    for factory_id, case_id, task_id in tasks:
        with Session(engine) as db, db.begin():
            state, case, task = _lock(db, factory_id, case_id, task_id)
            if task is None:
                continue
            kind = (
                "ESCALATION"
                if task.state == "ESCALATED"
                else ("INITIAL" if task.version == 1 else "TRANSFER")
            )
            _new(db, task, kind, f"task:{task_id}:{task.version}")
            if not _valid(db, state, case, task):
                row = db.scalar(
                    select(Notification).where(
                        Notification.dedupe_key == f"task:{task_id}:{task.version}"
                    )
                )
                if row is not None:
                    _finish(row, "CANCELLED", "TASK_CHANGED")
            added += 1
    for factory_id, case_id, task_id, reminder_id in reminders:
        with Session(engine) as db, db.begin():
            state, case, task = _lock(db, factory_id, case_id, task_id)
            reminder = db.get(TaskReminder, reminder_id, with_for_update=True)
            if (
                not _valid(db, state, case, task)
                or reminder is None
                or reminder.state != "QUEUED"
                or task is None
                or reminder.task_version != task.version
            ):
                if reminder is not None and reminder.state == "QUEUED":
                    reminder.state = "CANCELLED"
                continue
            _new(db, task, "REMINDER", f"reminder:{reminder_id}", reminder_id)
            added += 1
    return added


def _finish(row: Notification, state: str, code: str | None = None) -> None:
    row.send_state, row.error_code, row.updated_at = state, code, datetime.now(UTC)
    row.lease_token = row.lease_until = None


def maintain_pending(engine: Engine, settings: Settings) -> None:
    with Session(engine) as db:
        rows = list(
            db.execute(
                select(
                    Notification.factory_id,
                    Notification.case_id,
                    Notification.task_id,
                    Notification.notification_id,
                )
                .where(Notification.send_state.in_(PENDING | {"SENDING"}))
                .order_by(Notification.updated_at)
                .limit(100)
            )
        )
    for factory_id, case_id, task_id, notification_id in rows:
        with Session(engine) as db, db.begin():
            state, case, task = _lock(db, factory_id, case_id, task_id)
            row = db.get(Notification, notification_id, with_for_update=True)
            if row is None or row.send_state in TERMINAL_SEND:
                continue
            now = datetime.now(UTC)
            if row.send_state == "SENDING":
                if row.lease_until is None or row.lease_until <= now:
                    _finish(row, "UNKNOWN", "SMTP_RESULT_UNKNOWN")
                continue
            if not _valid(db, state, case, task, row):
                _finish(row, "CANCELLED", "TASK_CHANGED")
            elif channel_state(settings) == "NOT_ENABLED":
                _finish(row, "NOT_ENABLED", "MAIL_NOT_ENABLED")
            # Rotate the bounded scan even when a missing contact cannot be fixed by the worker.
            row.updated_at = now


@dataclass(frozen=True)
class Claim:
    notification_id: str
    factory_id: str
    case_id: str
    task_id: str
    token: str
    message: MailMessage


def _contact(db: Session, row: Notification, task: HumanTaskRecord) -> NotificationContact | None:
    contact = db.get(NotificationContact, (row.factory_id, row.role), with_for_update=True)
    if contact is None or not contact.enabled:
        return None
    if row.kind != "ESCALATION" and task.owner_id is not None and contact.user_id != task.owner_id:
        return None
    user = lock_user(db, contact.user_id)
    member = lock_membership(db, contact.user_id, row.factory_id, row.role)
    return contact if user is not None and user.active and member is not None else None


def claim_one(engine: Engine, settings: Settings) -> Claim | None:
    if channel_state(settings) == "NOT_ENABLED":
        return None
    now = datetime.now(UTC)
    with Session(engine) as db:
        candidates = list(
            db.execute(
                select(
                    Notification.factory_id,
                    Notification.case_id,
                    Notification.task_id,
                    Notification.notification_id,
                )
                .outerjoin(
                    NotificationContact,
                    and_(
                        NotificationContact.factory_id == Notification.factory_id,
                        NotificationContact.role == Notification.role,
                    ),
                )
                .where(
                    or_(
                        Notification.send_state.in_({"QUEUED", "NOT_ENABLED"}),
                        and_(Notification.send_state == "CLAIMED", Notification.lease_until <= now),
                        and_(
                            Notification.send_state == "NOT_CONFIGURED",
                            NotificationContact.enabled.is_(True),
                            NotificationContact.version
                            != func.coalesce(Notification.contact_version, 0),
                        ),
                    )
                )
                .order_by(Notification.created_at)
                .limit(100)
            )
        )
    for factory_id, case_id, task_id, notification_id in candidates:
        with Session(engine) as db, db.begin():
            state, case, task = _lock(db, factory_id, case_id, task_id)
            row = db.get(Notification, notification_id, with_for_update=True)
            if row is None or row.send_state not in PENDING:
                continue
            if row.send_state == "CLAIMED" and row.lease_until and row.lease_until > now:
                continue
            if not _valid(db, state, case, task, row):
                _finish(row, "CANCELLED", "TASK_CHANGED")
                continue
            uncertain = db.scalar(
                select(Notification.notification_id).where(
                    Notification.task_id == task_id,
                    Notification.send_state.in_({"SENDING", "UNKNOWN"}),
                    Notification.notification_id != notification_id,
                )
            )
            if uncertain:
                _finish(row, "CANCELLED", "PRIOR_SEND_UNKNOWN")
                continue
            assert task is not None
            contact = _contact(db, row, task)
            if contact is None:
                configured = db.get(NotificationContact, (factory_id, row.role))
                row.contact_version = configured.version if configured else None
                _finish(row, "NOT_CONFIGURED", "CONTACT_NOT_CONFIGURED")
                continue
            if row.attempts >= 3:
                _finish(row, "FAILED", "SEND_ATTEMPTS_EXHAUSTED")
                continue
            row.recipient_id, row.recipient_email = contact.user_id, contact.email
            row.contact_version = contact.version
            if settings.smtp_mode in {"starttls", "tls"}:
                try:
                    authorize_recipient(settings, contact.email)
                except MailConfigurationError as exc:
                    _finish(row, "FAILED", exc.code)
                    continue
            row.send_state, row.error_code = "CLAIMED", None
            row.attempts, row.lease_token = row.attempts + 1, str(uuid4())
            row.lease_until, row.updated_at = now + timedelta(seconds=LEASE_SECONDS), now
            task_url = (
                settings.public_origin.rstrip("/")
                + "/?"
                + urlencode({"factory_id": factory_id, "case_id": case_id, "task_id": task_id})
            )
            saved = db.get(SnapshotRecord, state.snapshot_id) if state else None
            subject_label = ""
            if saved:
                for collection, key, label in (
                    ("resources", "resource_id", "Machine"),
                    ("receipts", "receipt_id", "Receipt"),
                    ("orders", "order_id", "Order"),
                ):
                    if any(
                        item.get(key) == task.subject_id
                        for item in saved.document.get(collection, [])
                    ):
                        subject_label = f"{label} {task.subject_id}"
                        break
            return Claim(
                notification_id,
                factory_id,
                case_id,
                task_id,
                row.lease_token,
                MailMessage(
                    message_id=row.message_id,
                    recipient=contact.email,
                    task_id=task_id,
                    task_url=task_url,
                    fields=tuple(task.requested_fields),
                    factory_label=factory_id,
                    subject_label=subject_label,
                    role=row.role,
                    task_type=human_tasks._kind(db, task),
                    due_at=task.due_at,
                ),
            )
    return None


def before_data(engine: Engine, settings: Settings, claim: Claim) -> bool:
    with Session(engine) as db, db.begin():
        state, case, task = _lock(db, claim.factory_id, claim.case_id, claim.task_id)
        row = db.get(Notification, claim.notification_id, with_for_update=True)
        now = datetime.now(UTC)
        if (
            row is None
            or row.send_state != "CLAIMED"
            or row.lease_token != claim.token
            or row.lease_until is None
            or row.lease_until <= now
        ):
            return False
        if channel_state(settings) == "NOT_ENABLED":
            _finish(row, "NOT_ENABLED", "MAIL_NOT_ENABLED")
            return False
        if not _valid(db, state, case, task, row):
            _finish(row, "CANCELLED", "TASK_CHANGED")
            return False
        uncertain = db.scalar(
            select(Notification.notification_id).where(
                Notification.task_id == claim.task_id,
                Notification.notification_id != claim.notification_id,
                Notification.send_state.in_({"SENDING", "UNKNOWN"}),
            )
        )
        if uncertain:
            _finish(row, "CANCELLED", "PRIOR_SEND_UNKNOWN")
            return False
        assert task is not None
        contact = _contact(db, row, task)
        if (
            contact is None
            or contact.version != row.contact_version
            or contact.user_id != row.recipient_id
            or contact.email != row.recipient_email
        ):
            # No DATA was authorized. A new confirmed contact version may claim this same intent.
            _finish(row, "NOT_CONFIGURED", "CONTACT_CHANGED")
            return False
        if settings.smtp_mode in {"starttls", "tls"}:
            try:
                authorize_recipient(settings, contact.email)
            except MailConfigurationError as exc:
                _finish(row, "FAILED", exc.code)
                return False
        row.send_state, row.updated_at = "SENDING", now
        row.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        return True


def record_result(engine: Engine, claim: Claim, result: SendResult) -> None:
    with Session(engine) as db, db.begin():
        row = db.get(Notification, claim.notification_id, with_for_update=True)
        if (
            row is None
            or row.lease_token != claim.token
            or row.send_state not in {"CLAIMED", "SENDING"}
        ):
            return
        if result.state not in {"PROVIDER_ACCEPTED", "FAILED", "UNKNOWN", "CANCELLED"}:
            _finish(
                row, "UNKNOWN" if row.send_state == "SENDING" else "FAILED", "INVALID_MAIL_RESULT"
            )
            return
        if result.state == "PROVIDER_ACCEPTED" and row.send_state != "SENDING":
            _finish(row, "FAILED", "MAIL_DATA_NOT_AUTHORIZED")
            return
        _finish(row, result.state, result.code)


def deliver_notification(
    engine: Engine, settings: Settings, transport: SMTPTransport | None = None
) -> bool:
    maintain_pending(engine, settings)
    enqueue_pending(engine, settings)
    claim = claim_one(engine, settings)
    if claim is None:
        return False
    sender = transport or SMTPTransport(settings)
    try:
        result = sender.send(claim.message, lambda: before_data(engine, settings, claim))
    except Exception:
        # Leave the committed fence intact; lease recovery distinguishes pre-DATA from uncertainty.
        return True
    record_result(engine, claim, result)
    return True
