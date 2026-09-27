"""An exhausted delivery budget still reconciles the same external operation read-only."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_dynamic_factory_postgres import control
from test_dynamic_factory_postgres import dynamic_source as dynamic_source
from test_publication_postgres import approve_and_commit
from test_publication_postgres import publishing as publishing

from packages.domain.execution import PlanSubmission
from packages.integrations.factory_http import ConnectorError
from packages.planning.publication import Publication, deliver_one, publications
from packages.planning.service import synchronize
from services.factory_sim.storage import SourceAction


def make_due(engine, release_id):
    with Session(engine) as db, db.begin():
        row = db.get(Publication, release_id, with_for_update=True)
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)


def assert_readonly_wait(engine, release_id):
    with Session(engine) as db:
        row = db.get(Publication, release_id)
        assert row.attempts == 6
        assert row.state == "UNKNOWN" and row.document["source_state"] == "UNKNOWN"
        assert row.source_receipt is None and row.lease_until is None
        assert (
            timedelta(seconds=55) < row.next_attempt_at - datetime.now(UTC) <= timedelta(seconds=60)
        )


def source_actions(source):
    with Session(source[4]) as db:
        return db.scalar(
            select(func.count())
            .select_from(SourceAction)
            .where(
                SourceAction.factory_id == source[2].factory_id,
                SourceAction.kind == "plan.submit",
            )
        )


def test_six_unknown_attempts_recover_existing_acceptance_without_new_post(publishing):
    source, reader, writer, *_ = publishing
    engine, factory = source[3], source[2].factory_id
    _, committed = approve_and_commit(publishing)

    class LostAcceptance:
        calls = 0
        accepted = None

        def submit(self, submission):
            self.calls += 1
            self.accepted = writer.submit(submission)
            raise ConnectorError("Receipt lost after the source committed")

    class ReaderOutage:
        def action(self, factory_id, run_id, operation_id):
            assert (factory_id, run_id, operation_id) == (
                factory,
                source[2].run_id,
                committed.operation_id,
            )
            raise ConnectorError("Source read outage")

    lost = LostAcceptance()
    assert deliver_one(engine, reader, lost)
    for _ in range(5):
        make_due(engine, committed.release_id)
        assert deliver_one(engine, ReaderOutage(), lost)
    assert_readonly_wait(engine, committed.release_id)
    assert source_actions(source) == lost.calls == 1
    make_due(engine, committed.release_id)
    assert deliver_one(engine, reader, lost)
    recovered = publications(engine, factory)[0]["release"]
    assert recovered.source_state == "ACTIVE"
    assert recovered.operation_id == committed.operation_id
    assert recovered.source_receipt_id == lost.accepted.receipt_id
    with Session(engine) as db:
        row = db.get(Publication, committed.release_id)
        assert row.state == "DONE" and row.attempts == 6
        assert row.source_receipt == lost.accepted.model_dump(mode="json")
    assert source_actions(source) == lost.calls == 1


def test_no_action_after_six_send_attempts_remains_readonly_with_bounded_counter(publishing):
    source, reader, *_ = publishing
    engine = source[3]
    _, committed = approve_and_commit(publishing)

    class UnavailableWriter:
        calls = 0

        def submit(self, submission):
            self.calls += 1
            raise ConnectorError("No request reached the source")

    writer = UnavailableWriter()
    for _ in range(6):
        make_due(engine, committed.release_id)
        assert deliver_one(engine, reader, writer)
    assert writer.calls == 6 and source_actions(source) == 0
    for _ in range(3):
        assert_readonly_wait(engine, committed.release_id)
        assert not deliver_one(engine, reader, writer)
        make_due(engine, committed.release_id)
        assert deliver_one(engine, reader, writer)
        assert writer.calls == 6 and source_actions(source) == 0
    assert_readonly_wait(engine, committed.release_id)


def test_crash_on_last_claim_recovers_receipt_without_spending_another_send(publishing):
    source, reader, writer, *_ = publishing
    engine = source[3]
    _, committed = approve_and_commit(publishing)
    with Session(engine) as db, db.begin():
        row = db.get(Publication, committed.release_id, with_for_update=True)
        submission = PlanSubmission.model_validate(row.payload)
        row.attempts = 6
        row.state = "DELIVERING"
        row.lease_token = "worker-that-crashed"
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    accepted = writer.submit(submission)
    assert accepted.source_state == "ACTIVE"

    class NoMoreWrites:
        def submit(self, submission):
            raise AssertionError("Recovery after the last claim must never send another POST")

    assert deliver_one(engine, reader, NoMoreWrites())
    with Session(engine) as db:
        row = db.get(Publication, committed.release_id)
        assert row.attempts == 6 and row.state == "DONE"
        assert row.lease_token != "worker-that-crashed" and row.lease_until is None
        assert row.source_receipt == accepted.model_dump(mode="json")
    assert source_actions(source) == 1


def test_actuals_received_before_lost_receipt_reconcile_even_when_source_stays_paused(publishing):
    source, reader, writer, *_ = publishing
    engine, factory = source[3], source[2].factory_id
    _, committed = approve_and_commit(publishing)
    with Session(engine) as db:
        payload = PlanSubmission.model_validate(db.get(Publication, committed.release_id).payload)
    writer.submit(payload)
    assert control(source, "produce-before-receipt", "clock.step").status_code == 200
    actuals_first = synchronize(engine, reader, factory)
    assert actuals_first.actuals[0].state == "IN_PROGRESS"
    assert deliver_one(engine, reader, writer)
    unchanged = synchronize(engine, reader, factory)
    assert unchanged.content_hash == actuals_first.content_hash
    release = publications(engine, factory)[0]["release"]
    assert release.source_state == "ACTIVE" and release.execution_state == "IN_PROGRESS"
    assert release.source_receipt_id is not None and source_actions(source) == 1
