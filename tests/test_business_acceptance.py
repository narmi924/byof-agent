"""Source-side business agreements reject stale or unconfirmed commercial facts."""

from datetime import timedelta

import pytest

from packages.domain.business_acceptance import BusinessAcceptanceError, accept_business_option
from packages.domain.business_options import BusinessStudyRequest
from packages.domain.business_terms import BusinessTerms, DeliveryRule, ExpediteQuote
from packages.domain.models import Order, Snapshot, canonical_hash
from packages.domain.skf import load_skf_snapshot
from packages.planning.business_options import evaluate_business_options
from packages.planning.solver import solve
from services.factory_sim.engine import advance, evolve


def source_with_terms():
    source = load_skf_snapshot(development=True)
    data = source.model_dump(mode="python", exclude={"content_hash"})
    data["schema_version"] = "byof.snapshot/3"
    data["source"].update(source_revision="1", cursor="1")
    material = source.inventory[0]
    data["receipts"] = [
        {
            "receipt_id": "BIZ-INBOUND",
            "material_id": material.material_id,
            "unit": material.unit,
            "quantity": 100,
            "eta": source.snapshot_clock + timedelta(minutes=100),
            "status": "CONFIRMED",
            "version": 1,
        }
    ]
    quote = ExpediteQuote(
        quote_id="BIZ-QUOTE",
        receipt_id="BIZ-INBOUND",
        receipt_version=1,
        original_eta=data["receipts"][0]["eta"],
        expedited_eta=source.snapshot_clock + timedelta(minutes=10),
        quantity=100,
        valid_until=source.snapshot_clock + timedelta(minutes=60),
        source_reference="synthetic-test-quote",
        evidence_mode="synthetic",
        cost_minor=1250,
        currency="CNY",
    )
    data["business_terms"] = BusinessTerms(
        version="business-test/1",
        evidence_mode="synthetic",
        delivery_rules=(
            DeliveryRule(
                product_id=source.orders[0].product_id,
                partial_delivery_allowed=True,
                minimum_partial_quantity=50,
                max_deliveries=2,
            ),
        ),
        expedite_quotes=(quote,),
    )
    return Snapshot.model_validate(data)


def command(source, *, include_order=False, include_quote=False):
    payload = {
        "expected_snapshot_hash": source.content_hash,
        "terms_version": source.business_terms.version,
        "study_hash": "a" * 64,
    }
    if include_order:
        payload.update(
            order=Order.model_validate(
                {
                    **source.orders[0].model_dump(),
                    "order_id": "BIZ-URGENT",
                    "quantity": 100,
                    "due_at": source.snapshot_clock + timedelta(minutes=75),
                    "version": 1,
                    "split_revision": 1,
                    "status": "CONFIRMED",
                }
            ).model_dump(mode="json"),
            first_delivery_quantity=50,
            first_delivery_due_at=source.snapshot_clock + timedelta(minutes=75),
            final_delivery_due_at=source.snapshot_clock + timedelta(minutes=180),
        )
    if include_quote:
        payload.update(quote_id="BIZ-QUOTE", confirmed_cost_minor=1250, currency="CNY")
    return payload


def test_agreement_keeps_original_request_and_real_batch_promises_without_mutating_input():
    source = source_with_terms()
    original = source.model_dump_json()
    payload = command(source, include_order=True, include_quote=True)
    accepted = evolve(source, **accept_business_option(source, payload, event_id="accept-1"))
    assert source.model_dump_json() == original
    order = next(order for order in accepted.orders if order.order_id == "BIZ-URGENT")
    assert order.requested_due_at == source.snapshot_clock + timedelta(minutes=75)
    assert order.due_at == source.snapshot_clock + timedelta(minutes=180)
    assert all(order.hard_deadline for order in accepted.orders)
    batches = [batch for batch in accepted.production_batches if batch.order_id == order.order_id]
    assert [batch.delivery_due_at for batch in batches] == [
        payload["first_delivery_due_at"],
        payload["final_delivery_due_at"],
    ]
    assert all(batch.source_event_id == "accept-1" for batch in batches)
    assert accepted.receipts[0].eta == source.business_terms.expedite_quotes[0].expedited_eta
    assert accepted.receipts[0].version == source.receipts[0].version + 1
    assert accepted.active_plan_version is None and accepted.actuals == ()
    assert accepted.inventory == source.inventory


def test_late_completed_order_does_not_block_acceptance_and_formal_replanning():
    source = source_with_terms()
    data = source.model_dump(mode="python", exclude={"content_hash"})
    data["orders"][0].update(
        due_at=source.snapshot_clock + timedelta(minutes=1),
        hard_deadline=False,
    )
    source = Snapshot.model_validate(data)
    baseline = solve(source, time_limit=2)
    assert baseline.checker.status == "PASS"
    running = evolve(
        source, active_plan_version="late-plan", active_plan_hash=baseline.content_hash
    )
    completed = advance(running, baseline, minutes=100)
    historical_order = completed.orders[0]
    assert historical_order.status == "COMPLETED"
    new_order = Order.model_validate(
        {
            **historical_order.model_dump(),
            "order_id": "AFTER-LATE-HISTORY",
            "quantity": 50,
            "status": "CONFIRMED",
            "version": 1,
            "due_at": completed.snapshot_clock + timedelta(minutes=1000),
        }
    )
    study = evaluate_business_options(
        completed,
        baseline,
        BusinessStudyRequest(
            kind="urgent_order",
            order=new_order,
            total_time_limit=6,
        ),
    )
    option = study.options[0]
    assert option.status == "FEASIBLE" and option.protects_existing_commitments
    assert (
        next(impact for impact in option.impacts if impact.existing_commitment).tardiness_minutes
        > 0
    )
    changes = accept_business_option(
        completed,
        {
            "expected_snapshot_hash": completed.content_hash,
            "terms_version": completed.business_terms.version,
            "study_hash": canonical_hash(study),
            "order": new_order.model_dump(mode="json"),
            "first_delivery_quantity": new_order.quantity,
            "first_delivery_due_at": new_order.due_at,
            "final_delivery_due_at": new_order.due_at,
        },
        event_id="accept-after-late-history",
    )
    accepted = evolve(completed, **changes)
    assert accepted.orders[0] == historical_order
    assert accepted.orders[-1].hard_deadline
    assert accepted.actuals == completed.actuals
    assert accepted.reservations == completed.reservations
    assert accepted.inventory == completed.inventory
    formal = solve(accepted, baseline=baseline, time_limit=3)
    assert formal.has_solution and formal.checker.status == "PASS"
    assert formal.objective[0].value > 0  # The observed historical delay remains in the KPI.


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("expected_snapshot_hash", "b" * 64, "BUSINESS_FACTS_CHANGED"),
        ("terms_version", "changed", "BUSINESS_TERMS_CHANGED"),
        ("confirmed_cost_minor", None, "QUOTE_CHANGED_OR_EXPIRED"),
        ("confirmed_cost_minor", 1, "QUOTE_CHANGED_OR_EXPIRED"),
        ("currency", "USD", "QUOTE_CHANGED_OR_EXPIRED"),
        ("quote_id", "missing", "QUOTE_NOT_FOUND"),
    ],
)
def test_stale_source_or_missing_exact_quote_confirmation_is_rejected(field, value, code):
    source = source_with_terms()
    payload = command(source, include_quote=True)
    payload[field] = value
    with pytest.raises(BusinessAcceptanceError, match=code):
        accept_business_option(source, payload, event_id="reject")


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 2),
        ("quantity", 50),
        ("status", "CANCELLED"),
    ],
)
def test_current_receipt_must_still_match_the_quoted_version_and_quantity(field, value):
    source = source_with_terms()
    data = source.model_dump(mode="python", exclude={"content_hash"})
    data["receipts"][0][field] = value
    changed = Snapshot.model_validate(data)
    with pytest.raises(BusinessAcceptanceError, match="QUOTE_CHANGED_OR_EXPIRED"):
        accept_business_option(changed, command(changed, include_quote=True), event_id="reject")


def test_unknown_price_cannot_become_an_accepted_expedite():
    source = source_with_terms()
    data = source.model_dump(mode="python", exclude={"content_hash"})
    data["business_terms"]["expedite_quotes"][0].update(cost_minor=None, currency=None)
    unpriced = Snapshot.model_validate(data)
    payload = command(unpriced, include_quote=True)
    payload.update(confirmed_cost_minor=None, currency=None)
    with pytest.raises(BusinessAcceptanceError, match="QUOTE_PRICE_REQUIRED"):
        accept_business_option(unpriced, payload, event_id="reject")


@pytest.mark.parametrize("minutes", [-1, 0])
def test_first_delivery_cannot_be_accepted_in_the_past_or_at_now(minutes):
    source = source_with_terms()
    payload = command(source, include_order=True)
    payload["first_delivery_due_at"] = source.snapshot_clock + timedelta(minutes=minutes)
    with pytest.raises(BusinessAcceptanceError, match="INVALID_DELIVERY_AGREEMENT"):
        accept_business_option(source, payload, event_id="reject")


def test_partial_delivery_needs_source_permission_and_cannot_omit_remaining_demand():
    source = source_with_terms()
    data = source.model_dump(mode="python", exclude={"content_hash"})
    data["business_terms"]["delivery_rules"][0]["partial_delivery_allowed"] = False
    denied = Snapshot.model_validate(data)
    with pytest.raises(BusinessAcceptanceError, match="PARTIAL_DELIVERY_NOT_ALLOWED"):
        accept_business_option(denied, command(denied, include_order=True), event_id="reject")
    payload = command(source, include_order=True)
    payload["first_delivery_quantity"] = 150
    with pytest.raises(BusinessAcceptanceError, match="INVALID_DELIVERY_AGREEMENT"):
        accept_business_option(source, payload, event_id="reject")
