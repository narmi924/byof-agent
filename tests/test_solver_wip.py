"""Replanning keeps physical history, material reservations and protected future work."""

from datetime import timedelta

import pytest

from packages.domain.models import Snapshot, batch_operations, minute_offset
from packages.domain.skf import load_skf_snapshot
from packages.planning.checker import calculate_metrics
from packages.planning.disruption import recovery_operations
from packages.planning.solver import PlanningInputError, solve
from services.factory_sim.engine import advance, evolve, inject


def initial(*, first_changeover=0, root_release=0):
    data = load_skf_snapshot(development=True).model_dump(mode="json", exclude={"content_hash"})
    data["schema_version"] = "byof.snapshot/2"
    data["source"]["source_revision"] = "1"
    data["profile"]["policy"]["first_changeover_min"] = first_changeover
    source = Snapshot.model_validate(data)
    if root_release:
        root_type = next(s.resource_type for s in source.profile.routes if not s.predecessors)
        resource = next(r for r in data["resources"] if r["resource_type"] == root_type)
        resource["unavailable"] = [
            {
                "start_at": source.snapshot_clock,
                "end_at": source.snapshot_clock + timedelta(minutes=root_release),
            }
        ]
        source = Snapshot.model_validate(data)
    baseline = solve(source, time_limit=5)
    assert baseline.has_solution and baseline.checker.status == "PASS"
    return evolve(
        source, active_plan_version="plan-1", active_plan_hash=baseline.content_hash
    ), baseline


def checked(snapshot, baseline, *, allow_overtime=False):
    before = snapshot.model_dump_json()
    result = solve(snapshot, baseline=baseline, time_limit=5, allow_overtime=allow_overtime)
    assert result.has_solution, result.native_status
    assert result.checker.status == "PASS", result.checker.issues
    assert len(result.assignments) == len(batch_operations(snapshot)[1])
    assert snapshot.model_dump_json() == before
    expected = calculate_metrics(snapshot, result.assignments, baseline=baseline)
    assert [m.value for m in result.objective] == [m.value for m in expected]
    return result


def test_normal_wip_preserves_history_and_uses_only_confirmed_remaining_work():
    source, baseline = initial()
    source = advance(source, baseline, minutes=12)
    candidate = checked(source, baseline)
    by_id = {a.operation_id: a for a in candidate.assignments}
    for actual in source.actuals:
        assignment = by_id[actual.operation_id]
        assert assignment.start_at == actual.actual_start
        assert assignment.changeover_start == actual.changeover_start
        assert assignment.resource_id == actual.resource_id
        assert assignment.worker_id == actual.worker_id
        if actual.state == "COMPLETED":
            assert assignment.end_at == actual.actual_end
            assert assignment.resume_at is None
        else:
            assert assignment.resume_changeover_start == source.snapshot_clock
            assert assignment.end_at - assignment.resume_at == timedelta(
                minutes=actual.remaining_minutes
            )
    assert [m.value for m in candidate.objective] == [0, 0, 0, 0, 66]
    assert candidate.binding.baseline_plan_version == source.active_plan_version


def test_started_batch_does_not_allocate_or_consume_its_full_kit_again():
    source, baseline = initial()
    source = advance(source, baseline, minutes=12)
    inventory = [i.model_copy(update={"on_hand": i.reserved}) for i in source.inventory]
    source = evolve(source, inventory=inventory, receipts=())
    candidate = checked(source, baseline)
    assert candidate.checker.status == "PASS"
    assert all(i.on_hand == i.reserved for i in source.inventory)
    assert source.reservations and any(a.consumed for a in source.actuals)


def test_completed_history_is_not_rechecked_against_current_machine_downtime():
    source, baseline = initial()
    source = advance(source, baseline, minutes=12)
    completed = next(a for a in source.actuals if a.state == "COMPLETED")
    source = inject(
        source,
        event_id="past-machine-down",
        kind="resource.down",
        payload={"resource_id": completed.resource_id},
    )
    candidate = checked(source, baseline)
    old = next(a for a in candidate.assignments if a.operation_id == completed.operation_id)
    assert old.end_at == completed.actual_end


def test_setup_is_execution_history_and_retains_unfinished_setup_minutes():
    source, baseline = initial(first_changeover=5)
    source = advance(source, baseline, minutes=2)
    actual = source.actuals[0]
    assert actual.state == "SETUP" and actual.actual_start is None
    candidate = checked(source, baseline)
    assignment = next(a for a in candidate.assignments if a.operation_id == actual.operation_id)
    assert assignment.changeover_start == actual.changeover_start
    assert assignment.resume_changeover_start == source.snapshot_clock
    assert assignment.resume_at == assignment.start_at
    assert assignment.resume_at - assignment.resume_changeover_start == timedelta(minutes=3)
    assert not source.reservations and not actual.consumed
    assert [m.value for m in candidate.objective[2:4]] == [0, 0]


def test_unknown_remaining_is_a_confirmation_request_until_source_confirms_it():
    source, baseline = initial()
    source = advance(source, baseline, minutes=12)
    running = next(a for a in source.actuals if a.state == "IN_PROGRESS")
    source = inject(
        source, event_id="down", kind="resource.down", payload={"resource_id": running.resource_id}
    )
    with pytest.raises(PlanningInputError, match="WIP_CONFIRMATION_REQUIRED"):
        solve(source, baseline=baseline)
    source = inject(
        source,
        event_id="restored",
        kind="resource.restore",
        payload={"resource_id": running.resource_id},
    )
    with pytest.raises(PlanningInputError, match="WIP_CONFIRMATION_REQUIRED"):
        solve(source, baseline=baseline)
    source = advance(source, baseline, minutes=75)
    source = inject(
        source,
        event_id="remaining-verified",
        kind="execution.confirm_remaining",
        payload={
            "operation_id": running.operation_id,
            "remaining_minutes": running.remaining_minutes,
            "remaining_setup_minutes": 0,
        },
    )
    candidate = checked(source, baseline)
    resumed = next(a for a in candidate.assignments if a.operation_id == running.operation_id)
    assert resumed.start_at == running.actual_start
    assert resumed.resume_at >= source.snapshot_clock
    assert resumed.end_at - resumed.resume_at == timedelta(minutes=running.remaining_minutes)


@pytest.mark.parametrize("quality", ["UNKNOWN", "FAILED"])
def test_completed_quality_gate_requires_a_passing_source_result(quality):
    source, baseline = initial()
    gate = next(a for a in baseline.assignments if a.operation_id.endswith("OP60"))
    source = advance(
        source, baseline, minutes=minute_offset(source.snapshot_clock, gate.end_at, round_up=True)
    )
    source = inject(
        source,
        event_id="quality-unresolved",
        kind="quality.record",
        payload={"operation_id": gate.operation_id, "quality_state": quality},
    )
    with pytest.raises(PlanningInputError, match="QUALITY_CONFIRMATION_REQUIRED"):
        solve(source, baseline=baseline)


@pytest.mark.parametrize("root_release", [0, 59, 60])
def test_broken_frozen_dispatch_can_move_to_available_equipment(root_release):
    source, baseline = initial(root_release=root_release)
    root = next(a for a in baseline.assignments if a.operation_id.endswith("OP10"))
    original = next(r for r in source.resources if r.resource_id == root.resource_id)
    alternative = original.model_copy(update={"resource_id": "alternate-kit", "unavailable": ()})
    resources = [
        r.model_copy(update={"status": "DOWN"}) if r.resource_id == original.resource_id else r
        for r in source.resources
    ]
    source = evolve(source, resources=(*resources, alternative))
    assert root.operation_id in recovery_operations(source, baseline)
    candidate = solve(source, baseline=baseline, time_limit=5)
    assert candidate.has_solution and candidate.checker.status == "PASS", candidate.checker.issues
    root_assignment = next(a for a in candidate.assignments if a.operation_id == root.operation_id)
    assert root_assignment.resource_id == alternative.resource_id


def test_baseline_must_match_the_active_source_hash():
    source, baseline = initial()
    altered = baseline.model_dump(exclude={"content_hash"})
    altered["candidate_id"] = "different-candidate"
    changed = type(baseline).model_validate(altered)
    with pytest.raises(PlanningInputError, match="BASELINE_MISMATCH"):
        solve(source, baseline=changed)


def test_confirming_remaining_work_does_not_restore_a_failed_machine():
    source, baseline = initial()
    source = advance(source, baseline, minutes=2)
    actual = source.actuals[0]
    source = inject(
        source, event_id="down", kind="resource.down", payload={"resource_id": actual.resource_id}
    )
    source = inject(
        source,
        event_id="remaining",
        kind="execution.confirm_remaining",
        payload={
            "operation_id": actual.operation_id,
            "remaining_minutes": actual.remaining_minutes,
            "remaining_setup_minutes": 0,
        },
    )
    candidate = solve(source, baseline=baseline, time_limit=5)
    assert candidate.native_status == "INFEASIBLE" and not candidate.has_solution
    assert not candidate.assignments


def test_blocked_work_can_follow_an_urgent_other_product_with_real_changeover():
    source, baseline = initial()
    source = advance(source, baseline, minutes=2)
    blocked = source.actuals[0]
    source = inject(
        source, event_id="down", kind="resource.down", payload={"resource_id": blocked.resource_id}
    )
    source = advance(source, baseline, minutes=78)
    source = inject(
        source,
        event_id="restore",
        kind="resource.restore",
        payload={"resource_id": blocked.resource_id},
    )
    source = inject(
        source,
        event_id="remaining",
        kind="execution.confirm_remaining",
        payload={
            "operation_id": blocked.operation_id,
            "remaining_minutes": 30,
            "remaining_setup_minutes": 0,
        },
    )
    other_product = next(
        p for p in source.profile.products if p.product_id != source.orders[0].product_id
    )
    source = inject(
        source,
        event_id="urgent",
        kind="order.add",
        payload={
            "order_id": "urgent-other-product",
            "product_id": other_product.product_id,
            "quantity": 50,
            "due_at": source.snapshot_clock + timedelta(minutes=80),
            "priority_weight": 10,
            "hard_deadline": True,
            "version": 1,
        },
    )
    candidate = checked(source, baseline)
    resumed = next(a for a in candidate.assignments if a.operation_id == blocked.operation_id)
    preceding = next(
        a
        for a in candidate.assignments
        if a.operation_id.startswith("urgent-other-product")
        and a.resource_id == resumed.resource_id
    )
    assert preceding.end_at <= resumed.resume_changeover_start
    assert resumed.resume_at - resumed.resume_changeover_start == timedelta(
        minutes=source.profile.policy.different_product_changeover_min
    )
    assert resumed.start_at == blocked.actual_start
    assert len(candidate.assignments) == 16


def test_fully_completed_scope_returns_actual_history_without_future_actions():
    source, baseline = initial()
    source = advance(source, baseline, minutes=70)
    assert len(source.actuals) == 8 and all(a.state == "COMPLETED" for a in source.actuals)
    candidate = checked(source, baseline)
    assert all(a.resume_at is None for a in candidate.assignments)
    assert [m.value for m in candidate.objective[2:4]] == [0, 0]


def overtime_initial():
    source = load_skf_snapshot(development=True)
    origin = source.horizon.start_at
    data = source.model_dump(mode="python", exclude={"content_hash"})
    data["schema_version"] = "byof.snapshot/2"
    data["source"]["source_revision"] = "1"
    data["horizon"]["end_at"] = origin + timedelta(minutes=180)
    windows = [
        {"start_at": origin, "end_at": origin + timedelta(minutes=90), "kind": "NORMAL"},
        {
            "start_at": origin + timedelta(minutes=100),
            "end_at": origin + timedelta(minutes=180),
            "kind": "OVERTIME",
        },
    ]
    for resource in data["resources"]:
        resource["calendar"] = windows
    for worker in data["workers"]:
        worker["calendar"] = windows
        worker["overtime_available"] = True
    required = next(b for b in source.profile.bom if b.product_id == source.orders[0].product_id)
    stock = next(i for i in data["inventory"] if i["material_id"] == required.material_id)
    stock["on_hand"] = stock["reserved"] = 0
    data["receipts"] = [
        {
            "receipt_id": "supply-for-overtime",
            "material_id": required.material_id,
            "unit": stock["unit"],
            "quantity": required.quantity_per_unit * source.orders[0].quantity,
            "eta": origin + timedelta(minutes=100),
            "status": "CONFIRMED",
        }
    ]
    source = Snapshot.model_validate(data)
    baseline = solve(source, time_limit=5, allow_overtime=True)
    assert baseline.has_solution and baseline.checker.status == "PASS"
    assert baseline.objective[1].value == 66
    return evolve(
        source, active_plan_version="overtime-plan-1", active_plan_hash=baseline.content_hash
    ), baseline


def test_earlier_actual_receipt_can_reduce_overtime_below_the_approved_baseline():
    source, baseline = overtime_initial()
    source = inject(
        source,
        event_id="supply-arrived-early",
        kind="receipt.receive",
        payload={"receipt_id": "supply-for-overtime"},
    )
    candidate = checked(source, baseline)
    assert candidate.objective[1].value == -66
    assert candidate.required_consents == ()
    assert max(a.end_at for a in candidate.assignments) <= source.horizon.start_at + timedelta(
        minutes=90
    )


def test_actual_overtime_history_cancels_for_both_candidate_and_baseline():
    source, baseline = overtime_initial()
    source = advance(source, baseline, minutes=110)
    assert sum(len(a.segments) for a in source.actuals) > 0
    candidate = checked(source, baseline, allow_overtime=True)
    assert candidate.objective[1].value == 0
    assert [m.value for m in candidate.objective[2:4]] == [0, 0]


def test_added_demand_during_production_gets_a_complete_hint_and_a_checked_plan():
    from packages.domain.production_facts import material_shortfalls
    from packages.planning.solver import solve_with_evidence

    source, baseline = initial()
    source = advance(source, baseline, minutes=12)
    order = source.orders[0]
    source = inject(
        source,
        event_id="more-demand",
        kind="order.revise",
        payload={
            "order_id": order.order_id,
            "expected_version": order.version,
            "quantity": order.quantity + 50,
            "due_at": order.due_at.isoformat(),
            "priority_weight": order.priority_weight,
            "hard_deadline": order.hard_deadline,
        },
    )
    assert not material_shortfalls(source)
    candidate, evidence = solve_with_evidence(source, baseline=baseline, time_limit=10)
    operations = batch_operations(source)[1]
    assert evidence["hint_operations"] == len(operations)
    assert candidate.has_solution and candidate.checker.status == "PASS"


def solve_with_checked_hint(source, baseline):
    """Solve and prove the search hint itself passes the independent Checker."""
    from packages.domain.models import Candidate
    from packages.planning import solver as planning
    from packages.planning.checker import check_candidate

    captured = {}
    original = planning._build

    def spy(snapshot, allow_overtime, hint, base, not_before):
        captured["hint"] = hint
        return original(snapshot, allow_overtime, hint, base, not_before)

    planning._build = spy
    try:
        candidate = planning.solve(source, baseline=baseline, time_limit=10)
    finally:
        planning._build = original
    hint = captured["hint"]
    assert len(hint) == len(batch_operations(source)[1])
    data = candidate.model_dump(mode="json", exclude={"content_hash"})
    data.update(
        assignments=[a.model_dump(mode="json") for a in hint],
        native_status="FEASIBLE",
        has_solution=True,
        solver_passes=[],
        last_search_status=None,
        proven_objective_levels=0,
        objective=[
            m.model_dump(mode="json") | {"lower_bound": None}
            for m in calculate_metrics(source, hint, baseline=baseline)
        ],
    )
    assert (
        check_candidate(source, Candidate.model_validate(data), baseline=baseline).status == "PASS"
    )
    assert candidate.has_solution and candidate.checker.status == "PASS"
    return candidate


def test_absence_during_production_yields_a_valid_hint_and_a_checked_plan():
    source, baseline = initial()
    source = advance(source, baseline, minutes=12)
    busy = {a.worker_id for a in source.actuals if a.state != "COMPLETED"}
    worker = next(w for w in source.workers if w.worker_id not in busy)
    source = inject(
        source, event_id="absent", kind="worker.absent", payload={"worker_id": worker.worker_id}
    )
    solve_with_checked_hint(source, baseline)


def test_outage_left_unhandled_moves_missed_dispatches_to_a_checked_plan():
    source, baseline = initial()
    source = advance(source, baseline, minutes=12)
    running = next(a for a in source.actuals if a.state == "IN_PROGRESS")
    source = inject(
        source, event_id="down", kind="resource.down", payload={"resource_id": running.resource_id}
    )
    before = source
    source = advance(source, baseline, minutes=30)
    # Nothing is prepared behind the stopped work; those dispatches are missed instead.
    stopped = {a.operation_id for a in source.actuals if a.state == "BLOCKED"}
    assert running.operation_id in stopped
    assert all(a.state != "SETUP" or a.actual_start is not None for a in source.actuals)
    from services.factory_sim.engine import missed_dispatch_events

    assert missed_dispatch_events(before, source, baseline)
    for operation_id in stopped:
        source = inject(
            source,
            event_id=f"confirm-{operation_id}",
            kind="execution.confirm_remaining",
            payload={
                "operation_id": operation_id,
                "remaining_minutes": 5,
                "remaining_setup_minutes": 0,
            },
        )
    source = inject(
        source,
        event_id="restore",
        kind="resource.restore",
        payload={"resource_id": running.resource_id},
    )
    solve_with_checked_hint(source, baseline)


def test_timed_leave_keeps_measured_work_for_the_same_person_and_plans_after_return():
    source, baseline = initial()
    source = advance(source, baseline, minutes=12)
    running = next(a for a in source.actuals if a.state == "IN_PROGRESS")
    source = inject(
        source,
        event_id="leave",
        kind="worker.leave",
        payload={"worker_id": running.worker_id, "minutes": 30},
    )
    stopped = next(a for a in source.actuals if a.operation_id == running.operation_id)
    assert stopped.state == "BLOCKED" and stopped.remaining_minutes is not None
    worker = next(w for w in source.workers if w.worker_id == running.worker_id)
    assert worker.status == "AVAILABLE" and worker.unavailable[-1].end_at == (
        source.snapshot_clock + timedelta(minutes=30)
    )
    candidate = solve_with_checked_hint(source, baseline)
    resumed = next(a for a in candidate.assignments if a.operation_id == running.operation_id)
    assert resumed.worker_id == running.worker_id
    assert resumed.resume_changeover_start >= worker.unavailable[-1].end_at
