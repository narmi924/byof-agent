"""Immutable contracts; validation does not grant approval or activate a factory."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal, NoReturn, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from packages.domain.business_terms import BusinessTerms

Identifier = Annotated[StrictStr, Field(min_length=1, max_length=160, pattern=r"^[^\s]+$")]
Text = Annotated[StrictStr, Field(min_length=1, max_length=4000)]
Positive = Annotated[StrictInt, Field(gt=0)]
NonNegative = Annotated[StrictInt, Field(ge=0)]
Unary = Annotated[StrictInt, Field(ge=1, le=1)]
Digest = Annotated[StrictStr, Field(pattern=r"^[a-f0-9]{64}$")]
Unit = Literal["EA", "SET", "GFU"]
NativeStatus = Literal["OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN", "MODEL_INVALID"]


class ErrorCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    UNKNOWN_UNIT = "UNKNOWN_UNIT"
    UNSUPPORTED_BATCH_QUANTITY = "UNSUPPORTED_BATCH_QUANTITY"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    CYCLIC_ROUTE = "CYCLIC_ROUTE"
    INVALID_REFERENCE = "INVALID_REFERENCE"
    DUPLICATE_ID = "DUPLICATE_ID"
    INVALID_TIME = "INVALID_TIME"
    HASH_MISMATCH = "HASH_MISMATCH"
    INVENTORY_CONFLICT = "INVENTORY_CONFLICT"
    SOURCE_INCOMPLETE = "SOURCE_INCOMPLETE"
    BASELINE_CHANGED = "BASELINE_CHANGED"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"


def reject(code: ErrorCode, message: str) -> NoReturn:
    raise PydanticCustomError(code.value, message)


def _time_input(value: object) -> object:
    if not isinstance(value, (str, datetime)):
        reject(ErrorCode.INVALID_TIME, "Time must be an explicit timezone-aware ISO timestamp")
    return value


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


Timestamp = Annotated[AwareDatetime, BeforeValidator(_time_input), AfterValidator(_utc)]


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, validate_default=True, revalidate_instances="always"
    )


def canonical_hash(value: BaseModel | dict[str, object]) -> str:
    data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    raw = json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def versioned_digest(data: dict[str, Any]) -> str:
    """Retain v1 serialized hashes; never allow v2 facts to hide outside a v1 hash."""
    if data.get("schema_version") in {"byof.candidate/1", "byof.candidate/2"}:
        data = dict(data)
        if data.pop("new_actions_not_before", None) is not None:
            reject(ErrorCode.VERSION_MISMATCH, "New action timing requires candidate version 3")
    versions: dict[str, dict[str, Any]] = {
        "byof.snapshot/1": {
            "reservations": [],
            "active_plan_hash": None,
        },
        "byof.candidate/1": {
            "solver_passes": [],
            "last_search_status": None,
            "constant_objective_levels": [],
        },
    }
    legacy = versions.get(str(data.get("schema_version")))
    if legacy is None:
        return canonical_hash(data)
    data = json.loads(json.dumps(data))

    def strip(record: dict, defaults: dict) -> None:
        for name, default in defaults.items():
            if record.get(name, default) != default:
                reject(ErrorCode.VERSION_MISMATCH, "Version 2 facts require a version 2 contract")
            record.pop(name, None)

    strip(data, legacy)
    for resource in data.get("resources", []):
        strip(resource, {"last_operation_id": None, "last_product_id": None})
    for actual in data.get("actuals", []):
        strip(actual, {"changeover_start": None, "segments": [], "remaining_setup_minutes": None})
        if actual["actual_start"] is None or actual["state"] == "SETUP":
            reject(ErrorCode.VERSION_MISMATCH, "Setup execution requires version 2")
    for assignment in data.get("assignments", []):
        strip(assignment, {"resume_at": None, "resume_changeover_start": None})
    return canonical_hash(data)


def _unique(values: tuple[str, ...], label: str) -> None:
    if len(set(values)) != len(values):
        reject(ErrorCode.DUPLICATE_ID, f"Duplicate {label}")


class TimeWindow(Contract):
    start_at: Timestamp
    end_at: Timestamp

    @model_validator(mode="after")
    def positive_window(self) -> Self:
        if self.start_at >= self.end_at:
            reject(ErrorCode.INVALID_TIME, "Intervals are nonempty [start_at, end_at)")
        return self


class CalendarWindow(TimeWindow):
    kind: Literal["NORMAL", "OVERTIME"] = "NORMAL"


class Product(Contract):
    product_id: Identifier
    name: Text
    batch_size: Positive
    route_version: Identifier


class Material(Contract):
    material_id: Identifier
    name: Text
    unit: Unit

    @field_validator("unit", mode="before")
    @classmethod
    def supported_unit(cls, value: object) -> object:
        if value not in ("EA", "SET", "GFU"):
            reject(ErrorCode.UNKNOWN_UNIT, "Unit requires an explicit supported mapping")
        return value


class BOM(Contract):
    product_id: Identifier
    material_id: Identifier
    quantity_per_unit: Positive
    consume_step_id: Identifier


class RouteStep(Contract):
    step_id: Identifier
    product_id: Identifier
    route_version: Identifier
    operation_code: Identifier
    name: Text
    predecessors: tuple[Identifier, ...] = ()
    setup_min: NonNegative
    cycle_sec_per_unit: Positive
    resource_type: Identifier
    skill: Identifier
    quality_gate: StrictBool = False
    quality_threshold: None = None


def duration_minutes(step: RouteStep, quantity: int) -> int:
    if type(quantity) is not int or quantity <= 0:
        reject(ErrorCode.UNSUPPORTED_BATCH_QUANTITY, "Quantity must be a positive integer")
    return (step.setup_min * 60 + step.cycle_sec_per_unit * quantity + 59) // 60


def minute_offset(origin: datetime, value: datetime, *, round_up: bool) -> int:
    """Elapsed minutes retain nights/breaks; releases round up, deadlines round down."""
    if origin.tzinfo is None or value.tzinfo is None:
        reject(ErrorCode.INVALID_TIME, "Minute conversion requires timezone-aware timestamps")
    microseconds = (value - origin) // timedelta(microseconds=1)
    minute = 60_000_000
    return -(-microseconds // minute) if round_up else microseconds // minute


class Policy(Contract):
    policy_version: Identifier
    full_kit_before_first_operation: Literal[True] = True
    workers_per_operation: Unary = 1
    first_changeover_min: NonNegative
    same_product_changeover_min: NonNegative
    different_product_changeover_min: NonNegative
    freeze_window_min: NonNegative
    overtime_cost_per_minute: NonNegative | None = None
    progress_revalidation_enabled: StrictBool = False

    @field_validator(
        "full_kit_before_first_operation", "progress_revalidation_enabled", mode="before"
    )
    @classmethod
    def strict_policy_flags(cls, value: object) -> object:
        if type(value) is not bool:
            reject(ErrorCode.INVALID_INPUT, "Policy flags must be JSON booleans")
        return value


KNOWN_CAPABILITIES = frozenset(
    {
        "fixed_lot_exact_split",
        "acyclic_operation_precedence",
        "alternative_unary_resource",
        "one_qualified_worker_full_duration",
        "time_phased_supply",
        "full_kit_before_first_operation",
        "per_operation_consumption",
        "freeze_and_confirmed_wip",
        "independent_schedule_check",
        "versioned_human_approval",
    }
)
# Structural parsing is implemented; runtime scheduling/execution capability is not yet certified.
IMPLEMENTED_RUNTIME_CAPABILITIES: frozenset[str] = frozenset()


class FactoryProfile(Contract):
    schema_version: Literal["byof.factory-profile/1"] = "byof.factory-profile/1"
    factory_id: Identifier
    profile_id: Identifier
    version: Identifier
    timezone: Identifier
    activation_state: Literal["DRAFT", "NEEDS_CONFIRMATION", "VALIDATED", "ACTIVE", "BLOCKED"] = (
        "DRAFT"
    )
    evidence_mode: Literal["public_reference_plus_synthetic_operations", "synthetic", "enterprise"]
    source_digest: Digest
    required_capabilities: tuple[Identifier, ...]
    products: tuple[Product, ...]
    materials: tuple[Material, ...]
    bom: tuple[BOM, ...]
    routes: tuple[RouteStep, ...]
    policy: Policy

    @model_validator(mode="after")
    def validate_profile(self) -> Self:
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            reject(ErrorCode.INVALID_TIME, "Unknown display timezone")
        if not self.products or not self.materials or not self.routes or not self.bom:
            reject(
                ErrorCode.INVALID_INPUT, "A factory requires products, materials, BOM and routes"
            )
        _unique(tuple(p.product_id for p in self.products), "product")
        _unique(tuple(m.material_id for m in self.materials), "material")
        _unique(tuple(s.step_id for s in self.routes), "route step")
        _unique(tuple(f"{b.product_id}/{b.material_id}" for b in self.bom), "BOM item")
        _unique(self.required_capabilities, "required capability")
        if set(self.required_capabilities) - KNOWN_CAPABILITIES:
            reject(ErrorCode.UNSUPPORTED_CAPABILITY, "Factory requires unsupported capabilities")
        if self.activation_state == "ACTIVE":
            reject(
                ErrorCode.CONFIRMATION_REQUIRED, "Only the verified activation service may activate"
            )
        products = {p.product_id: p for p in self.products}
        materials = {m.material_id for m in self.materials}
        steps = {s.step_id: s for s in self.routes}
        for step in self.routes:
            product = products.get(step.product_id)
            if product is None or product.route_version != step.route_version:
                reject(ErrorCode.INVALID_REFERENCE, "Route must reference its product and version")
            _unique(step.predecessors, "route predecessor")
            if any(
                p not in steps or steps[p].product_id != step.product_id for p in step.predecessors
            ):
                reject(
                    ErrorCode.INVALID_REFERENCE, "Dependencies must exist in the same product route"
                )
        for product in self.products:
            route = tuple(s for s in self.routes if s.product_id == product.product_id)
            if not route:
                reject(ErrorCode.INVALID_REFERENCE, "Product has no routing steps")
            topological_route(route)
            _unique(tuple(s.operation_code for s in route), "product operation code")
            if sum(not s.predecessors for s in route) != 1:
                reject(ErrorCode.UNSUPPORTED_CAPABILITY, "Full-kit routing requires one entry step")
            if not any(b.product_id == product.product_id for b in self.bom):
                reject(ErrorCode.INVALID_REFERENCE, "Product has no material requirements")
        for item in self.bom:
            consumption_step = steps.get(item.consume_step_id)
            if (
                item.material_id not in materials
                or consumption_step is None
                or consumption_step.product_id != item.product_id
            ):
                reject(
                    ErrorCode.INVALID_REFERENCE,
                    "BOM references must match a material and product step",
                )
        return self


def topological_route(steps: tuple[RouteStep, ...]) -> tuple[RouteStep, ...]:
    pending = {s.step_id: s for s in steps}
    resolved: list[RouteStep] = []
    completed: set[str] = set()
    while pending:
        ready = sorted(k for k, s in pending.items() if set(s.predecessors) <= completed)
        if not ready:
            reject(ErrorCode.CYCLIC_ROUTE, "Routing has a cycle or unresolved dependency")
        for key in ready:
            resolved.append(pending.pop(key))
            completed.add(key)
    return tuple(resolved)


def _calendar(windows: tuple[CalendarWindow, ...]) -> None:
    ordered = sorted(windows, key=lambda w: w.start_at)
    if not ordered or any(a.end_at > b.start_at for a, b in zip(ordered, ordered[1:])):
        reject(ErrorCode.INVALID_TIME, "Availability calendars must be nonempty and nonoverlapping")


class Resource(Contract):
    resource_id: Identifier
    resource_type: Identifier
    operation_codes: tuple[Identifier, ...]
    capacity: Unary = 1
    status: Literal["AVAILABLE", "MAINTENANCE", "DOWN", "UNKNOWN"]
    calendar: tuple[CalendarWindow, ...]
    unavailable: tuple[TimeWindow, ...] = ()
    version: Positive = 1
    last_operation_id: Identifier | None = None
    last_product_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_resource(self) -> Self:
        if (self.last_operation_id is None) != (self.last_product_id is None):
            reject(ErrorCode.SOURCE_INCOMPLETE, "Equipment setup state needs operation and product")
        _calendar(self.calendar)
        if not self.operation_codes:
            reject(ErrorCode.INVALID_INPUT, "Resource must declare operation qualifications")
        _unique(self.operation_codes, "resource operation")
        return self


class Worker(Contract):
    worker_id: Identifier
    skills: tuple[Identifier, ...]
    status: Literal["AVAILABLE", "ABSENT", "UNKNOWN"]
    overtime_available: StrictBool
    calendar: tuple[CalendarWindow, ...]
    unavailable: tuple[TimeWindow, ...] = ()
    version: Positive = 1

    @model_validator(mode="after")
    def validate_worker(self) -> Self:
        _calendar(self.calendar)
        if not self.skills:
            reject(ErrorCode.INVALID_INPUT, "Worker must declare skills")
        _unique(self.skills, "worker skill")
        return self


class Order(Contract):
    order_id: Identifier
    product_id: Identifier
    # Total demand in this split revision; progress never reduces it or regenerates batch identities.
    quantity: NonNegative
    due_at: Timestamp
    requested_due_at: Timestamp | None = Field(default=None, exclude_if=lambda value: value is None)
    priority_weight: Positive
    hard_deadline: StrictBool
    version: Positive
    split_revision: Positive = 1
    status: Literal["CONFIRMED", "IN_PROGRESS", "COMPLETED", "CANCELLED"] = "CONFIRMED"

    @model_validator(mode="after")
    def zero_demand_is_cancelled(self) -> Self:
        if self.quantity == 0 and self.status != "CANCELLED":
            reject(ErrorCode.INVALID_INPUT, "Zero demand requires explicit cancellation")
        return self


class Inventory(Contract):
    material_id: Identifier
    unit: Unit
    on_hand: NonNegative
    reserved: NonNegative
    version: Positive = 1

    @model_validator(mode="after")
    def reservation_within_stock(self) -> Self:
        if self.reserved > self.on_hand:
            reject(ErrorCode.INVENTORY_CONFLICT, "Reserved quantity is included in on_hand")
        return self


class Receipt(Contract):
    receipt_id: Identifier
    material_id: Identifier
    unit: Unit
    quantity: Positive
    eta: Timestamp
    status: Literal["EXPECTED", "CONFIRMED", "RECEIVED", "CANCELLED"]
    received_at: Timestamp | None = None
    version: Positive = 1

    @model_validator(mode="after")
    def actual_receipt(self) -> Self:
        if (self.status == "RECEIVED") != (self.received_at is not None):
            reject(
                ErrorCode.INVALID_INPUT, "Only received supplies have an actual receipt timestamp"
            )
        return self


class Consumption(Contract):
    material_id: Identifier
    quantity: NonNegative
    unit: Unit
    event_id: Identifier


class Reservation(Contract):
    reservation_id: Identifier
    batch_id: Identifier
    material_id: Identifier
    quantity: NonNegative
    unit: Unit
    plan_version: Identifier
    source_event_id: Identifier
    created_at: Timestamp


class ExecutionSegment(TimeWindow):
    phase: Literal["SETUP", "PRODUCTION"]
    source_event_id: Identifier


class ActualExecution(Contract):
    operation_id: Identifier
    batch_id: Identifier
    route_version: Identifier
    state: Literal["SETUP", "IN_PROGRESS", "COMPLETED", "BLOCKED"]
    actual_start: Timestamp | None
    actual_end: Timestamp | None = None
    resource_id: Identifier
    worker_id: Identifier
    completed_quantity: NonNegative
    consumed: tuple[Consumption, ...] = ()
    quality_state: Literal["PENDING", "PASSED", "FAILED", "UNKNOWN"] = "UNKNOWN"
    remaining_minutes: NonNegative | None = None
    remaining_confirmed_by: Identifier | None = None
    version: Positive
    changeover_start: Timestamp | None = None
    segments: tuple[ExecutionSegment, ...] = ()
    remaining_setup_minutes: NonNegative | None = None

    @model_validator(mode="after")
    def actual_history(self) -> Self:
        if (self.state == "COMPLETED") != (self.actual_end is not None):
            reject(ErrorCode.INVALID_INPUT, "Only completed execution has an actual end")
        if self.state in ("IN_PROGRESS", "COMPLETED") and self.actual_start is None:
            reject(ErrorCode.SOURCE_INCOMPLETE, "Production requires an actual start")
        if self.state == "SETUP" and self.actual_start is not None:
            reject(ErrorCode.INVALID_INPUT, "Initial setup cannot claim production has started")
        if self.actual_end is not None and (
            self.actual_start is None or self.actual_end <= self.actual_start
        ):
            reject(ErrorCode.INVALID_TIME, "Actual end must follow actual start")
        if self.actual_start is None and (self.consumed or self.completed_quantity):
            reject(ErrorCode.INVENTORY_CONFLICT, "Setup cannot consume production materials")
        if self.changeover_start is not None and self.actual_start is not None:
            if self.changeover_start > self.actual_start:
                reject(ErrorCode.INVALID_TIME, "Actual setup follows production start")
        ordered = sorted(self.segments, key=lambda s: s.start_at)
        _unique(tuple(s.source_event_id for s in self.segments), "execution segment event")
        if list(self.segments) != ordered or any(
            a.end_at > b.start_at for a, b in zip(ordered, ordered[1:])
        ):
            reject(ErrorCode.INVALID_TIME, "Execution segments must retain nonoverlapping history")
        for segment in self.segments:
            if self.changeover_start is None or segment.start_at < self.changeover_start:
                reject(ErrorCode.INVALID_TIME, "Execution precedes its actual setup start")
            if segment.phase == "PRODUCTION" and (
                self.actual_start is None or segment.start_at < self.actual_start
            ):
                reject(ErrorCode.INVALID_TIME, "Production segment precedes actual start")
            if self.actual_end is not None and segment.end_at > self.actual_end:
                reject(ErrorCode.INVALID_TIME, "Execution follows actual completion")
        if (self.remaining_minutes is None) != (self.remaining_confirmed_by is None):
            reject(
                ErrorCode.CONFIRMATION_REQUIRED,
                "Known remaining work requires its confirmation source",
            )
        _unique(tuple(c.event_id for c in self.consumed), "consumption event")
        return self


class SourceEnvelope(Contract):
    source_system: Identifier
    source_revision: Identifier
    cursor: Identifier | None = None
    observed_at: Timestamp
    effective_at: Timestamp
    complete: StrictBool
    consistency: Literal["ATOMIC_SNAPSHOT", "VERIFIED_WATERMARK", "UNVERIFIED"]
    freshness: Literal["CURRENT", "STALE", "UNKNOWN"]
    ownership: Literal["enterprise_fact", "simulator_fact"]
    evidence_digest: Digest


class ConnectorCapabilities(Contract):
    schema_version: Literal["byof.connector-capabilities/1"] = "byof.connector-capabilities/1"
    read_snapshot: StrictBool
    read_changes: StrictBool
    query_detail: StrictBool
    accept_plan: StrictBool
    query_action: StrictBool
    idempotency: StrictBool
    conditional_acceptance: StrictBool
    snapshot_consistency: Literal["ATOMIC_SNAPSHOT", "VERIFIED_WATERMARK", "UNVERIFIED"]

    @model_validator(mode="after")
    def consistent_capabilities(self) -> Self:
        if self.conditional_acceptance and not self.accept_plan:
            reject(ErrorCode.INVALID_INPUT, "Conditional acceptance requires a plan acceptance API")
        if not self.read_snapshot and self.snapshot_consistency == "ATOMIC_SNAPSHOT":
            reject(ErrorCode.INVALID_INPUT, "Atomic snapshot capability requires snapshot reading")
        return self


class EndpointBinding(Contract):
    operation: Literal[
        "read_snapshot", "read_changes", "query_detail", "accept_plan", "query_action"
    ]
    endpoint_id: Identifier


class FieldMapping(Contract):
    entity: Literal["order", "inventory", "receipt", "resource", "worker"]
    source_field: Annotated[StrictStr, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.]*$")]
    target_field: Identifier

    @model_validator(mode="after")
    def allowed_target(self) -> Self:
        entity_types: dict[str, type[Contract]] = {
            "order": Order,
            "inventory": Inventory,
            "receipt": Receipt,
            "resource": Resource,
            "worker": Worker,
        }
        if self.target_field not in entity_types[self.entity].model_fields:
            reject(ErrorCode.INVALID_REFERENCE, "Unknown standard target field")
        return self


class UnitMapping(Contract):
    source_unit: Identifier
    target_unit: Unit
    multiplier_numerator: Positive = 1
    multiplier_denominator: Positive = 1


class StatusMapping(Contract):
    entity: Literal["order", "receipt", "resource", "worker"]
    source_status: Identifier
    target_status: Identifier

    @model_validator(mode="after")
    def allowed_status(self) -> Self:
        statuses = {
            "order": {"CONFIRMED", "IN_PROGRESS", "COMPLETED", "CANCELLED"},
            "receipt": {"EXPECTED", "CONFIRMED", "RECEIVED", "CANCELLED"},
            "resource": {"AVAILABLE", "MAINTENANCE", "DOWN", "UNKNOWN"},
            "worker": {"AVAILABLE", "ABSENT", "UNKNOWN"},
        }
        if self.target_status not in statuses[self.entity]:
            reject(ErrorCode.INVALID_INPUT, "Unknown target status cannot imply availability")
        return self


class ConnectorMapping(Contract):
    mapping_id: Identifier
    version: Positive
    factory_id: Identifier
    source_id: Identifier
    origin_id: Identifier
    endpoint_bindings: tuple[EndpointBinding, ...]
    fields: tuple[FieldMapping, ...]
    units: tuple[UnitMapping, ...]
    statuses: tuple[StatusMapping, ...]
    inventory_reserved_semantics: Literal["included_in_on_hand"]

    @model_validator(mode="after")
    def mapping_is_explicit(self) -> Self:
        if not self.endpoint_bindings or not self.fields or not self.units:
            reject(
                ErrorCode.SOURCE_INCOMPLETE, "Mapping needs endpoints, fields and explicit units"
            )
        if ":" in self.origin_id or "/" in self.origin_id:
            reject(
                ErrorCode.INVALID_INPUT, "Origin must identify a trusted registry entry, not a URL"
            )
        for endpoint in self.endpoint_bindings:
            if ":" in endpoint.endpoint_id or "/" in endpoint.endpoint_id:
                reject(ErrorCode.INVALID_INPUT, "Endpoint must identify a trusted registry entry")
        _unique(tuple(e.operation for e in self.endpoint_bindings), "connector operation")
        _unique(tuple(f"{f.entity}/{f.target_field}" for f in self.fields), "mapping target")
        _unique(tuple(u.source_unit for u in self.units), "source unit")
        _unique(tuple(f"{s.entity}/{s.source_status}" for s in self.statuses), "source status")
        return self


class EvidenceRecord(Contract):
    evidence_id: Identifier
    source_id: Identifier
    document_version: Identifier
    content_digest: Digest
    classification: Literal["PUBLIC_REFERENCE", "SYNTHETIC", "ENTERPRISE_CONFIRMED", "UNKNOWN"]
    supports_fields: tuple[Identifier, ...]
    limitations: Text


class EvidenceBundle(Contract):
    bundle_id: Identifier
    factory_id: Identifier
    version: Positive
    records: tuple[EvidenceRecord, ...]
    unresolved_fields: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def evidence_is_identified(self) -> Self:
        if not self.records:
            reject(ErrorCode.SOURCE_INCOMPLETE, "Factory evidence must identify its sources")
        _unique(tuple(r.evidence_id for r in self.records), "evidence record")
        return self


class ProductionBatch(Contract):
    """Persistent production identity; cancelled lots are permanent tombstones."""

    batch_id: Identifier
    order_id: Identifier
    product_id: Identifier
    route_version: Identifier
    quantity: Positive
    sequence: Positive
    purpose: Literal["CUSTOMER", "STOCK", "CANCELLED", "SCRAP"]
    source_event_id: Identifier
    changed_order_version: Positive
    delivery_due_at: Timestamp | None = Field(default=None, exclude_if=lambda value: value is None)


class FinishedGoodsLot(Contract):
    batch_id: Identifier
    product_id: Identifier
    quantity: Positive
    completed_at: Timestamp


class Snapshot(Contract):
    schema_version: Literal["byof.snapshot/1", "byof.snapshot/2", "byof.snapshot/3"] = (
        "byof.snapshot/1"
    )
    snapshot_id: Identifier
    factory_id: Identifier
    run_id: Identifier
    snapshot_clock: Timestamp
    horizon: TimeWindow
    source: SourceEnvelope
    planning_revision: Positive
    scope_version: Positive
    active_plan_version: Identifier | None
    active_plan_hash: Digest | None = None
    profile: FactoryProfile
    orders: tuple[Order, ...]
    inventory: tuple[Inventory, ...]
    receipts: tuple[Receipt, ...]
    resources: tuple[Resource, ...]
    workers: tuple[Worker, ...]
    actuals: tuple[ActualExecution, ...] = ()
    reservations: tuple[Reservation, ...] = ()
    production_batches: tuple[ProductionBatch, ...] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    business_terms: BusinessTerms | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    content_hash: Digest | None = None

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if self.schema_version != "byof.snapshot/3" and (
            self.production_batches is not None
            or self.business_terms is not None
            or any(order.quantity == 0 for order in self.orders)
            or any(order.requested_due_at is not None for order in self.orders)
        ):
            reject(
                ErrorCode.VERSION_MISMATCH,
                "Demand ledgers and business terms require snapshot version 3",
            )
        if self.factory_id != self.profile.factory_id:
            reject(
                ErrorCode.INVALID_REFERENCE, "Snapshot and profile belong to different factories"
            )
        for items, key in (
            (self.orders, "order_id"),
            (self.inventory, "material_id"),
            (self.receipts, "receipt_id"),
            (self.resources, "resource_id"),
            (self.workers, "worker_id"),
            (self.actuals, "operation_id"),
            (self.reservations, "reservation_id"),
        ):
            _unique(tuple(getattr(item, key) for item in items), key)
        products = {p.product_id: p for p in self.profile.products}
        materials = {m.material_id: m for m in self.profile.materials}
        for order in self.orders:
            product = products.get(order.product_id)
            if product is None:
                reject(ErrorCode.INVALID_REFERENCE, "Order references an unknown product")
            if order.quantity % product.batch_size:
                reject(
                    ErrorCode.UNSUPPORTED_BATCH_QUANTITY,
                    "Order cannot be exactly split into configured lots",
                )
            if self.schema_version == "byof.snapshot/3" and (
                (order.status == "CANCELLED") != (order.quantity == 0)
            ):
                reject(ErrorCode.INVALID_INPUT, "Cancelled demand must have zero customer quantity")
        if self.production_batches is not None:
            self.validate_production_batches(products)
        elif any(order.quantity == 0 for order in self.orders):
            reject(
                ErrorCode.SOURCE_INCOMPLETE, "Cancelled demand needs its persistent batch ledger"
            )
        supplies: tuple[Inventory | Receipt, ...] = (*self.inventory, *self.receipts)
        for item in supplies:
            material = materials.get(item.material_id)
            if material is None or item.unit != material.unit:
                reject(ErrorCode.INVALID_REFERENCE, "Supply material/unit differs from its profile")
        if {i.material_id for i in self.inventory} != set(materials):
            reject(ErrorCode.SOURCE_INCOMPLETE, "Each material needs an explicit inventory balance")
        for step in self.profile.routes:
            if not any(
                r.resource_type == step.resource_type and step.operation_code in r.operation_codes
                for r in self.resources
            ) or not any(step.skill in w.skills for w in self.workers):
                reject(ErrorCode.INVALID_REFERENCE, "Route lacks a qualified resource or worker")
        resource_ids = {r.resource_id for r in self.resources}
        worker_ids = {w.worker_id for w in self.workers}
        batches, operations = batch_operations(self, include_cancelled=True)
        batch_by_id = {b.batch_id: b for b in batches}
        operation_by_id = {o.operation_id: o for o in operations}
        for actual in self.actuals:
            if actual.resource_id not in resource_ids or actual.worker_id not in worker_ids:
                reject(ErrorCode.INVALID_REFERENCE, "Actual execution references missing resources")
            operation = operation_by_id.get(actual.operation_id)
            batch = batch_by_id.get(actual.batch_id)
            if operation is None or batch is None or operation.batch_id != actual.batch_id:
                reject(
                    ErrorCode.INVALID_REFERENCE,
                    "Actual execution must reference a known batch operation",
                )
            if (
                actual.route_version != batch.route_version
                or actual.completed_quantity > batch.quantity
            ):
                reject(
                    ErrorCode.INVALID_INPUT,
                    "Actual work must retain its route version and batch quantity",
                )
            if (actual.actual_start is not None and actual.actual_start > self.snapshot_clock) or (
                actual.actual_end is not None and actual.actual_end > self.snapshot_clock
            ):
                reject(
                    ErrorCode.INVALID_TIME,
                    "Actual production cannot be in the business clock future",
                )
            if any(s.end_at > self.snapshot_clock for s in actual.segments) or (
                actual.changeover_start is not None
                and actual.changeover_start > self.snapshot_clock
            ):
                reject(ErrorCode.INVALID_TIME, "Actual execution interval is in the future")
            for consumed in actual.consumed:
                if (
                    consumed.material_id not in materials
                    or consumed.unit != materials[consumed.material_id].unit
                ):
                    reject(
                        ErrorCode.INVALID_REFERENCE, "Actual consumption unit/reference mismatch"
                    )
        if self.schema_version in {"byof.snapshot/2", "byof.snapshot/3"}:
            self.validate_execution_ledger(batch_by_id, operation_by_id)
        digest = versioned_digest(self.model_dump(mode="json", exclude={"content_hash"}))
        if self.content_hash is not None and self.content_hash != digest:
            reject(ErrorCode.HASH_MISMATCH, "Snapshot content differs from its recorded hash")
        object.__setattr__(self, "content_hash", digest)
        return self

    def validate_production_batches(self, products: dict[str, Product]) -> None:
        assert self.production_batches is not None
        _unique(tuple(batch.batch_id for batch in self.production_batches), "production batch")
        orders = {order.order_id: order for order in self.orders}
        started = {actual.batch_id for actual in self.actuals}
        customer_quantity: dict[str, int] = {}
        for batch in self.production_batches:
            order = orders.get(batch.order_id)
            if order is None or batch.product_id != order.product_id:
                reject(
                    ErrorCode.INVALID_REFERENCE, "Production batch needs its original order/product"
                )
            product = products[batch.product_id]
            expected_id = f"{order.order_id}-R{order.split_revision:03d}-B{batch.sequence:03d}"
            if (
                batch.batch_id != expected_id
                or batch.route_version != product.route_version
                or batch.quantity != product.batch_size
                or batch.changed_order_version > order.version
            ):
                reject(
                    ErrorCode.INVALID_INPUT,
                    "Batch identity, original route, lot size or audit version differs",
                )
            if batch.purpose == "CANCELLED" and batch.batch_id in started:
                reject(
                    ErrorCode.INVALID_INPUT,
                    "Executed batches, including setup, cannot be cancelled",
                )
            if batch.purpose == "STOCK" and batch.batch_id not in started:
                reject(
                    ErrorCode.INVALID_INPUT, "Only committed production can become surplus stock"
                )
            if batch.purpose == "SCRAP" and not any(
                actual.batch_id == batch.batch_id and actual.quality_state == "FAILED"
                for actual in self.actuals
            ):
                reject(
                    ErrorCode.INVALID_INPUT, "Scrapped production needs failed inspection evidence"
                )
            if batch.purpose == "CUSTOMER":
                customer_quantity[batch.order_id] = (
                    customer_quantity.get(batch.order_id, 0) + batch.quantity
                )
        for order in self.orders:
            if customer_quantity.get(order.order_id, 0) != order.quantity:
                reject(
                    ErrorCode.INVALID_INPUT, "Customer batch quantities must equal current demand"
                )

    def validate_execution_ledger(
        self, batches: dict[str, Batch], operations: dict[str, Operation]
    ) -> None:
        if (self.active_plan_version is None) != (self.active_plan_hash is None):
            reject(ErrorCode.SOURCE_INCOMPLETE, "Active execution version needs its candidate hash")
        steps = {s.step_id: s for s in self.profile.routes}
        inventory = {i.material_id: i for i in self.inventory}
        reservations = {(r.batch_id, r.material_id): r for r in self.reservations}
        if len(reservations) != len(self.reservations):
            reject(ErrorCode.DUPLICATE_ID, "Batch material has multiple reservations")
        used: dict[tuple[str, str], int] = {}
        started = {a.batch_id for a in self.actuals if a.actual_start is not None}
        event_ids: list[str] = []
        segment_ids: list[str] = []
        for actual in self.actuals:
            segment_ids.extend(s.source_event_id for s in actual.segments)
            if actual.changeover_start is None:
                reject(ErrorCode.SOURCE_INCOMPLETE, "Execution needs its real setup start")
            batch = batches[actual.batch_id]
            if actual.state == "COMPLETED" and actual.completed_quantity != batch.quantity:
                reject(
                    ErrorCode.INVALID_INPUT, "Completed operation must account for its whole batch"
                )
            step = steps[operations[actual.operation_id].step_id]
            expected = (
                {
                    b.material_id: b.quantity_per_unit * batch.quantity
                    for b in self.profile.bom
                    if b.product_id == batch.product_id and b.consume_step_id == step.step_id
                }
                if actual.actual_start is not None
                else {}
            )
            consumed = {c.material_id: c.quantity for c in actual.consumed}
            if len(consumed) != len(actual.consumed) or consumed != expected:
                reject(
                    ErrorCode.INVENTORY_CONFLICT,
                    "Started operation must consume exactly its BOM share once",
                )
            for consumption in actual.consumed:
                event_ids.append(consumption.event_id)
                key = actual.batch_id, consumption.material_id
                used[key] = used.get(key, 0) + consumption.quantity
        _unique(tuple(event_ids), "global consumption event")
        _unique(tuple(segment_ids), "global execution segment event")
        for reservation in self.reservations:
            reserved_batch = batches.get(reservation.batch_id)
            stock = inventory.get(reservation.material_id)
            if (
                reserved_batch is None
                or stock is None
                or reservation.unit != stock.unit
                or reservation.batch_id not in started
                or reservation.created_at > self.snapshot_clock
            ):
                reject(
                    ErrorCode.INVALID_REFERENCE, "Reservation requires a started batch and material"
                )
            if not any(
                b.product_id == reserved_batch.product_id
                and b.material_id == reservation.material_id
                for b in self.profile.bom
            ):
                reject(ErrorCode.INVALID_REFERENCE, "Reservation is outside its batch BOM")
        for batch_id in started:
            batch = batches[batch_id]
            for bom in self.profile.bom:
                if bom.product_id != batch.product_id:
                    continue
                key = batch_id, bom.material_id
                remaining_reservation = reservations.get(key)
                if (
                    remaining_reservation is None
                    or remaining_reservation.quantity + used.get(key, 0)
                    != bom.quantity_per_unit * batch.quantity
                ):
                    reject(
                        ErrorCode.INVENTORY_CONFLICT,
                        "Consumed plus remaining reservation must equal original batch BOM",
                    )
        for stock in self.inventory:
            owned = sum(r.quantity for r in self.reservations if r.material_id == stock.material_id)
            if owned > stock.reserved:
                reject(
                    ErrorCode.INVENTORY_CONFLICT,
                    "Batch reservations exceed reserved physical stock",
                )


class Batch(Contract):
    batch_id: Identifier
    order_id: Identifier
    product_id: Identifier
    route_version: Identifier
    quantity: Positive


class Operation(Contract):
    operation_id: Identifier
    batch_id: Identifier
    step_id: Identifier


def batch_operations(
    snapshot: Snapshot, *, include_cancelled: bool = False
) -> tuple[tuple[Batch, ...], tuple[Operation, ...]]:
    """Identity-only expansion; constraint construction belongs independently to solver/checker."""
    batches: list[Batch] = []
    operations: list[Operation] = []
    if snapshot.production_batches is not None:
        for record in snapshot.production_batches:
            if record.purpose in {"CANCELLED", "SCRAP"} and not include_cancelled:
                continue
            batches.append(
                Batch(
                    **record.model_dump(
                        include={"batch_id", "order_id", "product_id", "route_version", "quantity"}
                    )
                )
            )
            route = tuple(s for s in snapshot.profile.routes if s.product_id == record.product_id)
            for step in topological_route(route):
                operations.append(
                    Operation(
                        operation_id=f"{record.batch_id}-{step.operation_code}",
                        batch_id=record.batch_id,
                        step_id=step.step_id,
                    )
                )
        return tuple(batches), tuple(operations)
    products = {p.product_id: p for p in snapshot.profile.products}
    for order in snapshot.orders:
        product = products[order.product_id]
        for index in range(order.quantity // product.batch_size):
            batch_id = f"{order.order_id}-R{order.split_revision:03d}-B{index + 1:03d}"
            batches.append(
                Batch(
                    batch_id=batch_id,
                    order_id=order.order_id,
                    product_id=product.product_id,
                    route_version=product.route_version,
                    quantity=product.batch_size,
                )
            )
            route = tuple(s for s in snapshot.profile.routes if s.product_id == product.product_id)
            for step in topological_route(route):
                operations.append(
                    Operation(
                        operation_id=f"{batch_id}-{step.operation_code}",
                        batch_id=batch_id,
                        step_id=step.step_id,
                    )
                )
    return tuple(batches), tuple(operations)


class VersionBinding(Contract):
    snapshot_hash: Digest
    planning_revision: Positive
    scope_version: Positive
    profile_version: Identifier
    policy_version: Identifier
    objective_version: Identifier
    baseline_plan_version: Identifier | None


class Assignment(TimeWindow):
    operation_id: Identifier
    resource_id: Identifier
    worker_id: Identifier
    changeover_start: Timestamp
    resume_at: Timestamp | None = None
    resume_changeover_start: Timestamp | None = None

    @model_validator(mode="after")
    def changeover_order(self) -> Self:
        if self.changeover_start > self.start_at:
            reject(ErrorCode.INVALID_TIME, "Changeover must immediately precede operation start")
        if (self.resume_at is None) != (self.resume_changeover_start is None):
            reject(
                ErrorCode.SOURCE_INCOMPLETE, "Continuation needs both future setup and work starts"
            )
        if self.resume_at is not None and self.resume_changeover_start is not None:
            if self.resume_at < self.start_at:
                reject(ErrorCode.INVALID_TIME, "Continuation cannot precede original production")
            if not (
                self.changeover_start
                <= self.resume_changeover_start
                <= self.resume_at
                < self.end_at
            ):
                reject(
                    ErrorCode.INVALID_TIME,
                    "Continuation falls outside execution history and completion",
                )
        return self


class Metric(Contract):
    name: Identifier
    value: StrictInt | None
    unit: Identifier
    lower_bound: StrictInt | None = None
    unknown_reason: Text | None = None

    @model_validator(mode="after")
    def unknown_metric(self) -> Self:
        if self.value is None and self.unknown_reason is None:
            reject(ErrorCode.INVALID_INPUT, "Unknown metrics must explain the missing input")
        return self


class EvidenceIssue(Contract):
    code: Identifier
    object_id: Identifier | None = None
    message: Text


class CheckReport(Contract):
    checker_version: Identifier
    snapshot_hash: Digest
    status: Literal["PASS", "FAIL", "NOT_RUN"]
    issues: tuple[EvidenceIssue, ...] = ()

    @model_validator(mode="after")
    def report_evidence(self) -> Self:
        if self.status == "PASS" and self.issues:
            reject(ErrorCode.INVALID_INPUT, "A passing checker report cannot retain violations")
        if self.status == "FAIL" and not self.issues:
            reject(ErrorCode.INVALID_INPUT, "A failed checker report requires evidence")
        return self


class ScenarioFact(Contract):
    field: Identifier
    value: StrictStr | StrictInt | StrictBool | None
    reason: Text
    confirmation_id: Identifier | None = None


class SolverPass(Contract):
    objective_name: Identifier
    native_status: NativeStatus
    has_solution: StrictBool
    objective_value: StrictInt | None
    best_bound: Annotated[float, Field(allow_inf_nan=False)] | None
    wall_time_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)]

    @model_validator(mode="after")
    def native_result(self) -> Self:
        if self.has_solution != (self.native_status in ("OPTIMAL", "FEASIBLE")):
            reject(ErrorCode.INVALID_INPUT, "Search status and solution presence disagree")
        if (self.objective_value is not None) != self.has_solution:
            reject(ErrorCode.INVALID_INPUT, "Only a feasible search has an incumbent objective")
        if self.objective_value is not None and self.best_bound is not None:
            if self.best_bound > self.objective_value:
                reject(ErrorCode.INVALID_INPUT, "Minimization lower bound exceeds its incumbent")
        return self


class Candidate(Contract):
    schema_version: Literal["byof.candidate/1", "byof.candidate/2", "byof.candidate/3"] = (
        "byof.candidate/2"
    )
    candidate_id: Identifier
    factory_id: Identifier
    version: Positive
    binding: VersionBinding
    native_status: NativeStatus
    solver_passes: tuple[SolverPass, ...] = ()
    last_search_status: NativeStatus | None = None
    constant_objective_levels: tuple[Identifier, ...] = ()
    has_solution: StrictBool
    termination_reason: Literal[
        "COMPLETED", "TIME_LIMIT", "CANCELLED", "WORKER_FAILURE", "MODEL_ERROR"
    ]
    objective: tuple[Metric, ...] = ()
    proven_objective_levels: NonNegative = 0
    assignments: tuple[Assignment, ...] = ()
    scenario: tuple[ScenarioFact, ...] = ()
    required_consents: tuple[Identifier, ...] = ()
    checker: CheckReport
    effective_not_before: Timestamp
    accept_before: Timestamp
    new_actions_not_before: Timestamp | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    empty_demand: StrictBool = Field(default=False, exclude_if=lambda value: value is False)
    content_hash: Digest | None = None

    @model_validator(mode="after")
    def candidate_evidence(self) -> Self:
        if self.schema_version == "byof.candidate/1":
            versioned_digest(self.model_dump(mode="json", exclude={"content_hash"}))
        if self.has_solution != (self.native_status in ("OPTIMAL", "FEASIBLE")):
            reject(ErrorCode.INVALID_INPUT, "Native status and solution presence disagree")
        if self.empty_demand and (
            self.schema_version != "byof.candidate/3" or self.assignments or not self.has_solution
        ):
            reject(
                ErrorCode.INVALID_INPUT,
                "Empty demand requires a version 3 solution without operations",
            )
        if bool(self.assignments) != self.has_solution and not self.empty_demand:
            reject(ErrorCode.INVALID_INPUT, "Only a solution may contain assigned operations")
        if self.proven_objective_levels > len(self.objective):
            reject(ErrorCode.INVALID_INPUT, "Proof cannot exceed the objective vector")
        if self.checker.snapshot_hash != self.binding.snapshot_hash:
            reject(ErrorCode.HASH_MISMATCH, "Checker and candidate refer to different snapshots")
        if self.accept_before <= self.effective_not_before:
            reject(ErrorCode.INVALID_TIME, "Candidate acceptance window must be nonempty")
        if self.new_actions_not_before is not None and (
            self.new_actions_not_before < self.effective_not_before
            or self.new_actions_not_before.second
            or self.new_actions_not_before.microsecond
        ):
            reject(ErrorCode.INVALID_TIME, "New actions require an explicit future business minute")
        _unique(tuple(a.operation_id for a in self.assignments), "candidate operation")
        if self.solver_passes:
            if self.last_search_status != self.solver_passes[-1].native_status:
                reject(
                    ErrorCode.INVALID_INPUT, "Final search status differs from the recorded pass"
                )
            retained = [p for p in self.solver_passes if p.has_solution]
            if bool(retained) != self.has_solution:
                reject(
                    ErrorCode.INVALID_INPUT, "Recorded searches do not support solution presence"
                )
            if retained and retained[-1].native_status != self.native_status:
                reject(
                    ErrorCode.INVALID_INPUT, "Candidate status must describe its retained solution"
                )
            proven = self.objective[: self.proven_objective_levels]
            if any(m.value is None or m.lower_bound != m.value for m in proven):
                reject(ErrorCode.INVALID_INPUT, "Proven objectives need matching exact bounds")
            proof_names = {m.name for m in proven}
            recorded_proofs = {
                p.objective_name for p in self.solver_passes if p.native_status == "OPTIMAL"
            }
            if not proof_names <= recorded_proofs | set(self.constant_objective_levels):
                reject(
                    ErrorCode.INVALID_INPUT,
                    "Proof requires a recorded optimal search or constant objective",
                )
            if any(
                p.objective_name in proof_names and p.native_status != "OPTIMAL"
                for p in self.solver_passes
            ):
                reject(
                    ErrorCode.INVALID_INPUT, "A search without proof cannot certify its objective"
                )
        digest = versioned_digest(self.model_dump(mode="json", exclude={"content_hash"}))
        if self.content_hash is not None and digest != self.content_hash:
            reject(ErrorCode.HASH_MISMATCH, "Candidate was changed after hashing")
        object.__setattr__(self, "content_hash", digest)
        return self


class Approval(Contract):
    approval_id: Identifier
    factory_id: Identifier
    candidate_hash: Digest
    binding: VersionBinding
    approver_id: Identifier
    approver_role: Literal["planner", "manager"]
    action_scope: Literal["publish_plan", "allow_overtime"]
    decision: Literal["APPROVED", "REJECTED"]
    decided_at: Timestamp
    expires_at: Timestamp
    clock: Literal["real"] = "real"

    @model_validator(mode="after")
    def valid_approval(self) -> Self:
        if self.expires_at <= self.decided_at:
            reject(ErrorCode.INVALID_TIME, "Approval expiry uses real time after the decision")
        if (self.action_scope == "allow_overtime") != (self.approver_role == "manager"):
            reject(ErrorCode.INVALID_INPUT, "Approval scope does not match the required role")
        return self


class Release(Contract):
    release_id: Identifier
    factory_id: Identifier
    operation_id: Identifier
    candidate_hash: Digest
    payload_hash: Digest
    approval_ids: tuple[Identifier, ...]
    expected_source_revision: Identifier
    expected_active_plan_version: Identifier | None
    local_state: Literal["LOCAL_COMMITTED"]
    source_state: Literal[
        "PENDING_SOURCE", "UNKNOWN", "ACCEPTED_PENDING_EFFECTIVE", "ACTIVE", "REJECTED"
    ]
    execution_state: Literal["NOT_STARTED", "IN_PROGRESS", "COMPLETED", "BLOCKED"] = "NOT_STARTED"
    source_receipt_id: Identifier | None = None
    committed_at: Timestamp
    effective_at: Timestamp | None = None

    @model_validator(mode="after")
    def release_receipts(self) -> Self:
        if not self.approval_ids:
            reject(
                ErrorCode.CONFIRMATION_REQUIRED,
                "Release requires recorded service-validated approvals",
            )
        if (
            self.source_state in ("ACCEPTED_PENDING_EFFECTIVE", "ACTIVE")
            and self.source_receipt_id is None
        ):
            reject(ErrorCode.INVALID_INPUT, "Source acceptance requires a receipt")
        if (self.source_state == "ACTIVE") != (self.effective_at is not None):
            reject(
                ErrorCode.INVALID_INPUT, "Only an effective source plan has an effective timestamp"
            )
        if self.execution_state != "NOT_STARTED" and self.source_state != "ACTIVE":
            reject(ErrorCode.INVALID_INPUT, "Execution progress requires an effective source plan")
        return self


class FieldChange(Contract):
    field: Identifier
    before: StrictStr | StrictInt | StrictBool | None
    after: StrictStr | StrictInt | StrictBool | None


class Event(Contract):
    schema_version: Literal["byof.event/1"] = "byof.event/1"
    event_id: Identifier
    factory_id: Identifier
    run_id: Identifier
    source_event_id: Identifier
    source_revision: Identifier
    entity_type: Identifier
    entity_id: Identifier
    entity_version: Positive
    event_type: Identifier
    occurred_at: Timestamp
    observed_at: Timestamp
    effective_at: Timestamp
    changes: tuple[FieldChange, ...]
    corrects_event_id: Identifier | None = None
    verification_state: Literal["PENDING_SERVER_CHECK"] = "PENDING_SERVER_CHECK"


class HumanTask(Contract):
    human_task_id: Identifier
    factory_id: Identifier
    case_id: Identifier
    version: Positive
    owner_id: Identifier
    owner_role: Identifier
    question: Text
    due_at: Timestamp
    clock: Literal["real"] = "real"
    send_state: Literal["NOT_ENABLED", "QUEUED", "PROVIDER_ACCEPTED", "FAILED", "UNKNOWN"] = (
        "NOT_ENABLED"
    )
    delivery_state: Literal["UNAVAILABLE", "DELIVERED", "BOUNCED", "UNKNOWN"] = "UNAVAILABLE"
    handling_state: Literal[
        "OPEN", "ACKNOWLEDGED", "RESPONDED", "REASSIGNED", "VERIFIED", "RESOLVED"
    ] = "OPEN"
    real_delivery_permitted: Literal[False] = False

    @field_validator("real_delivery_permitted", mode="before")
    @classmethod
    def delivery_flag(cls, value: object) -> object:
        if type(value) is not bool:
            reject(ErrorCode.INVALID_INPUT, "Delivery permission must be a JSON boolean")
        return value


class Preference(Contract):
    preference_id: Identifier
    factory_id: Identifier
    version: Positive
    scope_type: Literal["FACTORY", "PROCESS", "CASE"]
    scope_id: Identifier
    base_policy_version: Identifier
    selection: Literal["delivery_first", "stability_first", "overtime_first", "custom"]
    objective_order: tuple[
        Literal[
            "weighted_tardiness",
            "incremental_overtime_metric",
            "changed_operations",
            "total_start_shift",
            "makespan",
        ],
        ...,
    ]
    overtime_metric: Literal["minutes", "cost"] = "minutes"
    currency_rate: NonNegative | None = None
    state: Literal["PENDING_AUTHORIZED_CONFIRMATION"] = "PENDING_AUTHORIZED_CONFIRMATION"

    @model_validator(mode="after")
    def objective_contract(self) -> Self:
        _unique(self.objective_order, "objective")
        if not self.objective_order:
            reject(ErrorCode.INVALID_INPUT, "Preference needs an objective order")
        if (self.overtime_metric == "cost") != (self.currency_rate is not None):
            reject(
                ErrorCode.INVALID_INPUT,
                "Cost needs a verified rate; minutes must not invent a rate",
            )
        return self


class ToolResult(Contract):
    schema_version: Literal["byof.tool-result/1"] = "byof.tool-result/1"
    operation_id: Identifier
    factory_id: Identifier
    status: Literal["OK", "NEEDS_INPUT", "REJECTED", "UNAVAILABLE"]
    summary: Text
    source: SourceEnvelope | None = None
    snapshot: Snapshot | None = None
    candidate: Candidate | None = None
    issues: tuple[EvidenceIssue, ...] = ()
    retryable: StrictBool = False

    @model_validator(mode="after")
    def result_scope(self) -> Self:
        if any(
            item is not None and item.factory_id != self.factory_id
            for item in (self.snapshot, self.candidate)
        ):
            reject(ErrorCode.INVALID_REFERENCE, "Tool result cannot cross factory boundaries")
        if self.status == "REJECTED" and not self.issues:
            reject(ErrorCode.INVALID_INPUT, "Rejected tools must return actionable error details")
        return self
