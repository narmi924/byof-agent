"""Explicit source facts for delivery permission, supplier quotes and future overtime.

These commands change source conditions, never customer orders, material balances or
plan approvals. Version checks prevent an old form from overwriting newer source facts.
"""

from datetime import datetime

from packages.domain.business_terms import BusinessTerms, DeliveryRule, ExpediteQuote
from packages.domain.execution import (
    DeliveryRuleSet,
    ExpediteQuoteRemove,
    ExpediteQuoteSet,
    OvertimeWindowSet,
)
from packages.domain.models import CalendarWindow, Resource, Snapshot, Worker, canonical_hash

BUSINESS_CONTROLS = {
    "delivery_rule.set",
    "expedite_quote.set",
    "expedite_quote.remove",
    "overtime_window.set",
}


class SourceBusinessError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _terms(snapshot: Snapshot, expected: str | None) -> BusinessTerms | None:
    terms = snapshot.business_terms
    if expected != (terms.version if terms else None):
        raise SourceBusinessError("BUSINESS_TERMS_CHANGED")
    if terms is not None and terms.evidence_mode != "synthetic":
        raise SourceBusinessError("ENTERPRISE_TERMS_READ_ONLY")
    return terms


def _save_terms(terms: BusinessTerms | None, *, rules=None, quotes=None) -> dict:
    rules = tuple(rules) if rules is not None else terms.delivery_rules if terms else ()
    quotes = tuple(quotes) if quotes is not None else terms.expedite_quotes if terms else ()
    if terms is not None and rules == terms.delivery_rules and quotes == terms.expedite_quotes:
        raise SourceBusinessError("BUSINESS_TERMS_UNCHANGED")
    # Content and prior version are replay-stable; new replay event IDs must not change terms.
    digest = canonical_hash(
        {
            "previous_version": terms.version if terms else None,
            "delivery_rules": [rule.model_dump(mode="json") for rule in rules],
            "expedite_quotes": [quote.model_dump(mode="json") for quote in quotes],
        }
    )
    return {
        "schema_version": "byof.snapshot/3",
        "business_terms": BusinessTerms(
            version=f"terms:{digest}",
            evidence_mode="synthetic",
            delivery_rules=rules,
            expedite_quotes=quotes,
        ),
    }


def _replace(rows, changed, key):
    if any(getattr(row, key) == getattr(changed, key) for row in rows):
        return tuple(changed if getattr(row, key) == getattr(changed, key) else row for row in rows)
    return (*rows, changed)


def _minute(value: datetime) -> bool:
    return value.second == 0 and value.microsecond == 0


def source_business_changes(snapshot: Snapshot, kind: str, payload: dict) -> dict:
    if kind == "delivery_rule.set":
        rule_input = DeliveryRuleSet.model_validate(payload)
        terms = _terms(snapshot, rule_input.expected_terms_version)
        product = next(
            (p for p in snapshot.profile.products if p.product_id == rule_input.product_id), None
        )
        if product is None:
            raise SourceBusinessError("PRODUCT_NOT_FOUND")
        if rule_input.minimum_partial_quantity % product.batch_size or (
            rule_input.partial_delivery_allowed and rule_input.max_deliveries != 2
        ):
            raise SourceBusinessError("INVALID_DELIVERY_RULE")
        rule = DeliveryRule.model_validate(
            rule_input.model_dump(exclude={"expected_terms_version"})
        )
        return _save_terms(
            terms, rules=_replace(terms.delivery_rules if terms else (), rule, "product_id")
        )
    if kind == "expedite_quote.set":
        quote_input = ExpediteQuoteSet.model_validate(payload)
        terms = _terms(snapshot, quote_input.expected_terms_version)
        receipt = next(
            (r for r in snapshot.receipts if r.receipt_id == quote_input.receipt_id), None
        )
        if receipt is None or receipt.status not in {"EXPECTED", "CONFIRMED"}:
            raise SourceBusinessError("RECEIPT_NOT_PENDING")
        if receipt.version != quote_input.expected_receipt_version:
            raise SourceBusinessError("RECEIPT_VERSION_CHANGED")
        if (
            not all(_minute(t) for t in (quote_input.expedited_eta, quote_input.valid_until))
            or not snapshot.snapshot_clock
            < quote_input.expedited_eta
            < min(receipt.eta, snapshot.horizon.end_at)
            or quote_input.valid_until <= snapshot.snapshot_clock
        ):
            raise SourceBusinessError("INVALID_EXPEDITE_QUOTE_TIME")
        quote = ExpediteQuote(
            quote_id=quote_input.quote_id,
            receipt_id=receipt.receipt_id,
            receipt_version=receipt.version,
            original_eta=receipt.eta,
            expedited_eta=quote_input.expedited_eta,
            quantity=receipt.quantity,
            valid_until=quote_input.valid_until,
            source_reference=quote_input.source_reference,
            evidence_mode="synthetic",
            cost_minor=quote_input.cost_minor,
            currency=quote_input.currency,
        )
        return _save_terms(
            terms, quotes=_replace(terms.expedite_quotes if terms else (), quote, "quote_id")
        )
    if kind == "expedite_quote.remove":
        remove_input = ExpediteQuoteRemove.model_validate(payload)
        terms = _terms(snapshot, remove_input.expected_terms_version)
        if terms is None or not any(
            q.quote_id == remove_input.quote_id for q in terms.expedite_quotes
        ):
            raise SourceBusinessError("QUOTE_NOT_FOUND")
        return _save_terms(
            terms,
            quotes=tuple(q for q in terms.expedite_quotes if q.quote_id != remove_input.quote_id),
        )
    if kind != "overtime_window.set":
        raise SourceBusinessError("UNKNOWN_CONTROL")
    window_input = OvertimeWindowSet.model_validate(payload)
    rows = snapshot.workers if window_input.target_type == "worker" else snapshot.resources
    id_key = "worker_id" if window_input.target_type == "worker" else "resource_id"
    target = next((row for row in rows if getattr(row, id_key) == window_input.target_id), None)
    if target is None:
        raise SourceBusinessError("OBJECT_NOT_FOUND")
    if target.version != window_input.expected_version:
        raise SourceBusinessError("RESOURCE_VERSION_CHANGED")
    start, end = window_input.start_at, window_input.end_at
    if (
        not all(_minute(t) for t in (start, end))
        or not snapshot.snapshot_clock < start < end <= snapshot.horizon.end_at
    ):
        raise SourceBusinessError("INVALID_OVERTIME_WINDOW")
    windows = target.calendar
    if window_input.action == "add":
        if any(start < w.end_at and w.start_at < end for w in (*windows, *target.unavailable)):
            raise SourceBusinessError("OVERTIME_WINDOW_CONFLICT")
        windows = tuple(
            sorted(
                (*windows, CalendarWindow(start_at=start, end_at=end, kind="OVERTIME")),
                key=lambda w: w.start_at,
            )
        )
    else:
        selected = next(
            (
                w
                for w in windows
                if w.kind == "OVERTIME" and w.start_at == start and w.end_at == end
            ),
            None,
        )
        if selected is None:
            raise SourceBusinessError("OVERTIME_WINDOW_NOT_FOUND")
        windows = tuple(w for w in windows if w != selected)
        if not windows:
            raise SourceBusinessError("INVALID_OVERTIME_WINDOW")
    changes = {**target.model_dump(), "calendar": windows, "version": target.version + 1}
    if isinstance(target, Worker) and window_input.action == "add":
        changes["overtime_available"] = True
    changed = (
        Worker.model_validate(changes)
        if isinstance(target, Worker)
        else Resource.model_validate(changes)
    )
    return {
        "workers" if window_input.target_type == "worker" else "resources": tuple(
            changed if getattr(row, id_key) == window_input.target_id else row for row in rows
        )
    }
