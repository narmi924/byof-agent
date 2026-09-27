"""A real source outage interrupts its work while independent production keeps running."""

from datetime import timedelta

import pytest
import test_dynamic_factory_postgres as source_fixture
from test_dynamic_factory_postgres import control, plan_input, send_plan, snapshot
from test_dynamic_factory_postgres import dynamic_source as dynamic_source

from packages.domain.models import Candidate, Snapshot, batch_operations, minute_offset
from packages.domain.skf import load_skf_snapshot
from packages.planning.checker import check_candidate
from packages.planning.solver import PlanningInputError, solve


@pytest.fixture
def three_batch_source(monkeypatch, request):
    def development_input(*, development):
        assert development
        original = load_skf_snapshot(development=True)
        data = original.model_dump(mode="python", exclude={"content_hash"})
        data["orders"][0]["quantity"] = original.orders[0].quantity * 3
        enlarged = Snapshot.model_validate(data)
        assert enlarged.profile == original.profile
        assert enlarged.resources == original.resources and enlarged.workers == original.workers
        assert enlarged.horizon == original.horizon and enlarged.inventory == original.inventory
        return enlarged

    # Select the legal test input before the shared fixture imports its isolated factory.
    monkeypatch.setattr(source_fixture, "load_skf_snapshot", development_input)
    return request.getfixturevalue("dynamic_source")


def step(source, identity, minutes):
    response = control(source, identity, "clock.step", {"minutes": minutes})
    assert response.status_code == 200, response.json()
    return response.json()


def physical_work(actual):
    return sum((s.end_at - s.start_at).total_seconds() for s in actual.segments)


def assert_inventory_ledger(initial, current):
    received_before = {r.receipt_id for r in initial.receipts if r.status == "RECEIVED"}
    consumption_ids = [c.event_id for a in current.actuals for c in a.consumed]
    assert len(consumption_ids) == len(set(consumption_ids))
    for before in initial.inventory:
        stock = next(i for i in current.inventory if i.material_id == before.material_id)
        receipts = sum(
            r.quantity
            for r in current.receipts
            if r.material_id == before.material_id
            and r.status == "RECEIVED"
            and r.receipt_id not in received_before
        )
        consumed = sum(
            c.quantity
            for a in current.actuals
            for c in a.consumed
            if c.material_id == before.material_id
        )
        owned = sum(r.quantity for r in current.reservations if r.material_id == before.material_id)
        assert stock.on_hand == before.on_hand + receipts - consumed
        assert stock.reserved == before.reserved + owned
        assert stock.on_hand >= stock.reserved


def test_outage_unknown_remaining_independent_progress_and_confirmed_replanning(three_batch_source):
    source = three_batch_source
    initial = snapshot(source)
    batches, operations = batch_operations(initial)
    assert len(batches) == 3 and len(operations) == 24
    submission = plan_input(source, operation_id="accept-three-batches")
    baseline = Candidate.model_validate(submission["candidate"])
    assert baseline.checker.status == "PASS"
    accepted = send_plan(source, submission)
    assert accepted.status_code == 200 and accepted.json()["source_state"] == "ACTIVE"
    assert snapshot(source).active_plan_hash == baseline.content_hash

    step(source, "run-to-material-consuming-work", 13)
    before_fault = snapshot(source)
    interrupted = next(a for a in before_fault.actuals if a.state == "IN_PROGRESS" and a.consumed)
    independent = next(
        a
        for a in before_fault.actuals
        if a.state == "IN_PROGRESS"
        and a.resource_id != interrupted.resource_id
        and a.worker_id != interrupted.worker_id
    )
    assert interrupted.remaining_minutes > 0 and interrupted.remaining_setup_minutes == 0
    fault_payload = {"resource_id": interrupted.resource_id}
    fault = control(source, "equipment-failed", "resource.down", fault_payload)
    assert fault.status_code == 200
    blocked = snapshot(source)
    stopped = next(a for a in blocked.actuals if a.operation_id == interrupted.operation_id)
    assert stopped.state == "BLOCKED" and stopped.remaining_minutes is None
    assert stopped.remaining_confirmed_by is None
    assert (
        stopped.actual_start == interrupted.actual_start
        and stopped.consumed == interrupted.consumed
    )
    assert stopped.segments == interrupted.segments

    wait_result = step(source, "unrelated-production-continues", 5)
    progressed = snapshot(source)
    progressed_independent = next(
        a for a in progressed.actuals if a.operation_id == independent.operation_id
    )
    assert physical_work(progressed_independent) > physical_work(independent)
    assert progressed.snapshot_clock == before_fault.snapshot_clock + timedelta(minutes=5)
    still_blocked = next(
        a for a in progressed.actuals if a.operation_id == interrupted.operation_id
    )
    assert still_blocked.state == "BLOCKED" and still_blocked.segments == interrupted.segments
    assert still_blocked.consumed == interrupted.consumed
    assert_inventory_ledger(initial, progressed)

    assert (
        control(source, "equipment-failed", "resource.down", fault_payload).json() == fault.json()
    )
    assert step(source, "unrelated-production-continues", 5) == wait_result
    assert snapshot(source) == progressed
    with pytest.raises(PlanningInputError) as missing:
        solve(progressed, baseline=baseline, time_limit=5)
    assert missing.value.code == "WIP_CONFIRMATION_REQUIRED"
    assert snapshot(source) == progressed

    # Equipment remains unavailable until minute 120; the freeze rule is unchanged.
    target = initial.snapshot_clock + timedelta(minutes=120)
    assert max(a.start_at for a in baseline.assignments) < target
    remaining = minute_offset(progressed.snapshot_clock, target, round_up=True)
    tick = 0
    while remaining:
        chunk = min(remaining, 60)
        step(source, f"outage-wait-{tick}", chunk)
        remaining -= chunk
        tick += 1
    waiting = snapshot(source)
    assert waiting.snapshot_clock == target
    assert_inventory_ledger(initial, waiting)
    recovered = control(source, "equipment-repaired", "resource.restore", fault_payload)
    assert recovered.status_code == 200
    with pytest.raises(PlanningInputError) as still_unknown:
        solve(snapshot(source), baseline=baseline, time_limit=5)
    assert still_unknown.value.code == "WIP_CONFIRMATION_REQUIRED"
    confirmed = control(
        source,
        "technician-confirmed-remaining",
        "execution.confirm_remaining",
        {
            "operation_id": interrupted.operation_id,
            "remaining_minutes": interrupted.remaining_minutes,
            "remaining_setup_minutes": 0,
        },
    )
    assert confirmed.status_code == 200
    ready = snapshot(source)
    source_before_solve = ready.model_dump_json()
    candidate = solve(ready, baseline=baseline, time_limit=5)
    assert candidate.has_solution, (candidate.native_status, candidate.termination_reason)
    assert candidate.checker.status == "PASS", candidate.checker.issues
    assert check_candidate(ready, candidate, baseline=baseline).status == "PASS"
    assert len(candidate.assignments) == 24
    assignments = {a.operation_id: a for a in candidate.assignments}
    for actual in ready.actuals:
        planned = assignments[actual.operation_id]
        assert planned.resource_id == actual.resource_id and planned.worker_id == actual.worker_id
        assert planned.changeover_start == actual.changeover_start
        if actual.actual_start is not None:
            assert planned.start_at == actual.actual_start
        if actual.state == "COMPLETED":
            assert planned.end_at == actual.actual_end and planned.resume_at is None
    resumed = assignments[interrupted.operation_id]
    assert resumed.start_at == interrupted.actual_start
    assert resumed.end_at - resumed.resume_at == timedelta(minutes=interrupted.remaining_minutes)
    assert snapshot(source).model_dump_json() == source_before_solve
    assert_inventory_ledger(initial, ready)
    assert ready.profile.policy.freeze_window_min == initial.profile.policy.freeze_window_min == 60
