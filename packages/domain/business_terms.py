"""Source-owned delivery rules and priced supply options, never model inventions."""

from datetime import datetime, timezone
from typing import Annotated, Literal, Self

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
    model_validator,
)


def _explicit_time(value: object) -> object:
    if not isinstance(value, (datetime, str)):
        raise ValueError("A timezone-aware timestamp is required")
    return value


BusinessTime = Annotated[
    AwareDatetime,
    BeforeValidator(_explicit_time),
    AfterValidator(lambda value: value.astimezone(timezone.utc)),
]
Reference = Annotated[StrictStr, Field(min_length=1, max_length=160, pattern=r"^[^\s]+$")]


class BusinessContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ExpediteQuote(BusinessContract):
    quote_id: Reference
    receipt_id: Reference
    receipt_version: StrictInt = Field(gt=0)
    original_eta: BusinessTime
    expedited_eta: BusinessTime
    quantity: StrictInt = Field(gt=0)
    valid_until: BusinessTime
    source_reference: Reference
    evidence_mode: Literal["synthetic", "enterprise"]
    cost_minor: StrictInt | None = Field(default=None, ge=0)
    currency: Annotated[StrictStr, Field(pattern=r"^[A-Z]{3}$")] | None = None

    @model_validator(mode="after")
    def consistent_quote(self) -> Self:
        if self.expedited_eta >= self.original_eta:
            raise ValueError("Expedited arrival must be earlier than the existing ETA")
        if (self.cost_minor is None) != (self.currency is None):
            raise ValueError("Quote cost and currency must be supplied together")
        return self


class DeliveryRule(BusinessContract):
    product_id: Reference
    partial_delivery_allowed: StrictBool = False
    minimum_partial_quantity: StrictInt = Field(default=1, gt=0)
    max_deliveries: StrictInt = Field(default=2, ge=1, le=2)


class BusinessTerms(BusinessContract):
    version: Reference
    evidence_mode: Literal["synthetic", "enterprise"]
    delivery_rules: tuple[DeliveryRule, ...] = ()
    expedite_quotes: tuple[ExpediteQuote, ...] = ()

    @model_validator(mode="after")
    def unique_terms(self) -> Self:
        for values in (
            [rule.product_id for rule in self.delivery_rules],
            [quote.quote_id for quote in self.expedite_quotes],
        ):
            if len(values) != len(set(values)):
                raise ValueError("Business terms contain duplicate references")
        if any(quote.evidence_mode != self.evidence_mode for quote in self.expedite_quotes):
            raise ValueError("Quote evidence must match the source business terms")
        return self
