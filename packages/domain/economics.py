"""Versioned demonstration economics, never an enterprise price feed."""

from typing import Literal, Self

from pydantic import Field, StrictInt, model_validator

from packages.domain.models import Contract, NonNegative, Text

CATALOG_VERSION = "byof-demo-economics/1"
# SGD cents per finished bearing. Fixed synthetic assumptions for comparing plans,
# not SKF prices. Variable cost includes ordinary materials, labor and processing.
PRODUCT_RATES = {
    "BRG-6202-2RS1": (1200, 800),
    "BRG-6203-2RS1": (1400, 950),
    "BRG-6204-2RS1": (1800, 1200),
    "BRG-6205-2RS1": (2200, 1500),
}
OVERTIME_PREMIUM_PER_MINUTE = 30  # SGD 18/hour, above ordinary labor already costed.
LATE_BASIS_POINTS_PER_DAY = 100
MAX_LATE_BASIS_POINTS = 2000


class EconomicLine(Contract):
    label: Text
    amount_minor: NonNegative
    basis: Text


class EconomicSensitivity(Contract):
    extra_cost_percent: NonNegative = 20
    completion_delay_minutes: NonNegative = 60
    net_contribution_minor: StrictInt
    incremental_cash_outlay_minor: NonNegative
    basis: Text


class OptionEconomics(Contract):
    catalog_version: Literal["byof-demo-economics/1"] = "byof-demo-economics/1"
    evidence_mode: Literal["synthetic"] = "synthetic"
    currency: Literal["SGD"] = "SGD"
    status: Literal["ESTIMATED", "INCOMPLETE"]
    revenue_minor: NonNegative | None = None
    variable_cost_minor: NonNegative | None = None
    additional_cost_minor: NonNegative | None = None
    late_deduction_minor: NonNegative | None = None
    net_contribution_minor: StrictInt | None = None
    incremental_cash_outlay_minor: NonNegative | None = None
    improvement_minor: StrictInt | None = None
    comparison_option_id: str | None = None
    lines: tuple[EconomicLine, ...] = ()
    assumptions: tuple[Text, ...]
    missing: tuple[Text, ...] = ()
    adverse: EconomicSensitivity | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def consistent_totals(self) -> Self:
        if self.status == "INCOMPLETE":
            if self.net_contribution_minor is not None or self.improvement_minor is not None:
                raise ValueError("Incomplete economics cannot claim a realizable contribution")
            return self
        if (
            any(
                value is None
                for value in (
                    self.revenue_minor,
                    self.variable_cost_minor,
                    self.additional_cost_minor,
                    self.late_deduction_minor,
                    self.net_contribution_minor,
                    self.incremental_cash_outlay_minor,
                )
            )
            or self.missing
        ):
            raise ValueError("An estimate needs complete cost and delivery evidence")
        assert self.revenue_minor is not None and self.variable_cost_minor is not None
        assert self.additional_cost_minor is not None and self.late_deduction_minor is not None
        if (
            self.net_contribution_minor
            != self.revenue_minor
            - self.variable_cost_minor
            - self.additional_cost_minor
            - self.late_deduction_minor
        ):
            raise ValueError("Net contribution does not reconcile with its components")
        return self


def late_deduction(revenue_minor: int, minutes: int) -> int:
    """Daily pro-rata deduction, capped at 20%; no invented cancellation probability."""
    numerator = min(MAX_LATE_BASIS_POINTS * 1440, minutes * LATE_BASIS_POINTS_PER_DAY)
    return (revenue_minor * numerator + 1440 * 10000 - 1) // (1440 * 10000)
