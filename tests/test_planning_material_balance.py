"""A physical material deficit is evidence, not a guess about solver failure."""

from datetime import timedelta

from test_checker import example_snapshot

from packages.agent.planning_context import material_shortfalls
from packages.domain.models import Receipt
from packages.domain.skf import load_skf_snapshot


def test_confirmed_supply_shortfall_is_reported_in_source_units():
    snapshot = example_snapshot(batches=2)
    stock = snapshot.inventory[0].model_copy(update={"on_hand": 2})
    changed = snapshot.model_copy(update={"inventory": (stock,)})

    assert material_shortfalls(changed) == [
        {
            "material_id": "shared-part",
            "material_name": "Shared part",
            "unit": "EA",
            "unstarted_demand": 4,
            "unreserved_on_hand": 2,
            "confirmed_inbound": 0,
            "minimum_shortfall": 2,
        }
    ]


def test_confirmed_receipt_covers_deficit_but_cancelled_receipt_does_not():
    snapshot = example_snapshot(batches=2)
    stock = snapshot.inventory[0].model_copy(update={"on_hand": 2})
    receipt = Receipt(
        receipt_id="late-supply",
        material_id="shared-part",
        unit="EA",
        quantity=2,
        eta=snapshot.snapshot_clock + timedelta(minutes=10),
        status="CONFIRMED",
        version=1,
    )
    covered = snapshot.model_copy(update={"inventory": (stock,), "receipts": (receipt,)})
    assert material_shortfalls(covered) == []
    cancelled = covered.model_copy(
        update={"receipts": (receipt.model_copy(update={"status": "CANCELLED"}),)}
    )
    assert material_shortfalls(cancelled)[0]["minimum_shortfall"] == 2


def test_confirmed_receipt_after_planning_horizon_does_not_hide_shortage():
    snapshot = example_snapshot(batches=2)
    stock = snapshot.inventory[0].model_copy(update={"on_hand": 2})
    receipt = Receipt(
        receipt_id="after-horizon",
        material_id="shared-part",
        unit="EA",
        quantity=2,
        eta=snapshot.horizon.end_at + timedelta(minutes=1),
        status="CONFIRMED",
    )
    changed = snapshot.model_copy(update={"inventory": (stock,), "receipts": (receipt,)})
    assert material_shortfalls(changed)[0]["minimum_shortfall"] == 2


def test_full_workshop_supply_loss_plus_fifty_piece_order_proves_1300_seal_deficit():
    original = load_skf_snapshot()
    order = next(row for row in original.orders if row.product_id == "BRG-6202-2RS1")
    extra = order.model_copy(update={"order_id": "test-1", "quantity": 50})
    receipts = tuple(
        row.model_copy(update={"status": "CANCELLED"}) if row.receipt_id == "INB-005" else row
        for row in original.receipts
    )
    changed = original.model_copy(
        update={"orders": (*original.orders, extra), "receipts": receipts}
    )
    seal = next(
        row for row in material_shortfalls(changed) if row["material_id"] == "SEAL-RS1-6202"
    )
    assert seal["unstarted_demand"] == 3700
    assert seal["unreserved_on_hand"] == 2400
    assert seal["confirmed_inbound"] == 0
    assert seal["minimum_shortfall"] == 1300
