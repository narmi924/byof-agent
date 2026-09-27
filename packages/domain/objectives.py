"""Confirmed objective contracts preserve hard rules and express no monetary estimates."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Any, Literal, Self

from pydantic import Field, StrictInt, model_validator

from packages.domain.models import (
    Contract,
    Digest,
    ErrorCode,
    Identifier,
    NonNegative,
    Positive,
    Snapshot,
    Timestamp,
    canonical_hash,
    reject,
)

ObjectiveName = Literal[
    "weighted_tardiness",
    "incremental_overtime_metric",
    "changed_operations",
    "total_start_shift",
    "makespan",
]
Selection = Literal["delivery_first", "stability_first", "overtime_first", "custom"]
METRIC_NAMES: tuple[ObjectiveName, ...] = (
    "weighted_tardiness",
    "incremental_overtime_metric",
    "changed_operations",
    "total_start_shift",
    "makespan",
)
METRIC_UNITS: Mapping[ObjectiveName, str] = MappingProxyType(
    {name: "operations" if name == "changed_operations" else "minutes" for name in METRIC_NAMES}
)
PRESET_ORDERS: Mapping[str, tuple[ObjectiveName, ...]] = MappingProxyType(
    {
        "delivery_first": METRIC_NAMES,
        "stability_first": (
            "changed_operations",
            "total_start_shift",
            "weighted_tardiness",
            "incremental_overtime_metric",
            "makespan",
        ),
        "overtime_first": (
            "incremental_overtime_metric",
            "weighted_tardiness",
            "changed_operations",
            "total_start_shift",
            "makespan",
        ),
    }
)


class ObjectiveDefinition(Contract):
    selection: Selection = "delivery_first"
    objective_order: Annotated[tuple[ObjectiveName, ...], Field(min_length=5, max_length=5)] = (
        METRIC_NAMES
    )
    max_weighted_tardiness: NonNegative | None = None
    max_incremental_overtime_minutes: StrictInt | None = None

    @model_validator(mode="before")
    @classmethod
    def named_order(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if set(value) & {"weights", "currency_rate", "overtime_cost_per_minute", "rates"}:
            reject(
                ErrorCode.UNSUPPORTED_CAPABILITY, "Only ordered objectives with minute bounds work"
            )
        if "objective_order" not in value:
            selection = value.get("selection", "delivery_first")
            if selection == "custom":
                reject(
                    ErrorCode.SOURCE_INCOMPLETE, "Custom objectives require an explicit full order"
                )
            if isinstance(selection, str) and selection in PRESET_ORDERS:
                return {**value, "objective_order": PRESET_ORDERS[selection]}
        return value

    @model_validator(mode="after")
    def complete_order_and_boundaries(self) -> Self:
        if set(self.objective_order) != set(METRIC_NAMES):
            reject(
                ErrorCode.INVALID_INPUT, "Each of the five supported objectives is required once"
            )
        if self.selection != "custom" and self.objective_order != PRESET_ORDERS[self.selection]:
            reject(
                ErrorCode.INVALID_INPUT,
                "A named preference must retain its defined objective order",
            )
        before_delivery = self.objective_order.index("weighted_tardiness") > 0
        before_overtime = self.objective_order[
            : self.objective_order.index("incremental_overtime_metric")
        ]
        displaces_overtime = any(name != "weighted_tardiness" for name in before_overtime)
        if before_delivery and self.max_weighted_tardiness is None:
            reject(
                ErrorCode.CONFIRMATION_REQUIRED,
                "Lower delivery priority needs an explicit tardiness bound",
            )
        if displaces_overtime and self.max_incremental_overtime_minutes is None:
            reject(
                ErrorCode.CONFIRMATION_REQUIRED,
                "Lower overtime priority needs an explicit minute bound",
            )
        return self

    @property
    def order(self) -> tuple[ObjectiveName, ...]:
        return self.objective_order

    @property
    def bounds(self) -> dict[ObjectiveName, int]:
        result: dict[ObjectiveName, int] = {}
        if self.max_weighted_tardiness is not None:
            result["weighted_tardiness"] = self.max_weighted_tardiness
        if self.max_incremental_overtime_minutes is not None:
            # This is a difference from the active baseline, so a negative upper bound is valid.
            result["incremental_overtime_metric"] = self.max_incremental_overtime_minutes
        return result


class ObjectiveSource(Contract):
    preference_id: Identifier
    version: Positive
    scope_type: Literal["FACTORY", "PROCESS", "CASE"]
    scope_id: Identifier
    confirmed_by: Identifier
    confirmed_at: Timestamp
    clock: Literal["real"] = "real"
    product_id: Identifier | None = None
    route_version: Identifier | None = None

    @model_validator(mode="after")
    def process_selector(self) -> Self:
        if self.scope_type == "PROCESS":
            if self.product_id is None or self.route_version is None:
                reject(
                    ErrorCode.SOURCE_INCOMPLETE,
                    "A process preference needs product and route version",
                )
        elif self.product_id is not None or self.route_version is not None:
            reject(ErrorCode.INVALID_REFERENCE, "Only process preferences have a process selector")
        return self


class EffectiveObjective(Contract):
    schema_version: Literal["byof.effective-objective/1"] = "byof.effective-objective/1"
    factory_id: Identifier
    profile_version: Identifier
    policy_version: Identifier
    definition: ObjectiveDefinition
    sources: tuple[ObjectiveSource, ...]
    resolution_version: NonNegative = 0
    coordination_id: Identifier | None = None
    content_hash: Digest | None = None

    @model_validator(mode="after")
    def source_and_hash(self) -> Self:
        if not self.sources and (
            self.resolution_version == 0 or self.definition != ObjectiveDefinition()
        ):
            reject(
                ErrorCode.CONFIRMATION_REQUIRED,
                "An empty source set only represents a versioned return to unchanged defaults",
            )
        ids = [source.preference_id for source in self.sources]
        scopes = [(source.scope_type, source.scope_id) for source in self.sources]
        if len(set(ids)) != len(ids) or len(set(scopes)) != len(scopes):
            reject(
                ErrorCode.DUPLICATE_ID,
                "An effective objective cannot use competing source versions",
            )
        for source in self.sources:
            if source.scope_type == "FACTORY" and source.scope_id != self.factory_id:
                reject(ErrorCode.INVALID_REFERENCE, "Factory preference belongs to another factory")
        digest = canonical_hash(self.model_dump(mode="json", exclude={"content_hash"}))
        if self.content_hash is not None and self.content_hash != digest:
            reject(ErrorCode.HASH_MISMATCH, "Effective objective differs from its recorded hash")
        object.__setattr__(self, "content_hash", digest)
        return self

    @property
    def objective_version(self) -> str:
        return f"objective:{self.content_hash}"

    @property
    def order(self) -> tuple[ObjectiveName, ...]:
        return self.definition.order

    @property
    def bounds(self) -> dict[ObjectiveName, int]:
        return self.definition.bounds

    def validate_for(self, snapshot: Snapshot) -> None:
        """Validate references; the service separately authenticates confirmations and current heads."""
        checked = type(self).model_validate(self)
        if checked.factory_id != snapshot.factory_id:
            reject(
                ErrorCode.INVALID_REFERENCE, "Objective and snapshot belong to different factories"
            )
        if (
            checked.profile_version != snapshot.profile.version
            or checked.policy_version != snapshot.profile.policy.policy_version
        ):
            reject(
                ErrorCode.VERSION_MISMATCH, "Objective needs the bound factory and policy versions"
            )
        processes = {
            (product.product_id, product.route_version) for product in snapshot.profile.products
        }
        for source in checked.sources:
            if (
                source.scope_type == "PROCESS"
                and (source.product_id, source.route_version) not in processes
            ):
                reject(
                    ErrorCode.INVALID_REFERENCE,
                    "Process preference references an unknown product or route",
                )
