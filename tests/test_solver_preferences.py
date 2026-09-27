"""Confirmed priorities change real schedules without relaxing their business constraints."""

from datetime import timedelta

import pytest
from test_checker import (
    at,
    change_snapshot,
    example_assignments,
    example_snapshot,
    make_candidate,
)
from test_checker_wip import wip_case

from packages.domain.models import Candidate, Snapshot
from packages.domain.objectives import EffectiveObjective, ObjectiveDefinition, ObjectiveSource
from packages.planning import solver as planning
from packages.planning.checker import calculate_metrics, check_candidate


def preference(snapshot, selection="delivery_first", **bounds):
    return EffectiveObjective(
        factory_id=snapshot.factory_id,
        profile_version=snapshot.profile.version,
        policy_version=snapshot.profile.policy.policy_version,
        definition=ObjectiveDefinition(selection=selection, **bounds),
        sources=(
            ObjectiveSource(
                preference_id="confirmed-preference",
                version=1,
                scope_type="FACTORY",
                scope_id=snapshot.factory_id,
                confirmed_by="planner",
                confirmed_at=at(0),
            ),
        ),
    )


def values(candidate):
    return {metric.name: metric.value for metric in candidate.objective}


def activate(snapshot, baseline):
    data = snapshot.model_dump(exclude={"content_hash"})
    data.update(
        schema_version="byof.snapshot/2",
        snapshot_id="active-facts",
        planning_revision=2,
        active_plan_version="accepted-plan-1",
        active_plan_hash=baseline.content_hash,
    )
    data["source"]["source_revision"] = "2"
    return Snapshot.model_validate(data)


def manual(snapshot, objective, assignments=None, *, baseline=None, allow_overtime=False):
    assignments = assignments if assignments is not None else example_assignments()
    metrics = {m.name: m for m in calculate_metrics(snapshot, assignments, baseline=baseline)}
    candidate = make_candidate(
        snapshot,
        assignments,
        objective=tuple(metrics[name] for name in objective.order),
        allow_overtime=allow_overtime,
    )
    data = candidate.model_dump(exclude={"content_hash"})
    data["binding"]["objective_version"] = objective.objective_version
    return Candidate.model_validate(data)


def overtime_facts(*, hard_deadline=False):
    data = example_snapshot().model_dump(exclude={"content_hash"})
    data["orders"][0].update(due_at=at(6), hard_deadline=hard_deadline)
    data["profile"]["policy"]["freeze_window_min"] = 0
    for resource in data["resources"]:
        resource["calendar"] = [{"start_at": at(0), "end_at": at(60), "kind": "NORMAL"}]
    for worker in data["workers"]:
        worker["calendar"] = [
            {"start_at": at(0), "end_at": at(2), "kind": "NORMAL"},
            {"start_at": at(2), "end_at": at(6), "kind": "OVERTIME"},
            {"start_at": at(6), "end_at": at(60), "kind": "NORMAL"},
        ]
    return Snapshot.model_validate(data)


def test_none_preserves_default_schedule_metric_order_and_binding():
    snapshot = example_snapshot()
    implicit = planning.solve(snapshot, time_limit=5)
    explicit = planning.solve(snapshot, time_limit=5, objective=None)
    assert implicit.assignments == explicit.assignments
    assert implicit.objective == explicit.objective
    assert implicit.binding == explicit.binding
    assert implicit.binding.objective_version == "delivery-v1"
    assert implicit.checker == explicit.checker
    assert implicit.checker.status == "PASS"
    assert implicit.constant_objective_levels == explicit.constant_objective_levels


def test_explicit_delivery_contract_keeps_rules_and_binds_its_confirmed_version():
    snapshot = example_snapshot()
    objective = preference(snapshot)
    candidate, evidence = planning.solve_with_evidence(snapshot, time_limit=5, objective=objective)
    assert candidate.checker.status == "PASS"
    assert values(candidate) == {
        "weighted_tardiness": 0,
        "incremental_overtime_metric": 0,
        "changed_operations": 0,
        "total_start_shift": 0,
        "makespan": 6,
    }
    assert candidate.binding.objective_version == objective.objective_version
    assert evidence["objective_version"] == objective.objective_version
    assert evidence["objective_bounds"] == {}
    assert candidate.proven_objective_levels == 5


def test_restored_default_keeps_new_resolution_version_and_rejects_previous_approval_target():
    snapshot = example_snapshot()
    objective = EffectiveObjective(
        factory_id=snapshot.factory_id,
        profile_version=snapshot.profile.version,
        policy_version=snapshot.profile.policy.policy_version,
        resolution_version=2,
        definition=ObjectiveDefinition(),
        sources=(),
    )
    candidate = planning.solve(snapshot, time_limit=5, objective=objective)
    assert candidate.checker.status == "PASS"
    assert candidate.binding.objective_version == objective.objective_version
    assert candidate.binding.objective_version != "delivery-v1"
    next_epoch = EffectiveObjective.model_validate(
        {**objective.model_dump(exclude={"content_hash"}), "resolution_version": 3}
    )
    assert {i.code for i in check_candidate(snapshot, candidate, objective=next_epoch).issues} == {
        "VERSION_MISMATCH"
    }


def test_stability_prefers_unchanged_plan_only_within_confirmed_tardiness_limit():
    data = example_snapshot().model_dump(exclude={"content_hash"})
    data["profile"]["policy"]["freeze_window_min"] = 0
    data["orders"][0]["due_at"] = at(8)
    initial = Snapshot.model_validate(data)
    baseline = make_candidate(initial, example_assignments(shift=10))
    assert check_candidate(initial, baseline).status == "PASS"
    snapshot = activate(initial, baseline)
    delivery = planning.solve(snapshot, time_limit=5, baseline=baseline)
    objective = preference(
        snapshot,
        "stability_first",
        max_weighted_tardiness=24,
        max_incremental_overtime_minutes=0,
    )
    stable = planning.solve(snapshot, time_limit=5, baseline=baseline, objective=objective)
    assert delivery.checker.status == stable.checker.status == "PASS"
    assert values(delivery)["weighted_tardiness"] == 0
    assert values(delivery)["changed_operations"] > 0
    assert stable.assignments == baseline.assignments
    assert values(stable)["weighted_tardiness"] == 24
    assert values(stable)["changed_operations"] == values(stable)["total_start_shift"] == 0
    assert tuple(m.name for m in stable.objective) == objective.order
    assert stable.solver_passes[0].objective_name == "changed_operations"
    strict = preference(
        snapshot,
        "stability_first",
        max_weighted_tardiness=0,
        max_incremental_overtime_minutes=0,
    )
    corrected = planning.solve(snapshot, time_limit=5, baseline=baseline, objective=strict)
    assert corrected.checker.status == "PASS"
    assert values(corrected)["weighted_tardiness"] == 0
    assert values(corrected)["changed_operations"] > 0
    assert corrected.assignments != baseline.assignments


def test_overtime_priority_trades_only_confirmed_delay_for_actual_worker_minutes():
    snapshot = overtime_facts()
    delivery = planning.solve(snapshot, time_limit=5, allow_overtime=True)
    objective = preference(snapshot, "overtime_first", max_weighted_tardiness=12)
    reduced, evidence = planning.solve_with_evidence(
        snapshot, time_limit=5, allow_overtime=True, objective=objective
    )
    assert delivery.checker.status == reduced.checker.status == "PASS"
    assert values(delivery)["weighted_tardiness"] == 0
    assert values(delivery)["incremental_overtime_metric"] == 4
    assert values(reduced)["weighted_tardiness"] == 12
    assert values(reduced)["incremental_overtime_metric"] == 0
    assert values(reduced)["makespan"] == 10
    assert max(a.end_at for a in reduced.assignments) == at(10)
    assert reduced.required_consents == ("allow_overtime",)
    assert tuple(m.name for m in reduced.objective) == objective.order
    assert [p.objective_name for p in reduced.solver_passes] == [
        "incremental_overtime_metric",
        "weighted_tardiness",
        "makespan",
    ]
    assert reduced.constant_objective_levels == ("changed_operations", "total_start_shift")
    assert reduced.proven_objective_levels == 5
    assert all(m.value == m.lower_bound for m in reduced.objective)
    assert evidence["objective_bounds"] == {"weighted_tardiness": 12}
    assert [m.unit for m in reduced.objective] == [
        "minutes",
        "minutes",
        "operations",
        "minutes",
        "minutes",
    ]


def test_confirmed_bounds_cannot_relax_hard_deadline_or_grant_overtime_permission():
    snapshot = overtime_facts(hard_deadline=True)
    objective = preference(snapshot, "overtime_first", max_weighted_tardiness=1000)
    candidate = planning.solve(snapshot, time_limit=5, allow_overtime=True, objective=objective)
    assert candidate.checker.status == "PASS"
    assert values(candidate)["weighted_tardiness"] == 0
    assert values(candidate)["incremental_overtime_metric"] == 4
    without_permission = planning.solve(snapshot, time_limit=5, objective=objective)
    assert without_permission.native_status == "INFEASIBLE"
    assert not without_permission.has_solution
    incompatible = preference(
        snapshot, "overtime_first", max_weighted_tardiness=1000, max_incremental_overtime_minutes=0
    )
    blocked = planning.solve(snapshot, time_limit=5, allow_overtime=True, objective=incompatible)
    assert blocked.native_status == "INFEASIBLE"
    assert not blocked.has_solution


@pytest.mark.parametrize("bound, feasible", [(18, True), (17, False), (10**100, True)])
def test_tardiness_boundary_is_an_exact_hard_cp_constraint(bound, feasible):
    snapshot = change_snapshot(
        example_snapshot(), lambda data: data["orders"][0].update(due_at=at(0))
    )
    objective = preference(snapshot, max_weighted_tardiness=bound)
    candidate = planning.solve(snapshot, time_limit=5, objective=objective)
    assert candidate.has_solution is feasible
    assert candidate.native_status == ("OPTIMAL" if feasible else "INFEASIBLE")
    if feasible:
        assert candidate.checker.status == "PASS"
        assert values(candidate)["weighted_tardiness"] == 18
    else:
        assert not candidate.assignments
        assert candidate.proven_objective_levels == 0


def test_negative_incremental_overtime_cap_means_reduction_from_same_active_baseline():
    initial = overtime_facts()
    baseline = make_candidate(initial, allow_overtime=True)
    assert check_candidate(initial, baseline, allow_overtime=True).status == "PASS"
    snapshot = activate(initial, baseline)
    objective = preference(
        snapshot, "overtime_first", max_weighted_tardiness=12, max_incremental_overtime_minutes=-4
    )
    candidate = planning.solve(
        snapshot, time_limit=5, allow_overtime=True, baseline=baseline, objective=objective
    )
    assert candidate.checker.status == "PASS"
    assert values(candidate)["incremental_overtime_metric"] == -4
    assert values(candidate)["weighted_tardiness"] == 12
    assert candidate.objective[0].lower_bound == -4
    impossible = preference(
        snapshot, "overtime_first", max_weighted_tardiness=12, max_incremental_overtime_minutes=-5
    )
    rejected = planning.solve(
        snapshot, time_limit=5, allow_overtime=True, baseline=baseline, objective=impossible
    )
    assert rejected.native_status == "INFEASIBLE"


def test_negative_cap_without_baseline_does_not_invent_an_overtime_credit():
    snapshot = example_snapshot()
    objective = preference(snapshot, max_incremental_overtime_minutes=-(10**100))
    candidate = planning.solve(snapshot, time_limit=5, objective=objective)
    assert candidate.native_status == "INFEASIBLE"
    assert not candidate.has_solution


@pytest.mark.parametrize(
    "name,bound", [("weighted_tardiness", 17), ("incremental_overtime_metric", 3)]
)
def test_independent_checker_rejects_legal_manual_plan_outside_confirmed_bound(name, bound):
    snapshot = (
        change_snapshot(example_snapshot(), lambda data: data["orders"][0].update(due_at=at(0)))
        if name == "weighted_tardiness"
        else overtime_facts()
    )
    field = (
        "max_weighted_tardiness"
        if name == "weighted_tardiness"
        else "max_incremental_overtime_minutes"
    )
    objective = preference(snapshot, **{field: bound})
    candidate = manual(snapshot, objective, allow_overtime=True)
    report = check_candidate(snapshot, candidate, objective=objective, allow_overtime=True)
    assert report.status == "FAIL"
    violations = [i for i in report.issues if i.code == "OBJECTIVE_BOUND_EXCEEDED"]
    assert len(violations) == 1 and violations[0].object_id == name
    assert not {i.code for i in report.issues} - {"OBJECTIVE_BOUND_EXCEEDED"}


def test_checker_recomputes_negative_overtime_delta_instead_of_trusting_candidate_value():
    initial = overtime_facts()
    baseline = make_candidate(initial, allow_overtime=True)
    snapshot = activate(initial, baseline)
    objective = preference(snapshot, max_incremental_overtime_minutes=-1)
    candidate = manual(snapshot, objective, baseline=baseline, allow_overtime=True)
    data = candidate.model_dump(exclude={"content_hash"})
    data["objective"][1]["value"] = -1
    tampered = Candidate.model_validate(data)
    report = check_candidate(
        snapshot, tampered, objective=objective, baseline=baseline, allow_overtime=True
    )
    assert {i.code for i in report.issues} == {"OBJECTIVE_BOUND_EXCEEDED", "METRIC_MISMATCH"}


@pytest.mark.parametrize(
    "field", ["factory_id", "profile_version", "policy_version", "process", "hash"]
)
def test_solver_and_checker_reject_objective_reference_and_hash_mismatch(field):
    snapshot = example_snapshot()
    original = preference(snapshot)
    data = original.model_dump(exclude={"content_hash"})
    if field == "factory_id":
        data[field] = "another-factory"
        data["sources"][0]["scope_id"] = "another-factory"
    elif field == "process":
        data["sources"][0].update(
            scope_type="PROCESS",
            scope_id="process-preference",
            product_id="item-a",
            route_version="unknown-route",
        )
    elif field != "hash":
        data[field] = "unknown-version"
    objective = EffectiveObjective.model_validate(data)
    if field == "hash":
        objective = objective.model_copy(update={"content_hash": "0" * 64})
    with pytest.raises(planning.PlanningInputError) as exc:
        planning.solve(snapshot, time_limit=5, objective=objective)
    assert exc.value.code == "OBJECTIVE_MISMATCH"
    result = check_candidate(snapshot, manual(snapshot, original), objective=objective)
    assert {issue.code for issue in result.issues} == {"OBJECTIVE_MISMATCH"}


def test_unknown_objective_version_and_wrong_metric_order_are_rejected():
    snapshot = example_snapshot()
    objective = preference(snapshot, "overtime_first", max_weighted_tardiness=0)
    candidate = manual(snapshot, objective)
    unknown = check_candidate(snapshot, candidate)
    assert unknown.status == "FAIL"
    assert "VERSION_MISMATCH" in {issue.code for issue in unknown.issues}
    wrong = preference(snapshot, "overtime_first", max_weighted_tardiness=1)
    assert "VERSION_MISMATCH" in {
        issue.code for issue in check_candidate(snapshot, candidate, objective=wrong).issues
    }
    data = candidate.model_dump(exclude={"content_hash"})
    data["objective"] = list(data["objective"])
    data["objective"][0], data["objective"][1] = data["objective"][1], data["objective"][0]
    reordered = Candidate.model_validate(data)
    assert {i.code for i in check_candidate(snapshot, reordered, objective=objective).issues} == {
        "METRIC_MISMATCH"
    }


def test_custom_priority_retains_original_metric_units_and_exact_proof_order():
    snapshot = example_snapshot()
    order = (
        "makespan",
        "total_start_shift",
        "changed_operations",
        "incremental_overtime_metric",
        "weighted_tardiness",
    )
    objective = preference(
        snapshot,
        "custom",
        objective_order=order,
        max_weighted_tardiness=0,
        max_incremental_overtime_minutes=0,
    )
    candidate = planning.solve(snapshot, time_limit=5, objective=objective)
    assert candidate.checker.status == "PASS"
    assert tuple(metric.name for metric in candidate.objective) == order
    assert [metric.value for metric in candidate.objective] == [6, 0, 0, 0, 0]
    assert [metric.unit for metric in candidate.objective] == [
        "minutes",
        "minutes",
        "operations",
        "minutes",
        "minutes",
    ]
    assert candidate.solver_passes[0].objective_name == "makespan"
    assert candidate.proven_objective_levels == 5
    assert candidate.constant_objective_levels == (
        "total_start_shift",
        "changed_operations",
        "incremental_overtime_metric",
    )


def test_later_timeout_cannot_claim_proof_for_a_different_objective_order(monkeypatch):
    snapshot = overtime_facts()
    objective = preference(snapshot, "overtime_first", max_weighted_tardiness=12)
    configured = planning._configured_solver
    budgets = []

    def stop_second(seconds):
        native = configured(seconds)
        budgets.append(seconds)
        if len(budgets) == 2:
            native.parameters.max_time_in_seconds = 0
        return native

    monkeypatch.setattr(planning, "_configured_solver", stop_second)
    candidate = planning.solve(snapshot, time_limit=5, allow_overtime=True, objective=objective)
    assert candidate.checker.status == "PASS"
    assert candidate.native_status == "OPTIMAL"
    assert candidate.last_search_status == "UNKNOWN"
    assert candidate.proven_objective_levels == 1
    assert [p.objective_name for p in candidate.solver_passes] == [
        "incremental_overtime_metric",
        "weighted_tardiness",
    ]
    assert [p.native_status for p in candidate.solver_passes] == ["OPTIMAL", "UNKNOWN"]
    assert candidate.objective[0].value == candidate.objective[0].lower_bound == 0
    assert all(m.lower_bound is None for m in candidate.objective[1:])
    assert candidate.constant_objective_levels == ()
    assert 0 < budgets[1] < budgets[0] < 5


def test_preference_reordering_preserves_wip_history_and_does_not_reconsume_materials():
    snapshot, baseline, assignments = wip_case()
    objective = preference(
        snapshot, "stability_first", max_weighted_tardiness=0, max_incremental_overtime_minutes=0
    )
    candidate = planning.solve(snapshot, time_limit=5, baseline=baseline, objective=objective)
    assert candidate.checker.status == "PASS"
    assert tuple(candidate.assignments) == tuple(assignments)
    assert candidate.assignments[0].end_at == at(2)
    assert candidate.assignments[1].start_at == at(2)
    assert candidate.assignments[1].resume_at == at(3)
    assert candidate.assignments[1].end_at - candidate.assignments[1].resume_at == timedelta(
        minutes=1
    )
    assert values(candidate)["changed_operations"] == 0
    assert snapshot.inventory[0].reserved == 2
    assert snapshot.inventory[0].on_hand == 2


def test_stability_preferences_cannot_relax_the_existing_freeze_window():
    data = example_snapshot().model_dump(exclude={"content_hash"})
    data["orders"][0]["due_at"] = at(8)
    initial = Snapshot.model_validate(data)
    baseline = make_candidate(initial, example_assignments(shift=10))
    snapshot = activate(initial, baseline)
    objective = preference(
        snapshot, "stability_first", max_weighted_tardiness=0, max_incremental_overtime_minutes=0
    )
    candidate = planning.solve(snapshot, time_limit=5, baseline=baseline, objective=objective)
    assert candidate.native_status == "INFEASIBLE"
    assert not candidate.has_solution
