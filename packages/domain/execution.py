"""Conditional enterprise actions and private simulator commands are distinct contracts."""

from typing import Any, Literal, Self

from pydantic import Field, StrictBool, StrictInt, model_serializer, model_validator

from packages.domain.models import (
    Approval,
    Candidate,
    Contract,
    Digest,
    Identifier,
    NonNegative,
    Positive,
    Timestamp,
)
from packages.domain.objectives import EffectiveObjective
from packages.domain.revalidation import ValidationCertificate


class PlanSubmission(Contract):
    operation_id: Identifier
    factory_id: Identifier
    run_id: Identifier
    expected_source_revision: Identifier
    expected_snapshot_hash: Digest
    expected_active_plan_version: Identifier | None
    candidate: Candidate
    approvals: tuple[Approval, ...]
    objective: EffectiveObjective | None = None
    certificate: ValidationCertificate | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_payload(self, handler):
        result = handler(self)
        if self.objective is None:
            result.pop("objective", None)
        if self.certificate is None:
            result.pop("certificate", None)
        return result


class ActionReceipt(Contract):
    operation_id: Identifier
    factory_id: Identifier
    run_id: Identifier
    receipt_id: Identifier
    candidate_hash: Digest
    source_state: Literal["ACTIVE", "REJECTED"]
    plan_version: Identifier | None
    effective_at: Timestamp | None
    recorded_at: Timestamp
    error_code: Identifier | None = None

    @model_validator(mode="after")
    def conditional_result(self) -> Self:
        if self.source_state == "ACTIVE":
            if (
                self.plan_version is None
                or self.effective_at is None
                or self.error_code is not None
            ):
                raise ValueError(
                    "Active source receipt requires its execution version/time and no rejection"
                )
        elif (
            self.error_code is None
            or self.plan_version is not None
            or self.effective_at is not None
        ):
            raise ValueError(
                "Rejected source receipt requires its reason and cannot claim an effective plan"
            )
        return self


class SimulatorCommand(Contract):
    request_id: Identifier
    run_id: Identifier
    kind: Literal[
        "clock.step",
        "clock.run",
        "clock.pause",
        "resource.down",
        "resource.outage",
        "scenario.configure",
        "resource.restore",
        "worker.absent",
        "worker.leave",
        "worker.return",
        "receipt.receive",
        "receipt.delay",
        "receipt.shortfall",
        "receipt.cancel",
        "receipt.add",
        "order.add",
        "order.change",
        "business.accept",
        "treatment.apply",
        "order.revise",
        "inventory.reconcile",
        "quality.record",
        "quality.scrap",
        "execution.confirm_remaining",
        "delivery_rule.set",
        "expedite_quote.set",
        "expedite_quote.remove",
        "overtime_window.set",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)


class OrderChange(Contract):
    order_id: Identifier
    expected_version: Positive
    quantity: NonNegative
    due_at: Timestamp | None = None


class DeliveryRuleSet(Contract):
    expected_terms_version: Identifier | None
    product_id: Identifier
    partial_delivery_allowed: StrictBool
    minimum_partial_quantity: Positive
    max_deliveries: StrictInt = Field(ge=1, le=2)


class ExpediteQuoteSet(Contract):
    expected_terms_version: Identifier | None
    quote_id: Identifier
    receipt_id: Identifier
    expected_receipt_version: Positive
    expedited_eta: Timestamp
    valid_until: Timestamp
    source_reference: Identifier
    cost_minor: NonNegative | None
    currency: str | None = Field(pattern=r"^[A-Z]{3}$")


class ExpediteQuoteRemove(Contract):
    expected_terms_version: Identifier | None
    quote_id: Identifier


class OvertimeWindowSet(Contract):
    target_type: Literal["worker", "resource"]
    target_id: Identifier
    expected_version: Positive
    action: Literal["add", "remove"]
    start_at: Timestamp
    end_at: Timestamp


class ClockStep(Contract):
    minutes: StrictInt = Field(default=1, ge=1, le=60)


class RunSpeed(Contract):
    interval_ms: StrictInt = Field(default=1000, ge=100, le=60_000)


class TimedOutage(Contract):
    resource_id: Identifier
    minutes: StrictInt = Field(ge=1, le=240)


class TimedLeave(Contract):
    worker_id: Identifier
    minutes: StrictInt = Field(ge=1, le=240)


class ScenarioConfiguration(Contract):
    enabled: bool = Field(strict=True)
    seed: StrictInt = Field(default=1, ge=0, le=2_147_483_647)
    every_minutes: StrictInt = Field(default=90, ge=30, le=480)


class ReplayStart(Contract):
    request_id: Identifier
    expected_run_id: Identifier


class TodayRunStart(Contract):
    request_id: Identifier
    expected_run_id: Identifier
    scenario_version: Literal["workshop-full-2"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
