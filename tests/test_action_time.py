"""The review interval is a bound on new dispatch, not a shift of approved history."""

from datetime import timedelta

import pytest
from pydantic import ValidationError
from test_checker import at, example_assignments, example_snapshot, make_candidate
from test_solver_wip import initial

from packages.domain.models import Candidate, canonical_hash
from packages.planning.checker import check_candidate
from packages.planning.solver import PlanningInputError, solve
from services.factory_sim.engine import advance, evolve, inject


def test_new_plan_reserves_review_time_without_changing_the_business_horizon():
    source = example_snapshot()
    candidate = solve(source, time_limit=3, new_actions_not_before=at(15))
    assert candidate.has_solution and candidate.checker.status == "PASS"
    assert candidate.schema_version == "byof.candidate/3"
    assert candidate.effective_not_before == source.snapshot_clock
    assert candidate.new_actions_not_before == at(15)
    assert candidate.accept_before > at(15)
    assert all(a.changeover_start >= at(15) for a in candidate.assignments)
    assert max(a.end_at for a in candidate.assignments) <= source.horizon.end_at


def test_checker_independently_rejects_legal_but_premature_dispatch():
    source = example_snapshot()
    candidate = make_candidate(
        source, schema_version="byof.candidate/3", new_actions_not_before=at(15)
    )
    report = check_candidate(source, candidate)
    assert report.status == "FAIL"
    assert "NEW_ACTION_TOO_EARLY" in {i.code for i in report.issues}


def test_existing_dispatch_inside_review_interval_keeps_exact_assignments():
    source = example_snapshot()
    baseline = make_candidate(source)
    current = evolve(source, active_plan_version="accepted", active_plan_hash=baseline.content_hash)
    candidate = solve(current, baseline=baseline, time_limit=3, new_actions_not_before=at(15))
    assert candidate.has_solution and candidate.checker.status == "PASS"
    assert {a.operation_id: a for a in candidate.assignments} == {
        a.operation_id: a for a in baseline.assignments
    }


def test_checker_rejects_moving_approved_prefix_even_beyond_review_boundary():
    source = example_snapshot()
    baseline = make_candidate(source)
    current = evolve(source, active_plan_version="accepted", active_plan_hash=baseline.content_hash)
    changed = make_candidate(
        current,
        example_assignments(shift=15),
        schema_version="byof.candidate/3",
        new_actions_not_before=at(15),
    )
    report = check_candidate(current, changed, baseline=baseline)
    assert report.status == "FAIL"
    assert {"REVIEW_PREFIX_CHANGED", "FROZEN_OPERATION_CHANGED"} <= {i.code for i in report.issues}


@pytest.mark.parametrize(
    "boundary", [at(-1), at(60), at(100), at(1) + timedelta(seconds=1), at(1).replace(tzinfo=None)]
)
def test_invalid_dispatch_time_fails_before_search(boundary):
    with pytest.raises(PlanningInputError, match="INVALID_ACTION_TIME"):
        solve(example_snapshot(), new_actions_not_before=boundary)


def test_legacy_candidate_hash_is_unchanged_and_cannot_hide_a_new_boundary():
    old = make_candidate(example_snapshot())
    data = old.model_dump(mode="json", exclude={"content_hash", "new_actions_not_before"})
    assert old.content_hash == canonical_hash(data)
    assert (
        Candidate.model_validate({**data, "content_hash": old.content_hash}).content_hash
        == old.content_hash
    )
    with pytest.raises(ValidationError, match="version 3"):
        Candidate.model_validate({**data, "new_actions_not_before": at(15)})
    new = Candidate.model_validate(
        {**data, "schema_version": "byof.candidate/3", "new_actions_not_before": at(15)}
    )
    with pytest.raises(ValidationError, match="changed after hashing"):
        Candidate.model_validate({**new.model_dump(), "new_actions_not_before": at(16)})


def recovered_source():
    source, baseline = initial()
    source = advance(source, baseline, minutes=2)
    running = source.actuals[0]
    source = inject(
        source,
        event_id="down-for-review",
        kind="resource.down",
        payload={"resource_id": running.resource_id},
    )
    source = advance(source, baseline, minutes=75)
    source = inject(
        source,
        event_id="remaining-for-review",
        kind="execution.confirm_remaining",
        payload={
            "operation_id": running.operation_id,
            "remaining_minutes": 8,
            "remaining_setup_minutes": 0,
        },
    )
    source = inject(
        source,
        event_id="restored-for-review",
        kind="resource.restore",
        payload={"resource_id": running.resource_id},
    )
    source = advance(source, baseline, minutes=1)
    assert source.actuals[0].state == "IN_PROGRESS"
    assert source.actuals[0].remaining_minutes == 7
    return source, baseline


def test_recovered_work_continues_while_new_dispatch_reserves_review_time():
    source, baseline = recovered_source()
    boundary = source.snapshot_clock + timedelta(minutes=15)
    candidate = solve(source, baseline=baseline, time_limit=3, new_actions_not_before=boundary)
    assert candidate.has_solution and candidate.checker.status == "PASS", candidate.checker
    running = source.actuals[0]
    assignments = {a.operation_id: a for a in candidate.assignments}
    current = assignments[running.operation_id]
    assert current.resume_changeover_start == source.snapshot_clock
    assert current.end_at == source.snapshot_clock + timedelta(minutes=7)
    assert current.start_at == running.actual_start
    assert all(
        a.changeover_start >= boundary
        for a in candidate.assignments
        if a.operation_id != running.operation_id
    )
    assert candidate.accept_before >= boundary
    assert source.profile.policy.freeze_window_min == 60
