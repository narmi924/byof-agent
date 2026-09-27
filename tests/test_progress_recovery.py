"""Confirmed recovery advances independently of an obsolete active plan's end times."""

from copy import deepcopy
from typing import cast

import pytest
from sqlalchemy.orm import Session

from packages.domain.models import Assignment
from packages.planning.checker import calculate_metrics, check_candidate, check_revalidated_plan
from packages.planning.execution_projection import first_changed_occupancy
from packages.planning.progress_evidence import ProgressEvidenceError, validate_progress_chain
from packages.planning.solver import solve
from services.factory_sim.engine import SimulationError, advance, evolve, inject
from services.factory_sim.service import _write
from services.factory_sim.storage import World
from tests.test_checker import (
    at,
    change_assignment,
    change_snapshot,
    example_snapshot,
    make_candidate,
)
from tests.test_progress_evidence import Collector, batch


def recovered_case(*, setup=0, check_start=8):
    def configure(data):
        data["profile"]["version"] = "confirmed-recovery-example-1"
        data["profile"]["policy"].update(
            policy_version="confirmed-recovery-policy-1",
            freeze_window_min=0,
            progress_revalidation_enabled=True,
        )
        data["profile"]["routes"][0]["cycle_sec_per_unit"] = 240

    initial = change_snapshot(example_snapshot(), configure)
    assignments = tuple(
        Assignment(
            operation_id=f"order-a-R001-B001-{code}",
            resource_id=f"r{index}",
            worker_id=f"w{index}",
            changeover_start=at(start),
            start_at=at(start),
            end_at=at(end),
        )
        for index, (code, start, end) in enumerate(
            (
                ("begin", 0, 8),
                ("check", check_start, check_start + 2),
                ("finish", check_start + 2, check_start + 4),
            ),
            1,
        )
    )
    baseline = make_candidate(initial, assignments)
    assert check_candidate(initial, baseline).status == "PASS"
    source = evolve(initial, active_plan_hash=baseline.content_hash, active_plan_version="active-1")
    source = advance(source, baseline, minutes=2)
    first = source.actuals[0]
    assert first.remaining_minutes == 6
    source = inject(source, event_id="down", kind="resource.down", payload={"resource_id": "r1"})
    source = advance(source, baseline, minutes=12)
    source = inject(
        source, event_id="restored", kind="resource.restore", payload={"resource_id": "r1"}
    )
    source = inject(
        source,
        event_id="confirmed",
        kind="execution.confirm_remaining",
        payload={
            "operation_id": first.operation_id,
            "remaining_minutes": 6,
            "remaining_setup_minutes": setup,
        },
    )
    original = advance(source, baseline)
    actual = original.actuals[0]
    assert original.snapshot_clock == at(15)
    assert actual.state == "IN_PROGRESS" and actual.actual_start == at(0)
    assert len(original.actuals) == 1
    end = 20 + setup
    planned = (
        Assignment(
            operation_id=actual.operation_id,
            resource_id=actual.resource_id,
            worker_id=actual.worker_id,
            changeover_start=actual.changeover_start,
            start_at=actual.actual_start,
            resume_changeover_start=at(15),
            resume_at=at(14 + setup) if setup else at(15),
            end_at=at(end),
        ),
        *(
            Assignment(
                **{
                    **row.model_dump(),
                    "changeover_start": at(end + offset),
                    "start_at": at(end + offset),
                    "end_at": at(end + offset + 2),
                }
            )
            for row, offset in zip(baseline.assignments[1:], (2, 4))
        ),
    )
    candidate = make_candidate(
        original,
        planned,
        candidate_id="reviewable-recovery",
        objective=calculate_metrics(original, planned, baseline=baseline),
        accept_before=at(end + 3),
    )
    report = check_candidate(original, candidate, baseline=baseline)
    assert report.status == "PASS", report
    return original, baseline, candidate


def progress(original, baseline, ticks):
    current, changes = original, []
    for _ in range(ticks):
        after = advance(current, baseline)
        collector = Collector()
        _write(
            cast(Session, collector),
            World(factory_id=current.factory_id),
            current,
            after,
            "clock.tick",
            plan=baseline,
        )
        assert len(collector.rows) == 1
        changes.append(collector.rows[0].document)
        current = after
    return current, changes


@pytest.mark.parametrize("setup,ticks", [(0, 1), (0, 3), (0, 5), (0, 6), (3, 1), (3, 2), (3, 8)])
def test_recovered_confirmed_work_proves_multiple_ticks_without_moving_future_candidate_actions(
    setup, ticks
):
    original, baseline, candidate = recovered_case(setup=setup)
    saved = deepcopy(candidate.model_dump(mode="json"))
    assert baseline.assignments[0].end_at == at(8)
    assert first_changed_occupancy(original, candidate.assignments, baseline) == at(22 + setup)
    assert candidate.accept_before > original.snapshot_clock
    current, changes = progress(original, baseline, ticks)
    assert (
        len(
            validate_progress_chain(
                original, current, changes, baseline=baseline, candidate=candidate
            )
        )
        == 64
    )
    checked = check_revalidated_plan(original, current, candidate, baseline=baseline)
    assert checked.report.status == "PASS", checked.report
    assert checked.assignments[1:] == candidate.assignments[1:]
    assert candidate.model_dump(mode="json") == saved
    assert current.active_plan_hash == baseline.content_hash
    assert current.inventory == original.inventory and current.reservations == original.reservations
    assert len(current.actuals) == 1
    actual = current.actuals[0]
    assert actual.actual_start == original.actuals[0].actual_start == at(0)
    assert actual.consumed == original.actuals[0].consumed
    assert actual.segments[0] == original.actuals[0].segments[0]
    assert checked.assignments[0].end_at == candidate.assignments[0].end_at == at(20 + setup)
    if ticks >= 5 + setup:
        assert actual.state == "COMPLETED" and actual.actual_end == at(20 + setup)
        assert checked.assignments[0].resume_at is None
    else:
        assert actual.state == "IN_PROGRESS"
        assert checked.assignments[0].resume_changeover_start == current.snapshot_clock


def test_already_completed_recovery_history_is_authoritative_despite_obsolete_baseline_end():
    source, baseline, prior_candidate = recovered_case()
    original, _ = progress(source, baseline, 5)
    assert original.actuals[0].actual_end == at(20)
    assignment = prior_candidate.assignments[0].model_copy(
        update={"resume_at": None, "resume_changeover_start": None}
    )
    planned = (assignment, *prior_candidate.assignments[1:])
    candidate = make_candidate(
        original,
        planned,
        objective=calculate_metrics(original, planned, baseline=baseline),
        accept_before=at(23),
    )
    assert check_candidate(original, candidate, baseline=baseline).status == "PASS"
    current, changes = progress(original, baseline, 1)
    assert validate_progress_chain(
        original, current, changes, baseline=baseline, candidate=candidate
    )
    checked = check_revalidated_plan(original, current, candidate, baseline=baseline)
    assert checked.report.status == "PASS", checked.report
    assert checked.assignments[0] == assignment
    assert current.actuals == original.actuals


@pytest.mark.parametrize(
    "kind", ["remaining", "segments", "stock", "worker", "quality", "new_fault"]
)
def test_recovered_anchor_does_not_hide_subsequent_adverse_or_unproven_changes(kind):
    original, baseline, candidate = recovered_case()
    current, _ = progress(original, baseline, 1)
    if kind == "new_fault":
        current = inject(
            current, event_id="new-down", kind="resource.down", payload={"resource_id": "r1"}
        )
    else:

        def mutate(data):
            if kind == "remaining":
                data["actuals"][0]["remaining_minutes"] += 1
            elif kind == "segments":
                data["actuals"][0]["segments"][-1]["start_at"] = at(15).isoformat()
            elif kind == "stock":
                data["inventory"][0]["on_hand"] += 1
            elif kind == "worker":
                data["workers"][0]["calendar"][0]["end_at"] = at(29).isoformat()
            else:
                data["actuals"][0]["quality_state"] = "UNKNOWN"

        current = change_snapshot(current, mutate)
    with pytest.raises(ProgressEvidenceError):
        validate_progress_chain(
            original, current, [batch(original, current)], baseline=baseline, candidate=candidate
        )
    checked = check_revalidated_plan(original, current, candidate, baseline=baseline)
    assert checked.report.status == "FAIL"
    assert checked.assignments == () and checked.remaining_plan_hash is None


def test_expired_candidate_is_not_extended_after_recovery_and_future_action_mutation_is_detected():
    original, baseline, candidate = recovered_case()
    moved = change_assignment(
        candidate.assignments, 1, changeover_start=at(21), start_at=at(21), end_at=at(23)
    )
    assert first_changed_occupancy(original, moved, baseline) == at(21)
    expired = type(candidate).model_validate(
        {**candidate.model_dump(exclude={"content_hash"}), "accept_before": at(16)}
    )
    current, _ = progress(original, baseline, 1)
    checked = check_revalidated_plan(original, current, expired, baseline=baseline)
    assert checked.report.status == "FAIL"
    assert {issue.code for issue in checked.report.issues} == {"STALE_TIME"}


def test_first_missed_dispatch_after_anchor_remains_material_even_while_wip_progresses_normally():
    original, baseline, candidate = recovered_case(check_start=16)
    current, changes = progress(original, baseline, 2)
    assert current.actuals[0].remaining_minutes == 3 and len(current.actuals) == 1
    missed = [
        event
        for change in changes
        for event in change["events"]
        if event["event_type"] == "execution.dispatch_missed"
    ]
    assert len(missed) == 1 and missed[0]["entity_id"] == baseline.assignments[1].operation_id
    with pytest.raises(ProgressEvidenceError, match="BASELINE_PROGRESS_MISSING"):
        validate_progress_chain(original, current, changes, baseline=baseline, candidate=candidate)
    checked = check_revalidated_plan(original, current, candidate, baseline=baseline)
    assert checked.report.status == "FAIL"
    assert {issue.code for issue in checked.report.issues} == {"EXPECTED_PREFIX_MISSING"}


def test_invented_late_start_cannot_be_accepted_as_normal_execution_of_old_plan():
    original, baseline, candidate = recovered_case()
    before, changes = progress(original, baseline, 5)
    assert before.actuals[0].state == "COMPLETED" and len(before.actuals) == 1
    moved = change_assignment(
        candidate.assignments, 1, changeover_start=at(20), start_at=at(20), end_at=at(22)
    )
    unaccepted = make_candidate(original, moved)
    with pytest.raises(SimulationError, match="EXECUTION_PLAN_MISMATCH"):
        advance(before, unaccepted)
    # Forge a source claim from another plan's actuals; valid hashes cannot authorize its late start.
    alternative = change_snapshot(
        before, lambda data: data.update(active_plan_hash=unaccepted.content_hash)
    )
    current = change_snapshot(
        advance(alternative, unaccepted),
        lambda data: data.update(active_plan_hash=baseline.content_hash),
    )
    assert len(current.actuals) == 2 and current.actuals[1].actual_start == at(20)
    assert current.active_plan_hash == baseline.content_hash
    changes.append(batch(before, current))
    with pytest.raises(ProgressEvidenceError):
        validate_progress_chain(original, current, changes, baseline=baseline, candidate=candidate)
    checked = check_revalidated_plan(original, current, candidate, baseline=baseline)
    assert checked.report.status == "FAIL"
    assert "APPROVED_PREFIX_CHANGED" in {issue.code for issue in checked.report.issues}


def test_real_solver_review_boundary_survives_recovery_completion_and_multiple_normal_ticks():
    original, baseline, _ = recovered_case()
    candidate = solve(original, baseline=baseline, time_limit=3, new_actions_not_before=at(22))
    assert candidate.has_solution and candidate.checker.status == "PASS", candidate.checker
    assert candidate.new_actions_not_before == at(22)
    assert candidate.accept_before > at(22)
    assert candidate.assignments[0].resume_changeover_start == at(15)
    assert candidate.assignments[0].end_at == at(20)
    current, changes = progress(original, baseline, 6)
    assert validate_progress_chain(
        original, current, changes, baseline=baseline, candidate=candidate
    )
    checked = check_revalidated_plan(original, current, candidate, baseline=baseline)
    assert checked.report.status == "PASS", checked.report
    assert current.actuals[0].actual_end == at(20) and len(current.actuals) == 1
    assert checked.assignments[1:] == candidate.assignments[1:]
