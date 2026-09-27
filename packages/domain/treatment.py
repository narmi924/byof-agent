"""Finite, priced simulation actions shared by scenario calculation and execution."""

from datetime import timedelta
from typing import Literal

from pydantic import Field, StrictInt, model_validator

from packages.domain.demand import demand_changes
from packages.domain.execution import OrderChange
from packages.domain.models import Contract, Digest, Identifier, Snapshot, Timestamp


class TreatmentAction(Contract):
    kind: Literal["supply", "repair", "staff", "order_due", "order_quantity"]
    target_id: Identifier
    action_id: Identifier
    ready_at: Timestamp
    quantity: StrictInt = Field(default=0, ge=0, le=5000)
    expected_version: StrictInt = Field(ge=1)
    mode: Literal["standard", "express", "immediate"] = "standard"

    @model_validator(mode="after")
    def relevant_quantity(self):
        if (self.kind in {"supply", "order_quantity"}) != (self.quantity > 0):
            raise ValueError("Supply and revised order quantities must be positive")
        if self.kind == "supply" and self.quantity % 50:
            raise ValueError("Simulated supply is sold in packs of fifty material units")
        if self.kind != "supply" and self.mode != "standard":
            raise ValueError("Supply speed applies only to purchase actions")
        return self


class TreatmentApplication(Contract):
    expected_snapshot_hash: Digest
    catalog_version: Literal["byof-demo-economics/1"]
    actions: tuple[TreatmentAction, ...] = Field(min_length=1, max_length=24)


def material_rate(material_id: str) -> int | None:
    # SGD cents per component in the synthetic catalogue. Never a live supplier quote.
    prefix = material_id.split("-", 1)[0]
    return {
        "IR": 150,
        "OR": 150,
        "BALLSET": 80,
        "CAGE": 40,
        "SEAL": 20,
        "BOX": 10,
        "GREASE": 5,
    }.get(prefix)


def action_cost(action: TreatmentAction) -> tuple[int, int]:
    """Return additional economic expense and incremental cash, both SGD cents."""
    if action.kind == "supply":
        rate = material_rate(action.target_id)
        if rate is None:
            raise ValueError("No simulated material price")
        purchase = action.quantity * rate
        premium, transport = {
            "standard": (0, 1000),
            "express": (25, 3000),
            "immediate": (100, 8000),
        }[action.mode]
        extra = (purchase * premium + 99) // 100 + transport
        return extra, purchase + extra
    if action.kind == "repair":
        return 50000, 50000
    if action.kind == "staff":
        return 20000, 20000
    return 0, 0  # Order concessions are calculated once from original due dates.


def treatment_changes(snapshot: Snapshot, actions: tuple[TreatmentAction, ...]) -> dict:
    """Apply an approved finite bundle atomically; original qualifications are preserved."""
    data = snapshot.model_dump(mode="python", exclude={"content_hash"})
    data["receipts"] = list(data["receipts"])
    data["workers"] = list(data["workers"])
    now = snapshot.snapshot_clock
    changed: set[str] = set()
    for action in actions:
        immediate_supply = action.kind == "supply" and action.mode == "immediate"
        if (
            action.ready_at < now and not immediate_supply
        ) or action.ready_at >= snapshot.horizon.end_at:
            raise ValueError("Action availability is outside the current planning window")
        action_cost(action)
        if action.kind == "supply":
            immediate = immediate_supply
            stock = next(
                (r for r in data["inventory"] if r["material_id"] == action.target_id), None
            )
            if stock is None or stock["version"] != action.expected_version:
                raise ValueError("Material facts changed")
            committed_quantity = sum(
                r["quantity"]
                for r in data["receipts"]
                if r["material_id"] == action.target_id
                and r["receipt_id"].startswith(("supply:", "reduced-supply:"))
            )
            if committed_quantity + action.quantity > 5000:
                raise ValueError("Simulated supplier capacity for this run is exhausted")
            if any(r["receipt_id"] == action.action_id for r in data["receipts"]):
                raise ValueError("Purchase already exists")
            lead = {"standard": 120, "express": 30, "immediate": 0}[action.mode]
            if not immediate and action.ready_at < now + timedelta(minutes=lead):
                raise ValueError("Arrival is earlier than the priced supply capability")
            data["receipts"].append(
                {
                    "receipt_id": action.action_id,
                    "material_id": action.target_id,
                    "unit": stock["unit"],
                    "quantity": action.quantity,
                    "eta": action.ready_at,
                    "status": "RECEIVED" if immediate else "CONFIRMED",
                    "received_at": now if immediate else None,
                    "version": 1,
                }
            )
            if immediate:
                stock.update(
                    on_hand=stock["on_hand"] + action.quantity, version=stock["version"] + 1
                )
                changed.add("inventory")
            changed.add("receipts")
        elif action.kind == "repair":
            resource = next(
                (r for r in data["resources"] if r["resource_id"] == action.target_id), None
            )
            if resource is None or resource["version"] != action.expected_version:
                raise ValueError("Equipment facts changed")
            if action.ready_at < now + timedelta(minutes=60):
                raise ValueError("Expedited repair requires sixty simulated minutes")
            if resource["status"] not in {"DOWN", "MAINTENANCE"} and not any(
                w["start_at"] <= now < w["end_at"] for w in resource["unavailable"]
            ):
                raise ValueError("Equipment has no current outage to repair")
            # Retain future unrelated unavailability and elapsed outage evidence.
            windows = [
                w for w in resource["unavailable"] if not (w["start_at"] <= now < w["end_at"])
            ]
            windows.extend(
                {"start_at": w["start_at"], "end_at": now}
                for w in resource["unavailable"]
                if w["start_at"] < now < w["end_at"]
            )
            windows.append({"start_at": now, "end_at": action.ready_at})
            resource.update(
                status="AVAILABLE", unavailable=windows, version=resource["version"] + 1
            )
            changed.add("resources")
        elif action.kind == "staff":
            worker = next((r for r in data["workers"] if r["worker_id"] == action.target_id), None)
            if (
                worker is None
                or worker["version"] != action.expected_version
                or worker["status"] != "ABSENT"
            ):
                raise ValueError("Absent worker facts changed")
            if action.ready_at < now + timedelta(minutes=30):
                raise ValueError("Qualified agency cover needs thirty simulated minutes")
            if any(w["worker_id"] == action.action_id for w in data["workers"]):
                raise ValueError("Replacement worker already exists")
            data["workers"].append(
                {
                    **worker,
                    "worker_id": action.action_id,
                    "status": "AVAILABLE",
                    "unavailable": [{"start_at": now, "end_at": action.ready_at}],
                    "version": 1,
                }
            )
            changed.add("workers")
            # The cover takes over the work the absent person had to stop; it keeps its history.
            for actual in data["actuals"]:
                if actual["worker_id"] == action.target_id and actual["state"] == "BLOCKED":
                    actual.update(worker_id=action.action_id, version=actual["version"] + 1)
                    changed.add("actuals")
        elif action.kind == "order_quantity":
            current = Snapshot.model_validate(data)
            order = next((o for o in current.orders if o.order_id == action.target_id), None)
            if order is None or order.status != "CONFIRMED" or action.quantity >= order.quantity:
                raise ValueError("Only an unstarted order can reduce its agreed quantity")
            updates = demand_changes(
                current,
                OrderChange(
                    order_id=action.target_id,
                    expected_version=action.expected_version,
                    quantity=action.quantity,
                    due_at=order.due_at,
                ),
                event_id=action.action_id,
            )
            # Normalize model values for any later action in the same atomic bundle.
            current = Snapshot.model_validate({**data, **updates})
            normalized = current.model_dump(mode="python", exclude={"content_hash"})
            for field in updates:
                data[field] = (
                    list(normalized[field])
                    if isinstance(normalized[field], tuple)
                    else normalized[field]
                )
            changed.update(updates)
        else:
            order = next((r for r in data["orders"] if r["order_id"] == action.target_id), None)
            if (
                order is None
                or order["version"] != action.expected_version
                or order["status"] not in {"CONFIRMED", "IN_PROGRESS"}
            ):
                # Extending the promise date leaves batches and started work untouched.
                raise ValueError("Only an unchanged open order can renegotiate its due date")
            if action.ready_at <= order["due_at"]:
                raise ValueError("This simulated concession must extend the existing due date")
            order.update(due_at=action.ready_at, version=order["version"] + 1)
            changed.add("orders")
    return {field: data[field] for field in changed}


def project_treatment(
    snapshot: Snapshot, actions: tuple[TreatmentAction, ...], identity: str
) -> Snapshot:
    return Snapshot.model_validate(
        {
            **snapshot.model_dump(mode="python", exclude={"content_hash"}),
            **treatment_changes(snapshot, actions),
            "snapshot_id": identity,
        }
    )
