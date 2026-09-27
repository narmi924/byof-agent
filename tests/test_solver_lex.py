"""Search evidence distinguishes an incumbent from proof of later objectives."""

from datetime import timedelta

from ortools.sat.python import cp_model

from packages.domain.models import Snapshot
from packages.domain.skf import load_skf_snapshot
from packages.planning import solver as planning


def test_constants_do_not_create_search_passes_or_hide_nonconstant_makespan():
    candidate, evidence = planning.solve_with_evidence(
        load_skf_snapshot(development=True), time_limit=5
    )
    assert candidate.checker.status == "PASS"
    assert [p.objective_name for p in candidate.solver_passes] == [
        "weighted_tardiness",
        "makespan",
    ]
    assert all(p.native_status == "OPTIMAL" for p in candidate.solver_passes)
    assert candidate.last_search_status == "OPTIMAL"
    assert candidate.proven_objective_levels == 5
    assert candidate.objective[-1].value == candidate.objective[-1].lower_bound == 66
    assert evidence["constant_objective_levels"] == [
        "incremental_overtime_metric",
        "changed_operations",
        "total_start_shift",
    ]
    assert evidence["solver_wall_seconds"] == sum(
        p.wall_time_seconds for p in candidate.solver_passes
    )


def test_large_exact_objectives_no_longer_depend_on_mixed_radix_range():
    data = load_skf_snapshot(development=True).model_dump(mode="json", exclude={"content_hash"})
    data["orders"][0].update(due_at=data["horizon"]["start_at"], priority_weight=10**10)
    snapshot = Snapshot.model_validate(data)
    candidate = planning.solve(snapshot, time_limit=5)
    assert candidate.checker.status == "PASS"
    assert candidate.proven_objective_levels == 5
    assert candidate.objective[0].value == 66 * 10**10
    assert candidate.objective[0].lower_bound == 66 * 10**10
    assert candidate.objective[-1].value == 66


def test_feasible_first_level_stops_before_lower_priority_optimization(monkeypatch):
    data = load_skf_snapshot(development=True).model_dump(mode="json", exclude={"content_hash"})
    data["orders"][0].update(due_at=data["horizon"]["start_at"], priority_weight=1)
    snapshot = Snapshot.model_validate(data)
    original_dispatch = planning.dispatch

    def later_feasible_hint(snapshot, **kwargs):
        return tuple(
            assignment.model_copy(
                update={
                    "changeover_start": assignment.changeover_start + timedelta(minutes=100),
                    "start_at": assignment.start_at + timedelta(minutes=100),
                    "end_at": assignment.end_at + timedelta(minutes=100),
                }
            )
            for assignment in original_dispatch(snapshot, **kwargs)
        )

    class StopAfterFirst(cp_model.CpSolverSolutionCallback):
        def on_solution_callback(self):
            self.stop_search()

    native_solve = cp_model.CpSolver.solve
    calls = []

    def stop_after_first(self, model, callback=None):
        status = native_solve(self, model, StopAfterFirst())
        calls.append(status)
        return status

    monkeypatch.setattr(planning, "dispatch", later_feasible_hint)
    monkeypatch.setattr(cp_model.CpSolver, "solve", stop_after_first)
    candidate = planning.solve(snapshot, time_limit=5)
    assert calls == [cp_model.FEASIBLE]
    assert candidate.checker.status == "PASS"
    assert candidate.native_status == candidate.last_search_status == "FEASIBLE"
    assert candidate.proven_objective_levels == 0
    assert candidate.objective[0].value > 66
    assert all(metric.lower_bound is None for metric in candidate.objective[1:])
    assert len(candidate.solver_passes) == 1


def test_later_unknown_preserves_prior_incumbent_and_exact_proof_prefix(monkeypatch):
    original = planning._configured_solver
    budgets = []

    def limited_later_search(seconds):
        solver = original(seconds)
        budgets.append(seconds)
        if len(budgets) == 2:
            solver.parameters.max_time_in_seconds = 0
        return solver

    monkeypatch.setattr(planning, "_configured_solver", limited_later_search)
    candidate = planning.solve(load_skf_snapshot(development=True), time_limit=5)
    assert candidate.checker.status == "PASS"
    assert candidate.has_solution and len(candidate.assignments) == 8
    assert candidate.native_status == "OPTIMAL"
    assert candidate.last_search_status == "UNKNOWN"
    assert candidate.termination_reason == "TIME_LIMIT"
    assert candidate.proven_objective_levels == 4
    assert [p.native_status for p in candidate.solver_passes] == ["OPTIMAL", "UNKNOWN"]
    assert candidate.solver_passes[-1].objective_value is None
    assert candidate.solver_passes[-1].best_bound is None
    assert not candidate.solver_passes[-1].has_solution
    assert [m.lower_bound for m in candidate.objective] == [0, 0, 0, 0, None]
    assert 0 < budgets[1] < budgets[0] < 5


def test_no_incumbent_does_not_create_constant_proofs():
    candidate = planning.solve(load_skf_snapshot(development=True), time_limit=0.000001)
    assert candidate.native_status == candidate.last_search_status == "UNKNOWN"
    assert not candidate.has_solution and not candidate.assignments
    assert candidate.proven_objective_levels == 0
    assert len(candidate.solver_passes) == 1
    assert not candidate.solver_passes[0].has_solution
    assert all(m.value is None and m.lower_bound is None for m in candidate.objective)
