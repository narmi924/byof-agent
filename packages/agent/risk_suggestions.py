"""Small, source-backed prompts for a manager to start a risk investigation.

This projection does not run a model or assert a solver-proven delay. A click is
stored as a normal manager message and the Agent checks current facts again.
"""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.agent.cases_store import CaseInput, CaseRecord
from packages.domain.models import (
    CalendarWindow,
    Candidate,
    Event,
    Snapshot,
    batch_operations,
    canonical_hash,
)
from packages.integrations.sync import SourceBatch
from packages.planning.store import SnapshotRecord


def _business_change(event: Event, field: str) -> tuple[object, object] | None:
    return next(
        ((change.before, change.after) for change in event.changes if change.field == field),
        None,
    )


def _open_orders(snapshot: Snapshot) -> set[str]:
    return {o.product_id for o in snapshot.orders if o.status in {"CONFIRMED", "IN_PROGRESS"}}


def _affected_material(snapshot: Snapshot, material_id: str) -> bool:
    products = _open_orders(snapshot)
    return any(
        b.material_id == material_id and b.product_id in products for b in snapshot.profile.bom
    )


def _material_shortage(snapshot: Snapshot, material_id: str, baseline: Candidate | None) -> bool:
    """Can current confirmed supply cover unfinished consumption at planned times?"""
    stock = next((i for i in snapshot.inventory if i.material_id == material_id), None)
    if stock is None:
        return False
    free = stock.on_hand - stock.reserved
    arrivals = sorted(
        (r.eta, r.quantity)
        for r in snapshot.receipts
        if r.material_id == material_id and r.status == "CONFIRMED"
    )
    batches, operations = batch_operations(snapshot)
    batch_by_id = {b.batch_id: b for b in batches}
    needed = {
        operation.operation_id: bom.quantity_per_unit * batch_by_id[operation.batch_id].quantity
        for operation in operations
        for bom in snapshot.profile.bom
        if bom.material_id == material_id
        and bom.product_id == batch_by_id[operation.batch_id].product_id
        and bom.consume_step_id == operation.step_id
    }
    started = {a.operation_id for a in snapshot.actuals if a.actual_start is not None}
    open_order_ids = {
        o.order_id for o in snapshot.orders if o.status in {"CONFIRMED", "IN_PROGRESS"}
    }
    eligible = {
        operation.operation_id
        for operation in operations
        if batch_by_id[operation.batch_id].order_id in open_order_ids
        and operation.operation_id not in started
    }
    if baseline is None:
        return sum(qty for operation_id, qty in needed.items() if operation_id in eligible) > (
            free + sum(qty for eta, qty in arrivals if eta <= snapshot.horizon.end_at)
        )
    demand = 0
    for assignment in sorted(baseline.assignments, key=lambda a: (a.start_at, a.operation_id)):
        if assignment.operation_id not in eligible:
            continue
        quantity = needed.get(assignment.operation_id, 0)
        if not quantity:
            continue
        demand += quantity
        at = max(assignment.start_at, snapshot.snapshot_clock)
        available = free + sum(qty for eta, qty in arrivals if eta <= at)
        if demand > available:
            return True
    return False


def _planned_resource(
    snapshot: Snapshot, baseline: Candidate | None, identity: str, *, worker: bool
) -> bool:
    name = "worker_id" if worker else "resource_id"
    if any(
        getattr(actual, name) == identity and actual.state in {"SETUP", "IN_PROGRESS", "BLOCKED"}
        for actual in snapshot.actuals
    ):
        return True
    if baseline is None:
        routes = (
            step for step in snapshot.profile.routes if step.product_id in _open_orders(snapshot)
        )
        if worker:
            person = next((row for row in snapshot.workers if row.worker_id == identity), None)
            return person is not None and any(step.skill in person.skills for step in routes)
        machine = next((row for row in snapshot.resources if row.resource_id == identity), None)
        return machine is not None and any(
            step.resource_type == machine.resource_type
            and step.operation_code in machine.operation_codes
            for step in routes
        )
    completed = {a.operation_id for a in snapshot.actuals if a.state == "COMPLETED"}
    return any(
        getattr(a, name) == identity
        and a.operation_id not in completed
        and a.end_at > snapshot.snapshot_clock
        for a in baseline.assignments
    )


def _covers(calendar: tuple[CalendarWindow, ...], start: datetime, end: datetime) -> bool:
    cursor = start
    for window in sorted(calendar, key=lambda item: item.start_at):
        if window.start_at > cursor:
            break
        cursor = max(cursor, window.end_at)
        if cursor >= end:
            return True
    return False


def _removed_overtime_risk(
    snapshot: Snapshot, event: Event, baseline: Candidate | None
) -> str | None:
    """Only lost capacity actually used by the remaining active, approved plan."""
    if (
        baseline is None
        or snapshot.active_plan_hash != baseline.content_hash
        or "allow_overtime" not in baseline.required_consents
        or event.entity_type not in {"workers", "resources"}
    ):
        return None
    worker = event.entity_type == "workers"
    key = "worker_id" if worker else "resource_id"
    row = next(
        (
            item
            for item in getattr(snapshot, event.entity_type)
            if getattr(item, key) == event.entity_id
        ),
        None,
    )
    evidence = _business_change(event, "calendar")
    if (
        row is None
        or row.version < event.entity_version
        or row.status != "AVAILABLE"
        or worker
        and not row.overtime_available
        or evidence is None
        or not all(isinstance(value, str) for value in evidence)
    ):
        return None
    try:
        calendar_type = TypeAdapter(tuple[CalendarWindow, ...])
        before = calendar_type.validate_python(json.loads(str(evidence[0])))
        after = calendar_type.validate_python(json.loads(str(evidence[1])))
    except (ValueError, ValidationError):
        return None
    removed = [window for window in before if window not in after]
    if (
        row.version == event.entity_version
        and after != row.calendar
        or len(removed) != 1
        or removed[0].kind != "OVERTIME"
        or tuple(window for window in before if window != removed[0]) != after
    ):
        return None
    window = removed[0]
    _, operations = batch_operations(snapshot)
    unfinished = {item.operation_id for item in operations} - {
        item.operation_id for item in snapshot.actuals if item.state == "COMPLETED"
    }
    for assignment in baseline.assignments:
        if assignment.operation_id not in unfinished or getattr(assignment, key) != event.entity_id:
            continue
        start = max(
            snapshot.snapshot_clock,
            assignment.resume_changeover_start or assignment.changeover_start,
        )
        end = assignment.end_at
        if (
            start < end
            and start < window.end_at
            and end > window.start_at
            and not any(item.start_at < end and item.end_at > start for item in row.unavailable)
            and _covers(before, start, end)
            and not _covers(after, start, end)
            and not _covers(row.calendar, start, end)
        ):
            label = "Worker" if worker else "Machine"
            return (
                f"{label} {event.entity_id}: overtime window revoked; "
                f"unfinished operation {assignment.operation_id} of the original schedule lost its window, so the schedule needs checking"
            )
    return None


def _risk_fact(snapshot: Snapshot, event: Event, baseline: Candidate | None) -> str | None:
    """Only a current adverse fact that crosses the documented attention threshold."""
    if (event.factory_id, event.run_id) != (snapshot.factory_id, snapshot.run_id):
        return None
    if event.event_type == "overtime_window.set":
        return _removed_overtime_risk(snapshot, event, baseline)
    if event.entity_type == "inventory" and event.event_type == "inventory.reconcile":
        stock = next((i for i in snapshot.inventory if i.material_id == event.entity_id), None)
        amount = _business_change(event, "on_hand")
        if (
            stock is None
            or stock.version < event.entity_version
            or amount is None
            or type(amount[0]) is not int
            or type(amount[1]) is not int
            or amount[1] >= amount[0]
            or stock.on_hand > amount[1]
            or not _affected_material(snapshot, stock.material_id)
            or not _material_shortage(snapshot, stock.material_id, baseline)
        ):
            return None
        material = next(m for m in snapshot.profile.materials if m.material_id == stock.material_id)
        return f"Stock count of {material.name} changed from {amount[0]} to {amount[1]} {stock.unit}; the original schedule may lack material"
    if event.entity_type == "receipts":
        receipt = next((r for r in snapshot.receipts if r.receipt_id == event.entity_id), None)
        if (
            receipt is None
            or receipt.version != event.entity_version
            or not _affected_material(snapshot, receipt.material_id)
            or not _material_shortage(snapshot, receipt.material_id, baseline)
        ):
            return None
        material = next(
            m for m in snapshot.profile.materials if m.material_id == receipt.material_id
        )
        zone = ZoneInfo(snapshot.profile.timezone)
        name = f"Receipt {receipt.receipt_id} ({material.material_id} {material.name})"
        status = _business_change(event, "status")
        if status and status[1] == "CANCELLED" and receipt.status == "CANCELLED":
            return (
                f"{name}: expected arrival cancelled, originally {receipt.quantity} {receipt.unit}"
            )
        if receipt.status not in {"EXPECTED", "CONFIRMED"} or receipt.eta > snapshot.horizon.end_at:
            return None
        quantity = _business_change(event, "quantity")
        if quantity and type(quantity[0]) is int and type(quantity[1]) is int:
            before, after = int(quantity[0]), int(quantity[1])
            if before > after > 0 and receipt.quantity == after:
                return f"{name}: expected quantity reduced from {before} to {after} {receipt.unit}"
        eta = _business_change(event, "eta")
        if eta and isinstance(eta[0], str) and isinstance(eta[1], str):
            try:
                previous_eta, next_eta = (
                    datetime.fromisoformat(eta[0]),
                    datetime.fromisoformat(eta[1]),
                )
            except ValueError:
                return None
            if next_eta - previous_eta >= timedelta(minutes=30) and receipt.eta == next_eta:
                return (
                    f"{name}: expected arrival delayed from {previous_eta.astimezone(zone):%m-%d %H:%M} "
                    f"to {next_eta.astimezone(zone):%m-%d %H:%M}"
                )
        return None
    if event.entity_type == "orders":
        order = next((o for o in snapshot.orders if o.order_id == event.entity_id), None)
        if (
            event.event_type == "order.added"
            and order
            and order.version >= event.entity_version
            and order.status in {"CONFIRMED", "IN_PROGRESS"}
            and (
                baseline is not None
                and order.due_at <= snapshot.horizon.end_at
                or order.hard_deadline
                or order.due_at <= snapshot.snapshot_clock + timedelta(days=1)
            )
        ):
            return f"New order {order.order_id} ({order.quantity} pcs); check capacity and due date"
        if (
            event.event_type == "order.revise"
            and order
            and order.version >= event.entity_version
            and order.status in {"CONFIRMED", "IN_PROGRESS", "CANCELLED"}
        ):
            quantity = _business_change(event, "quantity")
            due = _business_change(event, "due_at")
            priority = _business_change(event, "priority_weight")
            hard = _business_change(event, "hard_deadline")
            # Name every changed business term; a quantity alone hides a date moved in.
            dates = ""
            earlier_by = timedelta(0)
            if due and isinstance(due[0], str) and isinstance(due[1], str):
                try:
                    previous_due = datetime.fromisoformat(due[0])
                    current_due = datetime.fromisoformat(due[1])
                except ValueError:
                    pass
                else:
                    if order.due_at == current_due and previous_due != current_due:
                        zone = ZoneInfo(snapshot.profile.timezone)
                        earlier_by = previous_due - current_due
                        dates = (
                            f"due date moved {'earlier' if earlier_by > timedelta(0) else 'later'}"
                            f" from {previous_due.astimezone(zone):%m-%d %H:%M}"
                            f" to {current_due.astimezone(zone):%m-%d %H:%M}"
                        )
            if quantity and type(quantity[0]) is int and type(quantity[1]) is int:
                if (
                    quantity[0] != quantity[1]
                    and order.quantity == quantity[1]
                    and (baseline is not None or quantity[1] > quantity[0])
                ):
                    return (
                        f"Order {order.order_id} demand changed from {quantity[0]} to"
                        f" {quantity[1]} pcs" + (f", {dates}" if dates else "")
                    )
            if earlier_by >= timedelta(minutes=30):
                return f"Order {order.order_id} {dates}; check the original schedule"
            if hard and hard == (False, True) and order.hard_deadline:
                return f"Order {order.order_id} changed to a hard due date; check the original schedule"
            if priority and type(priority[0]) is int and type(priority[1]) is int:
                if (
                    priority[1] > priority[0]
                    and order.priority_weight == priority[1]
                    and baseline is not None
                ):
                    return f"Order {order.order_id} priority raised; check the original schedule"
        return None
    if event.entity_type == "resources":
        resource = next((r for r in snapshot.resources if r.resource_id == event.entity_id), None)
        if (
            resource
            and resource.version == event.entity_version
            and _planned_resource(snapshot, baseline, resource.resource_id, worker=False)
            and (
                resource.status in {"DOWN", "MAINTENANCE"}
                or event.event_type == "resource.outage"
                and any(
                    w.start_at < snapshot.horizon.end_at
                    and w.end_at > snapshot.snapshot_clock
                    and w.end_at - w.start_at >= timedelta(minutes=30)
                    for w in resource.unavailable
                )
            )
        ):
            return f"Machine {resource.resource_id} is unavailable for the original schedule"
        return None
    if event.entity_type == "workers":
        worker = next((w for w in snapshot.workers if w.worker_id == event.entity_id), None)
        if (
            worker
            and worker.version == event.entity_version
            and _planned_resource(snapshot, baseline, worker.worker_id, worker=True)
        ):
            if worker.status == "ABSENT":
                return (
                    f"Worker {worker.worker_id} is absent, which may affect the original schedule"
                )
            if event.event_type == "worker.leave" and any(
                w.end_at > snapshot.snapshot_clock
                and w.end_at - w.start_at >= timedelta(minutes=30)
                for w in worker.unavailable
            ):
                return f"Worker {worker.worker_id} is on temporary leave, which may affect the original schedule"
        return None
    if event.entity_type == "actuals":
        actual = next((a for a in snapshot.actuals if a.operation_id == event.entity_id), None)
        if (
            actual
            and actual.version == event.entity_version
            and (actual.state == "BLOCKED" or actual.quality_state == "FAILED")
        ):
            return f"Operation {actual.operation_id} is blocked or failed quality"
    return None


def _handled_events(db: Session, snapshot: Snapshot) -> set[str]:
    rows = db.scalars(
        select(CaseInput.payload)
        .join(CaseRecord, CaseRecord.case_id == CaseInput.case_id)
        .where(
            CaseRecord.factory_id == snapshot.factory_id,
            CaseRecord.run_id == snapshot.run_id,
            CaseInput.kind == "USER",
            CaseInput.payload.has_key("source_event_ids"),
        )  # noqa: W601 - PostgreSQL JSONB operator
    )
    return {
        event_id
        for payload in rows
        for event_id in payload.get("source_event_ids", [])
        if isinstance(event_id, str)
    }


def current_suggestions(db: Session, snapshot: Snapshot, baseline: Candidate | None) -> list[dict]:
    """Read only: recent non-clock source changes, checked against the current snapshot."""
    handled = _handled_events(db, snapshot)
    planned_snapshot = (
        db.scalar(
            select(SnapshotRecord).where(
                SnapshotRecord.factory_id == snapshot.factory_id,
                SnapshotRecord.content_hash == baseline.binding.snapshot_hash,
            )
        )
        if baseline is not None
        else None
    )
    planned_revision = (
        str(planned_snapshot.document["source"]["source_revision"])
        if planned_snapshot is not None and planned_snapshot.document["run_id"] == snapshot.run_id
        else ""
    )
    planned_through = int(planned_revision) if planned_revision.isdecimal() else None
    batches = list(
        db.scalars(
            select(SourceBatch)
            .where(
                SourceBatch.factory_id == snapshot.factory_id,
                SourceBatch.run_id == snapshot.run_id,
                SourceBatch.document["cause"].as_string() != "clock.tick",
            )
            .order_by(SourceBatch.revision.desc())
            .limit(100)
        )
    )
    detected: list[tuple[SourceBatch, Event, str]] = []
    for batch in reversed(batches):
        if planned_through is not None and batch.revision <= planned_through:
            continue
        for raw in batch.document.get("events", []):
            event = Event.model_validate(raw)
            if event.event_id not in handled:
                fact = _risk_fact(snapshot, event, baseline)
                if fact:
                    detected.append((batch, event, fact))
    groups: list[list[tuple[SourceBatch, Event, str]]] = []
    for item in detected:
        if not groups or item[0].received_at - groups[-1][-1][0].received_at > timedelta(
            seconds=20
        ):
            groups.append([])
        groups[-1].append(item)
    result = []
    for group in reversed(groups[-3:]):
        facts = list(dict.fromkeys(item[2] for item in group))
        event_ids = [item[1].event_id for item in group]
        title = (
            f"Handle {len(facts)} changes on the latest shop floor"
            if len(facts) > 1
            else "Check the revoked overtime window"
            if group[0][1].event_type == "overtime_window.set"
            else {
                "receipts": "Handle the material supply change",
                "inventory": "Handle the stock shortage",
                "orders": "Assess the order demand change",
                "resources": "Handle the unavailable machine",
                "workers": "Handle the staff absence",
                "actuals": "Handle the blocked production",
            }.get(group[0][1].entity_type, "Analyze the shop floor change")
        )
        prompt_facts = facts[:4]
        if len(facts) > 4:
            prompt_facts.append(f"{len(facts) - 4} more changes at the same time")
        result.append(
            {
                "suggestion_id": canonical_hash(
                    {"run_id": snapshot.run_id, "event_ids": event_ids}
                ),
                "run_id": snapshot.run_id,
                "source_revision": snapshot.source.source_revision,
                "title": title,
                "detail": "; ".join(facts[:2]),
                "prompt": "Check against the latest factory facts: "
                + "; ".join(prompt_facts)
                + (
                    ". Analyze the impact on production and due dates and give me feasible plans to approve; the production clock does not start before the first plan is approved."
                    if baseline is None
                    else ". Analyze the impact on production and due dates, give me feasible plans to approve, and reserve 15 minutes of review time."
                ),
                "source_event_ids": event_ids,
            }
        )
    return result


def match_suggestion(
    db: Session, snapshot: Snapshot, baseline: Candidate | None, suggestion_id: str
) -> dict:
    from packages.auth import AccessError

    item = next(
        (
            s
            for s in current_suggestions(db, snapshot, baseline)
            if s["suggestion_id"] == suggestion_id
        ),
        None,
    )
    if item is None:
        raise AccessError(
            "SUGGESTION_OUTDATED",
            "The shop floor suggestion has changed; refresh and choose the latest one.",
            409,
        )
    return item
