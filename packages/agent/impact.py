"""Deterministic model wake-up and conservative impact scope, never plan approval."""

import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from typing import Any, TypedDict

from packages.domain.models import Candidate, Event, FieldChange, Snapshot, batch_operations


class Classification(TypedDict):
    material: bool
    urgent: bool
    reasons: list[str]


class ImpactInputError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


_COLLECTION_KEYS = {
    "orders": "order_id",
    "actuals": "operation_id",
    "resources": "resource_id",
    "workers": "worker_id",
    "inventory": "material_id",
    "receipts": "receipt_id",
}
_ACTUAL_FIELDS = {
    "batch_id",
    "route_version",
    "state",
    "actual_start",
    "actual_end",
    "changeover_start",
    "resource_id",
    "worker_id",
    "completed_quantity",
    "quality_state",
    "remaining_minutes",
    "remaining_setup_minutes",
    "remaining_confirmed_by",
    "version",
}


def _events(events: Sequence[Event], snapshot: Snapshot | None) -> tuple[Event, ...]:
    unique: dict[str, Event] = {}
    scope = (snapshot.factory_id, snapshot.run_id) if snapshot else None
    for value in events:
        event = Event.model_validate(value.model_dump())
        current = event.factory_id, event.run_id
        if scope is not None and current != scope:
            raise ImpactInputError("EVENT_SCOPE_MISMATCH")
        scope = current
        if len({c.field for c in event.changes}) != len(event.changes):
            raise ImpactInputError("DUPLICATE_FIELD_CHANGE")
        if event.event_id in unique and unique[event.event_id] != event:
            raise ImpactInputError("EVENT_ID_CONFLICT")
        unique[event.event_id] = event
    return tuple(unique.values())


def _changes(event: Event) -> dict[str, FieldChange]:
    return {c.field: c for c in event.changes if c.before != c.after}


def _row(snapshot: Snapshot | None, event: Event) -> dict[str, Any] | None:
    if snapshot is None or event.source_revision != snapshot.source.source_revision:
        return None
    key = _COLLECTION_KEYS.get(event.entity_type)
    if key is None:
        return None
    for row in getattr(snapshot, event.entity_type):
        if getattr(row, key) == event.entity_id:
            data = row.model_dump(mode="json")
            if data["version"] != event.entity_version:
                return None
            comparable = dict(data)
            if event.event_type == "overtime_window.set" and "calendar" in comparable:
                comparable["calendar"] = json.dumps(comparable["calendar"], sort_keys=True)
            if any(
                c.field not in comparable or comparable[c.field] != c.after for c in event.changes
            ):
                return None
            return data
    return None


def _decreased(change: FieldChange) -> bool:
    return (
        type(change.before) is int
        and type(change.after) is int
        and 0 <= change.after <= change.before
    )


def _normal_actual(event: Event, snapshot: Snapshot | None) -> bool:
    changes = _changes(event)
    if not changes or set(changes) - _ACTUAL_FIELDS:
        return False
    state = changes.get("state")
    row = _row(snapshot, event)
    if row is not None and (
        row["state"] == "BLOCKED" or row["quality_state"] in ("FAILED", "UNKNOWN")
    ):
        return False
    new = state is not None and state.before is None
    if new:
        required = {
            "batch_id",
            "route_version",
            "resource_id",
            "worker_id",
            "changeover_start",
            "remaining_minutes",
            "remaining_setup_minutes",
            "remaining_confirmed_by",
            "state",
        }
        if row is None or not required <= changes.keys():
            return False
        if any(changes[key].before is not None for key in required):
            return False
        if row["state"] not in ("SETUP", "IN_PROGRESS", "COMPLETED"):
            return False
        if row["remaining_minutes"] is None or row["remaining_setup_minutes"] is None:
            return False
        if row["quality_state"] not in ("PENDING", "PASSED"):
            return False
    elif any(
        key in changes
        for key in ("batch_id", "route_version", "resource_id", "worker_id", "changeover_start")
    ):
        return False
    if (
        state
        and not new
        and (state.before, state.after)
        not in (("SETUP", "IN_PROGRESS"), ("SETUP", "COMPLETED"), ("IN_PROGRESS", "COMPLETED"))
    ):
        return False
    completed = state is not None and state.after == "COMPLETED"
    if completed:
        quality = changes.get("quality_state")
        end = changes.get("actual_end")
        quantity = changes.get("completed_quantity")
        remaining = changes.get("remaining_minutes")
        if not (
            quality
            and quality.after == "PASSED"
            and end
            and end.before is None
            and isinstance(end.after, str)
            and quantity
            and type(quantity.after) is int
            and quantity.after > 0
            and remaining
            and remaining.after == 0
        ):
            return False
    elif not new and any(
        key in changes for key in ("quality_state", "actual_end", "completed_quantity")
    ):
        return False
    if "actual_start" in changes:
        start = changes["actual_start"]
        if row is None or start.before is not None or not isinstance(start.after, str):
            return False
        if state is None or state.before not in (None, "SETUP"):
            return False
        if row["state"] not in ("IN_PROGRESS", "COMPLETED"):
            return False
    for key in ("remaining_minutes", "remaining_setup_minutes"):
        if key in changes and not new and not _decreased(changes[key]):
            return False
    if "remaining_confirmed_by" in changes and not isinstance(
        changes["remaining_confirmed_by"].after, str
    ):
        return False
    # Labels may not hide blocked or changed quality facts, including an otherwise empty delta.
    return event.event_type not in ("execution.blocked", "quality.failed")


def _normal_inventory(
    snapshot: Snapshot | None, events: tuple[Event, ...], normal_actuals: set[str]
) -> set[str] | None:
    """Prove stock deltas using newly started operations and the actual ledger, by revision."""
    if snapshot is None or snapshot.schema_version not in {"byof.snapshot/2", "byof.snapshot/3"}:
        return None
    if (
        not snapshot.source.complete
        or snapshot.source.consistency == "UNVERIFIED"
        or snapshot.source.freshness != "CURRENT"
    ):
        return None
    if any(e.source_revision != snapshot.source.source_revision for e in events):
        return None
    if any(e.entity_type == "receipts" or e.corrects_event_id for e in events):
        return None
    actual_by_id = {a.operation_id: a for a in snapshot.actuals}
    new_starts = {}
    for event in events:
        if event.entity_type != "actuals":
            continue
        start = _changes(event).get("actual_start")
        if start and start.before is None and isinstance(start.after, str):
            if event.event_id not in normal_actuals or _row(snapshot, event) is None:
                return None
            new_starts[event.entity_id] = actual_by_id[event.entity_id]
    if not new_starts:
        return set()
    batches, operations = batch_operations(snapshot)
    batch_by_id = {b.batch_id: b for b in batches}
    operation_by_id = {o.operation_id: o for o in operations}
    stock_by_id = {s.material_id: s for s in snapshot.inventory}
    expected: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    new_batches = set()
    for actual in new_starts.values():
        batch = batch_by_id[actual.batch_id]
        step_id = operation_by_id[actual.operation_id].step_id
        batch_bom = [b for b in snapshot.profile.bom if b.product_id == batch.product_id]
        consumed = {c.material_id: c.quantity for c in actual.consumed}
        required = {
            b.material_id: b.quantity_per_unit * batch.quantity
            for b in batch_bom
            if b.consume_step_id == step_id
        }
        if consumed != required:
            return None
        for item in actual.consumed:
            if item.unit != stock_by_id[item.material_id].unit:
                return None
            expected[item.material_id][0] -= item.quantity
            expected[item.material_id][1] -= item.quantity
        prior_started = any(
            other.batch_id == actual.batch_id
            and other.actual_start is not None
            and other.operation_id not in new_starts
            for other in snapshot.actuals
        )
        if not prior_started:
            new_batches.add(actual.batch_id)
    for batch_id in new_batches:
        batch = batch_by_id[batch_id]
        starts = {a.actual_start for a in new_starts.values() if a.batch_id == batch_id}
        for bom in snapshot.profile.bom:
            if bom.product_id != batch.product_id:
                continue
            owned = next(
                (
                    r
                    for r in snapshot.reservations
                    if (r.batch_id == batch_id and r.material_id == bom.material_id)
                ),
                None,
            )
            if owned is None or owned.created_at not in starts:
                return None
            expected[bom.material_id][1] += bom.quantity_per_unit * batch.quantity
    inventory_events = [e for e in events if e.entity_type == "inventory"]
    if len({e.entity_id for e in inventory_events}) != len(inventory_events):
        return None
    actual_deltas: dict[str, list[int]] = {}
    for event in inventory_events:
        changes = _changes(event)
        if _row(snapshot, event) is None or set(changes) - {"on_hand", "reserved", "version"}:
            return None
        delta = []
        for key in ("on_hand", "reserved"):
            change = changes.get(key)
            if change is None:
                delta.append(0)
            elif type(change.before) is int and type(change.after) is int:
                delta.append(change.after - change.before)
            else:
                return None
        actual_deltas[event.entity_id] = delta
    if {k: v for k, v in expected.items() if any(v)} != {
        k: v for k, v in actual_deltas.items() if any(v)
    }:
        return None
    return {e.event_id for e in inventory_events}


def _normal_order(event: Event, snapshot: Snapshot | None) -> bool:
    changes = _changes(event)
    state = changes.get("status")
    if set(changes) - {"version", "status"} or state is None or snapshot is None:
        return False
    if _row(snapshot, event) is None:
        return False
    batches, operations = batch_operations(snapshot)
    non_customer = {
        b.batch_id for b in snapshot.production_batches or () if b.purpose != "CUSTOMER"
    }
    batch_ids = {
        b.batch_id
        for b in batches
        if b.order_id == event.entity_id and b.batch_id not in non_customer
    }
    required = [o for o in operations if o.batch_id in batch_ids]
    actuals = {a.operation_id: a for a in snapshot.actuals}
    if (state.before, state.after) == ("CONFIRMED", "IN_PROGRESS"):
        return any(
            o.operation_id in actuals and actuals[o.operation_id].actual_start is not None
            for o in required
        )
    if (state.before, state.after) == ("IN_PROGRESS", "COMPLETED"):
        steps = {s.step_id: s for s in snapshot.profile.routes}
        return bool(required) and all(
            o.operation_id in actuals
            and actuals[o.operation_id].state == "COMPLETED"
            and (
                not steps[o.step_id].quality_gate
                or actuals[o.operation_id].quality_state == "PASSED"
            )
            for o in required
        )
    return False


def _normal_setup_pointer(
    event: Event, snapshot: Snapshot | None, events: tuple[Event, ...], normal_actuals: set[str]
) -> bool:
    business = set(_changes(event)) - {"version"}
    if snapshot is None or not business or business - {"last_operation_id", "last_product_id"}:
        return False
    row = _row(snapshot, event)
    if row is None:
        return False
    actual = next((a for a in snapshot.actuals if a.operation_id == row["last_operation_id"]), None)
    if actual is None or actual.resource_id != event.entity_id:
        return False
    batches, _ = batch_operations(snapshot)
    batch = next(b for b in batches if b.batch_id == actual.batch_id)
    return row["last_product_id"] == batch.product_id and any(
        e.entity_id == actual.operation_id and e.event_id in normal_actuals for e in events
    )


def classify_events(events: Sequence[Event], snapshot: Snapshot | None = None) -> Classification:
    """Classify confirmed connector events. Quiet progress still needs version/risk checks."""
    if snapshot is not None:
        snapshot = Snapshot.model_validate(snapshot.model_dump())
    verified = _events(events, snapshot)
    normal_actuals = {
        e.event_id for e in verified if e.entity_type == "actuals" and _normal_actual(e, snapshot)
    }
    stock_proof = _normal_inventory(snapshot, verified, normal_actuals)
    stock_events = stock_proof or set()
    reasons: set[str] = set()
    if stock_proof is None and any(
        e.entity_type == "actuals"
        and any(
            c.field == "actual_start" and c.before is None and isinstance(c.after, str)
            for c in e.changes
        )
        for e in verified
    ):
        reasons.add("UNVERIFIED_PRODUCTION_LEDGER")
    for event in verified:
        changes = _changes(event)
        business = set(changes) - {"version"}
        if event.corrects_event_id:
            reasons.add("CORRECTED_BUSINESS_FACT")
        if (
            event.entity_type in _COLLECTION_KEYS
            and snapshot is not None
            and (
                event.source_revision == snapshot.source.source_revision
                and _row(snapshot, event) is None
            )
        ):
            reasons.add("EVENT_SNAPSHOT_MISMATCH")
            continue
        if event.entity_type == "actuals":
            if event.event_id not in normal_actuals:
                reasons.add("EXECUTION_OR_QUALITY_CHANGED")
        elif event.entity_type == "orders":
            normal = _normal_order(event, snapshot)
            if not normal and business:
                reasons.add("ORDER_CHANGED")
            elif not changes:
                reasons.add("MISSING_EVENT_DETAILS")
        elif event.entity_type == "inventory":
            if business and event.event_id not in stock_events:
                reasons.add("UNEXPLAINED_INVENTORY_CHANGE")
            elif not changes:
                reasons.add("MISSING_EVENT_DETAILS")
        elif event.entity_type == "resources":
            if event.event_type in {"resource.outage", "overtime_window.set"}:
                # The snapshot carries the time window; scalar event fields carry its version.
                reasons.add("RESOURCE_CHANGED")
            normal = _normal_setup_pointer(event, snapshot, verified, normal_actuals)
            if business and not normal:
                reasons.add("RESOURCE_CHANGED")
            elif not changes:
                reasons.add("MISSING_EVENT_DETAILS")
        elif event.entity_type in ("workers", "receipts"):
            if business or event.event_type in {"overtime_window.set", "worker.leave"}:
                reasons.add(
                    "WORKER_CHANGED" if event.entity_type == "workers" else "RECEIPT_CHANGED"
                )
            elif not changes:
                reasons.add("MISSING_EVENT_DETAILS")
        elif event.entity_type == "business_terms":
            reasons.add("BUSINESS_TERMS_CHANGED")
        elif event.entity_type == "clock":
            if business - {"snapshot_clock", "business_clock", "observed_at"}:
                reasons.add("UNKNOWN_BUSINESS_CHANGE")
            for change in changes.values():
                if change.field == "version":
                    continue
                try:
                    before, after = str(change.before), str(change.after)
                    first, last = datetime.fromisoformat(before), datetime.fromisoformat(after)
                    if first.tzinfo is None or last.tzinfo is None or last < first:
                        raise ValueError
                except (ValueError, TypeError):
                    reasons.add("BUSINESS_CLOCK_CHANGED")
        else:
            reasons.add("UNKNOWN_ENTITY_OR_EVENT")
        if event.event_type in ("execution.blocked", "quality.failed"):
            reasons.add("URGENT_EXECUTION_FACT")
    return {"material": bool(reasons), "urgent": bool(reasons), "reasons": sorted(reasons)}


def impact_report(
    snapshot: Snapshot, events: Sequence[Event], baseline: Candidate | None = None
) -> dict[str, Any]:
    """Report direct relations and possible scope, without asserting solver-proven delays."""
    snapshot = Snapshot.model_validate(snapshot.model_dump())
    verified = _events(events, snapshot)
    if baseline is not None:
        baseline = Candidate.model_validate(baseline.model_dump())
        if baseline.factory_id != snapshot.factory_id:
            raise ImpactInputError("BASELINE_FACTORY_MISMATCH")
        if (
            snapshot.active_plan_hash is not None
            and baseline.content_hash != snapshot.active_plan_hash
        ):
            raise ImpactInputError("BASELINE_HASH_MISMATCH")
    classification = classify_events(verified, snapshot=snapshot)
    batches, operations = batch_operations(snapshot)
    batch_by_id = {b.batch_id: b for b in batches}
    operation_by_id = {o.operation_id: o for o in operations}
    step_by_id = {s.step_id: s for s in snapshot.profile.routes}
    all_ids = {
        "orders": {o.order_id for o in snapshot.orders},
        "operations": set(operation_by_id),
        "resources": {r.resource_id for r in snapshot.resources},
        "workers": {w.worker_id for w in snapshot.workers},
        "materials": {m.material_id for m in snapshot.profile.materials},
    }
    direct: dict[str, set[str]] = {key: set() for key in all_ids}
    unknowns: set[str] = set()
    completed = {a.operation_id for a in snapshot.actuals if a.state == "COMPLETED"}
    assignments = {
        a.operation_id: (a.resource_id, a.worker_id)
        for a in (baseline.assignments if baseline else ())
    }
    assignments.update({a.operation_id: (a.resource_id, a.worker_id) for a in snapshot.actuals})
    for event in verified:
        category = {"actuals": "operations", "inventory": "materials"}.get(
            event.entity_type, event.entity_type
        )
        if category in direct:
            if event.entity_id in all_ids[category]:
                direct[category].add(event.entity_id)
            else:
                unknowns.add(f"{event.entity_type}:{event.entity_id}")
        elif event.entity_type == "receipts":
            receipt = next((r for r in snapshot.receipts if r.receipt_id == event.entity_id), None)
            if receipt:
                direct["materials"].add(receipt.material_id)
            else:
                unknowns.add(f"receipts:{event.entity_id}")
        elif event.entity_type == "business_terms":
            # Rules or quotes can affect any open customer promise; exact effects need a solve.
            direct["orders"].update(
                order.order_id
                for order in snapshot.orders
                if order.quantity > 0 and order.status not in {"COMPLETED", "CANCELLED"}
            )
        elif event.entity_type != "clock":
            unknowns.add(f"{event.entity_type}:{event.entity_id}")
    event_orders, event_materials = set(direct["orders"]), set(direct["materials"])
    event_resources, event_workers = set(direct["resources"]), set(direct["workers"])
    for operation in operations:
        batch = batch_by_id[operation.batch_id]
        step = step_by_id[operation.step_id]
        allocation = assignments.get(operation.operation_id)
        linked = batch.order_id in event_orders
        if operation.operation_id not in completed:
            linked |= allocation is not None and (
                allocation[0] in event_resources or allocation[1] in event_workers
            )
            # Whole-kit reservation is at the route root; consumption is at its own step.
            linked |= any(
                bom.product_id == batch.product_id
                and bom.material_id in event_materials
                and (bom.consume_step_id == step.step_id or not step.predecessors)
                for bom in snapshot.profile.bom
            )
        if linked:
            direct["operations"].add(operation.operation_id)
    for operation_id in direct["operations"]:
        operation = operation_by_id[operation_id]
        batch = batch_by_id[operation.batch_id]
        direct["orders"].add(batch.order_id)
        if operation_id in assignments:
            resource, worker = assignments[operation_id]
            direct["resources"].add(resource)
            direct["workers"].add(worker)
        direct["materials"].update(
            bom.material_id
            for bom in snapshot.profile.bom
            if bom.product_id == batch.product_id
            and (
                bom.consume_step_id == operation.step_id
                or not step_by_id[operation.step_id].predecessors
            )
        )
    descendants: set[str] = set()
    frontier = set(direct["operations"])
    while frontier:
        current = frontier.pop()
        source = operation_by_id[current]
        for operation in operations:
            if (
                operation.batch_id == source.batch_id
                and source.step_id in (step_by_id[operation.step_id].predecessors)
                and operation.operation_id not in direct["operations"] | descendants
            ):
                descendants.add(operation.operation_id)
                frontier.add(operation.operation_id)
    reasons = []
    if classification["material"]:
        reasons.append("LOCAL_BOUNDARY_NOT_PROVEN")
        if descendants:
            reasons.append("ROUTE_DEPENDENCIES")
        for key in ("resources", "workers", "materials"):
            if direct[key]:
                reasons.append("SHARED_" + key.upper())
        if any(e.entity_type == "orders" for e in verified):
            reasons.append("ORDER_SCOPE_MAY_EXPAND")
        if unknowns:
            reasons.append("UNRESOLVED_EVENT_IDENTITIES")
    return {
        "factory_id": snapshot.factory_id,
        "run_id": snapshot.run_id,
        "snapshot_hash": snapshot.content_hash,
        "source_revision": snapshot.source.source_revision,
        "classification": classification,
        "direct": {key: sorted(value) for key, value in direct.items()},
        "dependency_operations": sorted(descendants),
        "possible": {
            "scope": "FACTORY" if classification["material"] else "NO_MODEL_ACTION",
            **{
                key: sorted(value) if classification["material"] else []
                for key, value in all_ids.items()
            },
        },
        "expansion_reasons": reasons,
        "unknowns": sorted(unknowns),
        "delay": {"status": "NOT_EVALUATED", "minutes": None},
        "requires_full_check": True,
        "preserves_approval": False,
    }
