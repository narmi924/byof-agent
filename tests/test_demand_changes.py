"""Demand edits change commitments, never already executed production or batch identities."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from packages.domain.demand import finished_goods
from packages.domain.models import Candidate, Snapshot, batch_operations
from packages.domain.skf import load_skf_snapshot
from packages.domain.snapshot_delta import apply_delta, make_delta
from packages.planning.checker import check_candidate, check_revalidated_plan
from packages.planning.solver import solve
from services.factory_sim.engine import SimulationError, advance, evolve, inject


def initial(*, quantity=100, setup=0, progress=False):
    raw = load_skf_snapshot(development=True).model_dump(mode="json", exclude={"content_hash"})
    raw["schema_version"] = "byof.snapshot/2"
    raw["source"].update(source_revision="1", cursor="1")
    raw["orders"][0]["quantity"] = quantity
    raw["profile"]["policy"]["first_changeover_min"] = setup
    raw["profile"]["policy"]["progress_revalidation_enabled"] = progress
    for stock in raw["inventory"]:
        stock["on_hand"] *= 10
    snapshot = Snapshot.model_validate(raw)
    plan = solve(snapshot, time_limit=3)
    assert plan.checker.status == "PASS", plan.checker.issues
    return evolve(snapshot, active_plan_version="plan-1", active_plan_hash=plan.content_hash), plan


def change(snapshot, quantity, *, event="change-1", command="order.change", **fields):
    order = snapshot.orders[0]
    if command == "order.revise":
        fields = {
            "due_at": order.due_at,
            "priority_weight": order.priority_weight,
            "hard_deadline": order.hard_deadline,
            **fields,
        }
    return inject(
        snapshot,
        event_id=event,
        kind=command,
        payload={
            "order_id": order.order_id,
            "expected_version": order.version,
            "quantity": quantity,
            **fields,
        },
    )


def test_unstarted_cancellation_is_audited_empty_plan_and_old_plan_cannot_dispatch():
    snapshot, baseline = initial()
    cancelled = change(snapshot, 0)
    assert cancelled.orders[0].status == "CANCELLED"
    assert cancelled.orders[0].quantity == 0
    assert all(batch.purpose == "CANCELLED" for batch in cancelled.production_batches)
    assert all(batch.source_event_id == "change-1" for batch in cancelled.production_batches)
    assert cancelled.scope_version == snapshot.scope_version + 1
    assert batch_operations(cancelled) == ((), ())
    candidate = solve(cancelled, baseline=baseline, time_limit=3)
    assert candidate.empty_demand and candidate.has_solution and not candidate.assignments
    assert candidate.checker.status == "PASS", candidate.checker.issues
    assert check_candidate(cancelled, baseline, baseline=baseline).status == "FAIL"
    later = advance(cancelled, baseline, minutes=100)
    assert later.actuals == ()
    assert later.orders[0].status == "CANCELLED"
    assert later.inventory == snapshot.inventory


def test_increase_after_reduction_never_reuses_cancelled_identity():
    snapshot, _ = initial()
    reduced = change(snapshot, 50)
    increased = change(reduced, 100, event="increase-1")
    assert [batch.sequence for batch in increased.production_batches] == [1, 2, 3]
    assert [batch.purpose for batch in increased.production_batches] == [
        "CUSTOMER",
        "CANCELLED",
        "CUSTOMER",
    ]
    assert increased.production_batches[1] == reduced.production_batches[1]
    assert increased.orders[0].split_revision == snapshot.orders[0].split_revision
    assert snapshot.production_batches is None


@pytest.mark.parametrize("partial", [False, True])
def test_final_due_change_moves_final_lots_but_preserves_earlier_partial_promises(partial):
    from test_business_acceptance import command, source_with_terms

    from packages.domain.business_acceptance import accept_business_option

    source = source_with_terms()
    payload = command(source, include_order=True)
    if not partial:
        payload["first_delivery_quantity"] = payload["order"]["quantity"]
        payload["first_delivery_due_at"] = payload["final_delivery_due_at"]
    accepted = evolve(source, **accept_business_option(source, payload, event_id="accept"))
    order = next(o for o in accepted.orders if o.order_id == "BIZ-URGENT")
    due = source.snapshot_clock + timedelta(minutes=1000)
    # An unstarted whole order may already be late when the customer agrees to defer it.
    before = accepted if partial else advance(accepted, None, minutes=181)
    revised = inject(
        before,
        event_id="defer",
        kind="order.change",
        payload={
            "order_id": order.order_id,
            "expected_version": order.version,
            "quantity": order.quantity,
            "due_at": due,
        },
    )
    lots = [b for b in revised.production_batches if b.order_id == order.order_id]
    assert [b.delivery_due_at for b in lots] == [
        payload["first_delivery_due_at"] if partial else due,
        due,
    ]
    assert revised.actuals == before.actuals
    assert revised.orders[-1].requested_due_at == order.requested_due_at
    plan = solve(revised, time_limit=3)
    assert plan.has_solution and plan.checker.status == "PASS", plan.checker.issues


@pytest.mark.parametrize("setup", [0, 5])
@pytest.mark.parametrize("command", ["order.change", "order.revise"])
def test_started_or_setup_batch_finishes_as_stock_without_losing_execution(setup, command):
    snapshot, baseline = initial(setup=setup)
    started = advance(snapshot, baseline)
    assert len(started.actuals) == 1
    cancelled = change(started, 0, command=command)
    assert cancelled.actuals == started.actuals
    assert cancelled.reservations == started.reservations
    assert cancelled.inventory == started.inventory
    assert [batch.purpose for batch in cancelled.production_batches] == ["STOCK", "CANCELLED"]
    assert finished_goods(cancelled) == ()
    candidate = solve(cancelled, baseline=baseline, time_limit=3)
    assert candidate.checker.status == "PASS", candidate.checker.issues
    assert {assignment.operation_id for assignment in candidate.assignments} == {
        operation.operation_id for operation in batch_operations(cancelled)[1]
    }
    accepted = evolve(
        cancelled, active_plan_version="plan-2", active_plan_hash=candidate.content_hash
    )
    completed = advance(accepted, candidate, minutes=150)
    assert completed.orders[0].status == "CANCELLED"
    stock = finished_goods(completed)
    assert len(stock) == 1 and stock[0].quantity == 50
    assert finished_goods(advance(completed, candidate, minutes=1)) == stock
    assert len(completed.actuals) == len(batch_operations(completed)[1])
    assert all(actual.quality_state == "PASSED" for actual in completed.actuals)
    # Surplus is a separate product lot, never added back to raw-material on_hand.
    consumed = {}
    for actual in completed.actuals:
        for item in actual.consumed:
            consumed[item.material_id] = consumed.get(item.material_id, 0) + item.quantity
    assert all(
        row.on_hand == before.on_hand - consumed.get(row.material_id, 0)
        for row, before in zip(completed.inventory, snapshot.inventory)
    )


@pytest.mark.parametrize("command", ["order.change", "order.revise"])
def test_completed_failed_quality_is_not_free_finished_stock(command):
    snapshot, baseline = initial(quantity=50)
    completed = advance(snapshot, baseline, minutes=100)
    cancelled = change(completed, 0, command=command)
    assert finished_goods(cancelled)
    raw = cancelled.model_dump(mode="python", exclude={"content_hash"})
    raw["actuals"][0]["quality_state"] = "FAILED"
    assert finished_goods(Snapshot.model_validate(raw)) == ()


@pytest.mark.parametrize("quality_state", ["FAILED", "UNKNOWN"])
def test_reopened_demand_does_not_relabel_unqualified_completed_stock(quality_state):
    snapshot, baseline = initial(quantity=50)
    cancelled = change(advance(snapshot, baseline, minutes=100), 0)
    rejected = inject(
        cancelled,
        event_id="quality-result",
        kind="quality.record",
        payload={
            "operation_id": cancelled.actuals[-1].operation_id,
            "quality_state": quality_state,
        },
    )
    assert finished_goods(rejected) == ()
    original_batch = rejected.production_batches[0]
    reopened = change(rejected, 50, event="reopen-demand")
    assert reopened.production_batches[0] == original_batch
    assert reopened.production_batches[0].purpose == "STOCK"
    replacement = reopened.production_batches[1]
    assert replacement.purpose == "CUSTOMER" and replacement.quantity == 50
    assert replacement.sequence == original_batch.sequence + 1
    assert replacement.batch_id != original_batch.batch_id
    assert replacement.source_event_id == "reopen-demand"
    assert reopened.orders[0].status == "CONFIRMED"
    assert reopened.actuals == rejected.actuals
    assert reopened.reservations == rejected.reservations
    assert reopened.inventory == rejected.inventory
    assert finished_goods(reopened) == ()


def test_failed_batch_can_be_scrapped_with_history_and_replacement_plan():
    snapshot, baseline = initial(quantity=50, progress=True)
    completed = advance(snapshot, baseline, minutes=100)
    gate = next(
        actual
        for actual in completed.actuals
        if any(
            step.product_id == completed.orders[0].product_id
            and step.operation_code in actual.operation_id
            and step.quality_gate
            for step in completed.profile.routes
        )
    )
    failed = inject(
        completed,
        event_id="inspection-failed",
        kind="quality.record",
        payload={"operation_id": gate.operation_id, "quality_state": "FAILED"},
    )
    with pytest.raises(SimulationError, match="QUALITY_EVIDENCE_REQUIRED"):
        inject(
            failed,
            event_id="unsupported-pass",
            kind="quality.record",
            payload={"operation_id": gate.operation_id, "quality_state": "PASSED"},
        )
    disposed = inject(
        failed,
        event_id="scrap-confirmed",
        kind="quality.scrap",
        payload={"operation_id": gate.operation_id, "reason": "Recheck confirmed not repairable"},
    )
    assert [batch.purpose for batch in disposed.production_batches] == ["SCRAP", "CUSTOMER"]
    assert disposed.orders[0].quantity == 50
    assert disposed.orders[0].status == "CONFIRMED"
    assert disposed.actuals == failed.actuals
    assert disposed.inventory == failed.inventory
    with pytest.raises(SimulationError, match="BATCH_ALREADY_DISPOSED"):
        inject(
            disposed,
            event_id="late-pass",
            kind="quality.record",
            payload={
                "operation_id": gate.operation_id,
                "quality_state": "PASSED",
                "evidence": "Wrong late recheck record",
            },
        )
    assert gate.operation_id not in {
        operation.operation_id for operation in batch_operations(disposed)[1]
    }
    replacement = solve(
        disposed,
        baseline=baseline,
        time_limit=3,
        new_actions_not_before=disposed.snapshot_clock + timedelta(minutes=15),
    )
    assert replacement.has_solution
    assert replacement.checker.status == "PASS", replacement.checker.issues
    assert all(
        assignment.operation_id.startswith(disposed.production_batches[1].batch_id)
        for assignment in replacement.assignments
    )
    reviewed = advance(disposed, baseline, minutes=1)
    report = check_revalidated_plan(disposed, reviewed, replacement, baseline=baseline).report
    assert report.status == "PASS", report.issues
    executing = evolve(
        reviewed,
        active_plan_version="plan-2",
        active_plan_hash=replacement.content_hash,
    )
    later = advance(executing, replacement, minutes=30)
    assert any(
        actual.batch_id == disposed.production_batches[1].batch_id for actual in later.actuals
    )
    assert all(
        actual.operation_id
        in {
            operation.operation_id
            for operation in batch_operations(later, include_cancelled=True)[1]
        }
        for actual in later.actuals
    )
    assert next(
        actual for actual in later.actuals if actual.operation_id == gate.operation_id
    ) == next(actual for actual in disposed.actuals if actual.operation_id == gate.operation_id)


@pytest.mark.parametrize("command", ["order.change", "order.revise"])
def test_reopened_demand_can_reuse_qualified_completed_stock(command):
    snapshot, baseline = initial(quantity=50)
    cancelled = change(advance(snapshot, baseline, minutes=100), 0, command=command)
    assert sum(lot.quantity for lot in finished_goods(cancelled)) == 50
    reopened = change(cancelled, 50, event="reuse-qualified-stock", command=command)
    assert len(reopened.production_batches) == 1
    assert reopened.production_batches[0].batch_id == cancelled.production_batches[0].batch_id
    assert reopened.production_batches[0].purpose == "CUSTOMER"
    assert reopened.orders[0].status == "COMPLETED"
    assert reopened.actuals == cancelled.actuals
    withdrawn_again = change(reopened, 0, event="withdraw-restored-order", command=command)
    assert withdrawn_again.actuals == reopened.actuals
    assert sum(lot.quantity for lot in finished_goods(withdrawn_again)) == 50


def test_reduced_completed_demand_keeps_surplus_out_of_customer_deadline():
    snapshot, baseline = initial()
    completed = advance(snapshot, baseline, minutes=150)
    first_batch = batch_operations(completed)[0][0].batch_id
    customer_end = max(
        actual.actual_end for actual in completed.actuals if actual.batch_id == first_batch
    )
    reduced = change(completed, 50, due_at=customer_end)
    raw = reduced.model_dump(mode="python", exclude={"content_hash"})
    raw["orders"][0]["hard_deadline"] = True
    reduced = Snapshot.model_validate(raw)
    candidate = solve(reduced, baseline=baseline, time_limit=3)
    assert candidate.checker.status == "PASS", candidate.checker.issues
    assert candidate.objective[0].value == 0
    assert reduced.orders[0].status == "COMPLETED"
    assert reduced.actuals == completed.actuals
    assert sum(lot.quantity for lot in finished_goods(reduced)) == 50


def test_demand_delta_roundtrip_and_legacy_hash_are_preserved():
    snapshot, _ = initial()
    document = snapshot.model_dump(mode="json")
    assert "production_batches" not in document and "business_terms" not in document
    assert Snapshot.model_validate(document).content_hash == snapshot.content_hash
    revised = change(snapshot, 50)
    delta = make_delta(snapshot, revised)
    assert delta["set"]["production_batches"]
    assert apply_delta(snapshot, delta) == revised
    later = change(revised, 100, event="increase")
    assert apply_delta(revised, make_delta(revised, later)) == later


def test_quantity_change_revalidates_version_and_lot_quantity():
    snapshot, _ = initial()
    with pytest.raises(SimulationError, match="ORDER_VERSION_CHANGED"):
        inject(
            snapshot,
            event_id="stale",
            kind="order.change",
            payload={
                "order_id": snapshot.orders[0].order_id,
                "quantity": 50,
                "expected_version": 999,
            },
        )
    with pytest.raises(SimulationError, match="UNSUPPORTED_BATCH_QUANTITY"):
        change(snapshot, 51)
    with pytest.raises(ValidationError):
        change(snapshot, -1)


def test_legacy_zero_and_hidden_v3_ledger_are_rejected():
    snapshot, _ = initial()
    raw = snapshot.model_dump(mode="python", exclude={"content_hash"})
    raw["orders"][0].update(quantity=0, status="CANCELLED")
    with pytest.raises(ValidationError, match="VERSION_MISMATCH"):
        Snapshot.model_validate(raw)
    raw = snapshot.model_dump(mode="python", exclude={"content_hash"})
    raw["orders"][0]["requested_due_at"] = snapshot.orders[0].due_at
    with pytest.raises(ValidationError, match="VERSION_MISMATCH"):
        Snapshot.model_validate(raw)
    raw = change(snapshot, 0).model_dump(mode="python", exclude={"content_hash"})
    raw["schema_version"] = "byof.snapshot/2"
    with pytest.raises(ValidationError, match="VERSION_MISMATCH"):
        Snapshot.model_validate(raw)


def test_started_batch_cannot_be_relabelled_cancelled_even_if_order_is_zero():
    snapshot, baseline = initial()
    cancelled = change(advance(snapshot, baseline), 0)
    raw = cancelled.model_dump(mode="python", exclude={"content_hash"})
    raw["production_batches"][0]["purpose"] = "CANCELLED"
    with pytest.raises(ValidationError, match="Executed batches"):
        Snapshot.model_validate(raw)


def test_batch_delivery_commitment_is_enforced_by_solver_and_independent_checker():
    snapshot, baseline = initial()
    revised = change(snapshot, 100)
    raw = revised.model_dump(mode="python", exclude={"content_hash"})
    raw["production_batches"][0]["delivery_due_at"] = revised.snapshot_clock + timedelta(minutes=1)
    tight = Snapshot.model_validate(raw)
    infeasible = solve(tight, baseline=baseline, time_limit=3)
    assert not infeasible.has_solution
    candidate = solve(revised, baseline=baseline, time_limit=3)
    document = candidate.model_dump(mode="python", exclude={"content_hash"})
    document["binding"]["snapshot_hash"] = tight.content_hash
    document["checker"]["snapshot_hash"] = tight.content_hash
    rebound = Candidate.model_validate(document)
    result = check_candidate(tight, rebound, baseline=baseline)
    assert "BATCH_DELIVERY_DEADLINE" in {issue.code for issue in result.issues}


@pytest.mark.parametrize("started", [False, True])
def test_cancelled_scope_keeps_progress_review_valid_while_old_plan_runs(started):
    from test_progress_evidence import batch

    from packages.planning.disruption import recovery_operations
    from packages.planning.execution_projection import project_execution
    from packages.planning.progress_evidence import validate_progress_chain

    snapshot, baseline = initial()
    if started:
        snapshot = advance(snapshot, baseline)
    revised = change(snapshot, 0, command="order.revise")
    candidate = solve(
        revised,
        baseline=baseline,
        time_limit=3,
        new_actions_not_before=revised.snapshot_clock + timedelta(minutes=15),
    )
    assert candidate.has_solution and candidate.checker.status == "PASS"
    current = advance(revised, baseline)
    assert recovery_operations(current, baseline) == set()
    assignments = project_execution(revised, current, candidate, baseline=baseline)
    assert {a.operation_id for a in assignments} == {
        o.operation_id for o in batch_operations(revised)[1]
    }
    evidence = validate_progress_chain(
        revised, current, [batch(revised, current)], baseline=baseline, candidate=candidate
    )
    assert len(evidence) == 64
    assert current.orders[0].status == "CANCELLED"
    assert bool(assignments) == started
