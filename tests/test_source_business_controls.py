"""Explicit simulated business facts preserve production, permissions and physical supply."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from packages.agent.impact import classify_events, impact_report
from packages.domain.business_options import BusinessStudyRequest
from packages.domain.business_terms import BusinessTerms
from packages.domain.models import Snapshot
from packages.domain.snapshot_delta import apply_delta, make_delta
from packages.planning.business_options import _quote_error, evaluate_business_options
from packages.planning.business_service import validate_request
from packages.planning.solver import solve
from services.factory_sim.engine import SimulationError, advance, evolve, inject
from services.factory_sim.service import _write


def initial():
    from packages.domain.skf import load_skf_snapshot

    data = load_skf_snapshot(development=True).model_dump(exclude={"content_hash"})
    data["schema_version"] = "byof.snapshot/2"
    data["source"].update(source_revision="1", cursor="1")
    return Snapshot.model_validate(data)


def rule_payload(source):
    return {
        "expected_terms_version": source.business_terms.version if source.business_terms else None,
        "product_id": source.orders[0].product_id,
        "partial_delivery_allowed": True,
        "minimum_partial_quantity": 50,
        "max_deliveries": 2,
    }


def quote_payload(source):
    receipt = next(r for r in source.receipts if r.eta > source.snapshot_clock + timedelta(hours=2))
    return {
        "expected_terms_version": source.business_terms.version if source.business_terms else None,
        "quote_id": "SUPPLIER-QUOTE",
        "receipt_id": receipt.receipt_id,
        "expected_receipt_version": receipt.version,
        "expedited_eta": (source.snapshot_clock + timedelta(hours=1)).isoformat(),
        "valid_until": (source.snapshot_clock + timedelta(minutes=30)).isoformat(),
        "source_reference": "supplier-reference-1",
        "cost_minor": 1250,
        "currency": "CNY",
    }


def overtime_payload(source, target_type="worker", *, action="add"):
    target = source.workers[0] if target_type == "worker" else source.resources[0]
    # This is an explicit test window after the first day's existing overtime.
    first = next(w for w in target.calendar if w.kind == "OVERTIME")
    return {
        "target_type": target_type,
        "target_id": getattr(target, f"{target_type}_id"),
        "expected_version": target.version,
        "action": action,
        "start_at": first.end_at.isoformat(),
        "end_at": (first.end_at + timedelta(minutes=30)).isoformat(),
    }


def apply(source, kind, payload):
    before = source.model_dump_json()
    after = inject(source, event_id="explicit-control", kind=kind, payload=payload)
    assert source.model_dump_json() == before
    assert apply_delta(source, make_delta(source, after)) == after
    assert after.actuals == source.actuals and after.reservations == source.reservations
    assert after.active_plan_hash == source.active_plan_hash
    assert after.orders == source.orders and after.production_batches == source.production_batches
    assert after.inventory == source.inventory and after.receipts == source.receipts
    return after


def test_delivery_permission_is_versioned_source_rule_without_customer_or_production_mutation():
    source = initial()
    updated = apply(source, "delivery_rule.set", rule_payload(source))
    assert updated.schema_version == "byof.snapshot/3"
    assert updated.business_terms.evidence_mode == "synthetic"
    assert updated.business_terms.delivery_rules[0].partial_delivery_allowed
    validate_request(
        updated,
        BusinessStudyRequest(
            kind="urgent_order",
            existing_order_id=source.orders[0].order_id,
            partial_delivery_allowed=True,
        ),
    )
    # Closing permission does not discard any source demand or invent an order agreement.
    disabled = apply(
        updated,
        "delivery_rule.set",
        {**rule_payload(updated), "partial_delivery_allowed": False, "max_deliveries": 1},
    )
    assert disabled.business_terms.version != updated.business_terms.version
    assert not disabled.business_terms.delivery_rules[0].partial_delivery_allowed
    assert disabled.business_terms.expedite_quotes == ()


@pytest.mark.parametrize(
    "patch,code",
    [
        ({"expected_terms_version": "stale"}, "BUSINESS_TERMS_CHANGED"),
        ({"product_id": "unknown"}, "PRODUCT_NOT_FOUND"),
        ({"minimum_partial_quantity": 1}, "INVALID_DELIVERY_RULE"),
        ({"max_deliveries": 1}, "INVALID_DELIVERY_RULE"),
    ],
)
def test_delivery_rule_rejects_wrong_versions_products_and_unusable_lot_rules(patch, code):
    source = initial()
    with pytest.raises(SimulationError) as failure:
        inject(
            source,
            event_id="bad",
            kind="delivery_rule.set",
            payload={**rule_payload(source), **patch},
        )
    assert failure.value.code == code


@pytest.mark.parametrize(
    "patch",
    [
        {"minimum_partial_quantity": 0},
        {"max_deliveries": 3},
        {"partial_delivery_allowed": "true"},
        {"quantity": 200},
    ],
)
def test_delivery_rule_contract_forbids_coercions_and_hidden_source_order_changes(patch):
    source = initial()
    with pytest.raises(ValidationError):
        inject(
            source,
            event_id="bad",
            kind="delivery_rule.set",
            payload={**rule_payload(source), **patch},
        )


@pytest.mark.parametrize("price,currency", [(None, None), (0, "CNY"), (1250, "CNY")])
def test_quote_uses_current_receipt_identity_quantity_and_eta_without_accelerating_supply(
    price, currency
):
    source = initial()
    payload = {**quote_payload(source), "cost_minor": price, "currency": currency}
    updated = apply(source, "expedite_quote.set", payload)
    quote = updated.business_terms.expedite_quotes[0]
    receipt = next(r for r in source.receipts if r.receipt_id == payload["receipt_id"])
    assert (quote.receipt_version, quote.quantity, quote.original_eta) == (
        receipt.version,
        receipt.quantity,
        receipt.eta,
    )
    assert (quote.cost_minor, quote.currency) == (price, currency)
    assert quote.evidence_mode == "synthetic"
    request = BusinessStudyRequest(kind="material_shortage", expedite_quote_ids=(quote.quote_id,))
    assert _quote_error(updated, request, quote) is None
    delayed = inject(
        updated,
        event_id="delay",
        kind="receipt.delay",
        payload={
            "receipt_id": receipt.receipt_id,
            "eta": (receipt.eta + timedelta(hours=1)).isoformat(),
        },
    )
    assert _quote_error(delayed, request, quote) is not None
    removed = apply(
        updated,
        "expedite_quote.remove",
        {"expected_terms_version": updated.business_terms.version, "quote_id": quote.quote_id},
    )
    assert removed.business_terms.expedite_quotes == ()
    assert removed.business_terms.version != updated.business_terms.version


@pytest.mark.parametrize(
    "patch,code",
    [
        ({"expected_receipt_version": 99}, "RECEIPT_VERSION_CHANGED"),
        ({"receipt_id": "unknown"}, "RECEIPT_NOT_PENDING"),
        ({"expected_terms_version": "stale"}, "BUSINESS_TERMS_CHANGED"),
    ],
)
def test_quote_rejects_stale_or_missing_source_reference(patch, code):
    source = initial()
    with pytest.raises(SimulationError) as failure:
        inject(
            source,
            event_id="bad",
            kind="expedite_quote.set",
            payload={**quote_payload(source), **patch},
        )
    assert failure.value.code == code


@pytest.mark.parametrize(
    "field,minutes", [("expedited_eta", 0), ("expedited_eta", 99999), ("valid_until", 0)]
)
def test_quote_requires_future_offer_and_earlier_delivery_inside_horizon(field, minutes):
    source = initial()
    payload = {
        **quote_payload(source),
        field: (source.snapshot_clock + timedelta(minutes=minutes)).isoformat(),
    }
    with pytest.raises(SimulationError) as failure:
        inject(source, event_id="bad", kind="expedite_quote.set", payload=payload)
    assert failure.value.code == "INVALID_EXPEDITE_QUOTE_TIME"


@pytest.mark.parametrize(
    "patch",
    [
        {"cost_minor": -1},
        {"cost_minor": None},
        {"currency": None},
        {"quantity": 100},
        {"original_eta": "2026-09-14T01:00:00Z"},
    ],
)
def test_quote_forbids_unpaired_cost_and_caller_fabricated_supply(patch):
    source = initial()
    with pytest.raises(ValidationError):
        inject(
            source,
            event_id="bad",
            kind="expedite_quote.set",
            payload={**quote_payload(source), **patch},
        )


def test_simulator_does_not_relabel_enterprise_terms_and_versions_are_replay_stable():
    source = initial()
    configured = apply(source, "delivery_rule.set", rule_payload(source))
    replay = inject(
        source,
        event_id="different-replay-event",
        kind="delivery_rule.set",
        payload=rule_payload(source),
    )
    assert configured.business_terms == replay.business_terms
    enterprise = evolve(
        source,
        schema_version="byof.snapshot/3",
        business_terms=BusinessTerms(version="enterprise-v1", evidence_mode="enterprise"),
    )
    with pytest.raises(SimulationError) as failure:
        inject(
            enterprise, event_id="bad", kind="delivery_rule.set", payload=rule_payload(enterprise)
        )
    assert failure.value.code == "ENTERPRISE_TERMS_READ_ONLY"


@pytest.mark.parametrize("target_type", ["worker", "resource"])
def test_future_overtime_add_remove_keeps_normal_calendar_and_bumps_source_version(target_type):
    source = initial()
    if target_type == "worker":
        data = source.model_dump(exclude={"content_hash"})
        data["workers"][0]["overtime_available"] = False
        source = Snapshot.model_validate(data)
    payload = overtime_payload(source, target_type)
    updated = apply(source, "overtime_window.set", payload)
    old = source.workers[0] if target_type == "worker" else source.resources[0]
    row = updated.workers[0] if target_type == "worker" else updated.resources[0]
    assert row.version == old.version + 1
    assert len(row.calendar) == len(old.calendar) + 1
    assert [w for w in row.calendar if w.kind == "NORMAL"] == [
        w for w in old.calendar if w.kind == "NORMAL"
    ]
    if target_type == "worker":
        assert row.overtime_available
    removed = apply(
        updated,
        "overtime_window.set",
        {**payload, "action": "remove", "expected_version": row.version},
    )
    remaining = removed.workers[0] if target_type == "worker" else removed.resources[0]
    assert remaining.calendar == old.calendar and remaining.version == row.version + 1


@pytest.mark.parametrize(
    "patch,code",
    [
        ({"expected_version": 99}, "RESOURCE_VERSION_CHANGED"),
        ({"target_id": "unknown"}, "OBJECT_NOT_FOUND"),
        ({"action": "remove"}, "OVERTIME_WINDOW_NOT_FOUND"),
    ],
)
def test_overtime_requires_current_known_target_and_exact_existing_window(patch, code):
    source = initial()
    with pytest.raises(SimulationError) as failure:
        inject(
            source,
            event_id="bad",
            kind="overtime_window.set",
            payload={**overtime_payload(source), **patch},
        )
    assert failure.value.code == code


def test_overtime_cannot_overlap_normal_work_remove_normal_or_rewrite_started_time():
    source = initial()
    payload = overtime_payload(source)
    future_normal = source.workers[0].calendar[1]
    overlap = {
        **payload,
        "start_at": future_normal.start_at.isoformat(),
        "end_at": future_normal.end_at.isoformat(),
    }
    for body, code in [
        (overlap, "OVERTIME_WINDOW_CONFLICT"),
        ({**overlap, "action": "remove"}, "OVERTIME_WINDOW_NOT_FOUND"),
        ({**payload, "start_at": source.snapshot_clock.isoformat()}, "INVALID_OVERTIME_WINDOW"),
    ]:
        with pytest.raises(SimulationError) as failure:
            inject(source, event_id="bad", kind="overtime_window.set", payload=body)
        assert failure.value.code == code


def test_business_and_calendar_source_events_are_known_material_changes_without_automatic_new_case():
    source = initial()
    for kind, payload in [
        ("delivery_rule.set", rule_payload(source)),
        ("overtime_window.set", overtime_payload(source, "resource")),
    ]:
        after = apply(source, kind, payload)
        records = []

        class Writer:
            def add(self, item):
                records.append(item)

        class World:
            factory_id = source.factory_id
            document = source.model_dump(mode="json")

        _write(Writer(), World(), source, after, kind)
        from packages.domain.models import Event

        events = tuple(Event.model_validate(e) for e in records[0].document["events"])
        classification = classify_events(events, after)
        assert classification["material"]
        assert "EVENT_SNAPSHOT_MISMATCH" not in classification["reasons"]
        assert "UNKNOWN_ENTITY_OR_EVENT" not in classification["reasons"]
        assert impact_report(after, events)["classification"] == classification


def test_new_conditions_preserve_started_history_active_plan_and_material_consumption():
    source = initial()
    plan = solve(source, time_limit=2)
    assert plan.checker.status == "PASS"
    source = advance(
        evolve(source, active_plan_version="active", active_plan_hash=plan.content_hash),
        plan,
        minutes=1,
    )
    assert source.actuals
    for kind, payload in [
        ("delivery_rule.set", rule_payload(source)),
        ("expedite_quote.set", quote_payload(source)),
        ("overtime_window.set", overtime_payload(source)),
    ]:
        apply(source, kind, payload)


def overtime_only_source(source=None):
    """Small isolated capacity fixture; never applied to the preview factory."""
    source = source or initial()
    data = source.model_dump(exclude={"content_hash"})
    normal_end = source.snapshot_clock + timedelta(minutes=1)
    for row in (*data["resources"], *data["workers"]):
        row["calendar"] = [
            {"start_at": source.snapshot_clock, "end_at": normal_end, "kind": "NORMAL"}
        ]
    for worker in data["workers"]:
        worker["overtime_available"] = False
    source = Snapshot.model_validate(data)
    start, end = (
        source.snapshot_clock + timedelta(minutes=5),
        source.snapshot_clock + timedelta(minutes=120),
    )
    for target_type, rows in (("resource", source.resources), ("worker", source.workers)):
        for target in rows:
            source = apply(
                source,
                "overtime_window.set",
                {
                    "target_type": target_type,
                    "target_id": getattr(target, f"{target_type}_id"),
                    "expected_version": target.version,
                    "action": "add",
                    "start_at": start.isoformat(),
                    "end_at": end.isoformat(),
                },
            )
    return source


def test_recorded_overtime_increases_solvable_capacity_but_still_requires_manager_approval():
    source = overtime_only_source()
    normal = solve(source, time_limit=2, allow_overtime=False)
    assert normal.native_status == "INFEASIBLE"
    candidate = solve(source, time_limit=2, allow_overtime=True)
    assert candidate.has_solution and candidate.checker.status == "PASS"
    assert "allow_overtime" in candidate.required_consents
    assert source.active_plan_hash is None and source.actuals == ()


def test_recorded_delivery_rule_enables_checked_partial_option_without_discarding_demand():
    from test_business_options import proposed

    source = initial()
    order = proposed(source, quantity=100, due_minutes=75)
    source = inject(source, event_id="customer-order", kind="order.add", payload=order.model_dump())
    source = apply(source, "delivery_rule.set", rule_payload(source))
    request = BusinessStudyRequest(
        kind="urgent_order",
        existing_order_id=order.order_id,
        partial_delivery_allowed=True,
        minimum_partial_quantity=50,
        total_time_limit=5,
    )
    validate_request(source, request)
    before = source.model_dump_json()
    result = evaluate_business_options(source, None, request)
    partial = next(item for item in result.options if item.kind == "partial_delivery")
    assert partial.status == "FEASIBLE" and partial.candidate.checker.status == "PASS"
    assert partial.on_time_quantity == 50
    assert len(partial.deliveries) == 2 and sum(item.quantity for item in partial.deliveries) == 100
    assert partial.protects_existing_commitments and not partial.publishable
    assert source.model_dump_json() == before


@pytest.mark.parametrize("price", [1250, None])
def test_recorded_supplier_quote_enters_checked_comparison_without_receiving_material(price):
    from test_business_options import shortage

    source, _ = shortage()
    data = source.model_dump(exclude={"content_hash"})
    data["schema_version"] = "byof.snapshot/2"
    data["source"].update(source_revision="1", cursor="1")
    source = Snapshot.model_validate(data)
    source = apply(
        source,
        "expedite_quote.set",
        {
            "expected_terms_version": None,
            "quote_id": "RECORDED-QUOTE",
            "receipt_id": "TEST-INBOUND",
            "expected_receipt_version": 1,
            "expedited_eta": (source.snapshot_clock + timedelta(minutes=1)).isoformat(),
            "valid_until": (source.snapshot_clock + timedelta(minutes=30)).isoformat(),
            "source_reference": "test-supplier-confirmation",
            "cost_minor": price,
            "currency": "CNY" if price is not None else None,
        },
    )
    request = BusinessStudyRequest(
        kind="material_shortage",
        receipt_id="TEST-INBOUND",
        expedite_quote_ids=("RECORDED-QUOTE",),
        total_time_limit=5,
    )
    validate_request(source, request)
    before = source.model_dump_json()
    result = evaluate_business_options(
        source, None, request, expedite_quotes=source.business_terms.expedite_quotes
    )
    expedited = next(item for item in result.options if item.kind == "receipt_expedite")
    assert expedited.status == "FEASIBLE" and expedited.candidate.checker.status == "PASS"
    assert expedited.cost_minor == price
    assert expedited.currency == ("CNY" if price is not None else None)
    assert expedited.impacts[0].on_time_quantity == 50
    assert not expedited.publishable and expedited.protects_existing_commitments
    assert source.model_dump_json() == before
