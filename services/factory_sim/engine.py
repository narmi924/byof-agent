"""Minute-based physical execution. Reading facts never advances time or random state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from packages.domain.demand import (
    DemandChangeError,
    demand_changes,
    materialize_batches,
    new_order_batches,
)
from packages.domain.execution import OrderChange
from packages.domain.models import (
    Candidate,
    Event,
    FieldChange,
    Order,
    Receipt,
    Snapshot,
    batch_operations,
    canonical_hash,
    duration_minutes,
)
from packages.domain.source_business import (
    BUSINESS_CONTROLS,
    SourceBusinessError,
    source_business_changes,
)

MINUTE = timedelta(minutes=1)


class SimulationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def evolve(snapshot: Snapshot, **changes: Any) -> Snapshot:
    """A new source version preserves every prior immutable BYOF snapshot."""
    data = snapshot.model_dump(mode="python", exclude={"content_hash"})
    data.update(changes)
    revision = int(snapshot.source.source_revision) + 1
    data.update(
        schema_version="byof.snapshot/3"
        if data.get("schema_version") == "byof.snapshot/3"
        else "byof.snapshot/2",
        snapshot_id=f"{snapshot.run_id}-{revision}",
        planning_revision=snapshot.planning_revision + 1,
    )
    data["source"].update(
        source_revision=str(revision),
        cursor=str(revision),
        observed_at=datetime.now(UTC),
        effective_at=data["snapshot_clock"],
    )
    return Snapshot.model_validate(data)


def _covered(entity: dict, start: datetime, end: datetime, overtime: bool) -> bool:
    cursor = start
    for window in sorted(entity["calendar"], key=lambda window: window["start_at"]):
        if window["kind"] == "OVERTIME" and not overtime:
            continue
        if window["end_at"] <= cursor:
            continue
        if window["start_at"] > cursor:
            break
        cursor = max(cursor, window["end_at"])
        if cursor >= end:
            return not any(
                start < w["end_at"] and w["start_at"] < end for w in entity["unavailable"]
            )
    return False


def _available(
    resource: dict, worker: dict, start: datetime, end: datetime, overtime: bool
) -> bool:
    return (
        resource["status"] == "AVAILABLE"
        and worker["status"] == "AVAILABLE"
        and _covered(resource, start, end, overtime)
        and _covered(worker, start, end, overtime and worker["overtime_available"])
    )


def _segment(actual: dict, phase: str, start: datetime, end: datetime, event_id: str) -> None:
    segments = actual["segments"]
    if segments and segments[-1]["phase"] == phase and segments[-1]["end_at"] == start:
        segments[-1]["end_at"] = end
    else:
        segments.append(
            {"phase": phase, "start_at": start, "end_at": end, "source_event_id": event_id}
        )


def receive(data: dict, receipt_id: str, now: datetime) -> bool:
    receipt = next((r for r in data["receipts"] if r["receipt_id"] == receipt_id), None)
    if receipt is None:
        raise SimulationError("RECEIPT_NOT_FOUND")
    if receipt["status"] == "RECEIVED":
        return False
    if receipt["status"] == "CANCELLED":
        raise SimulationError("RECEIPT_CANCELLED")
    stock = next(i for i in data["inventory"] if i["material_id"] == receipt["material_id"])
    stock["on_hand"] += receipt["quantity"]
    stock["version"] += 1
    receipt.update(status="RECEIVED", received_at=now, version=receipt["version"] + 1)
    return True


def advance(snapshot: Snapshot, plan: Candidate | None, *, minutes: int = 1) -> Snapshot:
    if type(minutes) is not int or not 1 <= minutes <= 1440:
        raise SimulationError("INVALID_CLOCK_STEP")
    if plan is not None and (
        plan.factory_id != snapshot.factory_id or plan.content_hash != snapshot.active_plan_hash
    ):
        raise SimulationError("EXECUTION_PLAN_MISMATCH")
    if snapshot.active_plan_version is not None and plan is None:
        raise SimulationError("EXECUTION_PLAN_MISSING")
    if snapshot.snapshot_clock + minutes * MINUTE > snapshot.horizon.end_at:
        raise SimulationError("HORIZON_EXCEEDED")
    current = snapshot
    for _ in range(minutes):
        current = _tick(current, plan)
    return current


def missed_dispatch_events(
    before: Snapshot, after: Snapshot, plan: Candidate | None
) -> tuple[Event, ...]:
    """A crossed dispatch instant is evidence of a miss, never fabricated WIP."""
    if plan is None or before.snapshot_clock >= after.snapshot_clock:
        return ()
    if (
        before.factory_id != after.factory_id
        or before.run_id != after.run_id
        or plan.factory_id != after.factory_id
        or before.active_plan_hash != plan.content_hash
        or after.active_plan_hash != plan.content_hash
    ):
        raise SimulationError("EXECUTION_PLAN_MISMATCH")
    actual_ids = {a.operation_id for a in (*before.actuals, *after.actuals)}
    current_ids = {operation.operation_id for operation in batch_operations(after)[1]}
    all_ids = {
        operation.operation_id for operation in batch_operations(after, include_cancelled=True)[1]
    }
    events = []
    for assignment in plan.assignments:
        if assignment.operation_id not in current_ids:
            if assignment.operation_id in all_ids:
                continue
            raise SimulationError("EXECUTION_PLAN_SCOPE_MISMATCH")
        dispatch_at = assignment.resume_changeover_start or assignment.changeover_start
        if assignment.operation_id in actual_ids or not (
            before.snapshot_clock <= dispatch_at < after.snapshot_clock
        ):
            continue
        identity = "dispatch-missed:" + canonical_hash(
            {
                "factory_id": after.factory_id,
                "run_id": after.run_id,
                "operation_id": assignment.operation_id,
                "candidate_hash": plan.content_hash,
                "dispatch_at": dispatch_at.isoformat(),
            }
        )
        events.append(
            Event(
                event_id=identity,
                source_event_id=identity,
                factory_id=after.factory_id,
                run_id=after.run_id,
                source_revision=after.source.source_revision,
                entity_type="operations",
                entity_id=assignment.operation_id,
                entity_version=plan.version,
                event_type="execution.dispatch_missed",
                occurred_at=after.snapshot_clock,
                effective_at=after.snapshot_clock,
                observed_at=after.source.observed_at,
                changes=(
                    FieldChange(field="dispatch_state", before="SCHEDULED", after="MISSED"),
                    FieldChange(field="candidate_hash", before=None, after=plan.content_hash),
                    FieldChange(
                        field="scheduled_dispatch_at", before=None, after=dispatch_at.isoformat()
                    ),
                ),
            )
        )
    return tuple(events)


def _tick(snapshot: Snapshot, plan: Candidate | None) -> Snapshot:
    data = snapshot.model_dump(mode="python", exclude={"content_hash"})
    # Mutable copies are private to this transition; source objects remain immutable.
    for name in ("actuals", "reservations", "orders"):
        data[name] = list(data[name])
    for actual in data["actuals"]:
        actual["segments"] = list(actual["segments"])
        actual["consumed"] = list(actual["consumed"])
    now, end = snapshot.snapshot_clock, snapshot.snapshot_clock + MINUTE
    event_id = f"{snapshot.run_id}:{int(snapshot.source.source_revision) + 1}"
    for receipt in data["receipts"]:
        if receipt["status"] == "CONFIRMED" and receipt["eta"] <= now:
            receive(data, receipt["receipt_id"], now)

    resources = {r["resource_id"]: r for r in data["resources"]}
    workers = {w["worker_id"]: w for w in data["workers"]}
    stocks = {s["material_id"]: s for s in data["inventory"]}
    actuals = {a["operation_id"]: a for a in data["actuals"]}
    batches, operations = batch_operations(snapshot)
    batch_by_id = {b.batch_id: b for b in batches}
    operation_by_id = {o.operation_id: o for o in operations}
    all_operation_ids = {
        o.operation_id for o in batch_operations(snapshot, include_cancelled=True)[1]
    }
    step_by_id = {s.step_id: s for s in snapshot.profile.routes}
    step_to_operation = {(o.batch_id, o.step_id): o.operation_id for o in operations}
    overtime = plan is not None and "allow_overtime" in plan.required_consents

    def predecessors_done(operation_id: str) -> bool:
        operation = operation_by_id[operation_id]
        for parent in step_by_id[operation.step_id].predecessors:
            prior = actuals.get(step_to_operation[(operation.batch_id, parent)])
            if prior is None or prior["state"] != "COMPLETED":
                return False
            if prior["actual_end"] > now:
                return False
            if step_by_id[parent].quality_gate and prior["quality_state"] != "PASSED":
                return False
        return True

    def start_production(actual: dict) -> bool:
        operation = operation_by_id[actual["operation_id"]]
        batch = batch_by_id[operation.batch_id]
        if not predecessors_done(operation.operation_id):
            return False
        if actual["actual_start"] is not None:
            return True
        bom = [b for b in snapshot.profile.bom if b.product_id == batch.product_id]
        owned = {
            r["material_id"]: r for r in data["reservations"] if r["batch_id"] == batch.batch_id
        }
        if not owned:
            if step_by_id[operation.step_id].predecessors:
                return False
            if any(
                stocks[b.material_id]["on_hand"] - stocks[b.material_id]["reserved"]
                < b.quantity_per_unit * batch.quantity
                for b in bom
            ):
                return False
            for item in bom:
                quantity = item.quantity_per_unit * batch.quantity
                stock = stocks[item.material_id]
                stock["reserved"] += quantity
                stock["version"] += 1
                reservation = {
                    "reservation_id": f"{snapshot.run_id}:{batch.batch_id}:{item.material_id}",
                    "batch_id": batch.batch_id,
                    "material_id": item.material_id,
                    "quantity": quantity,
                    "unit": stock["unit"],
                    "plan_version": snapshot.active_plan_version,
                    "source_event_id": f"{event_id}:reserve:{batch.batch_id}:{item.material_id}",
                    "created_at": now,
                }
                data["reservations"].append(reservation)
                owned[item.material_id] = reservation
        for item in bom:
            if item.consume_step_id != operation.step_id:
                continue
            quantity = item.quantity_per_unit * batch.quantity
            if owned[item.material_id]["quantity"] < quantity:
                raise SimulationError("RESERVATION_MISSING")
            owned[item.material_id]["quantity"] -= quantity
            stock = stocks[item.material_id]
            stock["on_hand"] -= quantity
            stock["reserved"] -= quantity
            stock["version"] += 1
            actual["consumed"].append(
                {
                    "material_id": item.material_id,
                    "quantity": quantity,
                    "unit": stock["unit"],
                    "event_id": f"{event_id}:consume:{operation.operation_id}:{item.material_id}",
                }
            )
        actual.update(actual_start=now, state="IN_PROGRESS")
        return True

    def process(actual: dict) -> None:
        resource, worker = resources[actual["resource_id"]], workers[actual["worker_id"]]
        if not _available(resource, worker, now, end, overtime):
            actual.update(
                state="BLOCKED",
                remaining_minutes=None,
                remaining_confirmed_by=None,
                remaining_setup_minutes=None,
                version=actual["version"] + 1,
            )
            return
        setup = actual["remaining_setup_minutes"]
        if setup is None or actual["remaining_minutes"] is None:
            return
        if setup > 0:
            _segment(actual, "SETUP", now, end, f"{event_id}:setup:{actual['operation_id']}")
            actual["remaining_setup_minutes"] -= 1
        else:
            if not start_production(actual):
                actual["state"] = "BLOCKED"
                return
            _segment(actual, "PRODUCTION", now, end, f"{event_id}:work:{actual['operation_id']}")
            actual["remaining_minutes"] -= 1
            if actual["remaining_minutes"] == 0:
                batch = batch_by_id[actual["batch_id"]]
                actual.update(
                    state="COMPLETED",
                    actual_end=end,
                    completed_quantity=batch.quantity,
                    quality_state="PASSED",
                )
        actual["version"] += 1
        actual["remaining_confirmed_by"] = event_id

    occupied_resources, occupied_workers = set(), set()
    for actual in data["actuals"]:
        if actual["state"] in ("IN_PROGRESS", "SETUP"):
            if (
                actual["resource_id"] in occupied_resources
                or actual["worker_id"] in occupied_workers
            ):
                raise SimulationError("ACTUAL_OCCUPANCY_CONFLICT")
            occupied_resources.add(actual["resource_id"])
            occupied_workers.add(actual["worker_id"])
            process(actual)

    assignments = (
        sorted(plan.assignments, key=lambda a: (a.changeover_start, a.operation_id)) if plan else ()
    )
    for assignment in assignments:
        # Explicit demand withdrawal cancels only unstarted lots; old accepted plans
        # remain audit evidence but cannot dispatch a tombstoned production identity.
        if assignment.operation_id not in operation_by_id:
            if assignment.operation_id in all_operation_ids:
                continue
            raise SimulationError("EXECUTION_PLAN_SCOPE_MISMATCH")
        dispatch_at = assignment.resume_changeover_start or assignment.changeover_start
        if dispatch_at > now:
            continue
        old = actuals.get(assignment.operation_id)
        if old is None and dispatch_at < now:
            continue
        if old and (
            old["state"] != "BLOCKED"
            or old["remaining_minutes"] is None
            or old["remaining_setup_minutes"] is None
        ):
            continue
        if assignment.resource_id in occupied_resources or assignment.worker_id in occupied_workers:
            continue
        operation = operation_by_id[assignment.operation_id]
        batch = batch_by_id[operation.batch_id]
        step = step_by_id[operation.step_id]
        # A crew prepares the next step only while every earlier step is running or done.
        # Behind a stopped or unstarted step the dispatch is missed and goes back to planning.
        if (old is None or old["actual_start"] is None) and any(
            actuals.get(step_to_operation[(operation.batch_id, parent)], {}).get("state")
            not in ("COMPLETED", "IN_PROGRESS", "SETUP")
            for parent in step.predecessors
        ):
            continue
        resource, worker = resources[assignment.resource_id], workers[assignment.worker_id]
        if old and (
            old["resource_id"] != assignment.resource_id or old["worker_id"] != assignment.worker_id
        ):
            raise SimulationError("WIP_IDENTITY_CHANGED")
        if (
            resource["resource_type"] != step.resource_type
            or step.operation_code not in resource["operation_codes"]
            or step.skill not in worker["skills"]
        ):
            continue
        remaining = old["remaining_minutes"] if old else duration_minutes(step, batch.quantity)
        policy = snapshot.profile.policy
        if old and resource["last_operation_id"] == assignment.operation_id:
            setup = old["remaining_setup_minutes"]
        elif resource["last_product_id"] is None:
            setup = policy.first_changeover_min
        elif resource["last_product_id"] == batch.product_id:
            setup = policy.same_product_changeover_min
        else:
            setup = policy.different_product_changeover_min
        if not _available(resource, worker, now, now + (setup + remaining) * MINUTE, overtime):
            continue
        actual = old or {
            "operation_id": operation.operation_id,
            "batch_id": batch.batch_id,
            "route_version": batch.route_version,
            "state": "SETUP",
            "actual_start": None,
            "actual_end": None,
            "changeover_start": now,
            "resource_id": assignment.resource_id,
            "worker_id": assignment.worker_id,
            "completed_quantity": 0,
            "consumed": [],
            "quality_state": "PENDING",
            "remaining_minutes": remaining,
            "remaining_confirmed_by": event_id,
            "version": 1,
            "segments": [],
        }
        actual["remaining_setup_minutes"] = setup
        actual["state"] = "IN_PROGRESS" if actual["actual_start"] is not None else "SETUP"
        if setup == 0 and not start_production(actual):
            if old is not None:
                actual["state"] = "BLOCKED"
            continue
        if old is None:
            data["actuals"].append(actual)
            actuals[operation.operation_id] = actual
        resource.update(
            last_product_id=batch.product_id,
            last_operation_id=operation.operation_id,
            version=resource["version"] + 1,
        )
        occupied_resources.add(assignment.resource_id)
        occupied_workers.add(assignment.worker_id)
        process(actual)

    for order in data["orders"]:
        if order["status"] == "CANCELLED":
            continue
        customer_batches = (
            {batch.batch_id for batch in snapshot.production_batches if batch.purpose == "CUSTOMER"}
            if snapshot.production_batches is not None
            else set(batch_by_id)
        )
        operation_ids = [
            o.operation_id
            for o in operations
            if batch_by_id[o.batch_id].order_id == order["order_id"]
            and o.batch_id in customer_batches
        ]
        completed = all(actuals.get(o, {}).get("state") == "COMPLETED" for o in operation_ids)
        started = any(actuals.get(o, {}).get("actual_start") is not None for o in operation_ids)
        status = "COMPLETED" if completed else "IN_PROGRESS" if started else order["status"]
        if order["status"] != status:
            order.update(status=status, version=order["version"] + 1)
    for receipt in data["receipts"]:
        if receipt["status"] == "CONFIRMED" and receipt["eta"] <= end:
            receive(data, receipt["receipt_id"], end)
    return evolve(
        snapshot,
        snapshot_clock=end,
        **{
            key: data[key]
            for key in ("actuals", "reservations", "inventory", "receipts", "resources", "orders")
        },
    )


def inject(snapshot: Snapshot, *, event_id: str, kind: str, payload: dict) -> Snapshot:
    """Called only by the authorized source control service, never an Agent tool."""
    if kind in BUSINESS_CONTROLS:
        try:
            return evolve(snapshot, **source_business_changes(snapshot, kind, payload))
        except SourceBusinessError as exc:
            raise SimulationError(exc.code) from exc
    data = snapshot.model_dump(mode="python", exclude={"content_hash"})
    changed: set[str] = set()
    if kind == "resource.outage":
        from packages.domain.execution import TimedOutage

        outage = TimedOutage.model_validate(payload)
        row = next((r for r in data["resources"] if r["resource_id"] == outage.resource_id), None)
        if row is None or row["status"] != "AVAILABLE":
            raise SimulationError("OBJECT_NOT_AVAILABLE")
        start, end = snapshot.snapshot_clock, snapshot.snapshot_clock + MINUTE * outage.minutes
        if any(start < w["end_at"] and w["start_at"] < end for w in row["unavailable"]):
            raise SimulationError("RESOURCE_ALREADY_UNAVAILABLE")
        row["unavailable"] = (*row["unavailable"], {"start_at": start, "end_at": end})
        row["version"] += 1
        for actual in data["actuals"]:
            if actual["resource_id"] == outage.resource_id and actual["state"] in (
                "SETUP",
                "IN_PROGRESS",
            ):
                # This scripted outage has exact simulator telemetry. Preserve measured work;
                # the ordinary unknown-duration resource.down path still clears it.
                actual.update(
                    state="BLOCKED", remaining_confirmed_by=event_id, version=actual["version"] + 1
                )
        changed.update(("resources", "actuals"))
    elif kind == "worker.leave":
        from packages.domain.execution import TimedLeave

        leave = TimedLeave.model_validate(payload)
        row = next((w for w in data["workers"] if w["worker_id"] == leave.worker_id), None)
        if row is None or row["status"] != "AVAILABLE":
            raise SimulationError("OBJECT_NOT_AVAILABLE")
        start, end = snapshot.snapshot_clock, snapshot.snapshot_clock + MINUTE * leave.minutes
        if any(start < w["end_at"] and w["start_at"] < end for w in row["unavailable"]):
            raise SimulationError("WORKER_ALREADY_UNAVAILABLE")
        row["unavailable"] = (*row["unavailable"], {"start_at": start, "end_at": end})
        row["version"] += 1
        for actual in data["actuals"]:
            if actual["worker_id"] == leave.worker_id and actual["state"] in (
                "SETUP",
                "IN_PROGRESS",
            ):
                # A known return time keeps the measured work; the same person resumes it.
                actual.update(
                    state="BLOCKED", remaining_confirmed_by=event_id, version=actual["version"] + 1
                )
        changed.update(("workers", "actuals"))
    elif kind in ("resource.down", "resource.restore", "worker.absent", "worker.return"):
        resource_event = kind.startswith("resource.")
        entity = "resources" if resource_event else "workers"
        key = "resource_id" if resource_event else "worker_id"
        if set(payload) != {key}:
            raise SimulationError("INVALID_CONTROL_PAYLOAD")
        row = next((r for r in data[entity] if r[key] == payload[key]), None)
        if row is None:
            raise SimulationError("OBJECT_NOT_FOUND")
        next_status = {
            "resource.down": "DOWN",
            "resource.restore": "AVAILABLE",
            "worker.absent": "ABSENT",
            "worker.return": "AVAILABLE",
        }[kind]
        if row["status"] == next_status:
            raise SimulationError("SOURCE_STATE_UNCHANGED")
        row["status"] = next_status
        row["version"] += 1
        changed.add(entity)
        if kind in ("resource.down", "worker.absent"):
            for actual in data["actuals"]:
                if actual[key] == row[key] and actual["state"] in ("SETUP", "IN_PROGRESS"):
                    actual.update(
                        state="BLOCKED",
                        remaining_minutes=None,
                        remaining_confirmed_by=None,
                        remaining_setup_minutes=None,
                        version=actual["version"] + 1,
                    )
            changed.add("actuals")
    elif kind == "execution.confirm_remaining":
        if set(payload) != {"operation_id", "remaining_minutes", "remaining_setup_minutes"}:
            raise SimulationError("INVALID_CONTROL_PAYLOAD")
        actual = next(
            (a for a in data["actuals"] if a["operation_id"] == payload["operation_id"]), None
        )
        if actual is None or actual["state"] != "BLOCKED":
            raise SimulationError("EXECUTION_NOT_BLOCKED")
        if (
            type(payload["remaining_minutes"]) is not int
            or payload["remaining_minutes"] <= 0
            or type(payload["remaining_setup_minutes"]) is not int
            or payload["remaining_setup_minutes"] < 0
        ):
            raise SimulationError("INVALID_REMAINING_WORK")
        actual.update(
            remaining_minutes=payload["remaining_minutes"],
            remaining_setup_minutes=payload["remaining_setup_minutes"],
            remaining_confirmed_by=event_id,
            version=actual["version"] + 1,
        )
        changed.add("actuals")
    elif kind == "inventory.reconcile":
        if set(payload) != {"material_id", "expected_version", "counted_on_hand", "reason"}:
            raise SimulationError("INVALID_CONTROL_PAYLOAD")
        row = next(
            (i for i in data["inventory"] if i["material_id"] == payload["material_id"]), None
        )
        if row is None:
            raise SimulationError("OBJECT_NOT_FOUND")
        if (
            type(payload["expected_version"]) is not int
            or payload["expected_version"] != row["version"]
        ):
            raise SimulationError("INVENTORY_VERSION_CHANGED")
        counted = payload["counted_on_hand"]
        if (
            type(counted) is not int
            or counted < row["reserved"]
            or counted == row["on_hand"]
            or type(payload["reason"]) is not str
            or payload["reason"] not in {"COUNT_CORRECTION", "SCRAP"}
            or payload["reason"] == "SCRAP"
            and counted > row["on_hand"]
        ):
            raise SimulationError("INVALID_INVENTORY_RECONCILIATION")
        row.update(on_hand=counted, version=row["version"] + 1)
        changed.add("inventory")
    elif kind == "receipt.add":
        if set(payload) != {"receipt_id", "material_id", "quantity", "eta"}:
            raise SimulationError("INVALID_CONTROL_PAYLOAD")
        material = next(
            (m for m in snapshot.profile.materials if m.material_id == payload["material_id"]),
            None,
        )
        if material is None or any(
            r.receipt_id == payload["receipt_id"] for r in snapshot.receipts
        ):
            raise SimulationError("RECEIPT_ALREADY_EXISTS_OR_MATERIAL_UNKNOWN")
        receipt = Receipt.model_validate(
            {**payload, "unit": material.unit, "status": "CONFIRMED", "version": 1}
        )
        if receipt.eta <= snapshot.snapshot_clock:
            raise SimulationError("RECEIPT_ETA_NOT_FUTURE")
        data["receipts"] = [*data["receipts"], receipt.model_dump(mode="python")]
        changed.add("receipts")
    elif kind in ("receipt.receive", "receipt.delay", "receipt.shortfall", "receipt.cancel"):
        expected = {
            "receipt.receive": {"receipt_id"},
            "receipt.delay": {"receipt_id", "eta"},
            "receipt.shortfall": {"receipt_id", "quantity"},
            "receipt.cancel": {"receipt_id"},
        }[kind]
        if set(payload) != expected:
            raise SimulationError("INVALID_CONTROL_PAYLOAD")
        if kind == "receipt.receive":
            receive(data, payload["receipt_id"], snapshot.snapshot_clock)
            changed.update(("receipts", "inventory"))
        else:
            row = next(
                (r for r in data["receipts"] if r["receipt_id"] == payload["receipt_id"]), None
            )
            if row is None or row["status"] not in ("CONFIRMED", "EXPECTED"):
                raise SimulationError("RECEIPT_NOT_PENDING")
            if kind == "receipt.delay":
                revised = Receipt.model_validate({**row, "eta": payload["eta"]})
                if revised.eta <= row["eta"] or revised.eta <= snapshot.snapshot_clock:
                    raise SimulationError("RECEIPT_DELAY_NOT_LATER")
                row["eta"] = revised.eta
            elif kind == "receipt.shortfall":
                quantity = payload["quantity"]
                if type(quantity) is not int or not 0 < quantity < row["quantity"]:
                    raise SimulationError("INVALID_RECEIPT_SHORTFALL")
                row["quantity"] = quantity
            else:
                row["status"] = "CANCELLED"
            row["version"] += 1
            changed.add("receipts")
    elif kind == "quality.record":
        if (
            not {"operation_id", "quality_state"} <= set(payload)
            or set(payload) - {"operation_id", "quality_state", "evidence"}
            or payload["quality_state"]
            not in (
                "PASSED",
                "FAILED",
                "UNKNOWN",
            )
        ):
            raise SimulationError("INVALID_CONTROL_PAYLOAD")
        actual = next(
            (a for a in data["actuals"] if a["operation_id"] == payload["operation_id"]), None
        )
        if actual is None or actual["state"] != "COMPLETED":
            raise SimulationError("EXECUTION_NOT_COMPLETED")
        if any(
            batch.batch_id == actual["batch_id"] and batch.purpose == "SCRAP"
            for batch in snapshot.production_batches or ()
        ):
            raise SimulationError("BATCH_ALREADY_DISPOSED")
        if (
            actual["quality_state"] == "FAILED"
            and payload["quality_state"] == "PASSED"
            and (
                not isinstance(payload.get("evidence"), str)
                or not 1 <= len(payload["evidence"].strip()) <= 500
            )
        ):
            raise SimulationError("QUALITY_EVIDENCE_REQUIRED")
        actual.update(quality_state=payload["quality_state"], version=actual["version"] + 1)
        changed.add("actuals")
    elif kind == "quality.scrap":
        if (
            set(payload) != {"operation_id", "reason"}
            or not isinstance(payload["reason"], str)
            or not 1 <= len(payload["reason"].strip()) <= 500
        ):
            raise SimulationError("INVALID_CONTROL_PAYLOAD")
        failed = next(
            (
                actual
                for actual in snapshot.actuals
                if actual.operation_id == payload["operation_id"]
            ),
            None,
        )
        if failed is None or failed.state != "COMPLETED" or failed.quality_state != "FAILED":
            raise SimulationError("FAILED_INSPECTION_REQUIRED")
        if any(
            actual.batch_id == failed.batch_id and actual.state != "COMPLETED"
            for actual in snapshot.actuals
        ):
            raise SimulationError("BATCH_STILL_RUNNING")
        batches = list(materialize_batches(snapshot))
        batch = next((item for item in batches if item.batch_id == failed.batch_id), None)
        if batch is None or batch.purpose not in {"CUSTOMER", "STOCK"}:
            raise SimulationError("BATCH_ALREADY_DISPOSED")
        order = next(item for item in snapshot.orders if item.order_id == batch.order_id)
        next_version = order.version + int(batch.purpose == "CUSTOMER")
        batches = [
            item.model_copy(
                update={
                    "purpose": "SCRAP",
                    "source_event_id": event_id,
                    "changed_order_version": next_version,
                    "delivery_due_at": None,
                }
            )
            if item.batch_id == batch.batch_id
            else item
            for item in batches
        ]
        if batch.purpose == "CUSTOMER":
            sequence = max(item.sequence for item in batches if item.order_id == order.order_id) + 1
            batches.append(
                batch.model_copy(
                    update={
                        "batch_id": f"{order.order_id}-R{order.split_revision:03d}-B{sequence:03d}",
                        "sequence": sequence,
                        "purpose": "CUSTOMER",
                        "source_event_id": event_id,
                        "changed_order_version": next_version,
                        "delivery_due_at": batch.delivery_due_at,
                    }
                )
            )
            data["orders"] = [
                {
                    **item,
                    "status": "IN_PROGRESS"
                    if any(
                        candidate.purpose == "CUSTOMER"
                        and candidate.order_id == order.order_id
                        and any(
                            actual.batch_id == candidate.batch_id for actual in snapshot.actuals
                        )
                        for candidate in batches
                    )
                    else "CONFIRMED",
                    "version": next_version,
                }
                if item["order_id"] == order.order_id
                else item
                for item in data["orders"]
            ]
            changed.add("orders")
        data["production_batches"] = [item.model_dump(mode="python") for item in batches]
        data["schema_version"] = "byof.snapshot/3"
        data["scope_version"] = snapshot.scope_version + 1
        changed.update(("schema_version", "production_batches", "scope_version"))
    elif kind == "order.add":
        order = Order.model_validate(payload)
        if (
            order.status != "CONFIRMED"
            or order.version != 1
            or order.split_revision != 1
            or any(o.order_id == order.order_id for o in snapshot.orders)
        ):
            raise SimulationError("ORDER_ALREADY_EXISTS_OR_STARTED")
        data["orders"] = [*data["orders"], order.model_dump(mode="python")]
        if snapshot.production_batches is not None:
            product = next(
                (p for p in snapshot.profile.products if p.product_id == order.product_id), None
            )
            if product is None:
                raise SimulationError("INVALID_REFERENCE")
            data["production_batches"] = (
                *snapshot.production_batches,
                *new_order_batches(order, product, source_event_id=event_id),
            )
            changed.add("production_batches")
        data["scope_version"] += 1
        changed.update(("orders", "scope_version"))
    elif kind == "order.change":
        try:
            changes = demand_changes(
                snapshot, OrderChange.model_validate(payload), event_id=event_id
            )
        except DemandChangeError as exc:
            raise SimulationError(exc.code) from exc
        return evolve(snapshot, **changes)
    elif kind == "treatment.apply":
        from packages.domain.treatment import TreatmentApplication, treatment_changes

        command = TreatmentApplication.model_validate(payload)
        if command.expected_snapshot_hash != snapshot.content_hash:
            raise SimulationError("TREATMENT_FACTS_CHANGED")
        try:
            return evolve(snapshot, **treatment_changes(snapshot, command.actions))
        except ValueError as exc:
            raise SimulationError("TREATMENT_CONDITIONS_CHANGED") from exc
    elif kind == "business.accept":
        from packages.domain.business_acceptance import (
            BusinessAcceptanceError,
            accept_business_option,
        )

        try:
            changes = accept_business_option(snapshot, payload, event_id=event_id)
        except BusinessAcceptanceError as exc:
            raise SimulationError(exc.code) from exc
        return evolve(snapshot, **changes)
    elif kind == "order.revise":
        if set(payload) != {
            "order_id",
            "expected_version",
            "quantity",
            "due_at",
            "priority_weight",
            "hard_deadline",
        }:
            raise SimulationError("INVALID_CONTROL_PAYLOAD")
        row = next((o for o in data["orders"] if o["order_id"] == payload["order_id"]), None)
        if row is None or row["status"] not in {
            "CONFIRMED",
            "IN_PROGRESS",
            "COMPLETED",
            "CANCELLED",
        }:
            raise SimulationError("ORDER_NOT_REVISABLE")
        if (
            type(payload["expected_version"]) is not int
            or payload["expected_version"] != row["version"]
        ):
            raise SimulationError("ORDER_VERSION_CHANGED")
        try:
            changes = demand_changes(
                snapshot,
                OrderChange.model_validate(
                    {
                        key: payload[key]
                        for key in ("order_id", "expected_version", "quantity", "due_at")
                    }
                ),
                event_id=event_id,
            )
        except DemandChangeError as exc:
            code = (
                "ORDER_BATCH_QUANTITY_REQUIRED"
                if exc.code == "UNSUPPORTED_BATCH_QUANTITY"
                else exc.code
            )
            raise SimulationError(code) from exc
        demand_order = next(o for o in changes["orders"] if o.order_id == row["order_id"])
        revised_order = Order.model_validate(
            {
                **demand_order.model_dump(mode="python"),
                **{key: payload[key] for key in ("priority_weight", "hard_deadline")},
            }
        )
        if all(
            getattr(revised_order, key) == row[key]
            for key in ("quantity", "due_at", "priority_weight", "hard_deadline")
        ):
            raise SimulationError("ORDER_UNCHANGED")
        changes["orders"] = tuple(
            revised_order if o.order_id == revised_order.order_id else o for o in changes["orders"]
        )
        return evolve(snapshot, **changes)
    else:
        raise SimulationError("UNKNOWN_CONTROL")
    return evolve(snapshot, **{key: data[key] for key in changed})
