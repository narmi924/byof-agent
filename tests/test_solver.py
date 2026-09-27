"""Solver outcomes must satisfy raw-fact checks, including genuine rejection cases."""

from datetime import timedelta

import pytest

from packages.domain.models import Snapshot, batch_operations
from packages.domain.skf import load_skf_snapshot
from packages.planning.checker import calculate_metrics
from packages.planning.solver import PlanningInputError, solve


def facts():
    return load_skf_snapshot(development=True).model_dump(mode="json", exclude={"content_hash"})


def short_horizon(data, minutes=180):
    source = Snapshot.model_validate(data)
    data["horizon"]["end_at"] = (source.horizon.start_at + timedelta(minutes=minutes)).isoformat()
    return data


def assert_checked(snapshot, candidate):
    assert candidate.has_solution, candidate.native_status
    assert candidate.checker.status == "PASS", candidate.checker.issues
    assert len(candidate.assignments) == len(batch_operations(snapshot)[1])
    expected = calculate_metrics(snapshot, candidate.assignments)
    assert [(m.name, m.value, m.unit) for m in candidate.objective] == [
        (m.name, m.value, m.unit) for m in expected
    ]


def test_small_lexicographic_optimum_and_all_version_bindings():
    snapshot = load_skf_snapshot(development=True)
    candidate = solve(snapshot, time_limit=5)
    assert_checked(snapshot, candidate)
    assert candidate.native_status == "OPTIMAL"
    assert candidate.proven_objective_levels == 5
    assert [m.value for m in candidate.objective] == [0, 0, 0, 0, 66]
    assert [m.lower_bound for m in candidate.objective] == [m.value for m in candidate.objective]
    assert candidate.binding.snapshot_hash == snapshot.content_hash
    assert candidate.binding.planning_revision == snapshot.planning_revision
    assert candidate.binding.scope_version == snapshot.scope_version
    assert candidate.binding.profile_version == snapshot.profile.version
    assert candidate.binding.policy_version == snapshot.profile.policy.policy_version
    assert candidate.binding.objective_version == "delivery-v1"
    assert candidate.binding.baseline_plan_version is None


def test_revised_unstarted_batches_keep_a_complete_dispatch_hint():
    from packages.planning.solver import solve_with_evidence
    from scripts.import_factory import prepare_initial
    from services.factory_sim.engine import inject

    snapshot = prepare_initial(load_skf_snapshot(development=True))
    order = snapshot.orders[0]
    revised = inject(
        snapshot,
        event_id="revise-before-planning",
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
    assert revised.production_batches is not None
    candidate, evidence = solve_with_evidence(revised, time_limit=5)
    assert_checked(revised, candidate)
    assert evidence["hint_operations"] == len(candidate.assignments)


def test_route_order_comes_from_dependencies_not_input_or_code_sort():
    data = facts()
    data["profile"]["routes"].reverse()
    snapshot = Snapshot.model_validate(data)
    candidate = solve(snapshot, time_limit=5)
    assert_checked(snapshot, candidate)
    tasks = {a.operation_id.rsplit("-", 1)[-1]: a for a in candidate.assignments}
    assert tasks["OP30"].end_at <= tasks["OP60"].start_at
    assert tasks["OP60"].end_at <= tasks["OP40"].start_at
    assert tasks["OP70"].end_at <= tasks["OP80"].start_at


def test_one_employee_across_devices_cannot_overlap_including_changeovers():
    data = facts()
    data["orders"][0]["quantity"] = 100
    worker = data["workers"][0]
    worker["skills"] = sorted({s for w in data["workers"] for s in w["skills"]})
    data["workers"] = [worker]
    snapshot = Snapshot.model_validate(data)
    candidate = solve(snapshot, time_limit=5)
    assert_checked(snapshot, candidate)
    ordered = sorted(candidate.assignments, key=lambda a: a.changeover_start)
    assert all(a.end_at <= b.changeover_start for a, b in zip(ordered, ordered[1:]))
    assert {a.worker_id for a in ordered} == {worker["worker_id"]}


def test_material_eta_same_instant_full_kit_and_reserved_balance():
    data = facts()
    snapshot = Snapshot.model_validate(data)
    material = data["profile"]["bom"][0]["material_id"]
    stock = next(i for i in data["inventory"] if i["material_id"] == material)
    stock.update(on_hand=50, reserved=50)
    arrival = snapshot.horizon.start_at + timedelta(minutes=75)
    data["receipts"] = [r for r in data["receipts"] if r["material_id"] != material]
    data["receipts"].append(
        {
            "receipt_id": "same-instant",
            "material_id": material,
            "unit": stock["unit"],
            "quantity": 50,
            "eta": arrival.isoformat(),
            "status": "CONFIRMED",
        }
    )
    snapshot = Snapshot.model_validate(short_horizon(data))
    candidate = solve(snapshot, time_limit=5)
    assert_checked(snapshot, candidate)
    root = next(a for a in candidate.assignments if a.operation_id.endswith("-OP10"))
    assert root.start_at == arrival
    assert next(i for i in snapshot.inventory if i.material_id == material).reserved == 50


@pytest.mark.parametrize("status", ["EXPECTED", "RECEIVED", "CANCELLED"])
def test_unconfirmed_or_already_received_supply_is_not_counted_again(status):
    data = facts()
    source = Snapshot.model_validate(data)
    material = data["profile"]["bom"][0]["material_id"]
    stock = next(i for i in data["inventory"] if i["material_id"] == material)
    stock["on_hand"] = 0
    data["receipts"] = [r for r in data["receipts"] if r["material_id"] != material]
    receipt = {
        "receipt_id": "unusable",
        "material_id": material,
        "unit": stock["unit"],
        "quantity": 50,
        "eta": source.snapshot_clock.isoformat(),
        "status": status,
    }
    if status == "RECEIVED":
        receipt["received_at"] = source.snapshot_clock.isoformat()
    data["receipts"].append(receipt)
    candidate = solve(Snapshot.model_validate(short_horizon(data)), time_limit=5)
    assert candidate.native_status == "INFEASIBLE"
    assert not candidate.has_solution
    assert not candidate.assignments


def test_full_kit_rejects_missing_packaging_even_before_consumption_step():
    data = facts()
    product_id = data["orders"][0]["product_id"]
    material = next(
        b["material_id"]
        for b in data["profile"]["bom"]
        if b["product_id"] == product_id and b["consume_step_id"].endswith("OP80")
    )
    next(i for i in data["inventory"] if i["material_id"] == material)["on_hand"] = 0
    candidate = solve(Snapshot.model_validate(short_horizon(data)), time_limit=5)
    assert candidate.native_status == "INFEASIBLE"
    assert not candidate.has_solution


def test_hard_deadline_is_not_relaxed_into_tardiness():
    data = facts()
    origin = Snapshot.model_validate(data).horizon.start_at
    data["orders"][0].update(
        hard_deadline=True, due_at=(origin + timedelta(minutes=60)).isoformat()
    )
    candidate = solve(Snapshot.model_validate(data), time_limit=5)
    assert candidate.native_status == "INFEASIBLE"
    assert candidate.termination_reason == "COMPLETED"


def test_weighted_lateness_uses_all_elapsed_minutes_and_rounds_due_down():
    data = facts()
    origin = Snapshot.model_validate(data).horizon.start_at
    data["orders"][0].update(
        priority_weight=3, due_at=(origin + timedelta(minutes=30, seconds=30)).isoformat()
    )
    snapshot = Snapshot.model_validate(data)
    candidate = solve(snapshot, time_limit=5)
    assert_checked(snapshot, candidate)
    assert candidate.objective[0].value == 108


def test_breaks_cover_entire_resource_worker_occupation():
    data = facts()
    origin = Snapshot.model_validate(data).horizon.start_at
    calendar = [
        {
            "start_at": origin.isoformat(),
            "end_at": (origin + timedelta(minutes=15)).isoformat(),
            "kind": "NORMAL",
        },
        {
            "start_at": (origin + timedelta(minutes=60)).isoformat(),
            "end_at": (origin + timedelta(minutes=180)).isoformat(),
            "kind": "NORMAL",
        },
    ]
    for resource in data["resources"]:
        resource["calendar"] = calendar
    for worker in data["workers"]:
        worker["calendar"] = calendar
    snapshot = Snapshot.model_validate(short_horizon(data))
    candidate = solve(snapshot, time_limit=5)
    assert_checked(snapshot, candidate)
    for assignment in candidate.assignments:
        assert assignment.end_at <= origin + timedelta(
            minutes=15
        ) or assignment.changeover_start >= origin + timedelta(minutes=60)
    assert candidate.objective[4].value >= 116


def test_unavailable_interval_blocks_only_the_affected_resource():
    data = facts()
    origin = Snapshot.model_validate(data).horizon.start_at
    kit = next(r for r in data["resources"] if r["resource_id"] == "KIT-01")
    kit["unavailable"] = [
        {
            "start_at": origin.isoformat(),
            "end_at": (origin + timedelta(minutes=20, seconds=1)).isoformat(),
        }
    ]
    snapshot = Snapshot.model_validate(data)
    candidate = solve(snapshot, time_limit=5)
    assert_checked(snapshot, candidate)
    kit_assignment = next(a for a in candidate.assignments if a.resource_id == kit["resource_id"])
    assert kit_assignment.changeover_start == origin + timedelta(minutes=21)


def test_changeover_parameters_are_configured_and_depend_on_actual_predecessor():
    data = facts()
    data["orders"][0]["quantity"] = 100
    next_order = dict(data["orders"][0])
    next_order.update(
        order_id="another-order",
        product_id=data["profile"]["products"][1]["product_id"],
        quantity=50,
    )
    data["orders"].append(next_order)
    data["profile"]["policy"].update(
        first_changeover_min=2, same_product_changeover_min=3, different_product_changeover_min=7
    )
    snapshot = Snapshot.model_validate(data)
    candidate = solve(snapshot, time_limit=5)
    assert_checked(snapshot, candidate)
    batches, operations = batch_operations(snapshot)
    products = {b.batch_id: b.product_id for b in batches}
    product_for_op = {o.operation_id: products[o.batch_id] for o in operations}
    observed = set()
    for resource in snapshot.resources:
        work = sorted(
            (a for a in candidate.assignments if a.resource_id == resource.resource_id),
            key=lambda a: a.changeover_start,
        )
        previous = None
        for assignment in work:
            expected = (
                2
                if previous is None
                else 3
                if product_for_op[previous.operation_id] == product_for_op[assignment.operation_id]
                else 7
            )
            assert assignment.start_at - assignment.changeover_start == timedelta(minutes=expected)
            observed.add(expected)
            previous = assignment
    assert observed == {2, 3, 7}


def test_horizon_rounding_shortfall_is_infeasible_not_invalid_model():
    data = facts()
    origin = Snapshot.model_validate(data).horizon.start_at
    data["snapshot_clock"] = (origin + timedelta(seconds=30)).isoformat()
    data["horizon"]["end_at"] = (origin + timedelta(seconds=45)).isoformat()
    candidate = solve(Snapshot.model_validate(data), time_limit=5)
    assert candidate.native_status == "INFEASIBLE"


def test_overtime_is_a_named_unapproved_scenario_with_real_employee_minutes():
    data = facts()
    origin = Snapshot.model_validate(data).horizon.start_at
    calendar = [
        {
            "start_at": origin.isoformat(),
            "end_at": (origin + timedelta(minutes=5)).isoformat(),
            "kind": "NORMAL",
        },
        {
            "start_at": (origin + timedelta(minutes=5)).isoformat(),
            "end_at": (origin + timedelta(minutes=120)).isoformat(),
            "kind": "OVERTIME",
        },
    ]
    for item in [*data["resources"], *data["workers"]]:
        item["calendar"] = calendar
    snapshot = Snapshot.model_validate(short_horizon(data, 120))
    ordinary = solve(snapshot, time_limit=5)
    assert ordinary.native_status == "INFEASIBLE"
    overtime = solve(snapshot, time_limit=5, allow_overtime=True)
    assert_checked(snapshot, overtime)
    assert overtime.objective[1].value == 61
    assert overtime.objective[1].unit == "minutes"
    assert overtime.required_consents == ("allow_overtime",)
    assert overtime.scenario[0].field == "allow_overtime"
    assert overtime.scenario[0].value is True
    workers = {w.worker_id: w for w in snapshot.workers}
    assert all(workers[a.worker_id].overtime_available for a in overtime.assignments)


def test_down_resource_is_not_assigned_and_orders_are_not_dropped():
    data = facts()
    next(r for r in data["resources"] if r["resource_id"] == "ASMCELL-A")["status"] = "DOWN"
    snapshot = Snapshot.model_validate(data)
    candidate = solve(snapshot, time_limit=5)
    assert_checked(snapshot, candidate)
    assert "ASMCELL-A" not in {a.resource_id for a in candidate.assignments}
    next(r for r in data["resources"] if r["resource_id"] == "ASMCELL-B")["status"] = "UNKNOWN"
    rejected = solve(Snapshot.model_validate(data), time_limit=5)
    assert rejected.native_status == "INFEASIBLE"


def test_budget_exhaustion_is_unknown_not_proven_infeasible():
    candidate = solve(load_skf_snapshot(development=True), time_limit=0.000001)
    assert candidate.native_status == "UNKNOWN"
    assert candidate.termination_reason == "TIME_LIMIT"
    assert not candidate.has_solution
    assert not candidate.assignments
    assert candidate.proven_objective_levels == 0
    assert all(m.value is None and m.unknown_reason for m in candidate.objective)


@pytest.mark.parametrize("budget", [0, -1, float("nan"), float("inf")])
def test_invalid_budget_is_explicitly_rejected(budget):
    with pytest.raises(PlanningInputError, match="INVALID_BUDGET"):
        solve(load_skf_snapshot(development=True), time_limit=budget)


def test_existing_plan_and_unknown_wip_cannot_be_silently_replanned():
    data = facts()
    data["active_plan_version"] = "plan-1"
    with pytest.raises(PlanningInputError, match="UNSUPPORTED_BASELINE"):
        solve(Snapshot.model_validate(data), time_limit=5)
    data = facts()
    snapshot = Snapshot.model_validate(data)
    batches, operations = batch_operations(snapshot)
    data["actuals"] = [
        {
            "operation_id": operations[0].operation_id,
            "batch_id": batches[0].batch_id,
            "route_version": batches[0].route_version,
            "state": "BLOCKED",
            "actual_start": snapshot.snapshot_clock.isoformat(),
            "resource_id": "KIT-01",
            "worker_id": "W01",
            "completed_quantity": 0,
            "quality_state": "UNKNOWN",
            "version": 1,
        }
    ]
    with pytest.raises(PlanningInputError, match="UNSUPPORTED_WIP"):
        solve(Snapshot.model_validate(data), time_limit=5)
