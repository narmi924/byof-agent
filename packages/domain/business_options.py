"""Read-only business studies: a checked scenario is never an executable approval."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, StrictInt, model_validator

from packages.domain.economics import OptionEconomics
from packages.domain.models import (
    Candidate,
    Contract,
    Digest,
    Identifier,
    NonNegative,
    Order,
    Positive,
    Snapshot,
    Text,
    Timestamp,
)
from packages.domain.treatment import TreatmentAction


class BusinessStudyRequest(Contract):
    kind: Literal["urgent_order", "material_shortage", "production_exception"]
    subject_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    order: Order | None = None
    existing_order_id: Identifier | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    partial_delivery_allowed: StrictBool = False
    minimum_partial_quantity: Positive | None = None
    final_due_at: Timestamp | None = None
    receipt_id: Identifier | None = None
    expedite_quote_ids: tuple[Identifier, ...] = ()
    total_time_limit: Annotated[StrictInt, Field(ge=1, le=120)] = 30
    economic_priority: (
        Literal["contribution", "cash", "delivery", "stability", "overtime"] | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    max_cash_outlay_minor: NonNegative | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def relevant_fields(self) -> Self:
        if self.kind == "production_exception":
            if (
                self.order is not None
                or self.partial_delivery_allowed
                or self.expedite_quote_ids
                or self.receipt_id
                or self.final_due_at
                or self.minimum_partial_quantity
            ):
                raise ValueError(
                    "Exception treatment uses current facts and the versioned simulation catalogue"
                )
            return self
        if self.total_time_limit > 60:
            raise ValueError("Legacy business studies have a sixty-second search budget")
        if (
            self.subject_id is not None
            or self.economic_priority is not None
            or self.max_cash_outlay_minor is not None
        ):
            raise ValueError("Subject scope belongs to exception treatment")
        if len(set(self.expedite_quote_ids)) != len(self.expedite_quote_ids):
            raise ValueError("Expedite quote identifiers must be unique")
        if len(self.expedite_quote_ids) > 4:
            raise ValueError("A study compares at most four source quotes")
        if self.kind == "urgent_order":
            if (self.order is None) == (self.existing_order_id is None):
                raise ValueError("An urgent-order study needs exactly one order or source order ID")
            if self.order is not None and self.order.status != "CONFIRMED":
                raise ValueError("A proposed order must be complete and unstarted")
            if self.receipt_id is not None or self.expedite_quote_ids:
                raise ValueError("Receipt quotes belong to a material-shortage study")
            if self.minimum_partial_quantity is not None and not self.partial_delivery_allowed:
                raise ValueError("A partial quantity requires explicit partial-delivery permission")
            if (
                self.order is not None
                and self.final_due_at is not None
                and self.final_due_at < self.order.due_at
            ):
                raise ValueError("Final delivery cannot precede the requested first delivery")
        elif (
            self.order is not None
            or self.existing_order_id is not None
            or self.partial_delivery_allowed
            or self.minimum_partial_quantity is not None
            or self.final_due_at is not None
        ):
            raise ValueError("Order promises belong to an urgent-order study")
        return self


class OrderImpact(Contract):
    order_id: Identifier
    existing_commitment: StrictBool
    requested_due_at: Timestamp
    quantity: NonNegative
    quantity_unit: Literal["EA"] = "EA"
    on_time_quantity: NonNegative | None = None
    completion_at: Timestamp | None = None
    tardiness_minutes: NonNegative | None = None
    baseline_completion_at: Timestamp | None = None
    completion_change_minutes: StrictInt | None = None


class DeliveryPromise(Contract):
    quantity: Positive
    ready_at: Timestamp
    quantity_unit: Literal["EA"] = "EA"


class StudySearch(Contract):
    condition: Text
    time_limit_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    native_status: str
    checked_solution: StrictBool
    candidate_hash: Digest | None = None
    error_code: Identifier | None = None


class BusinessOption(Contract):
    option_id: Identifier
    kind: Literal[
        "normal",
        "overtime",
        "earliest_completion",
        "partial_delivery",
        "shared_material",
        "receipt_expedite",
        "wait_diagnostic",
        "treatment",
    ]
    title: Text
    status: Literal[
        "FEASIBLE", "INFEASIBLE", "UNKNOWN", "BLOCKED", "BUDGET_EXHAUSTED", "CHECK_FAILED"
    ]
    summary: Text
    assumptions: tuple[Text, ...]
    allow_overtime: StrictBool = False
    diagnostic_only: StrictBool = False
    dominated: StrictBool = Field(default=False, exclude_if=lambda value: not value)
    publishable: Literal[False] = False
    requires_business_confirmation: Literal[True] = True
    protects_existing_commitments: StrictBool | None = None
    quote_id: Identifier | None = None
    cost_minor: NonNegative | None = None
    currency: Annotated[str, Field(min_length=3, max_length=3)] | None = None
    cost_unit: Literal["minor_currency_unit"] = "minor_currency_unit"
    incremental_overtime_minutes: StrictInt | None = None
    overtime_unit: Literal["worker_minutes"] = "worker_minutes"
    changed_operations: NonNegative | None = None
    total_start_shift_minutes: NonNegative | None = None
    requested_quantity: NonNegative | None = None
    on_time_quantity: NonNegative | None = None
    completion_at: Timestamp | None = None
    deliveries: tuple[DeliveryPromise, ...] = ()
    earliest_completion_proven: StrictBool = False
    maximum_on_time_quantity_proven: StrictBool = False
    impacts: tuple[OrderImpact, ...] = ()
    searches: tuple[StudySearch, ...] = ()
    derived_snapshot: Snapshot | None = None
    candidate: Candidate | None = None
    economics: OptionEconomics | None = None
    actions: tuple[TreatmentAction, ...] = ()

    @model_validator(mode="after")
    def scenario_evidence(self) -> Self:
        if (self.cost_minor is None) != (self.currency is None):
            raise ValueError("A quoted cost requires both minor amount and currency")
        if self.status == "FEASIBLE":
            if (
                self.derived_snapshot is None
                or self.candidate is None
                or not self.candidate.has_solution
                or self.candidate.checker.status != "PASS"
                or self.candidate.binding.snapshot_hash != self.derived_snapshot.content_hash
            ):
                raise ValueError("A feasible business option needs its independently checked facts")
        if (self.earliest_completion_proven or self.maximum_on_time_quantity_proven) and (
            self.status != "FEASIBLE"
        ):
            raise ValueError("Optimality claims require a checked feasible option")
        return self

    def public_view(self) -> dict:
        return self.model_dump(mode="json", exclude={"derived_snapshot", "candidate"})


class BusinessStudy(Contract):
    schema_version: Literal["byof.business-study/1"] = "byof.business-study/1"
    study_id: Identifier
    factory_id: Identifier
    run_id: Identifier
    origin_snapshot_id: Identifier
    origin_snapshot_hash: Digest
    origin_snapshot_clock: Timestamp
    baseline_hash: Digest | None = None
    request: BusinessStudyRequest
    options: tuple[BusinessOption, ...]
    elapsed_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    publishable: Literal[False] = False

    def public_view(self) -> dict:
        view = self.model_dump(mode="json", exclude={"options"})
        view["options"] = [option.public_view() for option in self.options]
        return view
