"""Every supported adverse fact has a truthful next action while planning continues."""

from test_checker import at, example_assignments, example_snapshot, make_candidate

from packages.agent.recovery_paths import recovery_paths
from packages.domain.models import ActualExecution


def kinds(snapshot, baseline=None, *, solver_state=None):
    return {path["kind"] for path in recovery_paths(snapshot, baseline, solver_state=solver_state)}


def test_supply_loss_has_quantity_and_two_conditional_business_routes():
    source = example_snapshot(batches=2)
    low = source.inventory[0].model_copy(update={"on_hand": 1})
    changed = source.model_copy(update={"inventory": (low,)})
    paths = recovery_paths(changed)
    assert {p["kind"] for p in paths} == {"material_supply", "material_customer_terms"}
    assert "short by at least 3 EA" in paths[0]["evidence"]
    assert "Shop floor" in {step["owner"] for step in paths[0]["steps"]}
    assert "Do not assume purchasing is done" in paths[0]["prompt"]


def test_machine_and_worker_outage_keep_separate_recovery_routes():
    source = example_snapshot()
    machine = source.resources[0].model_copy(update={"status": "DOWN"})
    worker = source.workers[0].model_copy(update={"status": "ABSENT"})
    changed = source.model_copy(
        update={
            "resources": (machine, *source.resources[1:]),
            "workers": (worker, *source.workers[1:]),
        }
    )
    assert {"equipment_recovery", "workforce_recovery"} <= kinds(changed)


def test_blocked_wip_and_failed_quality_require_source_evidence_before_replan():
    source = example_snapshot()
    blocked = ActualExecution(
        operation_id="order-a-R001-B001-begin",
        batch_id="order-a-R001-B001",
        route_version="r1",
        state="BLOCKED",
        actual_start=at(0),
        resource_id="r1",
        worker_id="w1",
        completed_quantity=0,
        version=1,
        changeover_start=at(0),
    )
    failed = ActualExecution(
        operation_id="order-a-R001-B001-check",
        batch_id="order-a-R001-B001",
        route_version="r1",
        state="COMPLETED",
        actual_start=at(2),
        actual_end=at(4),
        resource_id="r2",
        worker_id="w2",
        completed_quantity=2,
        quality_state="FAILED",
        version=1,
        changeover_start=at(2),
    )
    paths = recovery_paths(source.model_copy(update={"actuals": (blocked, failed)}))
    assert {"confirm_wip", "quality_recovery"} <= {path["kind"] for path in paths}
    assert (
        "If unrecoverable, record the scrap basis"
        in next(p for p in paths if p["kind"] == "quality_recovery")["steps"][0]["action"]
    )


def test_revoked_capacity_window_is_separate_from_worker_absence():
    source = example_snapshot()
    baseline = make_candidate(source, example_assignments(shift=30))
    resources = tuple(
        row.model_copy(update={"calendar": row.calendar[:1]}) for row in source.resources
    )
    workers = tuple(row.model_copy(update={"calendar": row.calendar[:1]}) for row in source.workers)
    changed = source.model_copy(update={"resources": resources, "workers": workers})
    assert "capacity_window_recovery" in kinds(changed, baseline)


def test_solver_timeout_is_not_presented_as_proven_infeasibility():
    source = example_snapshot()
    unknown = recovery_paths(source, solver_state="UNKNOWN")
    assert [path["kind"] for path in unknown] == ["retry_computation"]
    # A timeout reads as "try another condition", never as a proven dead end.
    assert "try again" in unknown[0]["evidence"] and "no solution" not in unknown[0]["evidence"]
    proved = recovery_paths(source, solver_state="INFEASIBLE")
    assert [path["kind"] for path in proved] == ["commitment_recovery"]
