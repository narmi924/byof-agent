"""Confirmed objectives survive real worker/HTTP publication and uncertain receipt recovery."""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session
from test_dynamic_factory_postgres import dynamic_source as dynamic_source
from test_dynamic_factory_postgres import send_plan, snapshot
from test_publication_postgres import publishing as publishing

from packages.auth import AccessError, Grant, Principal
from packages.domain.execution import PlanSubmission
from packages.domain.models import Candidate
from packages.domain.objectives import EffectiveObjective, ObjectiveDefinition
from packages.integrations.factory_http import ConnectorError
from packages.persistence import Membership, connect
from packages.planning import preferences
from packages.planning.preference_store import (
    ObjectiveRecord,
    PreferenceAction,
    PreferenceCoordination,
    PreferenceHead,
    PreferenceProposal,
    PreferenceRevision,
    PreferenceState,
)
from packages.planning.publication import Publication, commit_publication, deliver_one, publications
from packages.planning.service import approve, request_solve, synchronize
from packages.planning.store import ApprovalRecord, CandidateRecord, SolveJob
from services.factory_sim.storage import SourceAction
from services.solver_worker.main import run_once


@pytest.fixture
def preference_publication(publishing):
    source, reader, writer, actor, *_ = publishing
    factory, engine = source[2].factory_id, source[3]
    with Session(engine) as db, db.begin():
        db.add(Membership(user_id=actor.user_id, factory_id=factory, role="admin"))
    administrator = Principal(
        user_id=actor.user_id,
        username=actor.username,
        grants=(*actor.grants, Grant(factory_id=factory, role="admin")),
    )
    try:
        yield source, reader, writer, administrator
    finally:
        owner = connect(os.environ["TEST_MIGRATION_DATABASE_URL"])
        assert owner.url.database == "byof_test"
        try:
            with owner.begin() as db:
                for table in (
                    ObjectiveRecord,
                    PreferenceAction,
                    PreferenceCoordination,
                    PreferenceHead,
                    PreferenceRevision,
                    PreferenceProposal,
                    PreferenceState,
                ):
                    db.execute(delete(table).where(table.factory_id == factory))
        finally:
            owner.dispose()


def confirm_preference(context, *, version=0, selection="overtime_first"):
    source, _, _, actor = context
    factory, engine = source[2].factory_id, source[3]
    definition = ObjectiveDefinition(
        selection=selection,
        **({"max_weighted_tardiness": 1000} if selection == "overtime_first" else {}),
    )
    proposed = preferences.propose(
        engine,
        actor,
        factory,
        preferences.ProposalInput(
            request_id="proposal-" + uuid4().hex,
            scope_type="FACTORY",
            scope_id=factory,
            definition=definition,
            expected_version=version,
            reason="Confirm this turn's factory objective order and tardiness minute bound",
        ),
    )
    assert proposed["state"] == "PENDING"
    confirmed = preferences.confirm(
        engine,
        actor,
        factory,
        proposed["proposal_id"],
        preferences.ConfirmationInput(
            request_id="confirmation-" + uuid4().hex,
            expected_state_version=version,
        ),
    )
    assert confirmed["version"] == confirmed["state_version"] == version + 1
    return confirmed


def solve_confirmed(context):
    source, reader, _, actor = context
    engine, factory = source[3], source[2].factory_id
    confirmed = confirm_preference(context)
    current = synchronize(engine, reader, factory)
    job = request_solve(
        engine,
        actor,
        factory,
        request_id="solve-preference-" + uuid4().hex,
        allow_overtime=False,
        time_limit=2,
    )
    assert job.objective_version.startswith("objective:")
    assert run_once(engine)
    with Session(engine) as db:
        completed = db.get(SolveJob, job.job_id)
        assert completed.state == "SUCCEEDED", completed.error_code
        assert completed.attempts == 1 and completed.lease_until is None
        saved = db.get(CandidateRecord, completed.candidate_id)
        candidate = Candidate.model_validate(saved.document)
        contract = db.get(ObjectiveRecord, completed.objective_version)
        objective = EffectiveObjective.model_validate(contract.document)
    assert candidate.has_solution and candidate.native_status == "OPTIMAL"
    assert candidate.checker.status == "PASS"
    assert len(candidate.assignments) == 8
    assert candidate.binding.snapshot_hash == current.content_hash
    assert (
        candidate.binding.objective_version == job.objective_version == objective.objective_version
    )
    assert tuple(metric.name for metric in candidate.objective) == objective.order
    assert candidate.objective[0].name == "incremental_overtime_metric"
    assert candidate.objective[0].value == 0
    assert candidate.proven_objective_levels == 5
    assert objective.sources[0].preference_id == confirmed["preference_id"]
    assert objective.sources[0].confirmed_by == actor.user_id
    return candidate, objective


def approve_candidate(context, candidate):
    source, _, _, actor = context
    approval = approve(
        source[3],
        actor,
        source[2].factory_id,
        candidate.candidate_id,
        request_id="approval-" + uuid4().hex,
        candidate_hash=candidate.content_hash,
        action_scope="publish_plan",
        decision="APPROVED",
    )
    assert approval.binding == candidate.binding
    assert approval.candidate_hash == candidate.content_hash
    return approval


def commit_candidate(context, candidate):
    source, _, _, actor = context
    return commit_publication(
        source[3],
        actor,
        source[2].factory_id,
        candidate.candidate_id,
        request_id="publication-" + uuid4().hex,
        candidate_hash=candidate.content_hash,
    )


def source_submit_count(source):
    with Session(source[4]) as db:
        return db.scalar(
            select(func.count())
            .select_from(SourceAction)
            .where(
                SourceAction.factory_id == source[2].factory_id, SourceAction.kind == "plan.submit"
            )
        )


def retry_now(engine, release):
    with engine.begin() as db:
        db.execute(
            update(Publication)
            .where(Publication.release_id == release.release_id)
            .values(next_attempt_at=datetime.now(UTC))
        )


class ObservedWriter:
    def __init__(self, writer, *, lose_receipt=False):
        self.writer = writer
        self.submissions = []
        self.lose_receipt = lose_receipt

    def submit(self, submission):
        self.submissions.append(submission)
        receipt = self.writer.submit(submission)
        if self.lose_receipt:
            raise ConnectorError("Controlled loss after the source transaction committed")
        return receipt


def test_confirmed_objective_reaches_worker_approval_and_http_source_without_becoming_execution(
    preference_publication,
):
    context = preference_publication
    source, reader, writer, _ = context
    factory, engine = source[2].factory_id, source[3]
    before = snapshot(source)
    candidate, objective = solve_confirmed(context)
    approval = approve_candidate(context, candidate)
    committed = commit_candidate(context, candidate)
    assert committed.local_state == "LOCAL_COMMITTED"
    assert committed.source_state == "PENDING_SOURCE"
    assert snapshot(source).active_plan_version is None
    with Session(engine) as db:
        row = db.get(Publication, committed.release_id)
        submission = PlanSubmission.model_validate(row.payload)
    assert submission.objective == objective
    assert submission.approvals == (approval,)
    assert submission.candidate == candidate
    observed = ObservedWriter(writer)
    assert deliver_one(engine, reader, observed)
    accepted = publications(engine, factory)[0]["release"]
    live = snapshot(source)
    assert accepted.source_state == "ACTIVE"
    assert accepted.execution_state == "NOT_STARTED"
    assert accepted.operation_id == committed.operation_id
    assert accepted.source_receipt_id is not None
    assert len(observed.submissions) == source_submit_count(source) == 1
    assert observed.submissions[0] == submission
    assert live.active_plan_hash == candidate.content_hash
    assert live.active_plan_version is not None
    assert live.actuals == before.actuals == ()
    assert live.inventory == before.inventory
    assert live.reservations == before.reservations == ()
    with Session(source[4]) as db:
        action = db.scalar(
            select(SourceAction).where(
                SourceAction.factory_id == factory,
                SourceAction.operation_id == committed.operation_id,
            )
        )
        assert action.request["objective"] == objective.model_dump(mode="json")
        assert action.result["source_state"] == "ACTIVE"
        assert action.result["receipt_id"] == accepted.source_receipt_id


@pytest.mark.parametrize("stage", ["APPROVED", "LOCAL_COMMITTED"])
def test_preference_change_after_approval_or_local_commit_prevents_any_source_submit(
    preference_publication, stage
):
    context = preference_publication
    source, reader, writer, actor = context
    engine, factory = source[3], source[2].factory_id
    candidate, objective = solve_confirmed(context)
    approval = approve_candidate(context, candidate)
    committed = commit_candidate(context, candidate) if stage == "LOCAL_COMMITTED" else None
    confirm_preference(context, version=1, selection="delivery_first")
    current = preferences.get_preferences(engine, actor, factory)
    assert current["state_version"] == 2
    assert current["effective"]["objective_version"] != objective.objective_version
    observed = ObservedWriter(writer)
    if committed is None:
        with pytest.raises(AccessError) as rejected:
            commit_candidate(context, candidate)
        assert rejected.value.code == "OBJECTIVE_CHANGED"
        assert publications(engine, factory) == []
        assert not deliver_one(engine, reader, observed)
    else:
        assert deliver_one(engine, reader, observed)
        result = publications(engine, factory)[0]
        assert result["release"].local_state == "LOCAL_COMMITTED"
        assert result["release"].source_state == "REJECTED"
        assert result["release"].source_receipt_id is None
        assert result["error_code"] == "OBJECTIVE_CHANGED"
    assert observed.submissions == []
    assert source_submit_count(source) == 0
    assert snapshot(source).active_plan_version is None
    with Session(engine) as db:
        old_approval = db.get(ApprovalRecord, approval.approval_id)
        assert old_approval.document == approval.model_dump(mode="json")
        assert (
            db.get(CandidateRecord, candidate.candidate_id).content_hash == candidate.content_hash
        )


def test_unknown_receipt_keeps_querying_original_action_after_preference_changes(
    preference_publication,
):
    context = preference_publication
    source, reader, writer, actor = context
    engine, factory = source[3], source[2].factory_id
    candidate, objective = solve_confirmed(context)
    approve_candidate(context, candidate)
    committed = commit_candidate(context, candidate)
    lost = ObservedWriter(writer, lose_receipt=True)
    assert deliver_one(engine, reader, lost)
    unknown = publications(engine, factory)[0]["release"]
    assert unknown.source_state == "UNKNOWN" and unknown.source_receipt_id is None
    accepted_source = snapshot(source)
    assert accepted_source.active_plan_hash == candidate.content_hash
    assert source_submit_count(source) == len(lost.submissions) == 1
    original_payload = lost.submissions[0].model_dump(mode="json")
    confirm_preference(context, version=1, selection="delivery_first")
    assert (
        preferences.get_preferences(engine, actor, factory)["effective"]["objective_version"]
        != objective.objective_version
    )

    class TemporarilyMissingReceipt:
        def __init__(self):
            self.queries = []

        def action(self, factory_id, run_id, operation_id):
            self.queries.append((factory_id, run_id, operation_id))
            receipt = reader.action(factory_id, run_id, operation_id)
            assert receipt is not None and receipt.source_state == "ACTIVE"
            return None

    unavailable = TemporarilyMissingReceipt()
    for _ in range(2):
        retry_now(engine, committed)
        assert deliver_one(engine, unavailable, lost)
        pending = publications(engine, factory)[0]
        assert pending["release"].source_state == "UNKNOWN"
        assert pending["release"].source_receipt_id is None
        assert pending["error_code"] == "SOURCE_RESULT_UNKNOWN"
        assert len(lost.submissions) == source_submit_count(source) == 1
        with Session(engine) as db:
            durable = db.get(Publication, committed.release_id)
            assert durable.state == "UNKNOWN" and durable.lease_until is None
            assert durable.payload == original_payload
            assert durable.source_receipt is None
    assert unavailable.queries == [(factory, accepted_source.run_id, committed.operation_id)] * 2
    retry_now(engine, committed)
    assert deliver_one(engine, reader, lost)
    reconciled = publications(engine, factory)[0]["release"]
    receipt = reader.action(factory, accepted_source.run_id, committed.operation_id)
    assert receipt is not None
    assert reconciled.source_state == "ACTIVE"
    assert reconciled.operation_id == committed.operation_id
    assert reconciled.source_receipt_id == receipt.receipt_id
    assert reconciled.candidate_hash == candidate.content_hash
    assert reconciled.execution_state == "NOT_STARTED"
    assert snapshot(source).content_hash == accepted_source.content_hash
    assert len(lost.submissions) == source_submit_count(source) == 1


@pytest.mark.parametrize(
    "invalid", ["missing", "different_version", "foreign_factory", "corrupted_hash"]
)
def test_http_execution_source_rejects_missing_or_invalid_objective_contract(
    preference_publication, invalid
):
    context = preference_publication
    source, reader, _, _ = context
    engine = source[3]
    candidate, _ = solve_confirmed(context)
    approve_candidate(context, candidate)
    committed = commit_candidate(context, candidate)
    with Session(engine) as db:
        payload = PlanSubmission.model_validate(
            db.get(Publication, committed.release_id).payload
        ).model_dump(mode="json")
    before = snapshot(source)
    if invalid == "missing":
        payload.pop("objective")
    elif invalid == "corrupted_hash":
        payload["objective"]["content_hash"] = "0" * 64
    else:
        data = {key: value for key, value in payload["objective"].items() if key != "content_hash"}
        if invalid == "different_version":
            data["resolution_version"] += 1
        else:
            data["factory_id"] = "different-factory"
            data["sources"][0]["scope_id"] = "different-factory"
        payload["objective"] = EffectiveObjective.model_validate(data).model_dump(mode="json")
    response = send_plan(source, payload)
    if invalid == "corrupted_hash":
        assert response.status_code == 422
        assert source_submit_count(source) == 0
        assert reader.action(before.factory_id, before.run_id, committed.operation_id) is None
    else:
        assert response.status_code == 200
        receipt = response.json()
        assert receipt["source_state"] == "REJECTED"
        assert receipt["error_code"] == "CHECK_FAILED"
        assert receipt["plan_version"] is None and receipt["effective_at"] is None
        assert receipt["candidate_hash"] == candidate.content_hash
        assert source_submit_count(source) == 1
        saved_receipt = reader.action(before.factory_id, before.run_id, committed.operation_id)
        assert saved_receipt is not None and saved_receipt.source_state == "REJECTED"
    after = snapshot(source)
    assert after.content_hash == before.content_hash
    assert after.active_plan_version is None and after.actuals == ()
    assert after.inventory == before.inventory
    local = publications(engine, before.factory_id)[0]["release"]
    assert local.source_state == "PENDING_SOURCE" and local.source_receipt_id is None
