"""Separate synthetic business demonstrations; original SKF seed records stay untouched.

Routes, machinery and skills come from the small SKF reference fixture. Quantities,
deadlines, the material shortage and its priced expedite offer are explicit scenarios,
not observations from a real factory. Each has its own factory/run identity.
"""

from datetime import timedelta

from packages.domain.business_options import BusinessStudyRequest
from packages.domain.business_terms import BusinessTerms, DeliveryRule, ExpediteQuote
from packages.domain.models import Order, Snapshot
from packages.domain.skf import load_skf_snapshot


def _base(factory_id: str) -> dict:
    source = load_skf_snapshot(development=True)
    data = source.model_dump(mode="python", exclude={"content_hash"})
    data.update(
        schema_version="byof.snapshot/3",
        factory_id=factory_id,
        snapshot_id=f"{factory_id}-initial-snapshot",
        run_id=f"{factory_id}-initial-run",
    )
    data["profile"]["factory_id"] = factory_id
    data["source"]["source_revision"] = "1"
    data["business_terms"] = BusinessTerms(
        version="synthetic-business-options-v1",
        evidence_mode="synthetic",
        delivery_rules=(
            DeliveryRule(
                product_id=source.orders[0].product_id,
                partial_delivery_allowed=True,
                minimum_partial_quantity=50,
                max_deliveries=2,
            ),
        ),
    )
    return data


def business_scenarios() -> list[tuple[Snapshot, BusinessStudyRequest | None]]:
    """Return independent initial facts and optional read-only study requests.

    Demand starts with 150 unstarted pieces, allowing an explicit later revision to
    50 pieces (including after production starts). Urgent proposes 100 pieces due
    after 75 minutes. Material requests a source-quoted, priced early receipt.
    These initial facts do not include a plan; applying changes and approvals is
    the responsibility of the normal source and planning services.
    """
    demand = _base("business-demand")
    demand["orders"][0]["quantity"] = 150
    urgent = _base("business-urgent")
    urgent_snapshot = Snapshot.model_validate(urgent)
    urgent_order = Order.model_validate(
        {
            **urgent_snapshot.orders[0].model_dump(),
            "order_id": "URGENT-PROPOSED",
            "quantity": 100,
            "priority_weight": 10,
            "due_at": urgent_snapshot.snapshot_clock + timedelta(minutes=75),
            "hard_deadline": True,
        }
    )
    urgent_request = BusinessStudyRequest(
        kind="urgent_order",
        order=urgent_order,
        partial_delivery_allowed=True,
        minimum_partial_quantity=50,
        total_time_limit=15,
    )
    material = _base("business-material")
    clock = material["snapshot_clock"]
    material["orders"][0]["due_at"] = clock + timedelta(minutes=80)
    for stock in material["inventory"]:
        if stock["material_id"] == "IR-6202":
            stock["on_hand"] = 0
    material["receipts"] = [
        receipt for receipt in material["receipts"] if receipt["material_id"] != "IR-6202"
    ]
    receipt_id, quote_id = "BUSINESS-IR6202-INBOUND", "BUSINESS-IR6202-EXPEDITE"
    material["receipts"].append(
        {
            "receipt_id": receipt_id,
            "material_id": "IR-6202",
            "unit": "EA",
            "quantity": 50,
            "eta": clock + timedelta(minutes=100),
            "status": "CONFIRMED",
            "version": 1,
        }
    )
    terms = BusinessTerms.model_validate(material["business_terms"])
    material["business_terms"] = BusinessTerms(
        **terms.model_dump(exclude={"expedite_quotes"}),
        expedite_quotes=(
            ExpediteQuote(
                quote_id=quote_id,
                receipt_id=receipt_id,
                receipt_version=1,
                original_eta=clock + timedelta(minutes=100),
                expedited_eta=clock + timedelta(minutes=10),
                quantity=50,
                valid_until=clock + timedelta(minutes=60),
                source_reference="business-material:synthetic-quote-v1",
                evidence_mode="synthetic",
                cost_minor=1250,
                currency="CNY",
            ),
        ),
    )
    material_request = BusinessStudyRequest(
        kind="material_shortage",
        receipt_id=receipt_id,
        expedite_quote_ids=(quote_id,),
        total_time_limit=15,
    )
    return [
        (Snapshot.model_validate(demand), None),
        (urgent_snapshot, urgent_request),
        (Snapshot.model_validate(material), material_request),
    ]
