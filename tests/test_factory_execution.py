from datetime import timedelta

import pytest

from packages.domain.models import Snapshot, batch_operations
from packages.domain.skf import load_skf_snapshot
from packages.planning.solver import solve
from services.factory_sim.engine import SimulationError, advance, evolve, inject


@pytest.fixture
def executing():
    data = load_skf_snapshot(development=True).model_dump(mode="json", exclude={"content_hash"})
    data["source"].update(source_revision="1", cursor="1")
    snapshot = Snapshot.model_validate(data)
    candidate = solve(snapshot, time_limit=2)
    assert candidate.checker.status == "PASS"
    snapshot = evolve(
        snapshot, active_plan_version="accepted-1", active_plan_hash=candidate.content_hash
    )
    return snapshot, candidate


def test_real_execution_reserves_then_consumes_once_and_finishes(executing):
    initial, plan = executing
    started = advance(initial, plan)
    assert len(started.actuals) == 1
    assert started.actuals[0].actual_start == initial.snapshot_clock
    assert started.actuals[0].remaining_minutes == 9
    assert started.reservations
    assert all(
        i.on_hand == before.on_hand for i, before in zip(started.inventory, initial.inventory)
    )
    finished = advance(started, plan, minutes=100)
    assert len(finished.actuals) == 8
    assert all(a.state == "COMPLETED" and a.completed_quantity == 50 for a in finished.actuals)
    assert finished.orders[0].status == "COMPLETED"
    assert all(r.quantity == 0 for r in finished.reservations)
    consumed = {i.material_id: 0 for i in initial.inventory}
    for actual in finished.actuals:
        for item in actual.consumed:
            consumed[item.material_id] += item.quantity
    for before, after in zip(initial.inventory, finished.inventory):
        assert before.on_hand - consumed[before.material_id] == after.on_hand
        assert after.reserved == before.reserved
    later = advance(finished, plan, minutes=10)
    assert later.actuals == finished.actuals
    assert later.inventory == finished.inventory
    assert initial.actuals == ()


def test_unsorted_resource_and_worker_calendars_execute_checked_plan(executing):
    initial, plan = executing
    raw = initial.model_dump(mode="json", exclude={"content_hash"})
    for group in ("resources", "workers"):
        for entity in raw[group]:
            entity["calendar"].reverse()
    unordered = Snapshot.model_validate(raw)
    assert any(
        list(r.calendar) != sorted(r.calendar, key=lambda w: w.start_at)
        for r in unordered.resources
    )
    expected = advance(initial, plan, minutes=100)
    actual = advance(unordered, plan, minutes=100)
    assert actual.actuals == expected.actuals
    assert actual.inventory == expected.inventory
    assert all(item.state == "COMPLETED" for item in actual.actuals)


def test_fault_unknown_remaining_requires_confirmed_fact_and_keeps_consumption(executing):
    snapshot, plan = executing
    snapshot = advance(snapshot, plan, minutes=13)
    active = next(a for a in snapshot.actuals if a.state == "IN_PROGRESS")
    assert active.consumed
    before = active
    stock = snapshot.inventory
    down = inject(
        snapshot,
        event_id="fault",
        kind="resource.down",
        payload={"resource_id": active.resource_id},
    )
    blocked = next(a for a in down.actuals if a.operation_id == active.operation_id)
    assert blocked.state == "BLOCKED" and blocked.remaining_minutes is None
    restored = inject(
        down,
        event_id="repair",
        kind="resource.restore",
        payload={"resource_id": active.resource_id},
    )
    waiting = advance(restored, plan, minutes=5)
    assert (
        next(a for a in waiting.actuals if a.operation_id == active.operation_id).state == "BLOCKED"
    )
    assert waiting.inventory == stock
    confirmed = inject(
        waiting,
        event_id="technician-confirmation",
        kind="execution.confirm_remaining",
        payload={
            "operation_id": active.operation_id,
            "remaining_minutes": before.remaining_minutes,
            "remaining_setup_minutes": 0,
        },
    )
    after = advance(confirmed, plan, minutes=10)
    resumed = next(a for a in after.actuals if a.operation_id == active.operation_id)
    assert resumed.state == "COMPLETED"
    assert resumed.actual_start == before.actual_start
    assert resumed.consumed == before.consumed
    assert len(resumed.segments) == 2
    production_minutes = sum(
        (s.end_at - s.start_at).total_seconds() / 60
        for s in resumed.segments
        if s.phase == "PRODUCTION"
    )
    assert production_minutes == 9
    assert resumed.actual_end > resumed.actual_start + timedelta(minutes=9)


def test_setup_is_not_fabricated_production_and_interruptions_do_not_use_paused_time(executing):
    snapshot, _ = executing
    raw = snapshot.model_dump(mode="python", exclude={"content_hash"})
    shared = next(
        r for r in snapshot.resources if "OP20" in r.operation_codes and "OP30" in r.operation_codes
    )
    raw["resources"] = [
        r
        for r in raw["resources"]
        if r["resource_type"] != shared.resource_type or r["resource_id"] == shared.resource_id
    ]
    raw.update(active_plan_version=None, active_plan_hash=None)
    unstarted = Snapshot.model_validate(raw)
    plan = solve(unstarted, time_limit=2)
    assert plan.checker.status == "PASS"
    snapshot = evolve(
        unstarted, active_plan_version="setup-plan", active_plan_hash=plan.content_hash
    )
    # A single eligible cell forces OP30 to follow OP20 on the same physical device.
    for _ in range(40):
        snapshot = advance(snapshot, plan)
        setups = [a for a in snapshot.actuals if a.state == "SETUP"]
        if setups:
            break
    assert setups
    actual = setups[0]
    assert actual.actual_start is None and actual.consumed == ()
    assert actual.segments[-1].phase == "SETUP"
    interrupted = inject(
        snapshot, event_id="absence", kind="worker.absent", payload={"worker_id": actual.worker_id}
    )
    waiting = advance(interrupted, plan, minutes=3)
    stalled = next(a for a in waiting.actuals if a.operation_id == actual.operation_id)
    assert stalled.segments == actual.segments
    assert stalled.actual_start is None and stalled.remaining_minutes is None


def test_receipt_delay_and_duplicate_receipt_never_double_stock(executing):
    snapshot, plan = executing
    receipt = snapshot.receipts[0]
    material = receipt.material_id
    before = next(i.on_hand for i in snapshot.inventory if i.material_id == material)
    first = inject(
        snapshot,
        event_id="arrival-1",
        kind="receipt.receive",
        payload={"receipt_id": receipt.receipt_id},
    )
    duplicate = inject(
        first,
        event_id="arrival-retry",
        kind="receipt.receive",
        payload={"receipt_id": receipt.receipt_id},
    )
    assert first.inventory == duplicate.inventory
    assert (
        next(i.on_hand for i in first.inventory if i.material_id == material)
        == before + receipt.quantity
    )
    with pytest.raises(SimulationError, match="RECEIPT_NOT_PENDING"):
        inject(
            first,
            event_id="late",
            kind="receipt.delay",
            payload={"receipt_id": receipt.receipt_id, "eta": snapshot.snapshot_clock},
        )
    near_receipt = snapshot.receipts[0].model_copy(
        update={"eta": snapshot.snapshot_clock + timedelta(minutes=2)}
    )
    snapshot = evolve(snapshot, receipts=(near_receipt, *snapshot.receipts[1:]))
    delay_to = snapshot.snapshot_clock + timedelta(minutes=5)
    delayed = inject(
        snapshot,
        event_id="delay",
        kind="receipt.delay",
        payload={"receipt_id": receipt.receipt_id, "eta": delay_to},
    )
    due = advance(delayed, plan, minutes=5)
    received = next(r for r in due.receipts if r.receipt_id == receipt.receipt_id)
    assert received.status == "RECEIVED" and received.received_at == delay_to


def test_failed_gate_blocks_successor_and_preserves_completed_history(executing):
    snapshot, plan = executing
    _, operations = batch_operations(snapshot)
    gate_step = next(
        s
        for s in snapshot.profile.routes
        if s.product_id == snapshot.orders[0].product_id and s.quality_gate
    )
    gate_id = next(o.operation_id for o in operations if o.step_id == gate_step.step_id)
    for _ in range(90):
        snapshot = advance(snapshot, plan)
        gate = next(
            (a for a in snapshot.actuals if a.operation_id == gate_id and a.state == "COMPLETED"),
            None,
        )
        if gate:
            break
    assert gate is not None
    failed = inject(
        snapshot,
        event_id="inspection",
        kind="quality.record",
        payload={"operation_id": gate_id, "quality_state": "UNKNOWN"},
    )
    later = advance(failed, plan, minutes=20)
    assert len(later.actuals) == len(failed.actuals)
    assert later.actuals == failed.actuals
    assert later.orders[0].status != "COMPLETED"


def test_order_entry_rejects_illegal_lot_and_new_scope_invalidates_snapshot(executing):
    snapshot, _ = executing
    order = snapshot.orders[0].model_dump(mode="json")
    order.update(order_id="urgent", quantity=51)
    with pytest.raises(ValueError, match="UNSUPPORTED_BATCH_QUANTITY"):
        inject(snapshot, event_id="illegal", kind="order.add", payload=order)
    order["quantity"] = 50
    changed = inject(snapshot, event_id="new-order", kind="order.add", payload=order)
    assert changed.scope_version == snapshot.scope_version + 1
    assert changed.content_hash != snapshot.content_hash
    assert len(changed.orders) == 2


def test_manual_stock_receipt_and_demand_controls_preserve_execution_boundaries(executing):
    snapshot, plan = executing
    stock = snapshot.inventory[0]
    counted = inject(
        snapshot,
        event_id="counted-stock",
        kind="inventory.reconcile",
        payload={
            "material_id": stock.material_id,
            "expected_version": stock.version,
            "counted_on_hand": stock.on_hand - 1,
            "reason": "COUNT_CORRECTION",
        },
    )
    assert counted.inventory[0].on_hand == stock.on_hand - 1
    assert counted.inventory[0].version == stock.version + 1
    with pytest.raises(SimulationError, match="INVENTORY_VERSION_CHANGED"):
        inject(
            counted,
            event_id="stale-count",
            kind="inventory.reconcile",
            payload={
                "material_id": stock.material_id,
                "expected_version": stock.version,
                "counted_on_hand": stock.on_hand - 2,
                "reason": "COUNT_CORRECTION",
            },
        )
    new_receipt = inject(
        counted,
        event_id="new-inbound",
        kind="receipt.add",
        payload={
            "receipt_id": "confirmed-next",
            "material_id": stock.material_id,
            "quantity": 30,
            "eta": snapshot.snapshot_clock + timedelta(hours=2),
        },
    )
    assert new_receipt.receipts[-1].unit == stock.unit
    assert new_receipt.inventory == counted.inventory
    with pytest.raises(SimulationError, match="RECEIPT_ETA_NOT_FUTURE"):
        inject(
            counted,
            event_id="past-inbound",
            kind="receipt.add",
            payload={
                "receipt_id": "past",
                "material_id": stock.material_id,
                "quantity": 30,
                "eta": snapshot.snapshot_clock,
            },
        )
    order = snapshot.orders[0]
    revision = {
        "order_id": order.order_id,
        "expected_version": order.version,
        "quantity": order.quantity + 50,
        "due_at": order.due_at,
        "priority_weight": order.priority_weight,
        "hard_deadline": order.hard_deadline,
    }
    revised = inject(snapshot, event_id="customer-change", kind="order.revise", payload=revision)
    assert revised.orders[0].quantity == order.quantity + 50
    assert revised.orders[0].split_revision == order.split_revision
    assert revised.scope_version == snapshot.scope_version + 1
    started = advance(snapshot, plan)
    changed_started = inject(
        started,
        event_id="increase-after-start",
        kind="order.revise",
        payload={**revision, "expected_version": started.orders[0].version},
    )
    assert changed_started.actuals == started.actuals
    assert changed_started.reservations == started.reservations
    assert changed_started.orders[0].quantity == order.quantity + 50
    with pytest.raises(SimulationError, match="INVALID_INVENTORY_RECONCILIATION"):
        inject(
            started,
            event_id="below-reserved",
            kind="inventory.reconcile",
            payload={
                "material_id": stock.material_id,
                "expected_version": next(
                    i for i in started.inventory if i.material_id == stock.material_id
                ).version,
                "counted_on_hand": 0,
                "reason": "COUNT_CORRECTION",
            },
        )


def test_reading_or_reserializing_does_not_advance_business_clock(executing):
    snapshot, _ = executing
    assert Snapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
    assert snapshot.snapshot_clock == load_skf_snapshot(development=True).snapshot_clock
    with pytest.raises(SimulationError, match="EXECUTION_PLAN_MISSING"):
        advance(snapshot, None)
    with pytest.raises(SimulationError, match="INVALID_CLOCK_STEP"):
        advance(snapshot, None, minutes=True)
