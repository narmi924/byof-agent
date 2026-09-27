"""A declared synthetic branching assembly runs through the unchanged solver and engine."""

import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.domain.models import Candidate, Snapshot, batch_operations, duration_minutes
from packages.domain.skf import load_skf_snapshot, verify_skf_baseline
from packages.planning.checker import check_candidate
from packages.planning.solver import solve
from services.factory_sim.engine import advance, evolve

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "data/development/assembly-branch.json"
EVIDENCE = ROOT / "data/development/assembly-branch.md"


def load_configuration() -> Snapshot:
    return Snapshot.model_validate_json(CONFIG.read_bytes())


@pytest.fixture(scope="module")
def execution():
    initial = load_configuration()
    candidate = solve(initial, time_limit=2, allow_overtime=False)
    assert candidate.has_solution, (candidate.native_status, candidate.termination_reason)
    assert candidate.checker.status == "PASS", candidate.checker
    independent = check_candidate(initial, candidate, allow_overtime=False)
    assert independent.status == "PASS", independent
    assert len(candidate.assignments) == 10
    # This is an engine unit test; HTTP acceptance and administrator activation are separate.
    current = evolve(
        initial,
        active_plan_version="synthetic-engine-plan-1",
        active_plan_hash=candidate.content_hash,
    )
    history = [current]
    for _ in range(60):
        current = advance(current, candidate)
        history.append(current)
        if len(current.actuals) == 10 and all(row.state == "COMPLETED" for row in current.actuals):
            break
    assert len(current.actuals) == 10
    assert all(row.state == "COMPLETED" for row in current.actuals)
    assert current.snapshot_clock <= initial.horizon.end_at
    return initial, candidate, history


def test_synthetic_profile_has_exact_lots_two_materials_and_an_unordered_branching_route():
    initial = load_configuration()
    document = json.loads(CONFIG.read_text(encoding="utf-8"))
    assert initial.content_hash == document["content_hash"]
    digest = hashlib.sha256(EVIDENCE.read_bytes()).hexdigest()
    assert initial.profile.source_digest == initial.source.evidence_digest == digest
    assert initial.profile.evidence_mode == "synthetic"
    assert initial.profile.activation_state == "DRAFT"
    assert initial.source.ownership == "simulator_fact"
    assert not initial.actuals and not initial.reservations and not initial.receipts
    assert initial.profile.timezone == "Asia/Singapore"
    assert initial.profile.products[0].batch_size == 10
    assert initial.profile.policy.freeze_window_min == 10
    assert not initial.profile.policy.progress_revalidation_enabled
    batches, operations = batch_operations(initial)
    assert len(initial.orders) == 1 and initial.orders[0].quantity == 20
    assert len(batches) == 2 and all(batch.quantity == 10 for batch in batches)
    assert len(operations) == 10
    routes = {row.step_id: row for row in initial.profile.routes}
    assert initial.profile.routes[0].step_id == "join"
    assert routes["kit"].predecessors == ()
    assert routes["shell"].predecessors == routes["contacts"].predecessors == ("kit",)
    assert set(routes["join"].predecessors) == {"shell", "contacts"}
    assert routes["verify"].predecessors == ("join",)
    assert sum(duration_minutes(route, 10) for route in routes.values()) == 14
    assert all(route.quality_threshold is None for route in routes.values())
    assert {row.material_id: row.on_hand for row in initial.inventory} == {
        "shell-part": 20,
        "contact-part": 40,
    }


def test_original_skf_baseline_remains_intact_and_distinct():
    verify_skf_baseline()
    skf = load_skf_snapshot()
    batches, operations = batch_operations(skf)
    assert (
        len(skf.orders),
        sum(row.quantity for row in skf.orders),
        len(batches),
        len(operations),
    ) == (
        6,
        5400,
        108,
        864,
    )
    assert skf.profile.products[0].batch_size == 50
    assert skf.profile != load_configuration().profile


def test_real_solver_checker_and_engine_finish_exactly_twenty_pieces(execution):
    initial, candidate, history = execution
    assert candidate.native_status in {"OPTIMAL", "FEASIBLE"}
    assert candidate.required_consents == ()
    assert candidate.binding.snapshot_hash == initial.content_hash
    assert candidate.binding.profile_version == "assembly-branch-1"
    final = history[-1]
    assert final.orders[0].status == "COMPLETED"
    assert final.orders[0].quantity == 20
    _, operations = batch_operations(initial)
    by_id = {row.operation_id: row for row in operations}
    verified = [row for row in final.actuals if by_id[row.operation_id].step_id == "verify"]
    assert len(verified) == 2 and sum(row.completed_quantity for row in verified) == 20
    assert all(
        row.quality_state == "PASSED" and row.remaining_minutes == 0 for row in final.actuals
    )
    assert all(row.actual_end <= initial.orders[0].due_at for row in final.actuals)
    assert final.profile == initial.profile
    assert not initial.actuals and not initial.reservations


def test_full_kit_reserves_both_materials_before_branch_consumption(execution):
    initial, _, history = execution
    batches, operations = batch_operations(initial)
    step = {row.operation_id: row.step_id for row in operations}
    for batch in batches:
        first = next(
            state
            for state in history
            if any(
                row.batch_id == batch.batch_id
                and step[row.operation_id] == "kit"
                and row.actual_start is not None
                for row in state.actuals
            )
        )
        kit = next(
            row
            for row in first.actuals
            if row.batch_id == batch.batch_id and step[row.operation_id] == "kit"
        )
        reserved = {
            row.material_id: row for row in first.reservations if row.batch_id == batch.batch_id
        }
        assert {key: row.quantity for key, row in reserved.items()} == {
            "shell-part": 10,
            "contact-part": 20,
        }
        assert all(row.created_at == kit.actual_start for row in reserved.values())
        assert kit.consumed == ()
        assert not any(row.consumed for row in first.actuals if row.batch_id == batch.batch_id)


def test_each_consumption_node_and_every_intermediate_inventory_balance_are_exact(execution):
    initial, _, history = execution
    original = {row.material_id: row.on_hand for row in initial.inventory}
    _, operations = batch_operations(initial)
    steps = {row.operation_id: row.step_id for row in operations}
    for current in history:
        for stock in current.inventory:
            consumed = sum(
                consumed.quantity
                for actual in current.actuals
                for consumed in actual.consumed
                if consumed.material_id == stock.material_id
            )
            reserved = sum(
                row.quantity for row in current.reservations if row.material_id == stock.material_id
            )
            assert stock.on_hand == original[stock.material_id] - consumed
            assert stock.reserved == reserved
            assert 0 <= reserved <= stock.on_hand
    final = history[-1]
    for actual in final.actuals:
        expected = {
            "shell": {"shell-part": 10},
            "contacts": {"contact-part": 20},
        }.get(steps[actual.operation_id], {})
        assert {row.material_id: row.quantity for row in actual.consumed} == expected
    events = [row.event_id for actual in final.actuals for row in actual.consumed]
    assert len(events) == len(set(events)) == 4
    assert len(final.reservations) == 4
    assert all(row.quantity == 0 for row in final.reservations)
    assert all(row.on_hand == row.reserved == 0 for row in final.inventory)


def test_branches_overlap_and_merge_waits_for_both_actual_predecessors(execution):
    initial, _, history = execution
    batches, operations = batch_operations(initial)
    actuals = {row.operation_id: row for row in history[-1].actuals}
    overlaps = []
    for batch in batches:
        by_step = {
            row.step_id: actuals[row.operation_id]
            for row in operations
            if row.batch_id == batch.batch_id
        }
        kit, shell, contacts, join, verify = (
            by_step[name] for name in ("kit", "shell", "contacts", "join", "verify")
        )
        assert shell.actual_start >= kit.actual_end
        assert contacts.actual_start >= kit.actual_end
        assert join.actual_start >= max(shell.actual_end, contacts.actual_end)
        assert verify.actual_start >= join.actual_end
        assert shell.quality_state == "PASSED"
        assert shell.resource_id != contacts.resource_id and shell.worker_id != contacts.worker_id
        overlaps.append(
            max(shell.actual_start, contacts.actual_start)
            < min(shell.actual_end, contacts.actual_end)
        )
    assert any(overlaps)


def test_skill_resource_and_full_setup_production_calendar_are_used_by_actual_execution(execution):
    initial, candidate, history = execution
    _, operations = batch_operations(initial)
    steps = {row.operation_id: row.step_id for row in operations}
    routes = {row.step_id: row for row in initial.profile.routes}
    resources = {row.resource_id: row for row in initial.resources}
    workers = {row.worker_id: row for row in initial.workers}
    assignments = {row.operation_id: row for row in candidate.assignments}
    for actual in history[-1].actuals:
        route = routes[steps[actual.operation_id]]
        resource, worker = resources[actual.resource_id], workers[actual.worker_id]
        assert resource.resource_type == route.resource_type
        assert route.operation_code in resource.operation_codes and route.skill in worker.skills
        assignment = assignments[actual.operation_id]
        assert actual.changeover_start == assignment.changeover_start
        assert actual.actual_start == assignment.start_at and actual.actual_end == assignment.end_at
        for entity in (resource, worker):
            assert any(
                window.start_at <= actual.changeover_start
                and actual.actual_end <= window.end_at
                and window.kind == "NORMAL"
                for window in entity.calendar
            )
        work = sum(
            (row.end_at - row.start_at for row in actual.segments if row.phase == "PRODUCTION"),
            timedelta(),
        )
        setup = sum(
            (row.end_at - row.start_at for row in actual.segments if row.phase == "SETUP"),
            timedelta(),
        )
        assert work == timedelta(minutes=duration_minutes(route, 10))
        assert setup == timedelta(minutes=1)


@pytest.mark.parametrize(
    "kind,code",
    [
        ("tail", "UNSUPPORTED_BATCH_QUANTITY"),
        ("missing_duration", "Field required"),
        ("cycle", "CYCLIC_ROUTE"),
        ("missing_worker_skill", "INVALID_REFERENCE"),
        ("missing_resource_qualification", "INVALID_REFERENCE"),
        ("extra_entry", "UNSUPPORTED_CAPABILITY"),
    ],
)
def test_invalid_synthetic_inputs_are_refused_without_changing_the_saved_input(kind, code):
    original_bytes = CONFIG.read_bytes()
    raw = load_configuration().model_dump(mode="json", exclude={"content_hash"})
    if kind == "tail":
        raw["orders"][0]["quantity"] = 19
    elif kind == "missing_duration":
        del raw["profile"]["routes"][0]["cycle_sec_per_unit"]
    elif kind == "cycle":
        next(row for row in raw["profile"]["routes"] if row["step_id"] == "kit")["predecessors"] = [
            "verify"
        ]
    elif kind == "missing_worker_skill":
        raw["workers"][0]["skills"] = ["WAREHOUSE_ONLY"]
    elif kind == "missing_resource_qualification":
        raw["resources"][0]["operation_codes"] = ["UNKNOWN_OPERATION"]
    else:
        next(row for row in raw["profile"]["routes"] if row["step_id"] == "shell")[
            "predecessors"
        ] = []
    with pytest.raises(ValidationError, match=code):
        Snapshot.model_validate(raw)
    assert CONFIG.read_bytes() == original_bytes


def test_independent_checker_rejects_a_missing_branch_operation(execution):
    initial, candidate, _ = execution
    _, operations = batch_operations(initial)
    missing = next(row.operation_id for row in operations if row.step_id == "contacts")
    raw = candidate.model_dump(mode="json", exclude={"content_hash"})
    raw["assignments"] = [row for row in raw["assignments"] if row["operation_id"] != missing]
    altered = Candidate.model_validate(raw)
    report = check_candidate(initial, altered)
    assert report.status == "FAIL"
    assert any(
        row.code == "MISSING_OPERATION" and row.object_id == missing for row in report.issues
    )
