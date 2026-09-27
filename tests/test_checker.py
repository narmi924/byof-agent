"""Hand-written schedules challenge the checker without importing any solver code."""

from datetime import datetime, timedelta, timezone

import pytest

from packages.domain.models import Assignment, Candidate, Snapshot
from packages.planning.checker import calculate_metrics, check_candidate

ORIGIN = datetime(2030, 1, 1, 8, tzinfo=timezone.utc)


def at(minutes: int) -> datetime:
    return ORIGIN + timedelta(minutes=minutes)


def example_snapshot(*, batches: int = 1, second_product: bool = False) -> Snapshot:
    calendar = [
        {"start_at": at(0), "end_at": at(30), "kind": "NORMAL"},
        {"start_at": at(30), "end_at": at(60), "kind": "OVERTIME"},
    ]
    products, routes, bom, orders = [], [], [], []
    for name in ("a", "b") if second_product else ("a",):
        product_id = f"item-{name}"
        products.append(
            {"product_id": product_id, "name": name, "batch_size": 2, "route_version": "r1"}
        )
        previous = []
        for code, resource_type, skill in (
            ("begin", "PREP", "PREP"),
            ("check", "TEST", "TEST"),
            ("finish", "PACK", "PACK"),
        ):
            step_id = f"{name}-{code}"
            routes.append(
                {
                    "step_id": step_id,
                    "product_id": product_id,
                    "route_version": "r1",
                    "operation_code": code,
                    "name": code,
                    "predecessors": previous,
                    "setup_min": 0,
                    "cycle_sec_per_unit": 60,
                    "resource_type": resource_type,
                    "skill": skill,
                    "quality_gate": code == "check",
                }
            )
            previous = [step_id]
        bom.append(
            {
                "product_id": product_id,
                "material_id": "shared-part",
                "quantity_per_unit": 1,
                "consume_step_id": f"{name}-finish",
            }
        )
        orders.append(
            {
                "order_id": f"order-{name}",
                "product_id": product_id,
                "quantity": batches * 2,
                "due_at": at(20),
                "priority_weight": 3,
                "hard_deadline": False,
                "version": 1,
            }
        )
    return Snapshot.model_validate(
        {
            "snapshot_id": "facts-1",
            "factory_id": "test-factory",
            "run_id": "run-1",
            "snapshot_clock": at(0),
            "horizon": {"start_at": at(0), "end_at": at(60)},
            "source": {
                "source_system": "test-source",
                "source_revision": "1",
                "observed_at": at(0),
                "effective_at": at(0),
                "complete": True,
                "consistency": "ATOMIC_SNAPSHOT",
                "freshness": "CURRENT",
                "ownership": "simulator_fact",
                "evidence_digest": "0" * 64,
            },
            "planning_revision": 1,
            "scope_version": 1,
            "active_plan_version": None,
            "profile": {
                "factory_id": "test-factory",
                "profile_id": "small-assembly",
                "version": "1",
                "timezone": "UTC",
                "evidence_mode": "synthetic",
                "source_digest": "0" * 64,
                "required_capabilities": ["fixed_lot_exact_split", "acyclic_operation_precedence"],
                "products": products,
                "materials": [{"material_id": "shared-part", "name": "Shared part", "unit": "EA"}],
                "bom": bom,
                "routes": routes,
                "policy": {
                    "policy_version": "policy-1",
                    "first_changeover_min": 0,
                    "same_product_changeover_min": 1,
                    "different_product_changeover_min": 5,
                    "freeze_window_min": 60,
                },
            },
            "orders": orders,
            "inventory": [
                {
                    "material_id": "shared-part",
                    "unit": "EA",
                    "on_hand": batches * 2 * len(products),
                    "reserved": 0,
                }
            ],
            "receipts": [],
            "resources": [
                {
                    "resource_id": rid,
                    "resource_type": kind,
                    "operation_codes": [code],
                    "status": "AVAILABLE",
                    "calendar": calendar,
                }
                for rid, kind, code in (
                    ("r1", "PREP", "begin"),
                    ("r2", "TEST", "check"),
                    ("r3", "PACK", "finish"),
                )
            ],
            "workers": [
                {
                    "worker_id": wid,
                    "skills": skills,
                    "status": "AVAILABLE",
                    "calendar": calendar,
                    "overtime_available": True,
                }
                for wid, skills in (
                    ("w1", ["PREP", "TEST", "PACK"]),
                    ("w2", ["TEST"]),
                    ("w3", ["PACK"]),
                )
            ],
        }
    )


def example_assignments(
    *, batches: int = 1, second_product: bool = False, shift: int = 0
) -> tuple[Assignment, ...]:
    assignments = []
    for product in ("a", "b") if second_product else ("a",):
        for batch in range(1, batches + 1):
            starts = (
                (0, 2, 4)
                if product == "a" and batch == 1
                else ((7, 14, 21) if product == "b" else (3, 6, 9))
            )
            changeover = 0 if product == "a" and batch == 1 else (5 if product == "b" else 1)
            for index, (code, start) in enumerate(zip(("begin", "check", "finish"), starts), 1):
                assignments.append(
                    Assignment(
                        operation_id=f"order-{product}-R001-B{batch:03d}-{code}",
                        resource_id=f"r{index}",
                        worker_id=f"w{index}",
                        changeover_start=at(start - changeover + shift),
                        start_at=at(start + shift),
                        end_at=at(start + 2 + shift),
                    )
                )
    return tuple(assignments)


def make_candidate(
    snapshot: Snapshot, assignments=None, *, allow_overtime=False, objective=None, **changes
) -> Candidate:
    assignments = assignments if assignments is not None else example_assignments()
    data = {
        "candidate_id": "plan-1",
        "factory_id": snapshot.factory_id,
        "version": 1,
        "binding": {
            "snapshot_hash": snapshot.content_hash,
            "planning_revision": snapshot.planning_revision,
            "scope_version": snapshot.scope_version,
            "profile_version": snapshot.profile.version,
            "policy_version": snapshot.profile.policy.policy_version,
            "objective_version": "delivery-v1",
            "baseline_plan_version": snapshot.active_plan_version,
        },
        "native_status": "FEASIBLE",
        "has_solution": True,
        "termination_reason": "TIME_LIMIT",
        "assignments": assignments,
        "objective": objective
        if objective is not None
        else calculate_metrics(snapshot, assignments),
        "required_consents": ("allow_overtime",) if allow_overtime else (),
        "scenario": (
            {
                "field": "allow_overtime",
                "value": True,
                "reason": "Overtime scenario pending manager approval",
            },
        )
        if allow_overtime
        else (),
        "effective_not_before": snapshot.snapshot_clock,
        "accept_before": at(60),
        "checker": {
            "checker_version": "NOT_RUN",
            "snapshot_hash": snapshot.content_hash,
            "status": "NOT_RUN",
        },
    }
    data.update(changes)
    return Candidate.model_validate(data)


def change_snapshot(snapshot: Snapshot, change) -> Snapshot:
    data = snapshot.model_dump(mode="json", exclude={"content_hash"})
    change(data)
    return Snapshot.model_validate(data)


def change_assignment(assignments, index, **changes):
    result = list(assignments)
    data = result[index].model_dump()
    data.update(changes)
    result[index] = Assignment.model_validate(data)
    return tuple(result)


def assert_failed(snapshot, candidate, code, **kwargs):
    result = check_candidate(snapshot, candidate, **kwargs)
    assert result.status == "FAIL", result
    assert code in {issue.code for issue in result.issues}, result


def test_manual_plan_has_independently_calculated_metrics_and_quality_dependencies():
    snapshot = example_snapshot()
    candidate = make_candidate(snapshot)
    assert [(m.name, m.value, m.unit) for m in candidate.objective] == [
        ("weighted_tardiness", 0, "minutes"),
        ("incremental_overtime_metric", 0, "minutes"),
        ("changed_operations", 0, "operations"),
        ("total_start_shift", 0, "minutes"),
        ("makespan", 6, "minutes"),
    ]
    result = check_candidate(snapshot, candidate)
    assert result.status == "PASS" and result.issues == ()
    assert result.snapshot_hash == snapshot.content_hash


def test_missing_extra_and_duplicate_operations_never_pass():
    snapshot = example_snapshot()
    legal = make_candidate(snapshot)
    assert_failed(
        snapshot,
        make_candidate(
            snapshot, legal.assignments[:1] + legal.assignments[2:], objective=legal.objective
        ),
        "MISSING_OPERATION",
    )
    extra = Assignment(
        operation_id="unexpected",
        resource_id="r1",
        worker_id="w1",
        changeover_start=at(8),
        start_at=at(8),
        end_at=at(10),
    )
    assert_failed(
        snapshot,
        make_candidate(snapshot, legal.assignments + (extra,), objective=legal.objective),
        "EXTRA_OPERATION",
    )
    bypassed = legal.model_copy(update={"assignments": legal.assignments + (legal.assignments[0],)})
    assert_failed(snapshot, bypassed, "INVALID_CONTRACT")


def test_wrong_duration_and_quality_gate_reordering_fail():
    snapshot = example_snapshot()
    assignments = example_assignments()
    bad = change_assignment(assignments, 0, end_at=at(1))
    assert_failed(snapshot, make_candidate(snapshot, bad), "WRONG_DURATION")
    bad = change_assignment(assignments, 2, changeover_start=at(3), start_at=at(3), end_at=at(5))
    assert_failed(snapshot, make_candidate(snapshot, bad), "QUALITY_PRECEDENCE")


def test_same_worker_on_different_equipment_is_rejected():
    snapshot = example_snapshot(batches=2)
    legal = example_assignments(batches=2)
    assert check_candidate(snapshot, make_candidate(snapshot, legal)).status == "PASS"
    bad = change_assignment(legal, 1, worker_id="w1")
    assert_failed(snapshot, make_candidate(snapshot, bad), "WORKER_CONFLICT")


def test_resource_changeover_overlap_is_rejected_even_when_operation_times_are_adjacent():
    snapshot = example_snapshot(batches=2)
    legal = example_assignments(batches=2)
    bad = change_assignment(legal, 3, changeover_start=at(1), start_at=at(2), end_at=at(4))
    assert bad[0].end_at == bad[3].start_at
    assert_failed(snapshot, make_candidate(snapshot, bad), "RESOURCE_CONFLICT")


def test_changeover_occupies_employee_even_when_work_intervals_do_not_overlap():
    snapshot = change_snapshot(
        example_snapshot(batches=2), lambda data: data["workers"][1]["skills"].append("PREP")
    )
    assignments = example_assignments(batches=2)
    assignments = change_assignment(
        assignments, 3, worker_id="w2", changeover_start=at(3), start_at=at(4), end_at=at(6)
    )
    assignments = change_assignment(assignments, 4, worker_id="w1")
    assert assignments[1].end_at == assignments[3].start_at
    assert_failed(snapshot, make_candidate(snapshot, assignments), "WORKER_CONFLICT")


@pytest.mark.parametrize("state", ["UNKNOWN", "DOWN", "MAINTENANCE"])
def test_nonavailable_resource_states_cannot_be_assigned(state):
    snapshot = change_snapshot(
        example_snapshot(), lambda data: data["resources"][0].update(status=state)
    )
    assert_failed(snapshot, make_candidate(snapshot), "RESOURCE_UNAVAILABLE")


def test_resource_and_worker_qualifications_are_both_checked():
    snapshot = example_snapshot()
    bad = change_assignment(example_assignments(), 0, resource_id="r2")
    assert_failed(snapshot, make_candidate(snapshot, bad), "RESOURCE_QUALIFICATION")
    bad = change_assignment(example_assignments(), 0, worker_id="w2")
    assert_failed(snapshot, make_candidate(snapshot, bad), "WORKER_QUALIFICATION")


def test_same_and_different_product_changeovers_follow_actual_resource_sequence():
    for options, index, expected in (({"batches": 2}, 3, 1), ({"second_product": True}, 3, 5)):
        snapshot = example_snapshot(**options)
        legal = example_assignments(**options)
        assert check_candidate(snapshot, make_candidate(snapshot, legal)).status == "PASS"
        bad = change_assignment(
            legal, index, changeover_start=legal[index].start_at - timedelta(minutes=expected - 1)
        )
        assert_failed(snapshot, make_candidate(snapshot, bad), "CHANGEOVER")


def test_lunch_gap_cannot_be_crossed_by_work_or_changeover():
    def gap(data):
        data["resources"][0]["calendar"] = [
            {"start_at": at(0), "end_at": at(2)},
            {"start_at": at(3), "end_at": at(30)},
        ]

    snapshot = change_snapshot(example_snapshot(batches=2), gap)
    assert_failed(
        snapshot, make_candidate(snapshot, example_assignments(batches=2)), "RESOURCE_CALENDAR"
    )

    def worker_gap(data):
        data["workers"][1]["calendar"] = [
            {"start_at": at(0), "end_at": at(3)},
            {"start_at": at(4), "end_at": at(30)},
        ]

    snapshot = change_snapshot(example_snapshot(), worker_gap)
    assert_failed(snapshot, make_candidate(snapshot), "WORKER_CALENDAR")


def test_unavailability_intervals_include_changeover_and_preserve_half_open_edges():
    snapshot = change_snapshot(
        example_snapshot(),
        lambda data: data["workers"][0].update(unavailable=[{"start_at": at(2), "end_at": at(3)}]),
    )
    assert check_candidate(snapshot, make_candidate(snapshot)).status == "PASS"
    snapshot = change_snapshot(
        snapshot,
        lambda data: data["workers"][0].update(unavailable=[{"start_at": at(1), "end_at": at(3)}]),
    )
    assert_failed(snapshot, make_candidate(snapshot), "WORKER_UNAVAILABLE_INTERVAL")


def with_receipt(snapshot, *, quantity=2, eta=0, state="CONFIRMED", on_hand=0, reserved=0):
    def update(data):
        data["inventory"][0].update(on_hand=on_hand, reserved=reserved)
        data["receipts"] = [
            {
                "receipt_id": "incoming-1",
                "material_id": "shared-part",
                "unit": "EA",
                "quantity": quantity,
                "eta": at(eta),
                "status": state,
                "received_at": at(0) if state == "RECEIVED" else None,
            }
        ]

    return change_snapshot(snapshot, update)


def test_full_kit_requires_late_consumed_material_at_the_first_operation():
    snapshot = with_receipt(example_snapshot(), eta=0)
    assert check_candidate(snapshot, make_candidate(snapshot)).status == "PASS"
    late = with_receipt(example_snapshot(), eta=1)
    assert_failed(late, make_candidate(late), "MATERIAL_SHORTAGE")
    assert (
        check_candidate(late, make_candidate(late, example_assignments(shift=1))).status == "PASS"
    )


def test_time_inventory_is_aggregated_across_batches_and_honors_same_instant_arrival():
    snapshot = with_receipt(example_snapshot(batches=2), eta=3, on_hand=2)
    assignments = example_assignments(batches=2)
    assert check_candidate(snapshot, make_candidate(snapshot, assignments)).status == "PASS"
    late = with_receipt(example_snapshot(batches=2), eta=4, on_hand=2)
    assert_failed(late, make_candidate(late, assignments), "MATERIAL_SHORTAGE")


def test_received_expected_and_reserved_stock_are_not_free_additional_supply():
    for state in ("RECEIVED", "EXPECTED", "CANCELLED"):
        snapshot = with_receipt(example_snapshot(batches=2), on_hand=2, state=state)
        assert_failed(
            snapshot, make_candidate(snapshot, example_assignments(batches=2)), "MATERIAL_SHORTAGE"
        )
    snapshot = change_snapshot(
        example_snapshot(), lambda data: data["inventory"][0].update(reserved=1)
    )
    assert_failed(snapshot, make_candidate(snapshot), "MATERIAL_SHORTAGE")


def test_hard_deadline_cannot_be_replaced_with_a_soft_penalty():
    snapshot = change_snapshot(
        example_snapshot(), lambda data: data["orders"][0].update(due_at=at(5))
    )
    candidate = make_candidate(snapshot)
    assert candidate.objective[0].value == 3
    assert check_candidate(snapshot, candidate).status == "PASS"
    hard = change_snapshot(snapshot, lambda data: data["orders"][0].update(hard_deadline=True))
    assert_failed(hard, make_candidate(hard), "HARD_DEADLINE")


def test_overtime_needs_a_declared_scenario_employee_eligibility_and_pending_consent():
    snapshot = example_snapshot()
    assignments = example_assignments(shift=30)
    candidate = make_candidate(snapshot, assignments, allow_overtime=True)
    assert [metric.value for metric in candidate.objective] == [48, 6, 0, 0, 36]
    assert check_candidate(snapshot, candidate, allow_overtime=True).status == "PASS"
    assert_failed(snapshot, candidate, "UNSUPPORTED_SCENARIO")
    assert_failed(snapshot, make_candidate(snapshot, assignments), "WORKER_CALENDAR")
    missing_consent = make_candidate(
        snapshot, assignments, allow_overtime=True, required_consents=()
    )
    assert_failed(snapshot, missing_consent, "MISSING_CONSENT", allow_overtime=True)
    ineligible = change_snapshot(
        snapshot, lambda data: data["workers"][0].update(overtime_available=False)
    )
    assert_failed(
        ineligible,
        make_candidate(ineligible, assignments, allow_overtime=True),
        "WORKER_CALENDAR",
        allow_overtime=True,
    )


def test_objective_tampering_and_impossible_lower_bound_are_rejected():
    snapshot = example_snapshot()
    candidate = make_candidate(snapshot)
    objective = [metric.model_dump() for metric in candidate.objective]
    objective[-1]["value"] = 0
    assert_failed(snapshot, make_candidate(snapshot, objective=objective), "METRIC_MISMATCH")
    objective[-1].update(value=6, lower_bound=7)
    assert_failed(
        snapshot, make_candidate(snapshot, objective=objective), "INVALID_OBJECTIVE_BOUND"
    )


def test_new_rows_scope_versions_fact_hash_and_objective_versions_cannot_be_ignored():
    snapshot = example_snapshot()
    old = make_candidate(snapshot)
    newer = example_snapshot(batches=2)
    assert_failed(newer, old, "VERSION_MISMATCH")
    new_order = example_snapshot(second_product=True)
    assert {o.order_id for o in new_order.orders} - {o.order_id for o in snapshot.orders} == {
        "order-b"
    }
    assert_failed(new_order, old, "VERSION_MISMATCH")
    for field, value in (
        ("scope_version", 2),
        ("planning_revision", 2),
        ("objective_version", "unapproved-priority"),
    ):
        binding = old.binding.model_dump()
        binding[field] = value
        assert_failed(snapshot, make_candidate(snapshot, binding=binding), "VERSION_MISMATCH")
    bad_hash = old.model_copy(update={"content_hash": "f" * 64})
    assert_failed(snapshot, bad_hash, "INVALID_CONTRACT")


def test_known_actuals_and_existing_plan_are_explicitly_not_certified_yet():
    snapshot = example_snapshot()
    candidate = make_candidate(snapshot)
    assert_failed(snapshot, candidate, "UNSUPPORTED_BASELINE", baseline=candidate)
    active = change_snapshot(snapshot, lambda data: data.update(active_plan_version="release-1"))
    assert_failed(active, make_candidate(active), "UNSUPPORTED_BASELINE")

    def actual(data):
        data["actuals"] = [
            {
                "operation_id": "order-a-R001-B001-begin",
                "batch_id": "order-a-R001-B001",
                "route_version": "r1",
                "state": "BLOCKED",
                "actual_start": at(0),
                "resource_id": "r1",
                "worker_id": "w1",
                "completed_quantity": 0,
                "version": 1,
            }
        ]

    wip = change_snapshot(snapshot, actual)
    assert_failed(wip, make_candidate(wip), "UNSUPPORTED_WIP")


def test_stale_incomplete_sources_and_past_occupancy_are_rejected():
    for update in ({"complete": False}, {"freshness": "STALE"}, {"consistency": "UNVERIFIED"}):
        snapshot = change_snapshot(example_snapshot(), lambda data: data["source"].update(update))
        assert_failed(snapshot, make_candidate(snapshot), "SOURCE_NOT_CURRENT")
    snapshot = change_snapshot(example_snapshot(), lambda data: data.update(snapshot_clock=at(1)))
    assert_failed(snapshot, make_candidate(snapshot), "OUTSIDE_PLANNING_WINDOW")


def test_metric_baseline_comparison_is_independent_of_baseline_reported_values():
    snapshot = example_snapshot()
    baseline = make_candidate(snapshot)
    shifted = example_assignments(shift=1)
    metrics = calculate_metrics(snapshot, shifted, baseline=baseline)
    assert [metric.value for metric in metrics] == [0, 0, 3, 3, 7]
    overtime_baseline = make_candidate(snapshot, example_assignments(shift=30), allow_overtime=True)
    metrics = calculate_metrics(snapshot, example_assignments(), baseline=overtime_baseline)
    assert metrics[1].value == -6


def test_no_solution_and_unknown_scenarios_are_not_certified():
    snapshot = example_snapshot()
    candidate = make_candidate(
        snapshot, (), objective=(), has_solution=False, native_status="UNKNOWN"
    )
    assert_failed(snapshot, candidate, "NO_SOLUTION")
    recovery = (
        {
            "field": "expected_recovery",
            "value": "2030-01-01T08:00:00Z",
            "reason": "Unconfirmed assumption",
        },
    )
    assert_failed(snapshot, make_candidate(snapshot, scenario=recovery), "UNSUPPORTED_SCENARIO")


def test_fractional_assignment_minutes_horizon_and_expired_acceptance_are_rejected():
    snapshot = example_snapshot()
    fractional = tuple(
        Assignment.model_validate(
            {
                **assignment.model_dump(),
                "changeover_start": assignment.changeover_start + timedelta(seconds=1),
                "start_at": assignment.start_at + timedelta(seconds=1),
                "end_at": assignment.end_at + timedelta(seconds=1),
            }
        )
        for assignment in example_assignments()
    )
    assert_failed(snapshot, make_candidate(snapshot, fractional), "INVALID_TIME_GRID")
    short = change_snapshot(snapshot, lambda data: data["horizon"].update(end_at=at(5)))
    assert_failed(short, make_candidate(short), "OUTSIDE_PLANNING_WINDOW")
    later = change_snapshot(snapshot, lambda data: data.update(snapshot_clock=at(2)))
    assert_failed(
        later, make_candidate(later, effective_not_before=at(0), accept_before=at(1)), "STALE_TIME"
    )


def test_future_actual_receipt_cannot_backdate_fact_availability():
    snapshot = with_receipt(example_snapshot(), state="RECEIVED", on_hand=2)
    snapshot = change_snapshot(snapshot, lambda data: data["receipts"][0].update(received_at=at(1)))
    assert_failed(snapshot, make_candidate(snapshot), "FUTURE_ACTUAL_RECEIPT")
