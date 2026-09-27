"""Local commit, source acceptance and actual execution are separate business outcomes."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session
from test_dynamic_factory_postgres import control, snapshot
from test_dynamic_factory_postgres import dynamic_source as dynamic_source

from packages.auth import AccessError, Grant, Principal
from packages.integrations.factory_http import ConnectorError, FactoryExecution, FactoryHTTP
from packages.integrations.sync import RunSwitch, SourceBatch
from packages.persistence import Membership, User, connect
from packages.planning.publication import Publication, commit_publication, deliver_one, publications
from packages.planning.service import approve, request_solve, synchronize
from packages.planning.store import (
    ApprovalRecord,
    CandidateRecord,
    FactoryState,
    SnapshotRecord,
    SolveJob,
)
from services.factory_sim.storage import SourceAction
from services.solver_worker.main import run_once


@pytest.fixture
def publishing(dynamic_source):
    source = dynamic_source
    client, tokens, initial, engine, _ = source
    reader = FactoryHTTP(str(client.base_url), tokens["reader"])
    writer = FactoryExecution(str(client.base_url), tokens["writer"])
    user_id = "publisher-" + uuid4().hex
    with Session(engine) as db, db.begin():
        db.add(User(user_id=user_id, username=user_id, password_hash="not-a-login", active=True))
        db.flush()
        db.add(Membership(user_id=user_id, factory_id=initial.factory_id, role="planner"))
    actor = Principal(
        user_id=user_id,
        username=user_id,
        grants=(Grant(factory_id=initial.factory_id, role="planner"),),
    )
    state = synchronize(engine, reader, initial.factory_id)
    job = request_solve(
        engine, actor, state.factory_id, request_id="solve", allow_overtime=False, time_limit=2
    )
    assert run_once(engine)
    with Session(engine) as db:
        complete = db.get(SolveJob, job.job_id)
        assert complete.state == "SUCCEEDED"
        candidate = db.get(CandidateRecord, complete.candidate_id)
        candidate_id, candidate_hash = candidate.candidate_id, candidate.content_hash
    try:
        yield source, reader, writer, actor, candidate_id, candidate_hash
    finally:
        reader.close()
        writer.close()
        owner = connect(os.environ["TEST_MIGRATION_DATABASE_URL"])
        assert owner.url.database == "byof_test"
        with owner.begin() as db:
            for table in (
                RunSwitch,
                SourceBatch,
                Publication,
                ApprovalRecord,
                CandidateRecord,
                SolveJob,
                FactoryState,
                SnapshotRecord,
            ):
                db.execute(delete(table).where(table.factory_id == initial.factory_id))
            db.execute(delete(Membership).where(Membership.user_id == user_id))
            db.execute(delete(User).where(User.user_id == user_id))
        owner.dispose()


def approve_and_commit(context):
    source, _, _, actor, candidate_id, candidate_hash = context
    engine, factory = source[3], source[2].factory_id
    approved = approve(
        engine,
        actor,
        factory,
        candidate_id,
        request_id="approval",
        candidate_hash=candidate_hash,
        action_scope="publish_plan",
        decision="APPROVED",
    )
    release = commit_publication(
        engine,
        actor,
        factory,
        candidate_id,
        request_id="publication",
        candidate_hash=candidate_hash,
    )
    return approved, release


def test_publication_requires_approval_and_records_local_source_execution_separately(publishing):
    source, reader, writer, actor, candidate_id, candidate_hash = publishing
    engine, factory = source[3], source[2].factory_id
    with pytest.raises(AccessError) as rejected:
        commit_publication(
            engine,
            actor,
            factory,
            candidate_id,
            request_id="not-approved",
            candidate_hash=candidate_hash,
        )
    assert rejected.value.code == "APPROVAL_REQUIRED"
    assert publications(engine, factory) == []
    _, committed = approve_and_commit(publishing)
    assert committed.local_state == "LOCAL_COMMITTED" and committed.source_state == "PENDING_SOURCE"
    assert (
        committed.execution_state == "NOT_STARTED" and snapshot(source).active_plan_version is None
    )
    assert deliver_one(engine, reader, writer)
    accepted = publications(engine, factory)[0]["release"]
    assert accepted.source_state == "ACTIVE" and accepted.execution_state == "NOT_STARTED"
    assert accepted.source_receipt_id is not None and snapshot(source).actuals == ()
    assert control(source, "start-physical", "clock.step").status_code == 200
    live = synchronize(engine, reader, factory)
    assert live.actuals[0].state == "IN_PROGRESS" and live.actuals[0].remaining_minutes == 9
    assert live.reservations


def test_lost_remote_receipt_is_reconciled_before_any_resend(publishing):
    source, reader, writer, _, _, _ = publishing
    engine, factory = source[3], source[2].factory_id
    _, committed = approve_and_commit(publishing)

    class LostReceipt:
        calls = 0

        def submit(self, submission):
            self.calls += 1
            writer.submit(submission)
            raise ConnectorError("Test connection lost after source commit")

    lost = LostReceipt()
    assert deliver_one(engine, reader, lost)
    assert publications(engine, factory)[0]["release"].source_state == "UNKNOWN"
    assert snapshot(source).active_plan_version is not None
    with engine.begin() as db:
        db.execute(
            update(Publication)
            .where(Publication.release_id == committed.release_id)
            .values(next_attempt_at=datetime.now(UTC))
        )
    assert deliver_one(engine, reader, lost)
    assert lost.calls == 1
    assert publications(engine, factory)[0]["release"].source_state == "ACTIVE"
    with Session(source[4]) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(SourceAction)
                .where(SourceAction.factory_id == factory, SourceAction.kind == "plan.submit")
            )
            == 1
        )


def test_approval_rejection_after_local_commit_prevents_external_write(publishing):
    source, reader, writer, actor, candidate_id, candidate_hash = publishing
    engine, factory = source[3], source[2].factory_id
    approve_and_commit(publishing)
    approve(
        engine,
        actor,
        factory,
        candidate_id,
        request_id="revocation",
        candidate_hash=candidate_hash,
        action_scope="publish_plan",
        decision="REJECTED",
    )
    assert deliver_one(engine, reader, writer)
    result = publications(engine, factory)[0]
    assert (
        result["release"].source_state == "REJECTED" and result["error_code"] == "APPROVAL_REQUIRED"
    )
    assert snapshot(source).active_plan_version is None
    with Session(source[4]) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(SourceAction)
                .where(SourceAction.factory_id == factory)
            )
            == 0
        )


def test_new_fact_after_commit_is_conditionally_rejected_and_idempotency_is_stable(publishing):
    source, reader, writer, actor, candidate_id, candidate_hash = publishing
    engine, factory = source[3], source[2].factory_id
    _, committed = approve_and_commit(publishing)
    retry = commit_publication(
        engine,
        actor,
        factory,
        candidate_id,
        request_id="publication",
        candidate_hash=candidate_hash,
    )
    assert retry == committed
    assert control(source, "intervening-clock", "clock.step").status_code == 200
    assert deliver_one(engine, reader, writer)
    result = publications(engine, factory)[0]
    assert result["release"].source_state == "REJECTED"
    assert result["error_code"] == "SOURCE_CONDITIONS_CHANGED"
    assert snapshot(source).active_plan_version is None


def test_worker_restart_reclaims_expired_lease_and_revoked_membership_prevents_write(publishing):
    source, reader, writer, actor, *_ = publishing
    engine, factory = source[3], source[2].factory_id
    _, committed = approve_and_commit(publishing)
    with engine.begin() as db:
        db.execute(
            update(Publication)
            .where(Publication.release_id == committed.release_id)
            .values(
                state="DELIVERING",
                lease_token="dead-worker",
                lease_until=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        db.execute(delete(Membership).where(Membership.user_id == actor.user_id))
    assert deliver_one(engine, reader, writer)
    assert publications(engine, factory)[0]["release"].source_state == "REJECTED"
    assert snapshot(source).active_plan_version is None
