"""Source-produced progress and adversarial deltas challenge deterministic wake-up decisions."""

import json
from collections.abc import Sequence
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session

from packages.agent.impact import ImpactInputError, classify_events, impact_report
from packages.domain.models import Event, Snapshot, batch_operations
from services.factory_sim.engine import advance, evolve, inject
from services.factory_sim.service import _write
from services.factory_sim.storage import SourceChange, World
from tests.test_checker import (
    at,
    change_snapshot,
    example_assignments,
    example_snapshot,
    make_candidate,
)


class ChangeCollector:
    def __init__(self):
        self.rows: list[SourceChange] = []

    def add(self, row: SourceChange) -> None:
        self.rows.append(row)


def source_events(before: Snapshot, after: Snapshot, cause="clock.tick") -> tuple[Event, ...]:
    collector = ChangeCollector()
    world = World(factory_id=before.factory_id)
    _write(cast(Session, collector), world, before, after, cause)
    assert len(collector.rows) == 1
    return tuple(Event.model_validate(row) for row in collector.rows[0].document["events"])


def event(
    entity_type: str,
    entity_id: str,
    changes: dict[str, tuple[Any, Any]],
    *,
    event_type="execution.progress",
    snapshot: Snapshot | None = None,
    **extra,
) -> Event:
    snapshot = snapshot or example_snapshot()
    return Event.model_validate(
        {
            "event_id": f"event-{entity_type}-{entity_id}",
            "source_event_id": f"source-{entity_id}",
            "factory_id": snapshot.factory_id,
            "run_id": snapshot.run_id,
            "source_revision": snapshot.source.source_revision,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "entity_version": 1,
            "event_type": event_type,
            "occurred_at": snapshot.snapshot_clock,
            "effective_at": snapshot.snapshot_clock,
            "observed_at": snapshot.source.observed_at,
            "changes": [
                {"field": key, "before": before, "after": after}
                for key, (before, after) in changes.items()
            ],
            **extra,
        }
    )


def change_event(value: Event, **changes) -> Event:
    return Event.model_validate({**value.model_dump(), **changes})


def active_case():
    initial = example_snapshot()
    plan = make_candidate(initial)
    snapshot = evolve(
        initial, active_plan_version="source-plan-1", active_plan_hash=plan.content_hash
    )
    return snapshot, plan


def tick_case(tick: int = 1):
    before, plan = active_case()
    for _ in range(tick - 1):
        before = advance(before, plan)
    after = advance(before, plan)
    return before, after, plan, source_events(before, after)


def assert_material(events: Sequence[Event], snapshot=None, reason=None):
    result = classify_events(events, snapshot=snapshot)
    assert result["material"] and result["urgent"], result
    if reason:
        assert reason in result["reasons"]


@pytest.mark.parametrize("schema_version", ["byof.snapshot/2", "byof.snapshot/3"])
def test_source_entire_legal_route_progress_reservation_and_consumption_do_not_wake_model(
    schema_version,
):
    before, plan = active_case()
    if schema_version == "byof.snapshot/3":
        from packages.domain.demand import materialize_batches

        before = evolve(
            before, schema_version=schema_version, production_batches=materialize_batches(before)
        )
    stock_changes, completions = 0, 0
    for _ in range(6):
        after = advance(before, plan)
        events = source_events(before, after)
        stock_changes += sum(e.entity_type == "inventory" for e in events)
        completions += sum(e.event_type == "execution.completed" for e in events)
        assert classify_events(events, snapshot=after) == {
            "material": False,
            "urgent": False,
            "reasons": [],
        }, [(e.entity_type, e.changes) for e in events]
        report = impact_report(after, events, plan)
        assert report["possible"]["scope"] == "NO_MODEL_ACTION"
        assert report["preserves_approval"] is False
        assert report["requires_full_check"] is True
        before = after
    assert stock_changes == 2 and completions == 3
    assert before.inventory[0].on_hand == before.inventory[0].reserved == 0
    assert sum(c.quantity for a in before.actuals for c in a.consumed) == 2
    assert before.orders[0].status == "COMPLETED"


def test_consumption_requires_matching_snapshot_revision_and_full_actual_evidence():
    _, current, plan, events = tick_case(5)
    assert_material(events, reason="UNEXPLAINED_INVENTORY_CHANGE")
    assert_material(events, advance(current, plan), "UNEXPLAINED_INVENTORY_CHANGE")
    missing_start = tuple(e for e in events if e.entity_type != "actuals")
    assert_material(missing_start, current, "UNEXPLAINED_INVENTORY_CHANGE")


@pytest.mark.parametrize("batches,second_product,ticks", [(2, False, 11), (1, True, 23)])
def test_multiple_batches_and_real_changeover_progress_are_quiet(batches, second_product, ticks):
    initial = example_snapshot(batches=batches, second_product=second_product)
    plan = make_candidate(
        initial, example_assignments(batches=batches, second_product=second_product)
    )
    before = evolve(
        initial, active_plan_version="source-plan-1", active_plan_hash=plan.content_hash
    )
    setups = 0
    for _ in range(ticks):
        after = advance(before, plan)
        events = source_events(before, after)
        setups += sum(e.event_type == "execution.setup" for e in events)
        assert classify_events(events, after)["material"] is False, events
        before = after
    assert setups > 0
    assert len(before.actuals) == 6 and all(a.state == "COMPLETED" for a in before.actuals)
    assert before.inventory[0].on_hand == before.inventory[0].reserved == 0


def test_same_tick_root_consumption_proves_zero_reserved_delta():
    initial = change_snapshot(
        example_snapshot(), lambda data: data["profile"]["bom"][0].update(consume_step_id="a-begin")
    )
    plan = make_candidate(initial)
    before = evolve(
        initial, active_plan_version="source-plan-1", active_plan_hash=plan.content_hash
    )
    after = advance(before, plan)
    events = source_events(before, after)
    stock = next(e for e in events if e.entity_type == "inventory")
    assert "reserved" not in {c.field for c in stock.changes}
    assert after.inventory[0].reserved == 0 and after.inventory[0].on_hand == 0
    assert classify_events(events, after)["material"] is False


@pytest.mark.parametrize(
    "source_field,value",
    [
        ("complete", False),
        ("consistency", "UNVERIFIED"),
        ("freshness", "STALE"),
    ],
)
def test_unverified_or_stale_source_cannot_explain_away_inventory(source_field, value):
    _, current, _, events = tick_case(5)
    current = change_snapshot(current, lambda data: data["source"].update({source_field: value}))
    assert_material(events, current, "UNEXPLAINED_INVENTORY_CHANGE")


def test_incomplete_material_event_batch_does_not_prove_consumption():
    _, current, _, events = tick_case(5)
    actual = next(e for e in events if e.entity_type == "actuals")
    stripped = change_event(actual, changes=[c for c in actual.changes if c.field != "state"])
    changed = [stripped if e is actual else e for e in events]
    assert_material(changed, current, "UNEXPLAINED_INVENTORY_CHANGE")


@pytest.mark.parametrize("tick", [1, 5])
def test_omitted_stock_change_does_not_turn_new_start_into_proven_progress(tick):
    _, current, _, events = tick_case(tick)
    incomplete = [e for e in events if e.entity_type != "inventory"]
    assert_material(incomplete, current, "UNVERIFIED_PRODUCTION_LEDGER")


def test_false_order_completion_without_completed_actuals_is_material():
    snapshot = change_snapshot(
        example_snapshot(), lambda data: data["orders"][0].update(status="COMPLETED")
    )
    assert_material(
        [event("orders", "order-a", {"status": ("IN_PROGRESS", "COMPLETED")})],
        snapshot,
        "ORDER_CHANGED",
    )


@pytest.mark.parametrize("tick", [1, 5])
def test_stock_delta_off_by_one_is_material_even_under_progress_label(tick):
    _, current, _, events = tick_case(tick)
    changed = []
    for item in events:
        if item.entity_type == "inventory":
            fields = [c.model_dump() for c in item.changes]
            balance = next(c for c in fields if c["field"] in ("on_hand", "reserved"))
            balance["before"] += 1
            item = change_event(item, changes=fields, event_type="execution.progress")
        changed.append(item)
    assert_material(changed, current, "UNEXPLAINED_INVENTORY_CHANGE")


def test_inventory_adjustment_cannot_claim_production_without_ledger():
    stock = event("inventory", "shared-part", {"on_hand": (3, 2)})
    assert_material([stock], example_snapshot(), "UNEXPLAINED_INVENTORY_CHANGE")


def test_receipt_and_consumption_same_revision_remain_material():
    before, plan = active_case()
    before = change_snapshot(
        before,
        lambda data: data["receipts"].append(
            {
                "receipt_id": "arrival",
                "material_id": "shared-part",
                "unit": "EA",
                "quantity": 2,
                "eta": at(1).isoformat(),
                "status": "CONFIRMED",
                "version": 1,
            }
        ),
    )
    after = advance(before, plan)
    events = source_events(before, after)
    result = classify_events(events, after)
    assert result["material"] and result["urgent"]
    assert {"RECEIPT_CHANGED", "UNEXPLAINED_INVENTORY_CHANGE"} <= set(result["reasons"])


@pytest.mark.parametrize(
    "entity,identity,changes",
    [
        ("resources", "r1", {"status": ("AVAILABLE", "DOWN")}),
        ("workers", "w1", {"status": ("AVAILABLE", "ABSENT")}),
        ("orders", "order-a", {"quantity": (2, 4)}),
        ("orders", "order-a", {"priority_weight": (3, 10)}),
        ("actuals", "operation", {"state": ("IN_PROGRESS", "BLOCKED")}),
        ("actuals", "operation", {"remaining_minutes": (1, None)}),
        ("actuals", "operation", {"remaining_minutes": (1, 3)}),
        ("actuals", "operation", {"quality_state": ("PASSED", "FAILED")}),
        ("actuals", "operation", {"quality_state": ("UNKNOWN", "PASSED")}),
        ("receipts", "arrival", {"eta": (at(1).isoformat(), at(5).isoformat())}),
    ],
)
def test_progress_label_does_not_hide_real_business_changes(entity, identity, changes):
    assert_material([event(entity, identity, changes)])


def test_normal_remaining_progress_and_forward_clock_need_no_model():
    progress = event(
        "actuals",
        "operation",
        {
            "remaining_minutes": (10, 9),
            "remaining_confirmed_by": ("old", "new"),
            "version": (2, 3),
        },
    )
    clock = event("clock", "business", {"snapshot_clock": (at(0).isoformat(), at(1).isoformat())})
    assert classify_events([progress, clock])["material"] is False
    assert classify_events([]) == {"material": False, "urgent": False, "reasons": []}


@pytest.mark.parametrize(
    "entity,kind",
    [
        ("resources", "clock.tick"),
        ("actuals", "execution.progress"),
        ("actuals", "execution.completed"),
        ("custom", "execution.progress"),
    ],
)
def test_unknown_or_missing_details_are_conservative(entity, kind):
    assert_material([event(entity, "missing", {}, event_type=kind)])


def test_completion_without_quality_evidence_is_not_ordinary_progress():
    assert_material(
        [
            event(
                "actuals",
                "operation",
                {
                    "state": ("IN_PROGRESS", "COMPLETED"),
                    "actual_end": (None, at(2).isoformat()),
                    "completed_quantity": (0, 2),
                    "remaining_minutes": (1, 0),
                },
                event_type="execution.completed",
            )
        ]
    )


def test_corrections_and_clock_reversal_are_material():
    correction = event(
        "actuals", "operation", {"remaining_minutes": (10, 9)}, corrects_event_id="prior"
    )
    assert_material([correction], reason="CORRECTED_BUSINESS_FACT")
    clock = event("clock", "business", {"snapshot_clock": (at(1).isoformat(), at(0).isoformat())})
    assert_material([clock], reason="BUSINESS_CLOCK_CHANGED")


def test_current_snapshot_cannot_be_overridden_by_false_event_after_value():
    _, current, _, events = tick_case()
    actual = next(e for e in events if e.entity_type == "actuals")
    changes = [c.model_dump() for c in actual.changes]
    next(c for c in changes if c["field"] == "remaining_minutes")["after"] = 900
    assert_material([change_event(actual, changes=changes)], current, "EVENT_SNAPSHOT_MISMATCH")


def test_resource_impact_uses_assignment_and_dag_not_operation_number_order():
    before, plan = active_case()
    before = advance(before, plan)
    after = inject(before, event_id="stop", kind="resource.down", payload={"resource_id": "r1"})
    events = source_events(before, after, "resource.down")
    report = impact_report(after, events, plan)
    _, operations = batch_operations(after)
    by_step = {o.step_id: o.operation_id for o in operations}
    assert report["direct"]["operations"] == [by_step["a-begin"]]
    assert report["direct"]["orders"] == ["order-a"]
    assert report["direct"]["resources"] == ["r1"]
    assert report["direct"]["workers"] == ["w1"]
    assert report["dependency_operations"] == sorted([by_step["a-check"], by_step["a-finish"]])
    assert report["possible"]["operations"] == sorted(o.operation_id for o in operations)
    assert report["delay"] == {"status": "NOT_EVALUATED", "minutes": None}
    assert {"ROUTE_DEPENDENCIES", "LOCAL_BOUNDARY_NOT_PROVEN"} <= set(report["expansion_reasons"])
    json.dumps(report)


def test_new_urgent_order_expands_scope_while_case_waits_and_keeps_old_work():
    before, plan = active_case()
    urgent = before.orders[0].model_dump(mode="json")
    urgent.update(order_id="urgent-order", priority_weight=10, due_at=at(7).isoformat())
    after = inject(before, event_id="urgent", kind="order.add", payload=urgent)
    events = tuple(
        change_event(e, event_type="execution.progress") for e in source_events(before, after)
    )
    report = impact_report(after, events, plan)
    _, operations = batch_operations(after)
    assert report["classification"]["material"] and report["classification"]["urgent"]
    assert report["direct"]["orders"] == ["urgent-order"]
    assert len(report["direct"]["operations"]) == 3
    assert report["possible"]["orders"] == ["order-a", "urgent-order"]
    assert report["possible"]["operations"] == sorted(o.operation_id for o in operations)
    assert "ORDER_SCOPE_MAY_EXPAND" in report["expansion_reasons"]
    assert report["delay"]["minutes"] is None


def test_material_impact_uses_bom_consumption_and_full_kit_roots():
    snapshot = example_snapshot()
    report = impact_report(snapshot, [event("inventory", "shared-part", {"on_hand": (3, 2)})])
    _, operations = batch_operations(snapshot)
    by_step = {o.step_id: o.operation_id for o in operations}
    assert report["direct"]["operations"] == sorted([by_step["a-begin"], by_step["a-finish"]])
    assert report["dependency_operations"] == [by_step["a-check"]]
    assert report["direct"]["resources"] == []
    assert report["possible"]["resources"] == ["r1", "r2", "r3"]


def test_completed_actual_is_not_resource_future_impact():
    before, plan = active_case()
    before = advance(before, plan, minutes=2)
    after = inject(before, event_id="stop", kind="resource.down", payload={"resource_id": "r1"})
    report = impact_report(after, source_events(before, after, "resource.down"), plan)
    assert report["direct"]["resources"] == ["r1"]
    assert report["direct"]["operations"] == []
    assert report["classification"]["material"] is True


@pytest.mark.parametrize("field,value", [("factory_id", "elsewhere"), ("run_id", "replay-2")])
def test_cross_factory_or_run_events_are_rejected(field, value):
    snapshot = example_snapshot()
    original = event("resources", "r1", {"status": ("AVAILABLE", "DOWN")})
    other = change_event(original, **{field: value})
    with pytest.raises(ImpactInputError, match="EVENT_SCOPE_MISMATCH"):
        impact_report(snapshot, [other])
    with pytest.raises(ImpactInputError, match="EVENT_SCOPE_MISMATCH"):
        classify_events([original, other])


def test_duplicate_is_idempotent_but_changed_identity_or_ambiguous_fields_rejected():
    original = event("resources", "r1", {"status": ("AVAILABLE", "DOWN")})
    assert classify_events([original, original]) == classify_events([original])
    with pytest.raises(ImpactInputError, match="EVENT_ID_CONFLICT"):
        classify_events([original, change_event(original, event_type="resource.restore")])
    with pytest.raises(ImpactInputError, match="DUPLICATE_FIELD_CHANGE"):
        classify_events([change_event(original, changes=original.changes * 2)])


def test_unknown_ids_do_not_invent_direct_business_facts_and_scope_is_conservative():
    snapshot = example_snapshot()
    report = impact_report(snapshot, [event("resources", "missing", {"status": (None, "DOWN")})])
    assert report["unknowns"] == ["resources:missing"]
    assert report["direct"]["resources"] == []
    assert report["possible"]["scope"] == "FACTORY"
    assert report["possible"]["resources"] == ["r1", "r2", "r3"]


def test_baseline_must_belong_to_factory_and_match_active_content():
    snapshot, plan = active_case()
    other_data = plan.model_dump(exclude={"content_hash"})
    other_data["factory_id"] = "another-factory"
    other = type(plan).model_validate(other_data)
    with pytest.raises(ImpactInputError, match="BASELINE_FACTORY_MISMATCH"):
        impact_report(snapshot, [], other)
    other_data["factory_id"] = plan.factory_id
    other_data["candidate_id"] = "different-plan"
    with pytest.raises(ImpactInputError, match="BASELINE_HASH_MISMATCH"):
        impact_report(snapshot, [], type(plan).model_validate(other_data))
