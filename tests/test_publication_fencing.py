"""A source read cannot authorize a stale worker or poison durable receipt recovery."""

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from test_dynamic_factory_postgres import dynamic_source as dynamic_source
from test_dynamic_factory_postgres import snapshot
from test_publication_postgres import approve_and_commit
from test_publication_postgres import publishing as publishing
from test_revalidation_postgres import certificate, commit
from test_revalidation_postgres import progress as progress

from packages.domain.execution import PlanSubmission
from packages.integrations import capabilities
from packages.persistence import connect
from packages.planning import publication, revalidation_check
from packages.planning.publication import (
    Publication,
    commit_publication,
    deliver_one,
    publications,
)
from packages.planning.service import approve
from packages.planning.store import ApprovalRecord
from services.factory_sim.storage import SourceAction


@pytest.mark.parametrize("lease_change", ["reclaimed", "expired"])
def test_worker_losing_lease_during_source_query_never_sends_the_plan(publishing, lease_change):
    source, reader, writer, *_ = publishing
    engine, factory = source[3], source[2].factory_id
    _, committed = approve_and_commit(publishing)
    replacement = "successor-worker"

    class ClaimLostDuringQuery:
        def action(self, factory_id, run_id, operation_id):
            assert reader.action(factory_id, run_id, operation_id) is None
            values = {"lease_until": datetime.now(UTC) - timedelta(seconds=1)}
            if lease_change == "reclaimed":
                values = {
                    "lease_token": replacement,
                    "lease_until": datetime.now(UTC) + timedelta(seconds=60),
                }
            with engine.begin() as db:
                db.execute(
                    update(Publication)
                    .where(Publication.release_id == committed.release_id)
                    .values(**values)
                )
            return None

    class ObservedWriter:
        calls = 0

        def submit(self, submission):
            self.calls += 1
            return writer.submit(submission)

    observed = ObservedWriter()
    assert deliver_one(engine, ClaimLostDuringQuery(), observed)
    assert observed.calls == 0
    assert snapshot(source).active_plan_version is None
    with Session(engine) as db:
        row = db.get(Publication, committed.release_id)
        assert row.state == "DELIVERING"
        assert row.document["source_state"] == "PENDING_SOURCE"
        if lease_change == "reclaimed":
            assert row.lease_token == replacement
    with Session(source[4]) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(SourceAction)
                .where(SourceAction.factory_id == factory, SourceAction.kind == "plan.submit")
            )
            == 0
        )


def test_wrong_candidate_hash_in_query_receipt_stays_unknown_and_recovers_without_resend(
    publishing,
):
    source, reader, writer, *_ = publishing
    engine, factory = source[3], source[2].factory_id
    _, committed = approve_and_commit(publishing)
    with Session(engine) as db:
        row = db.get(Publication, committed.release_id)
        submission = PlanSubmission.model_validate(row.payload)
    accepted = writer.submit(submission)
    assert accepted.source_state == "ACTIVE"

    class CorruptedReceipt:
        def action(self, factory_id, run_id, operation_id):
            receipt = reader.action(factory_id, run_id, operation_id)
            assert receipt is not None and receipt.candidate_hash != "0" * 64
            return receipt.model_copy(update={"candidate_hash": "0" * 64})

    class UnexpectedWriter:
        def submit(self, submission):
            raise AssertionError("An existing source action must never be submitted again")

    assert deliver_one(engine, CorruptedReceipt(), UnexpectedWriter())
    unknown = publications(engine, factory)[0]
    assert unknown["release"].source_state == "UNKNOWN"
    assert unknown["release"].source_receipt_id is None
    assert unknown["error_code"] == "SOURCE_RESULT_UNKNOWN"
    with Session(engine) as db, db.begin():
        row = db.get(Publication, committed.release_id, with_for_update=True)
        assert row.state == "UNKNOWN" and row.lease_until is None
        assert row.source_receipt is None
        row.next_attempt_at = datetime.now(UTC)
    assert deliver_one(engine, reader, UnexpectedWriter())
    recovered = publications(engine, factory)[0]["release"]
    assert recovered.source_state == "ACTIVE"
    assert recovered.operation_id == committed.operation_id
    assert recovered.source_receipt_id == accepted.receipt_id
    with Session(source[4]) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(SourceAction)
                .where(SourceAction.factory_id == factory, SourceAction.kind == "plan.submit")
            )
            == 1
        )


def controlled_real_clock(monkeypatch):
    class Clock:
        value = datetime.now(UTC)

        @classmethod
        def now(cls, timezone):
            return cls.value.astimezone(timezone)

    for module in (publication, capabilities, revalidation_check):
        monkeypatch.setattr(module, "datetime", Clock)
    return Clock


def assert_no_send_after_capability_delay(
    source, reader, writer, release, clock, *, delay_seconds, expected_error
):
    engine, factory = source[3], source[2].factory_id
    before = snapshot(source)
    with Session(engine) as db:
        original_payload = db.get(Publication, release.release_id).payload
    with Session(source[4]) as db:
        source_ids = set(
            db.scalars(select(SourceAction.action_id).where(SourceAction.factory_id == factory))
        )

    class DelayedCapabilities:
        calls = 0

        def action(self, *args):
            return reader.action(*args)

        def capabilities(self):
            current = reader.capabilities()
            self.calls += 1
            clock.value += timedelta(seconds=delay_seconds)
            return current

    class ObservedWriter:
        calls = 0

        def submit(self, body):
            self.calls += 1
            return writer.submit(body)

    delayed, observed = DelayedCapabilities(), ObservedWriter()
    assert deliver_one(engine, delayed, observed)
    assert delayed.calls == 1 and observed.calls == 0
    with Session(engine) as db:
        row = db.get(Publication, release.release_id)
        assert row.payload == original_payload
        assert row.document["operation_id"] == release.operation_id
        assert row.document["payload_hash"] == release.payload_hash
        assert row.source_receipt is None and row.document["source_receipt_id"] is None
        if expected_error is None:
            assert row.state == "DELIVERING"
            assert row.document["source_state"] == "PENDING_SOURCE"
            assert row.lease_until < clock.value
        else:
            assert row.state == "DONE" and row.error_code == expected_error
            assert row.document["source_state"] == "REJECTED"
    assert snapshot(source) == before
    with Session(source[4]) as db:
        assert (
            set(
                db.scalars(select(SourceAction.action_id).where(SourceAction.factory_id == factory))
            )
            == source_ids
        )


def test_lease_expiring_during_final_capability_read_prevents_source_write(publishing, monkeypatch):
    source, reader, writer, *_ = publishing
    _, release = approve_and_commit(publishing)
    clock = controlled_real_clock(monkeypatch)
    assert_no_send_after_capability_delay(
        source, reader, writer, release, clock, delay_seconds=61, expected_error=None
    )


def test_approval_expiring_during_final_capability_read_prevents_source_write(
    publishing, monkeypatch
):
    source, reader, writer, actor, candidate_id, digest = publishing
    engine, factory = source[3], source[2].factory_id
    approved = approve(
        engine,
        actor,
        factory,
        candidate_id,
        request_id="short-lived-approval",
        candidate_hash=digest,
        action_scope="publish_plan",
        decision="APPROVED",
    )
    clock = controlled_real_clock(monkeypatch)
    owner = connect(os.environ["TEST_MIGRATION_DATABASE_URL"])
    try:
        assert owner.url.database == "byof_test"
        with Session(owner) as db, db.begin():
            row = db.get(ApprovalRecord, approved.approval_id)
            row.document = {
                **row.document,
                "expires_at": (clock.value + timedelta(seconds=5)).isoformat(),
            }
    finally:
        owner.dispose()
    release = commit_publication(
        engine,
        actor,
        factory,
        candidate_id,
        request_id="publish-short-lived-approval",
        candidate_hash=digest,
    )
    assert_no_send_after_capability_delay(
        source,
        reader,
        writer,
        release,
        clock,
        delay_seconds=6,
        expected_error="APPROVAL_REQUIRED",
    )


@pytest.mark.parametrize("dynamic_source", [{"progress_revalidation": True}], indirect=True)
def test_certificate_expiring_during_final_capability_read_prevents_source_write(
    progress, monkeypatch
):
    ctx = progress
    cert = certificate(ctx)
    release = commit(ctx, cert)
    clock = controlled_real_clock(monkeypatch)
    assert_no_send_after_capability_delay(
        ctx.source,
        ctx.reader,
        ctx.writer,
        release,
        clock,
        delay_seconds=31,
        expected_error="VALIDATION_EXPIRED",
    )
