"""Explicit review time is durable, fenced and tied to the original solve request."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from test_case_tools_postgres import count, operation
from test_case_tools_postgres import tools_case as tools_case
from test_dynamic_factory_postgres import control
from test_dynamic_factory_postgres import dynamic_source as dynamic_source
from test_publication_postgres import publishing as publishing

from packages.agent.case_tools import execute_operation, recover_operation
from packages.auth import AccessError, Grant, Principal
from packages.domain.models import Candidate, Snapshot
from packages.persistence import Membership
from packages.planning.service import claim_job, complete_job, job_view, request_solve, synchronize
from packages.planning.store import CandidateRecord, FactoryState, SnapshotRecord, SolveJob
from services.solver_worker.main import run_once


def current(context):
    engine, factory = context[0][3], context[0][2].factory_id
    with Session(engine) as db:
        state = db.get(FactoryState, factory)
        return Snapshot.model_validate(db.get(SnapshotRecord, state.snapshot_id).document)


def request(context, when, request_id="review-reserve"):
    return request_solve(
        context[0][3],
        context[3],
        context[0][2].factory_id,
        request_id=request_id,
        allow_overtime=False,
        time_limit=2,
        new_actions_not_before=when,
    )


def test_explicit_time_persists_and_worker_passes_it_to_real_solver(publishing):
    snapshot = current(publishing)
    when = snapshot.snapshot_clock + timedelta(minutes=15)
    job = request(publishing, when)
    assert job_view(job)["new_actions_not_before"] == when
    engine = publishing[0][3]
    with Session(engine) as db:
        assert db.get(SolveJob, job.job_id).new_actions_not_before == when
    assert run_once(engine)
    with Session(engine) as db:
        finished = db.get(SolveJob, job.job_id)
        assert finished.state == "SUCCEEDED"
        candidate = Candidate.model_validate(
            db.get(CandidateRecord, finished.candidate_id).document
        )
        assert candidate.schema_version == "byof.candidate/3"
        assert candidate.new_actions_not_before == when
        assert candidate.effective_not_before == snapshot.snapshot_clock
        assert candidate.checker.status == "PASS" and candidate.has_solution
        assert all(item.changeover_start >= when for item in candidate.assignments)


def test_same_id_keeps_original_absolute_time_after_business_clock_advances(publishing):
    snapshot = current(publishing)
    when = snapshot.snapshot_clock + timedelta(minutes=15)
    job = request(publishing, when)
    assert request(publishing, when).job_id == job.job_id
    for changed in (None, when + timedelta(minutes=1)):
        with pytest.raises(AccessError) as rejected:
            request(publishing, changed)
        assert rejected.value.code == "IDEMPOTENCY_CONFLICT"
    assert (
        control(publishing[0], "advance-past-review", "clock.step", {"minutes": 16}).status_code
        == 200
    )
    latest = synchronize(publishing[0][3], publishing[1], snapshot.factory_id)
    assert latest.snapshot_clock > when
    retried = request(publishing, when)
    assert retried.job_id == job.job_id and retried.snapshot_id == snapshot.snapshot_id
    assert retried.new_actions_not_before == when
    assert count(publishing[0][3], SolveJob, snapshot.factory_id) == 2


@pytest.mark.parametrize("kind", ["past", "horizon", "seconds", "microseconds", "naive", "boolean"])
def test_invalid_new_action_time_has_zero_new_jobs(publishing, kind):
    snapshot = current(publishing)
    when = {
        "past": snapshot.snapshot_clock - timedelta(minutes=1),
        "horizon": snapshot.horizon.end_at,
        "seconds": snapshot.snapshot_clock + timedelta(seconds=1),
        "microseconds": snapshot.snapshot_clock + timedelta(microseconds=1),
        "naive": snapshot.snapshot_clock.replace(tzinfo=None),
        "boolean": True,
    }[kind]
    with pytest.raises(AccessError) as rejected:
        request(publishing, when)
    assert rejected.value.code == "INVALID_NEW_ACTIONS_TIME"
    assert count(publishing[0][3], SolveJob, snapshot.factory_id) == 1


def test_earliest_at_current_clock_is_legal_and_zero_default_remains_none(publishing):
    snapshot = current(publishing)
    assert (
        request(publishing, snapshot.snapshot_clock).new_actions_not_before
        == snapshot.snapshot_clock
    )
    default = request_solve(
        publishing[0][3],
        publishing[3],
        snapshot.factory_id,
        request_id="default-time",
        allow_overtime=False,
        time_limit=2,
    )
    assert default.new_actions_not_before is None


def test_time_selection_does_not_grant_planning_role_or_bypass_revocation(publishing):
    snapshot = current(publishing)
    actor = publishing[3]
    without_role = Principal(
        user_id=actor.user_id,
        username=actor.username,
        grants=(Grant(factory_id=snapshot.factory_id, role="manager"),),
    )
    with pytest.raises(AccessError) as denied:
        request_solve(
            publishing[0][3],
            without_role,
            snapshot.factory_id,
            request_id="no-role",
            allow_overtime=False,
            time_limit=2,
            new_actions_not_before=snapshot.snapshot_clock,
        )
    assert denied.value.code == "FORBIDDEN"
    with publishing[0][3].begin() as db:
        db.execute(
            delete(Membership).where(
                Membership.user_id == actor.user_id,
                Membership.factory_id == snapshot.factory_id,
                Membership.role == "planner",
            )
        )
    with pytest.raises(AccessError) as revoked:
        request(publishing, snapshot.snapshot_clock)
    assert revoked.value.code == "AUTHORIZATION_REVOKED"
    assert count(publishing[0][3], SolveJob, snapshot.factory_id) == 1


def test_worker_cannot_drop_claimed_time_when_storing_candidate(publishing):
    snapshot = current(publishing)
    job = request(publishing, snapshot.snapshot_clock + timedelta(minutes=15))
    engine = publishing[0][3]
    claimed = claim_job(engine)
    assert claimed.job_id == job.job_id
    with Session(engine) as db:
        baseline = Candidate.model_validate(db.get(CandidateRecord, publishing[4]).document)
    with pytest.raises(ValueError, match="outside claimed snapshot"):
        complete_job(engine, claimed, baseline)
    with Session(engine) as db:
        assert db.get(SolveJob, job.job_id).candidate_id is None
        assert (
            len(
                list(
                    db.scalars(
                        select(CandidateRecord).where(
                            CandidateRecord.factory_id == snapshot.factory_id
                        )
                    )
                )
            )
            == 1
        )


def test_agent_explicit_time_reaches_durable_job_and_recovery_preserves_it(tools_case):
    context, _ = tools_case
    snapshot = current(context)
    when = snapshot.snapshot_clock + timedelta(minutes=15)
    op = operation(
        tools_case,
        "solve_scenario",
        {"allow_overtime": False, "time_limit": 2, "new_actions_not_before": when.isoformat()},
    )
    result = execute_operation(context[0][3], context[3], op)
    assert result["status"] == "PENDING"
    assert datetime.fromisoformat(result["new_actions_not_before"]).astimezone(UTC) == when
    assert recover_operation(context[0][3], context[3], op) == result
    with Session(context[0][3]) as db:
        saved = db.get(SolveJob, result["job_id"])
        assert saved.request_id == op.operation_id and saved.new_actions_not_before == when
    assert count(context[0][3], SolveJob, snapshot.factory_id) == 2


@pytest.mark.parametrize("change", ["missing_time", "changed_time", "missing_case"])
def test_agent_recovery_rejects_job_whose_registered_time_or_case_changed(tools_case, change):
    context, _ = tools_case
    snapshot = current(context)
    when = snapshot.snapshot_clock + timedelta(minutes=15)
    op = operation(
        tools_case,
        "solve_scenario",
        {"allow_overtime": False, "time_limit": 2, "new_actions_not_before": when.isoformat()},
    )
    result = execute_operation(context[0][3], context[3], op)
    assert result["status"] == "PENDING"
    with Session(context[0][3]) as db, db.begin():
        job = db.get(SolveJob, result["job_id"])
        if change == "missing_case":
            job.case_id = None
        else:
            job.new_actions_not_before = (
                None if change == "missing_time" else when + timedelta(minutes=1)
            )
    rejected = recover_operation(context[0][3], context[3], op)
    assert rejected["status"] == "REJECTED"
    assert rejected["code"] == "IDEMPOTENCY_CONFLICT"
    assert count(context[0][3], SolveJob, snapshot.factory_id) == 2
