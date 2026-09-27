"""A real timed outage must be reviewable while unaffected production keeps running."""

from datetime import timedelta

import pytest
from test_checker import example_assignments, example_snapshot, make_candidate
from test_progress_evidence import batch
from test_solver_wip import initial

from packages.domain.models import Snapshot
from packages.planning.checker import calculate_metrics, check_candidate, check_revalidated_plan
from packages.planning.dispatch import recovery_dispatch
from packages.planning.disruption import recovery_operations
from packages.planning.progress_evidence import ProgressEvidenceError, validate_progress_chain
from packages.planning.solver import solve
from services.factory_sim.engine import advance, evolve, inject


def disrupted():
    source, baseline = initial()
    source = evolve(
        source,
        profile=source.profile.model_copy(
            update={
                "policy": source.profile.policy.model_copy(
                    update={"progress_revalidation_enabled": True}
                )
            }
        ),
    )
    source = advance(source, baseline, minutes=3)
    running = next(a for a in source.actuals if a.state == "IN_PROGRESS")
    source = inject(
        source,
        event_id="reviewed-outage",
        kind="resource.outage",
        payload={"resource_id": running.resource_id, "minutes": 20},
    )
    return source, baseline


@pytest.mark.parametrize("review_minutes", [None, 15])
def test_zero_wip_worker_absence_releases_frozen_dispatch_and_can_replan(review_minutes):
    source, baseline = initial()
    assert not recovery_operations(source, baseline)
    absent_worker = baseline.assignments[0].worker_id
    source = inject(
        source,
        event_id="zero-wip-worker-absence",
        kind="worker.absent",
        payload={"worker_id": absent_worker},
    )
    assert not source.actuals
    frozen = {
        assignment.operation_id
        for assignment in baseline.assignments
        if assignment.worker_id == absent_worker
        and source.snapshot_clock
        <= assignment.start_at
        < source.snapshot_clock + timedelta(minutes=source.profile.policy.freeze_window_min)
    }
    assert frozen and frozen <= recovery_operations(source, baseline)
    candidate = solve(
        source,
        baseline=baseline,
        time_limit=5,
        new_actions_not_before=(
            source.snapshot_clock + timedelta(minutes=review_minutes)
            if review_minutes is not None
            else None
        ),
    )
    assert candidate.has_solution and candidate.checker.status == "PASS"
    assert all(assignment.worker_id != absent_worker for assignment in candidate.assignments)


def test_zero_wip_resource_down_marks_frozen_dispatch_as_recovery():
    source, baseline = initial()
    assert not recovery_operations(source, baseline)
    stopped_resource = baseline.assignments[0].resource_id
    source = inject(
        source,
        event_id="zero-wip-resource-down",
        kind="resource.down",
        payload={"resource_id": stopped_resource},
    )
    assert not source.actuals
    assert baseline.assignments[0].operation_id in recovery_operations(source, baseline)


def test_outage_recovery_keeps_review_time_and_confirmed_history():
    source, baseline = disrupted()
    boundary = source.snapshot_clock + timedelta(minutes=15)
    candidate = solve(source, baseline=baseline, time_limit=5, new_actions_not_before=boundary)
    assert candidate.has_solution, candidate.native_status
    assert candidate.checker.status == "PASS", candidate.checker.issues
    for actual in source.actuals:
        assignment = next(a for a in candidate.assignments if a.operation_id == actual.operation_id)
        assert assignment.start_at == actual.actual_start
        assert assignment.resource_id == actual.resource_id
        assert assignment.resume_changeover_start >= boundary


def test_existing_blockage_stays_unchanged_while_reviewer_reads_new_plan():
    source, baseline = disrupted()
    candidate = solve(
        source,
        baseline=baseline,
        time_limit=5,
        new_actions_not_before=source.snapshot_clock + timedelta(minutes=15),
    )
    assert candidate.has_solution
    current = source
    changes = []
    for _ in range(7):
        after = advance(current, baseline)
        changes.append(batch(current, after))
        current = after
    result = check_revalidated_plan(source, current, candidate, baseline=baseline)
    assert result.report.status == "PASS", result.report.issues
    assert validate_progress_chain(source, current, changes, baseline=baseline, candidate=candidate)


def test_a_new_outage_during_review_is_not_treated_as_unchanged_blockage():
    source, baseline = disrupted()
    candidate = solve(
        source,
        baseline=baseline,
        time_limit=5,
        new_actions_not_before=source.snapshot_clock + timedelta(minutes=15),
    )
    resource = next(r for r in source.resources if not r.unavailable)
    current = inject(
        source,
        event_id="another-outage",
        kind="resource.outage",
        payload={"resource_id": resource.resource_id, "minutes": 25},
    )
    result = check_revalidated_plan(source, current, candidate, baseline=baseline)
    assert result.report.status == "FAIL"
    with pytest.raises(ProgressEvidenceError):
        validate_progress_chain(
            source,
            current,
            [batch(source, current, "resource.outage")],
            baseline=baseline,
            candidate=candidate,
        )


def _independent_lines():
    data = example_snapshot(second_product=True).model_dump(exclude={"content_hash"})
    data["profile"]["policy"]["progress_revalidation_enabled"] = True
    for field, key in (("resources", "resource_id"), ("workers", "worker_id")):
        data[field] = (*data[field], *({**r, key: r[key] + "-independent"} for r in data[field]))
    source = Snapshot.model_validate(data)
    assignments = tuple(
        a.model_copy(
            update={
                "resource_id": a.resource_id + "-independent",
                "worker_id": a.worker_id + "-independent",
                "changeover_start": a.start_at,
            }
        )
        if a.operation_id.startswith("order-b")
        else a
        for a in example_assignments(second_product=True)
    )
    baseline = make_candidate(source, assignments)
    assert check_candidate(source, baseline).status == "PASS"
    return source, assignments, baseline


def test_zero_wip_recovery_hint_keeps_unaffected_line_and_passes_checker():
    source, assignments, baseline = _independent_lines()
    source = evolve(source, active_plan_version="original", active_plan_hash=baseline.content_hash)
    source = inject(
        source,
        event_id="line-a-outage-before-start",
        kind="resource.outage",
        payload={"resource_id": "r1", "minutes": 20},
    )
    boundary = source.snapshot_clock + timedelta(minutes=15)
    hint = recovery_dispatch(source, baseline, new_actions_not_before=boundary)
    assert len(hint) == len(assignments)
    hinted = {assignment.operation_id: assignment for assignment in hint}
    for assignment in assignments:
        if assignment.operation_id.startswith("order-b"):
            assert hinted[assignment.operation_id] == assignment
    candidate = make_candidate(
        source,
        hint,
        objective=calculate_metrics(source, hint, baseline=baseline),
        schema_version="byof.candidate/3",
        new_actions_not_before=boundary,
    )
    report = check_candidate(source, candidate, baseline=baseline)
    assert report.status == "PASS", report.issues


def test_unaffected_line_stays_frozen_and_continues_during_recovery_review():
    source, assignments, baseline = _independent_lines()
    source = evolve(source, active_plan_version="original", active_plan_hash=baseline.content_hash)
    source = advance(source, baseline)
    source = inject(
        source,
        event_id="line-a-outage",
        kind="resource.outage",
        payload={"resource_id": "r1", "minutes": 20},
    )
    candidate = solve(
        source,
        baseline=baseline,
        time_limit=3,
        new_actions_not_before=source.snapshot_clock + timedelta(minutes=15),
    )
    assert candidate.has_solution and candidate.checker.status == "PASS"
    for assignment in assignments:
        if assignment.operation_id.startswith("order-b"):
            assert (
                next(a for a in candidate.assignments if a.operation_id == assignment.operation_id)
                == assignment
            )
    current, changes = source, []
    for _ in range(9):
        after = advance(current, baseline)
        changes.append(batch(current, after))
        current = after
    assert any(
        a.operation_id.startswith("order-b") and a.state == "COMPLETED" for a in current.actuals
    )
    checked = check_revalidated_plan(source, current, candidate, baseline=baseline)
    assert checked.report.status == "PASS", checked.report.issues
    assert validate_progress_chain(source, current, changes, baseline=baseline, candidate=candidate)
