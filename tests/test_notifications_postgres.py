"""Real PostgreSQL notification authorization, crash recovery, and local SMTP evidence."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session
from test_human_tasks_postgres import due_now, reply, task
from test_human_tasks_postgres import human_context as human_context
from test_mail import capture, settings
from test_mail_demo import config as real_settings

from packages.agent.cases_store import CaseRecord
from packages.agent.human_tasks import (
    HumanTaskRecord,
    TaskReminder,
    get_task,
    tick_reminders,
    transfer,
)
from packages.auth import AccessError
from packages.integrations.contacts import ContactInput, configure_contact, list_contacts
from packages.integrations.mail import SendResult
from packages.integrations.notification_store import (
    ContactAction,
    Notification,
    NotificationContact,
)
from packages.integrations.notifications import (
    before_data,
    claim_one,
    deliver_notification,
    enqueue_pending,
    list_notifications,
    maintain_pending,
    record_result,
)
from packages.persistence import Membership, User, connect
from packages.planning.store import FactoryState


@pytest.fixture
def context(human_context):
    ctx = human_context
    # Product-generated Case identifiers are UUIDs; keep this shared human fixture's facts unchanged.
    with Session(ctx.engine) as db, db.begin():
        case = db.get(CaseRecord, ctx.case_id)
        ctx.case_id = case.case_id = str(uuid4())
    try:
        yield ctx
    finally:
        owner = connect(os.environ["TEST_MIGRATION_DATABASE_URL"])
        try:
            with owner.begin() as db:
                for model in (Notification, ContactAction, NotificationContact):
                    db.execute(delete(model).where(model.factory_id == ctx.factory))
        finally:
            owner.dispose()


def configure(ctx, role="maintainer", actor="admin", **overrides):
    return configure_contact(
        ctx.engine,
        ctx.actors[actor],
        ctx.factory,
        ContactInput(
            **{
                "request_id": str(uuid4()),
                "role": role,
                "user_id": ctx.actors[role].user_id,
                "email": role + "@test.invalid",
                "enabled": True,
                "expected_version": 0,
                **overrides,
            }
        ),
    )


def rows(ctx):
    with Session(ctx.engine) as db:
        return list(
            db.scalars(
                select(Notification)
                .where(Notification.factory_id == ctx.factory)
                .order_by(Notification.created_at)
            )
        )


class Sender:
    def __init__(self, hook=lambda: None, result=SendResult("PROVIDER_ACCEPTED")):
        self.hook, self.result, self.messages = hook, result, []

    def send(self, message, before_data):
        self.hook()
        if not before_data():
            return SendResult("CANCELLED", "NOTIFICATION_NO_LONGER_CURRENT")
        self.messages.append(message)
        return self.result


def test_contact_configuration_requires_admin_and_live_target_role(context):
    ctx = context
    for actor in ("planner", "maintainer", "manager", "outsider"):
        with pytest.raises(AccessError):
            configure(ctx, actor=actor)
    for target in ("outsider", "warehouse", "missing"):
        with pytest.raises(AccessError) as error:
            configure(ctx, user_id=ctx.actors[target].user_id if target != "missing" else "missing")
        assert error.value.code == "INVALID_CONTACT_USER"
    assert rows(ctx) == []
    assert configure(ctx)["version"] == 1
    result = list_contacts(ctx.engine, ctx.actors["admin"], ctx.factory, settings())
    assert result["channel_state"] == "CAPTURE"
    assert result["contacts"][0]["email"] == "maintainer@test.invalid"
    assert ctx.actors["outsider"].user_id not in {u["user_id"] for u in result["eligible_users"]}


@pytest.mark.parametrize(
    "email",
    [
        "a@test.invalid\r\nBcc:b@test.invalid",
        "a@test.invalid,b@test.invalid",
        "Name <a@test.invalid>",
        ".a@test.invalid",
        "a..b@test.invalid",
    ],
)
def test_contact_rejects_header_and_list_injection(email):
    with pytest.raises(ValidationError):
        ContactInput(
            request_id="contact",
            role="maintainer",
            user_id="a",
            email=email,
            enabled=True,
            expected_version=0,
        )


def test_contact_cas_idempotency_and_immutable_audit(context):
    ctx = context
    first = configure(ctx, request_id="save")
    assert configure(ctx, request_id="save") == first
    with pytest.raises(AccessError) as error:
        configure(ctx, request_id="save", email="changed@test.invalid")
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(AccessError) as error:
        configure(ctx, expected_version=0)
    assert error.value.code == "CONTACT_VERSION_CHANGED"
    assert configure(ctx, expected_version=1, enabled=False)["version"] == 2
    with Session(ctx.engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(ContactAction)
                .where(ContactAction.factory_id == ctx.factory)
            )
            == 2
        )
    with pytest.raises(ProgrammingError), ctx.engine.begin() as db:
        db.execute(delete(ContactAction).where(ContactAction.factory_id == ctx.factory))


def test_revoked_admin_cannot_reuse_prior_contact_operation(context):
    ctx = context
    configure(ctx, request_id="save")
    with Session(ctx.engine) as db, db.begin():
        db.get(User, ctx.actors["admin"].user_id).active = False
    with pytest.raises(AccessError) as error:
        configure(ctx, request_id="save")
    assert error.value.code == "AUTHORIZATION_REVOKED"


def test_concurrent_contact_updates_preserve_one_version(context):
    ctx = context
    configure(ctx)

    def save(index):
        try:
            return configure(ctx, expected_version=1, email=f"a{index}@test.invalid")["version"]
        except AccessError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, range(2)))
    assert sorted(str(value) for value in results) == ["2", "CONTACT_VERSION_CHANGED"]


def test_concurrent_intents_and_delivery_send_exactly_once_without_closing_task(context):
    ctx = context
    record, sender = task(ctx), Sender()
    configure(ctx)
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: enqueue_pending(ctx.engine, settings()), range(3)))
        list(pool.map(lambda _: deliver_notification(ctx.engine, settings(), sender), range(3)))
    saved = rows(ctx)
    assert len(saved) == len(sender.messages) == 1
    assert saved[0].send_state == "PROVIDER_ACCEPTED" and saved[0].attempts == 1
    current = get_task(ctx.engine, ctx.actors["maintainer"], ctx.factory, record["task_id"])
    assert current["state"] == "OPEN" and current["response"] is None
    assert (
        current["send_state"] == "PROVIDER_ACCEPTED" and current["delivery_state"] == "UNAVAILABLE"
    )
    with Session(ctx.engine) as db:
        assert db.get(CaseRecord, ctx.case_id).closure is None


def test_disabled_channel_never_enqueues_or_sends(context):
    ctx, sender = context, Sender()
    task(ctx)
    configure(ctx)
    assert not deliver_notification(ctx.engine, settings(smtp_mode="disabled"), sender)
    assert rows(ctx) == [] and sender.messages == []


def test_real_mode_resolves_separate_roles_and_preserves_task_links(context):
    ctx, sender = context, Sender()
    first = task(ctx)
    second = task(
        ctx,
        "ask-warehouse",
        role="warehouse",
        question="Please check the receipt",
        fields=["receipt_eta"],
        subject_id=ctx.snapshot.receipts[0].receipt_id,
    )
    configure(ctx, email="maintenance@example.com")
    configure(ctx, role="warehouse", email="stores@example.com")
    configuration = real_settings(
        real_email_allowlist="maintenance@example.com,stores@example.com",
        test_email_recipient="smoke@example.com",
        public_origin="http://192.168.1.50:18080",
    )
    assert deliver_notification(ctx.engine, configuration, sender)
    assert deliver_notification(ctx.engine, configuration, sender)
    assert {m.recipient for m in sender.messages} == {
        "maintenance@example.com",
        "stores@example.com",
    }
    assert {m.task_id for m in sender.messages} == {first["task_id"], second["task_id"]}
    for message in sender.messages:
        assert message.task_url.startswith("http://192.168.1.50:18080/?factory_id=")
        assert f"task_id={message.task_id}" in message.task_url
        assert message.factory_label == ctx.factory and message.due_at is not None
        assert message.subject_label and "smoke@example.com" not in message.task_url
    assert all(row.send_state == "PROVIDER_ACCEPTED" for row in rows(ctx))


def test_real_recipient_policy_rejection_is_audited_without_transport(context):
    ctx, sender = context, Sender()
    task(ctx)
    configure(ctx, email="blocked@example.com")
    assert not deliver_notification(
        ctx.engine, real_settings(real_email_allowlist="allowed@example.com"), sender
    )
    row = rows(ctx)[0]
    assert (row.send_state, row.error_code, row.attempts) == ("FAILED", "RECIPIENT_NOT_ALLOWED", 0)
    assert row.recipient_email == "blocked@example.com" and not sender.messages


def test_real_email_disabled_and_policy_changed_before_data_cannot_send(context):
    ctx, sender = context, Sender()
    task(ctx)
    configure(ctx, email="maintenance@example.com")
    configuration = real_settings(real_email_allowlist="maintenance@example.com")
    assert not deliver_notification(
        ctx.engine, configuration.model_copy(update={"allow_real_email": False}), sender
    )
    assert not rows(ctx)
    enqueue_pending(ctx.engine, configuration)
    claim = claim_one(ctx.engine, configuration)
    assert claim is not None
    assert not before_data(
        ctx.engine, real_settings(real_email_allowlist="other@example.com"), claim
    )
    assert rows(ctx)[0].error_code == "RECIPIENT_NOT_ALLOWED"


def test_smtp_authentication_failure_is_persisted_without_secret(context, monkeypatch):
    import smtplib
    from unittest.mock import Mock

    from test_mail import SMTPStub

    from packages.integrations import mail

    ctx = context
    task(ctx)
    configure(ctx, email="maintenance@example.com")
    stub = SMTPStub()
    stub.login = Mock(side_effect=smtplib.SMTPAuthenticationError(535, b"private-test-marker"))
    monkeypatch.setattr(mail.smtplib, "SMTP", Mock(return_value=stub))
    assert deliver_notification(
        ctx.engine, real_settings(real_email_allowlist="maintenance@example.com")
    )
    row = rows(ctx)[0]
    assert row.send_state == "FAILED" and row.error_code == "SMTP_AUTHENTICATION_FAILED"
    assert "data" not in stub.calls and "private-test-marker" not in row.error_code


def test_missing_contact_is_explicit_and_configuration_wakes_original_intent(context):
    ctx, sender = context, Sender()
    task(ctx)
    assert not deliver_notification(ctx.engine, settings(), sender)
    initial = rows(ctx)[0]
    assert initial.send_state == "NOT_CONFIGURED" and initial.attempts == 0
    configure(ctx)
    assert deliver_notification(ctx.engine, settings(), sender)
    current = rows(ctx)[0]
    assert (
        current.notification_id == initial.notification_id
        and current.message_id == initial.message_id
    )
    assert current.send_state == "PROVIDER_ACCEPTED" and len(sender.messages) == 1


@pytest.mark.parametrize(
    "change", ["reply", "contact", "disable", "revoke", "run", "case", "transfer"]
)
def test_state_is_rechecked_after_smtp_setup_before_data(context, change):
    ctx = context
    record = task(ctx)
    configure(ctx)

    def mutate():
        if change == "reply":
            reply(ctx, record)
        elif change in {"contact", "disable"}:
            configure(
                ctx,
                expected_version=1,
                email="replacement@test.invalid",
                enabled=change != "disable",
            )
        elif change == "transfer":
            transfer(
                ctx.engine,
                ctx.actors["planner"],
                ctx.factory,
                record["task_id"],
                request_id="transfer",
                expected_task_version=1,
                target_role="warehouse",
                reason="Take over",
            )
        else:
            with Session(ctx.engine) as db, db.begin():
                if change == "revoke":
                    db.execute(
                        delete(Membership).where(
                            Membership.user_id == ctx.actors["maintainer"].user_id
                        )
                    )
                elif change == "run":
                    db.get(FactoryState, ctx.factory).run_id = "another-run"
                else:
                    db.get(CaseRecord, ctx.case_id).state = "CANCELLED"

    sender = Sender(mutate)
    assert deliver_notification(ctx.engine, settings(), sender)
    assert sender.messages == []
    assert rows(ctx)[0].send_state == (
        "NOT_CONFIGURED" if change in {"contact", "disable", "revoke"} else "CANCELLED"
    )


def expire(ctx, notification_id):
    with Session(ctx.engine) as db, db.begin():
        db.get(Notification, notification_id).lease_until = datetime.now(UTC) - timedelta(seconds=1)


def test_pre_data_crash_recovers_same_message_and_fences_old_worker(context):
    ctx, sender = context, Sender()
    task(ctx)
    configure(ctx)
    enqueue_pending(ctx.engine, settings())
    old = claim_one(ctx.engine, settings())
    assert old is not None
    expire(ctx, old.notification_id)
    replacement = claim_one(ctx.engine, settings())
    assert replacement is not None and replacement.notification_id == old.notification_id
    assert replacement.message == old.message and replacement.token != old.token
    assert not before_data(ctx.engine, settings(), old)
    result = sender.send(
        replacement.message, lambda: before_data(ctx.engine, settings(), replacement)
    )
    record_result(ctx.engine, replacement, result)
    record_result(ctx.engine, old, SendResult("FAILED", "OLD_WORKER"))
    assert rows(ctx)[0].send_state == "PROVIDER_ACCEPTED"
    assert rows(ctx)[0].attempts == 2 and len(sender.messages) == 1


def test_three_pre_data_crashes_are_bounded(context):
    ctx = context
    task(ctx)
    configure(ctx)
    enqueue_pending(ctx.engine, settings())
    identifiers = []
    for _ in range(3):
        claim = claim_one(ctx.engine, settings())
        assert claim is not None
        identifiers.append(claim.notification_id)
        expire(ctx, claim.notification_id)
    assert claim_one(ctx.engine, settings()) is None
    assert len(set(identifiers)) == 1
    assert rows(ctx)[0].send_state == "FAILED" and rows(ctx)[0].attempts == 3


def test_lost_post_data_result_remains_unknown_and_never_resends(context):
    ctx, sender = context, Sender()
    record = task(ctx)
    configure(ctx)
    enqueue_pending(ctx.engine, settings())
    claim = claim_one(ctx.engine, settings())
    assert claim is not None
    # External acceptance occurred but the worker died before recording the result.
    assert (
        sender.send(claim.message, lambda: before_data(ctx.engine, settings(), claim)).state
        == "PROVIDER_ACCEPTED"
    )
    expire(ctx, claim.notification_id)
    for _ in range(4):
        assert not deliver_notification(ctx.engine, settings(), sender)
    assert rows(ctx)[0].send_state == "UNKNOWN" and len(sender.messages) == 1
    due_now(ctx, record)
    tick_reminders(ctx.engine)
    assert not deliver_notification(ctx.engine, settings(), sender)
    assert len(sender.messages) == 1
    assert rows(ctx)[-1].error_code == "PRIOR_SEND_UNKNOWN"


def test_unknown_provider_result_blocks_automatic_reminders(context):
    ctx = context
    record = task(ctx)
    configure(ctx)
    sender = Sender(result=SendResult("UNKNOWN", "SMTP_RESULT_UNKNOWN"))
    assert deliver_notification(ctx.engine, settings(), sender)
    due_now(ctx, record)
    tick_reminders(ctx.engine)
    assert not deliver_notification(ctx.engine, settings(), sender)
    assert len(sender.messages) == 1 and rows(ctx)[0].send_state == "UNKNOWN"


def test_reply_cancels_queued_reminders_but_keeps_accepted_evidence(context):
    ctx, sender = context, Sender()
    record = task(ctx)
    configure(ctx)
    deliver_notification(ctx.engine, settings(), sender)
    due_now(ctx, record)
    tick_reminders(ctx.engine)
    enqueue_pending(ctx.engine, settings())
    reply(ctx, record)
    assert not deliver_notification(ctx.engine, settings(), sender)
    assert [row.send_state for row in rows(ctx)] == ["PROVIDER_ACCEPTED", "CANCELLED"]
    assert len(sender.messages) == 1


def test_reminders_and_escalation_use_role_contacts_and_independent_processing_state(context):
    ctx, sender = context, Sender()
    record = task(ctx)
    configure(ctx)
    configure(ctx, role="manager")
    assert deliver_notification(ctx.engine, settings(), sender)
    for _ in range(3):
        due_now(ctx, record)
        tick_reminders(ctx.engine)
        assert deliver_notification(ctx.engine, settings(), sender)
    assert [row.kind for row in rows(ctx)] == ["INITIAL", "REMINDER", "REMINDER", "ESCALATION"]
    assert [msg.recipient for msg in sender.messages] == ["maintainer@test.invalid"] * 3 + [
        "manager@test.invalid"
    ]
    assert not deliver_notification(ctx.engine, settings(), sender)
    current = get_task(ctx.engine, ctx.actors["maintainer"], ctx.factory, record["task_id"])
    assert (
        current["state"] == "ESCALATED"
        and current["reminders_count"] == 2
        and current["response"] is None
    )


def test_notification_read_does_not_leak_recipient_or_grant_task_access(context):
    ctx, sender = context, Sender()
    record = task(ctx)
    configure(ctx)
    deliver_notification(ctx.engine, settings(), sender)
    for role in ("warehouse", "outsider", "admin"):
        with pytest.raises(AccessError):
            list_notifications(ctx.engine, ctx.actors[role], ctx.factory, record["task_id"])
    first = list_notifications(ctx.engine, ctx.actors["maintainer"], ctx.factory, record["task_id"])
    assert first == list_notifications(
        ctx.engine, ctx.actors["maintainer"], ctx.factory, record["task_id"]
    )
    assert "recipient_email" not in first["notifications"][0]
    assert "@test.invalid" not in str(first)


def test_real_smtp_capture_has_one_message_and_postgres_acceptance_without_delivery_claim(context):
    ctx = context
    record = task(ctx)
    configure(ctx)
    with capture() as (server, configuration):
        assert deliver_notification(ctx.engine, configuration)
        assert not deliver_notification(ctx.engine, configuration)
        paths = list(server.mailbox.glob("*.eml"))
        assert len(paths) == 1
        mail = BytesParser(policy=policy.default).parsebytes(paths[0].read_bytes())
        saved = rows(ctx)[0]
        assert mail["Message-ID"] == saved.message_id and saved.send_state == "PROVIDER_ACCEPTED"
        body = mail.get_content()
        assert record["task_id"] in body and ctx.case_id in body and ctx.factory in body
        assert record["question"] not in body
    assert (
        get_task(ctx.engine, ctx.actors["maintainer"], ctx.factory, record["task_id"])[
            "delivery_state"
        ]
        == "UNAVAILABLE"
    )


def test_invalid_old_run_intent_is_cancelled_without_any_send(context):
    ctx = context
    task(ctx)
    configure(ctx)
    with Session(ctx.engine) as db, db.begin():
        db.get(FactoryState, ctx.factory).run_id = "old-run"
    sender = Sender()
    assert not deliver_notification(ctx.engine, settings(), sender)
    assert sender.messages == [] and rows(ctx)[0].send_state == "CANCELLED"


def test_disabling_channel_after_claim_cancels_data_permission(context):
    ctx = context
    task(ctx)
    configure(ctx)
    enqueue_pending(ctx.engine, settings())
    claim = claim_one(ctx.engine, settings())
    assert claim is not None
    assert not before_data(ctx.engine, settings(smtp_mode="disabled"), claim)
    assert rows(ctx)[0].send_state == "NOT_ENABLED"
    maintain_pending(ctx.engine, settings(smtp_mode="disabled"))
    assert rows(ctx)[0].send_state == "NOT_ENABLED"


def test_contact_changed_before_data_recovers_escalation_without_new_intent(context):
    ctx = context
    record = task(ctx)
    for _ in range(3):
        due_now(ctx, record)
        tick_reminders(ctx.engine)
    configure(ctx, role="manager")
    old = Sender(
        lambda: configure(ctx, role="manager", expected_version=1, email="new@test.invalid")
    )
    assert deliver_notification(ctx.engine, settings(), old)
    first = rows(ctx)[0]
    assert first.kind == "ESCALATION" and first.send_state == "NOT_CONFIGURED"
    assert old.messages == []
    replacement = Sender()
    assert deliver_notification(ctx.engine, settings(), replacement)
    second = rows(ctx)[0]
    assert second.notification_id == first.notification_id and second.message_id == first.message_id
    assert second.send_state == "PROVIDER_ACCEPTED" and second.attempts == 2
    assert (
        len(replacement.messages) == 1 and replacement.messages[0].recipient == "new@test.invalid"
    )


def test_hundred_expired_reminders_do_not_starve_a_later_valid_reminder(context):
    ctx = context
    stale = [
        task(ctx, f"old-{index}", question=f"Machine {index} needs an expected time")
        for index in range(50)
    ]
    now = datetime.now(UTC)
    with Session(ctx.engine) as db, db.begin():
        for record in stale:
            row = db.get(HumanTaskRecord, record["task_id"])
            row.version, row.state = 2, "ESCALATED"
            for ordinal in (1, 2):
                db.add(
                    TaskReminder(
                        reminder_id=str(uuid4()),
                        factory_id=ctx.factory,
                        case_id=ctx.case_id,
                        task_id=row.task_id,
                        task_version=1,
                        ordinal=ordinal,
                        state="QUEUED",
                        scheduled_at=now - timedelta(days=1),
                        created_at=now - timedelta(days=1),
                    )
                )
    current = task(ctx, "valid-current", question="An expected time is still needed")
    due_now(ctx, current)
    tick_reminders(ctx.engine)
    enqueue_pending(ctx.engine, settings())
    enqueue_pending(ctx.engine, settings())
    with Session(ctx.engine) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(TaskReminder)
                .where(TaskReminder.factory_id == ctx.factory, TaskReminder.state == "CANCELLED")
            )
            == 100
        )
    assert (
        len(
            [
                row
                for row in rows(ctx)
                if row.task_id == current["task_id"] and row.kind == "REMINDER"
            ]
        )
        == 1
    )
