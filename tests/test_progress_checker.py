"""Revalidation preserves approved physical intent and independently checks current execution."""

from copy import deepcopy
from datetime import timedelta

import pytest
from test_checker import (
    at,
    change_assignment,
    change_snapshot,
    example_assignments,
    example_snapshot,
    make_candidate,
)
from test_progress_evidence import batch
from test_solver_preferences import preference

from packages.domain.models import Candidate
from packages.planning.checker import calculate_metrics, check_candidate, check_revalidated_plan
from packages.planning.execution_projection import (
    first_changed_occupancy,
    remaining_actions,
    remaining_actions_hash,
)
from packages.planning.progress_evidence import validate_progress_chain
from packages.planning.solver import solve
from services.factory_sim.engine import advance, evolve, inject


def initial_case(*, enabled=True, second=False, freeze=0):
    data = example_snapshot(second_product=second).model_dump(exclude={"content_hash"})
    data["profile"]["policy"].update(
        progress_revalidation_enabled=enabled, freeze_window_min=freeze
    )
    for order in data["orders"]:
        order["due_at"] = at(30)
    from packages.domain.models import Snapshot

    initial = Snapshot.model_validate(data)
    baseline = make_candidate(initial, example_assignments(second_product=second))
    assert check_candidate(initial, baseline).status == "PASS"
    original = evolve(
        initial, active_plan_hash=baseline.content_hash, active_plan_version="accepted-baseline-1"
    )
    candidate = solve(original, baseline=baseline, time_limit=5)
    assert candidate.checker.status == "PASS"
    return original, baseline, candidate


def assert_rejected(original, current, candidate, baseline, code, *, objective=None):
    result = check_revalidated_plan(
        original, current, candidate, baseline=baseline, objective=objective
    )
    assert result.report.status == "FAIL", result
    assert code in {issue.code for issue in result.report.issues}, result.report
    assert result.assignments == () and result.metrics == () and result.remaining_plan_hash is None
    assert result.report.snapshot_hash == current.content_hash


@pytest.mark.parametrize("minutes", [1, 2, 3, 6])
def test_normal_prefix_keeps_actual_history_and_future_intent_without_reissuing_solver_proof(
    minutes,
):
    original, baseline, candidate = initial_case()
    current = advance(original, baseline, minutes=minutes)
    before = deepcopy(candidate.model_dump(mode="json"))
    original_hash, current_hash = original.content_hash, current.content_hash
    result = check_revalidated_plan(original, current, candidate, baseline=baseline)
    assert result.report.status == "PASS", result.report
    assert result.report.snapshot_hash == current.content_hash
    assert len(result.assignments) == 3
    actuals = {row.operation_id: row for row in current.actuals}
    old = {row.operation_id: row for row in candidate.assignments}
    for item in result.assignments:
        actual = actuals.get(item.operation_id)
        if actual is None:
            assert item == old[item.operation_id]
        elif actual.state == "COMPLETED":
            assert item.start_at == actual.actual_start and item.end_at == actual.actual_end
            assert item.changeover_start == actual.changeover_start
            assert item.resource_id == actual.resource_id and item.worker_id == actual.worker_id
            assert item.resume_at is None and item.resume_changeover_start is None
        else:
            assert item.start_at == actual.actual_start
            assert item.changeover_start == actual.changeover_start
            assert item.end_at == old[item.operation_id].end_at
            assert item.resume_changeover_start == current.snapshot_clock
            assert item.end_at - item.resume_at == actual.remaining_minutes * timedelta(minutes=1)
    future = remaining_actions(current, result.assignments)
    assert not (
        {row["operation_id"] for row in future}
        & {row.operation_id for row in current.actuals if row.state == "COMPLETED"}
    )
    if minutes == 6:
        assert future == ()
    assert result.remaining_plan_hash == remaining_actions_hash(current, result.assignments)
    assert result.metrics == calculate_metrics(current, result.assignments, baseline=baseline)
    assert all(metric.lower_bound is None for metric in result.metrics)
    assert candidate.proven_objective_levels == 5
    assert candidate.native_status == "OPTIMAL"
    assert candidate.model_dump(mode="json") == before
    assert original.content_hash == original_hash and current.content_hash == current_hash
    strict = check_candidate(current, candidate, baseline=baseline)
    assert strict.status == "FAIL"
    assert "VERSION_MISMATCH" in {issue.code for issue in strict.issues}


def test_partial_setup_preserves_historical_changeover_and_unstarted_production_time():
    original, baseline, candidate = initial_case(second=True)
    current = advance(original, baseline, minutes=3)
    setup = next(row for row in current.actuals if row.state == "SETUP")
    assert setup.actual_start is None and setup.remaining_setup_minutes == 4
    result = check_revalidated_plan(original, current, candidate, baseline=baseline)
    assert result.report.status == "PASS", result.report
    projected = next(row for row in result.assignments if row.operation_id == setup.operation_id)
    planned = next(row for row in candidate.assignments if row.operation_id == setup.operation_id)
    assert projected.changeover_start == setup.changeover_start == at(2)
    assert projected.resume_changeover_start == at(3)
    assert projected.start_at == projected.resume_at == planned.start_at == at(7)
    assert projected.end_at == planned.end_at == at(9)
    assert projected.resource_id == planned.resource_id and projected.worker_id == planned.worker_id
    assert setup.consumed == ()


def test_revalidation_from_an_existing_in_progress_snapshot_keeps_prior_segment_identity():
    original, baseline, _ = initial_case()
    original = advance(original, baseline, minutes=1)
    candidate = solve(original, baseline=baseline, time_limit=5)
    current = advance(original, baseline, minutes=2)
    result = check_revalidated_plan(original, current, candidate, baseline=baseline)
    assert result.report.status == "PASS", result.report
    first = next(row for row in current.actuals if row.actual_start == at(0))
    assert first.segments[0].source_event_id == original.actuals[0].segments[0].source_event_id
    assert result.assignments[0].start_at == at(0) and result.assignments[0].end_at == at(2)
    assert result.assignments[1].resume_at == at(3)


@pytest.mark.parametrize("ticks", [1, 4, 5])
def test_resumed_changeover_keeps_preinterruption_start_and_segments(ticks):
    source, baseline, _ = initial_case(second=True)
    source = advance(source, baseline)
    interrupted = source.actuals[0]
    source = inject(
        source,
        event_id="first-product-interrupted",
        kind="resource.down",
        payload={"resource_id": interrupted.resource_id},
    )
    source = inject(
        source,
        event_id="resource-restored-before-confirmation",
        kind="resource.restore",
        payload={"resource_id": interrupted.resource_id},
    )
    source = advance(source, baseline, minutes=8)
    assert source.snapshot_clock == at(9)
    assert any(
        row.operation_id != interrupted.operation_id
        and row.resource_id == interrupted.resource_id
        and row.state == "COMPLETED"
        for row in source.actuals
    )
    source = inject(
        source,
        event_id="confirmed-after-other-product",
        kind="execution.confirm_remaining",
        payload={
            "operation_id": interrupted.operation_id,
            "remaining_minutes": interrupted.remaining_minutes,
            "remaining_setup_minutes": 0,
        },
    )
    resumed_baseline = solve(source, baseline=baseline, time_limit=5)
    assert resumed_baseline.checker.status == "PASS", resumed_baseline.checker
    resumed = next(
        row for row in resumed_baseline.assignments if row.operation_id == interrupted.operation_id
    )
    assert resumed.resume_at - resumed.resume_changeover_start == timedelta(minutes=5)
    source = evolve(
        source,
        active_plan_version="accepted-resumed-plan",
        active_plan_hash=resumed_baseline.content_hash,
    )
    original = advance(source, resumed_baseline)
    old = next(row for row in original.actuals if row.operation_id == interrupted.operation_id)
    assert old.state == "IN_PROGRESS" and old.remaining_setup_minutes == 4
    assert old.actual_start == interrupted.actual_start == at(0)
    assert [(row.phase, row.start_at, row.end_at) for row in old.segments] == [
        ("PRODUCTION", at(0), at(1)),
        ("SETUP", at(9), at(10)),
    ]
    candidate = solve(original, baseline=resumed_baseline, time_limit=5)
    assert candidate.checker.status == "PASS", candidate.checker
    current, changes = original, []
    for _ in range(ticks):
        after = advance(current, resumed_baseline)
        changes.append(batch(current, after))
        current = after
    assert (
        len(
            validate_progress_chain(
                original, current, changes, baseline=resumed_baseline, candidate=candidate
            )
        )
        == 64
    )
    result = check_revalidated_plan(original, current, candidate, baseline=resumed_baseline)
    assert result.report.status == "PASS", result.report
    actual = next(row for row in current.actuals if row.operation_id == interrupted.operation_id)
    projected = next(
        row for row in result.assignments if row.operation_id == interrupted.operation_id
    )
    assert projected.start_at == interrupted.actual_start
    assert projected.changeover_start == interrupted.changeover_start
    assert actual.consumed == interrupted.consumed
    assert actual.segments[0] == interrupted.segments[0]
    assert actual.state == ("COMPLETED" if ticks == 5 else "IN_PROGRESS")
    if ticks < 5:
        assert projected.resume_at == at(14)
        assert projected.resume_changeover_start == current.snapshot_clock
    else:
        assert projected.resume_at is None and projected.end_at == actual.actual_end == at(15)


def test_policy_only_extends_new_candidate_window_and_default_behavior_is_unchanged():
    for enabled in (False, True):
        original, baseline, candidate = initial_case(enabled=enabled)
        assert candidate.accept_before == (original.horizon.end_at if enabled else at(1))
        assert candidate.assignments == baseline.assignments
    old = candidate.model_dump(exclude={"content_hash"})
    old["accept_before"] = at(1)
    already_approved_short_window = Candidate.model_validate(old)
    current = advance(original, baseline, minutes=1)
    assert_rejected(original, current, already_approved_short_window, baseline, "STALE_TIME")


def changed_future_case():
    original, _, _ = initial_case()
    initial = change_snapshot(
        original, lambda data: data.update(active_plan_hash=None, active_plan_version=None)
    )
    delayed = change_assignment(
        example_assignments(), 2, changeover_start=at(14), start_at=at(14), end_at=at(16)
    )
    baseline = make_candidate(initial, delayed)
    original = evolve(
        initial, active_plan_hash=baseline.content_hash, active_plan_version="delayed-baseline"
    )
    objective = preference(
        original,
        "custom",
        objective_order=(
            "makespan",
            "weighted_tardiness",
            "incremental_overtime_metric",
            "changed_operations",
            "total_start_shift",
        ),
        max_weighted_tardiness=0,
        max_incremental_overtime_minutes=0,
    )
    candidate = solve(original, baseline=baseline, objective=objective, time_limit=5)
    assert candidate.checker.status == "PASS"
    return original, baseline, candidate, objective


def test_new_window_is_bound_to_first_changed_action_and_fresh_metrics_follow_objective_order():
    original, baseline, candidate, objective = changed_future_case()
    assert candidate.assignments[:2] == baseline.assignments[:2]
    assert candidate.assignments[-1].start_at == at(4)
    assert first_changed_occupancy(original, candidate.assignments, baseline) == at(4)
    assert candidate.accept_before == at(5)
    current = advance(original, baseline, minutes=3)
    result = check_revalidated_plan(
        original, current, candidate, baseline=baseline, objective=objective
    )
    assert result.report.status == "PASS", result.report
    assert result.assignments[-1] == candidate.assignments[-1]
    assert tuple(metric.name for metric in result.metrics) == objective.order
    assert result.metrics[0].value == 6 and result.metrics[0].lower_bound is None
    expired = advance(original, baseline, minutes=5)
    assert_rejected(original, expired, candidate, baseline, "STALE_TIME", objective=objective)


def test_changed_objective_contract_cannot_be_substituted_during_revalidation():
    original, baseline, candidate, objective = changed_future_case()
    current = advance(original, baseline, minutes=1)
    different = type(objective).model_validate(
        {**objective.model_dump(exclude={"content_hash"}), "resolution_version": 99}
    )
    assert_rejected(original, current, candidate, baseline, "VERSION_MISMATCH", objective=different)
    assert_rejected(original, current, candidate, baseline, "VERSION_MISMATCH")


def test_prefix_that_old_plan_executed_differently_from_candidate_is_not_rewritten():
    original, baseline, _ = initial_case()
    delayed = change_assignment(
        example_assignments(), 0, changeover_start=at(1), start_at=at(1), end_at=at(3)
    )
    delayed = change_assignment(delayed, 1, changeover_start=at(3), start_at=at(3), end_at=at(5))
    delayed = change_assignment(delayed, 2, changeover_start=at(5), start_at=at(5), end_at=at(7))
    candidate = make_candidate(
        original,
        delayed,
        objective=calculate_metrics(original, delayed, baseline=baseline),
        accept_before=at(2),
    )
    assert check_candidate(original, candidate, baseline=baseline).status == "PASS"
    current = advance(original, baseline, minutes=1)
    assert_rejected(original, current, candidate, baseline, "APPROVED_PREFIX_CHANGED")


def test_freeze_that_now_protects_an_approved_change_still_enters_complete_checker():
    original, baseline, _ = initial_case(freeze=4)
    moved = change_assignment(
        example_assignments(), 2, changeover_start=at(5), start_at=at(5), end_at=at(7)
    )
    candidate = make_candidate(
        original,
        moved,
        objective=calculate_metrics(original, moved, baseline=baseline),
        accept_before=at(6),
    )
    assert check_candidate(original, candidate, baseline=baseline).status == "PASS"
    current = advance(original, baseline, minutes=3)
    assert_rejected(original, current, candidate, baseline, "FROZEN_OPERATION_CHANGED")


@pytest.mark.parametrize(
    "kind,code",
    [
        ("stock", "REVALIDATION_INVENTORY_CHANGED"),
        ("worker", "REVALIDATION_FACTS_CHANGED"),
        ("resource", "REVALIDATION_RESOURCES_CHANGED"),
        ("order", "REVALIDATION_ORDERS_CHANGED"),
        ("receipt", "REVALIDATION_FACTS_CHANGED"),
        ("scope", "REVALIDATION_SCOPE_CHANGED"),
        ("profile", "REVALIDATION_SCOPE_CHANGED"),
        ("baseline", "BASELINE_MISMATCH"),
        ("source", "REVALIDATION_SOURCE_CHANGED"),
        ("status", "NON_NORMAL_ORDER_PROGRESS"),
        ("setup_pointer", "NON_NORMAL_RESOURCE_SETUP"),
    ],
)
def test_normal_progress_label_cannot_hide_new_scope_or_other_changed_business_facts(kind, code):
    original, baseline, candidate = initial_case()
    current = advance(original, baseline, minutes=1)

    def change(data):
        if kind == "stock":
            data["inventory"][0]["on_hand"] += 1
        elif kind == "worker":
            data["workers"][0]["overtime_available"] = False
        elif kind == "resource":
            data["resources"][0]["status"] = "DOWN"
        elif kind == "order":
            data["orders"].append(
                {**data["orders"][0], "order_id": "new-urgent-order", "status": "CONFIRMED"}
            )
        elif kind == "receipt":
            data["receipts"].append(
                {
                    "receipt_id": "unapproved-receipt",
                    "material_id": "shared-part",
                    "unit": "EA",
                    "quantity": 1,
                    "eta": at(10),
                    "status": "CONFIRMED",
                }
            )
        elif kind == "scope":
            data["scope_version"] += 1
        elif kind == "profile":
            data["profile"]["routes"][0]["cycle_sec_per_unit"] = 120
        elif kind == "baseline":
            data["active_plan_hash"] = "a" * 64
        elif kind == "source":
            data["source"]["source_system"] = "another-source"
        elif kind == "status":
            data["orders"][0]["status"] = "COMPLETED"
        else:
            data["resources"][1].update(
                last_operation_id=baseline.assignments[0].operation_id, last_product_id="item-a"
            )

    current = change_snapshot(current, change)
    assert_rejected(original, current, candidate, baseline, code)


@pytest.mark.parametrize(
    "kind,code",
    [
        ("blocked", "NON_NORMAL_EXECUTION"),
        ("remaining", "NON_NORMAL_EXECUTION"),
        ("quality", "NON_NORMAL_EXECUTION"),
        ("segments", "NON_NORMAL_EXECUTION_SEGMENTS"),
        ("worker", "BASELINE_EXECUTION_CHANGED"),
    ],
)
def test_unknown_interrupted_or_changed_execution_never_becomes_a_continuation(kind, code):
    original, baseline, candidate = initial_case()
    current = advance(original, baseline, minutes=1)

    def change(data):
        actual = data["actuals"][0]
        if kind == "blocked":
            actual["state"] = "BLOCKED"
        elif kind == "remaining":
            actual.update(remaining_minutes=None, remaining_confirmed_by=None)
        elif kind == "quality":
            actual["quality_state"] = "FAILED"
        elif kind == "segments":
            actual["segments"] = []
        else:
            actual["worker_id"] = "w2"

    current = change_snapshot(current, change)
    assert_rejected(original, current, candidate, baseline, code)


def test_preexisting_actual_and_consumption_history_cannot_be_replaced_with_same_quantity():
    original, baseline, _ = initial_case()
    original = advance(original, baseline, minutes=1)
    candidate = solve(original, time_limit=5, baseline=baseline)
    current = advance(original, baseline, minutes=2)
    current = change_snapshot(
        current,
        lambda data: data["actuals"][0]["segments"][0].update(
            source_event_id="replacement-history"
        ),
    )
    assert_rejected(original, current, candidate, baseline, "ACTUAL_HISTORY_CHANGED")


def test_missing_expected_execution_and_changed_completed_history_fail_closed():
    original, baseline, candidate = initial_case()
    missing = evolve(original, snapshot_clock=at(1))
    assert_rejected(original, missing, candidate, baseline, "EXPECTED_PREFIX_MISSING")
    original = advance(original, baseline, minutes=2)
    candidate = solve(original, baseline=baseline, time_limit=5)
    current = advance(original, baseline, minutes=1)
    current = change_snapshot(
        current, lambda data: data["actuals"][0].update(quality_state="UNKNOWN")
    )
    assert_rejected(original, current, candidate, baseline, "ACTUAL_HISTORY_CHANGED")


def test_policy_disabled_and_tampered_original_candidate_cannot_use_revalidation():
    original, baseline, candidate = initial_case(enabled=False)
    current = advance(original, baseline, minutes=1)
    assert_rejected(original, current, candidate, baseline, "PROGRESS_REVALIDATION_DISABLED")
    original, baseline, candidate = initial_case()
    data = candidate.model_dump(exclude={"content_hash"})
    data["assignments"][0]["end_at"] = at(1)
    tampered = Candidate.model_validate(data)
    assert tampered.checker.status == "PASS"
    current = advance(original, baseline, minutes=1)
    assert_rejected(original, current, tampered, baseline, "WRONG_DURATION")
