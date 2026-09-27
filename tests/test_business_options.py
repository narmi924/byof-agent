"""Business studies keep promises, complete production and source facts separate.

The small SKF fixture and explicitly changed dates/supply below are synthetic test cases,
not measurements or claims about an SKF plant.
"""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from packages.domain.business_options import BusinessStudyRequest
from packages.domain.business_terms import ExpediteQuote
from packages.domain.demand import materialize_batches
from packages.domain.models import (
    Candidate,
    CheckReport,
    EvidenceIssue,
    Order,
    Snapshot,
    VersionBinding,
    batch_operations,
)
from packages.domain.skf import load_skf_snapshot
from packages.planning import business_options
from packages.planning.business_options import evaluate_business_options
from packages.planning.business_service import validate_request
from packages.planning.checker import check_candidate
from packages.planning.solver import solve
from services.factory_sim.engine import advance, evolve, inject


def test_infeasibility_message_distinguishes_proof_from_an_unfinished_search():
    # A proof and a search that ran out of time read differently, without disclaimers.
    assert "No schedule can keep" in business_options._failure("INFEASIBLE")
    assert "time limit" not in business_options._failure("INFEASIBLE")
    assert "within the time limit" in business_options._failure("UNKNOWN")


def test_portfolio_economics_are_repeatable_and_do_not_use_an_invalid_baseline():
    from packages.domain.economics import PRODUCT_RATES, late_deduction
    from packages.planning.business_economics import estimate, with_economics

    snapshot = load_skf_snapshot(development=True)
    request = BusinessStudyRequest(
        kind="urgent_order", order=proposed(snapshot), total_time_limit=4
    )
    study = evaluate_business_options(snapshot, None, request)
    feasible = next(option for option in study.options if option.status == "FEASIBLE")
    economics = estimate(snapshot, feasible)
    assert economics == estimate(snapshot, feasible)
    assert economics.status == "ESTIMATED" and economics.evidence_mode == "synthetic"
    products = {order.order_id: order.product_id for order in feasible.derived_snapshot.orders}
    assert economics.revenue_minor == sum(
        i.quantity * PRODUCT_RATES[products[i.order_id]][0] for i in feasible.impacts
    )
    assert (
        economics.net_contribution_minor
        == economics.revenue_minor
        - economics.variable_cost_minor
        - economics.additional_cost_minor
        - economics.late_deduction_minor
    )
    assert late_deduction(100_000, 1440) == 1000
    assert late_deduction(100_000, 1440 * 100) == 20_000
    unknown = feasible.model_copy(update={"status": "UNKNOWN", "candidate": None, "kind": "normal"})
    quoted = feasible.model_copy(
        update={
            "kind": "receipt_expedite",
            "quote_id": "missing-price",
            "cost_minor": None,
            "currency": None,
        }
    )
    assert estimate(snapshot, quoted).net_contribution_minor is None
    assert estimate(snapshot, quoted).missing
    compared = with_economics(snapshot, [unknown, feasible.model_copy(update={"kind": "overtime"})])
    assert all(option.economics.improvement_minor is None for option in compared)


def test_expedite_cost_is_counted_once_and_currency_mismatch_remains_unknown():
    from packages.planning.business_economics import estimate

    snapshot = load_skf_snapshot(development=True)
    study = evaluate_business_options(
        snapshot,
        None,
        BusinessStudyRequest(kind="urgent_order", order=proposed(snapshot), total_time_limit=4),
    )
    base = next(option for option in study.options if option.status == "FEASIBLE")
    fee = base.model_copy(
        update={
            "kind": "receipt_expedite",
            "quote_id": "demo-fee",
            "cost_minor": 2500,
            "currency": "SGD",
        }
    )
    before, after = estimate(snapshot, base), estimate(snapshot, fee)
    assert after.net_contribution_minor == before.net_contribution_minor - 2500
    assert after.incremental_cash_outlay_minor == before.incremental_cash_outlay_minor + 2500
    assert after.variable_cost_minor == before.variable_cost_minor
    foreign = fee.model_copy(update={"currency": "CNY"})
    assert estimate(snapshot, foreign).status == "INCOMPLETE"


def proposed(snapshot, *, quantity=50, due_minutes=1000):
    return Order.model_validate(
        {
            **snapshot.orders[0].model_dump(),
            "order_id": "PROPOSED",
            "status": "CONFIRMED",
            "quantity": quantity,
            "priority_weight": 10,
            "due_at": snapshot.snapshot_clock + timedelta(minutes=due_minutes),
        }
    )


def shortage():
    snapshot = load_skf_snapshot(development=True)
    data = snapshot.model_dump(mode="python", exclude={"content_hash"})
    data["orders"][0]["due_at"] = snapshot.snapshot_clock + timedelta(minutes=80)
    material = "IR-6202"
    stock = next(row for row in data["inventory"] if row["material_id"] == material)
    stock["on_hand"] = 0
    data["receipts"] = [row for row in data["receipts"] if row["material_id"] != material]
    data["receipts"].append(
        {
            "receipt_id": "TEST-INBOUND",
            "material_id": material,
            "unit": stock["unit"],
            "quantity": 50,
            "eta": snapshot.snapshot_clock + timedelta(minutes=100),
            "status": "CONFIRMED",
            "version": 1,
        }
    )
    snapshot = Snapshot.model_validate(data)
    quote = ExpediteQuote(
        quote_id="TEST-QUOTE",
        receipt_id="TEST-INBOUND",
        receipt_version=1,
        original_eta=snapshot.snapshot_clock + timedelta(minutes=100),
        expedited_eta=snapshot.snapshot_clock,
        quantity=50,
        valid_until=snapshot.snapshot_clock + timedelta(minutes=60),
        source_reference="synthetic-test-quote",
        evidence_mode="synthetic",
        cost_minor=1250,
        currency="CNY",
    )
    return snapshot, quote


def test_comparison_isolated_checked_and_public_view_cannot_publish():
    snapshot = load_skf_snapshot(development=True)
    untouched = snapshot.model_dump_json()
    request = BusinessStudyRequest(
        kind="urgent_order", order=proposed(snapshot), total_time_limit=4
    )
    study = evaluate_business_options(snapshot, None, request)
    assert snapshot.model_dump_json() == untouched
    assert study.origin_snapshot_hash == snapshot.content_hash
    assert [o.kind for o in study.options] == ["normal", "overtime", "earliest_completion"]
    for option in study.options:
        assert option.status == "FEASIBLE"
        assert not option.publishable
        assert option.protects_existing_commitments
        assert option.candidate.checker.status == "PASS"
        assert option.candidate.binding.snapshot_hash == option.derived_snapshot.content_hash
        assert option.candidate.binding.snapshot_hash != snapshot.content_hash
        assert check_candidate(snapshot, option.candidate).status == "FAIL"
        assert option.requested_quantity == option.on_time_quantity == 50
        assert sum(delivery.quantity for delivery in option.deliveries) == 50
    for option in study.public_view()["options"]:
        assert "candidate" not in option and "derived_snapshot" not in option
        assert option["overtime_unit"] == "worker_minutes"


def test_urgent_priority_cannot_displace_an_existing_due_date():
    original = load_skf_snapshot(development=True)
    data = original.model_dump(mode="python", exclude={"content_hash"})
    data["orders"][0]["due_at"] = original.snapshot_clock + timedelta(minutes=66)
    snapshot = Snapshot.model_validate(data)
    request = BusinessStudyRequest(
        kind="urgent_order", order=proposed(snapshot, due_minutes=66), total_time_limit=4
    )
    study = evaluate_business_options(snapshot, None, request)
    assert study.options[0].status == study.options[1].status == "INFEASIBLE"
    earliest = study.options[2]
    assert earliest.status == "FEASIBLE"
    assert earliest.completion_at > request.order.due_at
    old = next(impact for impact in earliest.impacts if impact.existing_commitment)
    assert old.tardiness_minutes == 0
    assert earliest.derived_snapshot.orders[0].due_at == snapshot.orders[0].due_at
    assert earliest.derived_snapshot.orders[0].hard_deadline


def test_completed_late_history_remains_visible_without_blocking_new_promises():
    initial = load_skf_snapshot(development=True)
    data = initial.model_dump(mode="python", exclude={"content_hash"})
    data["schema_version"] = "byof.snapshot/2"
    data["source"].update(source_revision="1", cursor="1")
    data["orders"][0].update(
        due_at=initial.snapshot_clock + timedelta(minutes=1),
        hard_deadline=False,
    )
    initial = Snapshot.model_validate(data)
    baseline = solve(initial, time_limit=2)
    assert baseline.checker.status == "PASS"
    executing = evolve(
        initial, active_plan_version="late-plan", active_plan_hash=baseline.content_hash
    )
    completed = advance(executing, baseline, minutes=100)
    assert completed.orders[0].status == "COMPLETED"
    assert max(actual.actual_end for actual in completed.actuals) > completed.orders[0].due_at
    source_document = completed.model_dump_json()
    new_order = proposed(completed).model_copy(update={"version": 1})
    study = evaluate_business_options(
        completed,
        baseline,
        BusinessStudyRequest(kind="urgent_order", order=new_order, total_time_limit=6),
    )
    normal = study.options[0]
    assert normal.status == "FEASIBLE", normal.summary
    assert normal.protects_existing_commitments is True
    history = next(impact for impact in normal.impacts if impact.existing_commitment)
    assert history.tardiness_minutes > 0
    assert history.completion_at == max(actual.actual_end for actual in completed.actuals)
    assert normal.derived_snapshot.orders[0] == completed.orders[0]
    assert normal.derived_snapshot.actuals == completed.actuals
    assert normal.candidate.checker.status == "PASS"
    assert any(
        "Unfinished" in assumption and "actual late records" in assumption
        for assumption in normal.assumptions
    )
    assert completed.model_dump_json() == source_document


def test_partial_delivery_keeps_complete_order_and_counts_terminal_operations():
    snapshot = load_skf_snapshot(development=True)
    order = proposed(snapshot, quantity=100, due_minutes=75)
    study = evaluate_business_options(
        snapshot,
        None,
        BusinessStudyRequest(
            kind="urgent_order", order=order, partial_delivery_allowed=True, total_time_limit=5
        ),
    )
    assert study.options[0].status == "INFEASIBLE"
    partial = study.options[-1]
    assert partial.status == "FEASIBLE"
    assert partial.on_time_quantity == 50
    assert partial.requested_quantity == 100
    assert sum(delivery.quantity for delivery in partial.deliveries) == 100
    assert partial.deliveries[0].ready_at == order.due_at
    assert order.due_at < partial.deliveries[1].ready_at == partial.completion_at
    assert partial.maximum_on_time_quantity_proven
    derived = partial.derived_snapshot
    assert next(o for o in derived.orders if o.order_id == order.order_id).quantity == 100
    batches, operations = batch_operations(derived)
    assert sum(b.quantity for b in batches if b.order_id == order.order_id) == 100
    assert len(partial.candidate.assignments) == len(operations)
    selected = [b for b in derived.production_batches if b.order_id == order.order_id]
    assert sum(b.quantity for b in selected if b.delivery_due_at == order.due_at) == 50
    # A completed upstream OP10/OP20 is not treated as a deliverable bearing.
    assignments = {a.operation_id: a for a in partial.candidate.assignments}
    final_ends = [assignments[f"{b.batch_id}-OP80"].end_at for b in selected]
    assert partial.completion_at == max(final_ends)
    assert partial.on_time_quantity == sum(50 for end in final_ends if end <= order.due_at)


def test_earliest_completion_promises_one_full_delivery_despite_earlier_batch_availability():
    snapshot = load_skf_snapshot(development=True)
    order = proposed(snapshot, quantity=100, due_minutes=75)
    study = evaluate_business_options(
        snapshot,
        None,
        BusinessStudyRequest(kind="urgent_order", order=order, total_time_limit=5),
    )
    option = next(option for option in study.options if option.kind == "earliest_completion")
    assert option.status == "FEASIBLE"
    assert option.completion_at > order.due_at
    assert option.on_time_quantity == 50
    assert option.impacts[-1].on_time_quantity == 50
    assert len(option.deliveries) == 1
    assert option.deliveries[0].quantity == order.quantity == 100
    assert option.deliveries[0].ready_at == option.completion_at
    public = option.public_view()
    assert public["on_time_quantity"] == 50
    assert len(public["deliveries"]) == 1 and public["deliveries"][0]["quantity"] == 100


def test_source_ledger_is_preserved_and_new_urgent_batches_are_added():
    source = load_skf_snapshot(development=True)
    data = source.model_dump(exclude={"content_hash"})
    data.update(schema_version="byof.snapshot/3", production_batches=materialize_batches(source))
    snapshot = Snapshot.model_validate(data)
    study = evaluate_business_options(
        snapshot,
        None,
        BusinessStudyRequest(kind="urgent_order", order=proposed(snapshot), total_time_limit=3),
    )
    normal = study.options[0]
    assert normal.status == "FEASIBLE"
    assert normal.derived_snapshot.production_batches[:1] == snapshot.production_batches
    assert (
        sum(
            b.quantity
            for b in normal.derived_snapshot.production_batches
            if b.order_id == "PROPOSED"
        )
        == 50
    )


def test_wip_baseline_history_freeze_and_reserved_material_survive_study():
    data = load_skf_snapshot(development=True).model_dump(exclude={"content_hash"})
    data["schema_version"] = "byof.snapshot/2"
    data["source"]["source_revision"] = "1"
    source = Snapshot.model_validate(data)
    baseline = solve(source, time_limit=2)
    source = evolve(source, active_plan_version="plan-1", active_plan_hash=baseline.content_hash)
    source = advance(source, baseline, minutes=12)
    before = source.model_dump_json()
    study = evaluate_business_options(
        source,
        baseline,
        BusinessStudyRequest(kind="urgent_order", order=proposed(source), total_time_limit=5),
    )
    normal = study.options[0]
    assert normal.status == "FEASIBLE"
    assert source.model_dump_json() == before
    assert normal.derived_snapshot.actuals == source.actuals
    assert normal.derived_snapshot.reservations == source.reservations
    assert normal.derived_snapshot.active_plan_hash == baseline.content_hash
    assert (
        check_candidate(normal.derived_snapshot, normal.candidate, baseline=baseline).status
        == "PASS"
    )


def test_broken_frozen_work_is_replanned_without_relaxing_the_source_outage():
    data = load_skf_snapshot(development=True).model_dump(exclude={"content_hash"})
    data["schema_version"] = "byof.snapshot/2"
    data["source"]["source_revision"] = "1"
    source = Snapshot.model_validate(data)
    baseline = solve(source, time_limit=2)
    source = evolve(source, active_plan_version="plan-1", active_plan_hash=baseline.content_hash)
    frozen = next(a for a in baseline.assignments if a.operation_id.endswith("-OP20"))
    source = inject(
        source,
        event_id="test-outage",
        kind="resource.outage",
        payload={"resource_id": frozen.resource_id, "minutes": 60},
    )
    study = evaluate_business_options(
        source,
        baseline,
        BusinessStudyRequest(kind="urgent_order", order=proposed(source), total_time_limit=3),
    )
    assert all(option.status == "FEASIBLE" for option in study.options)
    for option in study.options:
        report = check_candidate(
            option.derived_snapshot,
            option.candidate,
            baseline=baseline,
            allow_overtime="allow_overtime" in option.candidate.required_consents,
        )
        assert report.status == "PASS", (option.kind, report.issues)
    assert all(
        option.derived_snapshot.profile.policy.freeze_window_min == 60 for option in study.options
    )


def test_wait_diagnostic_exposes_lateness_but_quote_is_checked_without_duplicate_supply():
    snapshot, quote = shortage()
    original = snapshot.model_dump_json()
    request = BusinessStudyRequest(
        kind="material_shortage",
        receipt_id=quote.receipt_id,
        expedite_quote_ids=(quote.quote_id,),
        total_time_limit=4,
    )
    study = evaluate_business_options(snapshot, None, request, expedite_quotes=(quote,))
    shared, overtime, diagnostic, expedited = study.options
    assert shared.status == overtime.status == "INFEASIBLE"
    assert diagnostic.status == "FEASIBLE"
    assert diagnostic.diagnostic_only and not diagnostic.protects_existing_commitments
    assert diagnostic.impacts[0].tardiness_minutes > 0
    assert expedited.status == "FEASIBLE" and expedited.protects_existing_commitments
    assert diagnostic.completion_at == diagnostic.impacts[0].completion_at
    assert expedited.completion_at == expedited.impacts[0].completion_at
    assert expedited.completion_at < diagnostic.completion_at
    assert expedited.cost_minor == 1250 and expedited.currency == "CNY"
    assert expedited.cost_unit == "minor_currency_unit"
    assert expedited.quote_id == quote.quote_id
    assert sum(r.quantity for r in expedited.derived_snapshot.receipts) == sum(
        r.quantity for r in snapshot.receipts
    )
    assert expedited.derived_snapshot.inventory == snapshot.inventory
    assert snapshot.model_dump_json() == original


@pytest.mark.parametrize("change", [{"receipt_version": 2}, {"quantity": 100}, {"expired": True}])
def test_stale_or_mismatched_source_quotes_are_blocked(change):
    snapshot, quote = shortage()
    change = dict(change)
    if change.pop("expired", False):
        change["valid_until"] = snapshot.snapshot_clock
    quote = ExpediteQuote.model_validate({**quote.model_dump(), **change})
    result = evaluate_business_options(
        snapshot,
        None,
        BusinessStudyRequest(
            kind="material_shortage", expedite_quote_ids=(quote.quote_id,), total_time_limit=2
        ),
        expedite_quotes=(quote,),
    )
    assert result.options[-1].status == "BLOCKED"
    assert result.options[-1].candidate is None


def unknown(snapshot):
    return Candidate(
        candidate_id="unknown",
        factory_id=snapshot.factory_id,
        version=1,
        binding=VersionBinding(
            snapshot_hash=snapshot.content_hash,
            planning_revision=snapshot.planning_revision,
            scope_version=snapshot.scope_version,
            profile_version=snapshot.profile.version,
            policy_version=snapshot.profile.policy.policy_version,
            objective_version="delivery-v1",
            baseline_plan_version=snapshot.active_plan_version,
        ),
        native_status="UNKNOWN",
        has_solution=False,
        termination_reason="TIME_LIMIT",
        checker=CheckReport(
            checker_version="not-run", snapshot_hash=snapshot.content_hash, status="NOT_RUN"
        ),
        effective_not_before=snapshot.snapshot_clock,
        accept_before=snapshot.snapshot_clock + timedelta(minutes=1),
    )


def test_unknown_cannot_prove_earliest_or_largest_partial_quantity(monkeypatch):
    snapshot = load_skf_snapshot(development=True)
    monkeypatch.setattr(business_options, "solve", lambda snapshot, **kwargs: unknown(snapshot))
    study = evaluate_business_options(
        snapshot,
        None,
        BusinessStudyRequest(
            kind="urgent_order",
            order=proposed(snapshot, quantity=100),
            partial_delivery_allowed=True,
            total_time_limit=1,
        ),
    )
    assert all(option.status == "UNKNOWN" for option in study.options)
    assert all(not option.earliest_completion_proven for option in study.options)
    assert all(not option.maximum_on_time_quantity_proven for option in study.options)


def test_unknown_probe_retains_checked_offer_without_claiming_earliest(monkeypatch):
    snapshot = load_skf_snapshot(development=True)
    calls = [0]

    def interrupted(snapshot, **kwargs):
        calls[0] += 1
        return solve(snapshot, **kwargs) if calls[0] <= 3 else unknown(snapshot)

    monkeypatch.setattr(business_options, "solve", interrupted)
    result = evaluate_business_options(
        snapshot,
        None,
        BusinessStudyRequest(kind="urgent_order", order=proposed(snapshot), total_time_limit=4),
    )
    option = result.options[-1]
    assert option.status == "FEASIBLE"
    assert not option.earliest_completion_proven
    assert option.searches[-1].native_status == "UNKNOWN"
    assert option.candidate.checker.status == "PASS"
    assert option.candidate.content_hash == option.searches[-2].candidate_hash


def test_independent_check_failure_cannot_be_a_feasible_offer(monkeypatch):
    snapshot = load_skf_snapshot(development=True)

    def reject(snapshot, candidate, **kwargs):
        return CheckReport(
            checker_version="test-independent",
            snapshot_hash=snapshot.content_hash,
            status="FAIL",
            issues=(EvidenceIssue(code="TEST_CONFLICT", message="Conflict"),),
        )

    monkeypatch.setattr(business_options, "check_candidate", reject)
    result = evaluate_business_options(
        snapshot,
        None,
        BusinessStudyRequest(kind="urgent_order", order=proposed(snapshot), total_time_limit=3),
    )
    assert all(option.status == "CHECK_FAILED" for option in result.options)
    assert all(option.protects_existing_commitments is None for option in result.options)


def test_all_options_share_one_deadline_not_one_budget_each(monkeypatch):
    snapshot = load_skf_snapshot(development=True)
    clock = [0.0]
    budgets = []
    monkeypatch.setattr(business_options, "time", SimpleNamespace(perf_counter=lambda: clock[0]))

    def exhaust(snapshot, *, time_limit, **kwargs):
        assert clock[0] < 1
        budgets.append(time_limit)
        clock[0] += time_limit
        return unknown(snapshot)

    monkeypatch.setattr(business_options, "solve", exhaust)
    study = evaluate_business_options(
        snapshot,
        None,
        BusinessStudyRequest(
            kind="urgent_order",
            order=proposed(snapshot, quantity=100),
            partial_delivery_allowed=True,
            total_time_limit=1,
        ),
    )
    assert sum(budgets) <= 1.00001
    assert len(budgets) == 4
    assert study.elapsed_seconds <= 1.00001
    assert all(not option.earliest_completion_proven for option in study.options)


def test_request_rejects_unbounded_budget_untrusted_quote_payload_and_silent_partial():
    with pytest.raises(ValidationError):
        BusinessStudyRequest(kind="material_shortage", total_time_limit=61)
    with pytest.raises(ValidationError):
        BusinessStudyRequest.model_validate({"kind": "material_shortage", "expedite_quotes": []})
    snapshot = load_skf_snapshot(development=True)
    with pytest.raises(ValidationError):
        BusinessStudyRequest(
            kind="urgent_order", order=proposed(snapshot), minimum_partial_quantity=50
        )


def test_expected_receipt_becomes_confirmed_only_in_quote_scenario():
    snapshot, quote = shortage()
    data = snapshot.model_dump(exclude={"content_hash"})
    next(r for r in data["receipts"] if r["receipt_id"] == quote.receipt_id)["status"] = "EXPECTED"
    snapshot = Snapshot.model_validate(data)
    result = evaluate_business_options(
        snapshot,
        None,
        BusinessStudyRequest(
            kind="material_shortage", expedite_quote_ids=(quote.quote_id,), total_time_limit=3
        ),
        expedite_quotes=(quote,),
    )
    option = result.options[-1]
    assert option.status == "FEASIBLE"
    assert (
        next(r for r in snapshot.receipts if r.receipt_id == quote.receipt_id).status == "EXPECTED"
    )
    receipt = next(r for r in option.derived_snapshot.receipts if r.receipt_id == quote.receipt_id)
    assert receipt.status == "CONFIRMED" and receipt.quantity == quote.quantity


def test_demonstrations_are_separate_synthetic_factories_not_seed_edits():
    from packages.domain.business_scenarios import business_scenarios

    original = load_skf_snapshot().content_hash
    scenarios = business_scenarios()
    assert [s.factory_id for s, _ in scenarios] == [
        "business-demand",
        "business-urgent",
        "business-material",
    ]
    assert len({s.run_id for s, _ in scenarios}) == 3
    assert all(s.business_terms.evidence_mode == "synthetic" for s, _ in scenarios)
    assert scenarios[0][0].orders[0].quantity == 150
    assert scenarios[0][1] is None
    assert scenarios[1][1].order.quantity == 100
    assert scenarios[1][1].order.due_at - scenarios[1][0].snapshot_clock == timedelta(minutes=75)
    assert scenarios[2][1].expedite_quote_ids == (
        scenarios[2][0].business_terms.expedite_quotes[0].quote_id,
    )
    assert load_skf_snapshot().content_hash == original


def test_existing_urgent_order_comparison_preserves_source_batches_and_other_commitments():
    original = load_skf_snapshot(development=True)
    target = proposed(original, quantity=100, due_minutes=75).model_copy(
        update={"version": 4, "split_revision": 3}
    )
    data = original.model_dump(exclude={"content_hash"})
    data["schema_version"] = "byof.snapshot/3"
    data["orders"] = (*data["orders"], target)
    source = Snapshot.model_validate(data)
    batches = materialize_batches(source)
    cancelled = batches[-1].model_copy(
        update={"batch_id": f"{target.order_id}-R003-B003", "sequence": 3, "purpose": "CANCELLED"}
    )
    data["production_batches"] = (*batches, cancelled)
    source = Snapshot.model_validate(data)
    before = source.model_dump_json()
    request = BusinessStudyRequest(
        kind="urgent_order",
        existing_order_id=target.order_id,
        partial_delivery_allowed=True,
        total_time_limit=5,
    )
    result = evaluate_business_options(source, None, request)
    assert result.request == request and result.request.order is None
    assert result.options[0].status == result.options[1].status == "INFEASIBLE"
    earliest, partial = result.options[2:]
    assert earliest.status == partial.status == "FEASIBLE"
    assert earliest.completion_at > target.due_at
    assert partial.on_time_quantity == 50
    assert partial.requested_quantity == 100
    assert sum(delivery.quantity for delivery in partial.deliveries) == 100
    for option in (earliest, partial):
        derived = option.derived_snapshot
        assert len(derived.orders) == len(source.orders)
        assert derived.scope_version == source.scope_version
        assert derived.actuals == source.actuals
        assert derived.reservations == source.reservations
        assert [batch.batch_id for batch in derived.production_batches] == [
            batch.batch_id for batch in source.production_batches
        ]
        for old, changed in zip(source.production_batches, derived.production_batches, strict=True):
            assert old.model_dump(exclude={"delivery_due_at"}) == changed.model_dump(
                exclude={"delivery_due_at"}
            )
        derived_target = next(
            order for order in derived.orders if order.order_id == target.order_id
        )
        assert derived_target.version == 4 and derived_target.split_revision == 3
        assert derived_target.quantity == target.quantity
        assert option.protects_existing_commitments is True
        old_impact, target_impact = option.impacts
        assert old_impact.existing_commitment and target_impact.existing_commitment
        assert old_impact.tardiness_minutes == 0
        assert target_impact.requested_due_at == target.due_at
        assert target_impact.tardiness_minutes > 0
        assert derived.orders[0].due_at == source.orders[0].due_at
        assert derived.orders[0].hard_deadline
        assert derived.production_batches[-1] == cancelled
    assert source.model_dump_json() == before


def test_existing_order_normal_and_overtime_use_the_same_source_order_without_additions():
    source = load_skf_snapshot(development=True)
    assert source.business_terms is None
    request = BusinessStudyRequest(
        kind="urgent_order", existing_order_id=source.orders[0].order_id, total_time_limit=3
    )
    validate_request(source, request)
    result = evaluate_business_options(source, None, request)
    assert [option.kind for option in result.options] == [
        "normal",
        "overtime",
        "earliest_completion",
    ]
    for option in result.options[:2]:
        assert option.status == "FEASIBLE"
        assert len(option.derived_snapshot.orders) == len(source.orders)
        assert option.requested_quantity == option.on_time_quantity == source.orders[0].quantity
        assert option.derived_snapshot.production_batches == source.production_batches
        assert len(option.impacts) == len(source.orders)
        assert len(option.deliveries) == 1
        assert option.cost_minor is None and option.currency is None


def test_shortage_without_source_terms_compares_waiting_and_never_fabricates_a_quote():
    source, _unused_quote = shortage()
    assert source.business_terms is None
    before = source.model_dump_json()
    request = BusinessStudyRequest(kind="material_shortage", total_time_limit=3)
    validate_request(source, request)
    result = evaluate_business_options(source, None, request)
    assert [option.kind for option in result.options] == [
        "shared_material",
        "overtime",
        "wait_diagnostic",
    ]
    shared, overtime, diagnostic = result.options
    assert shared.status == overtime.status == "INFEASIBLE"
    assert diagnostic.status == "FEASIBLE"
    assert diagnostic.impacts[0].tardiness_minutes > 0
    for option in result.options:
        assert option.quote_id is None and option.cost_minor is None and option.currency is None
        assert option.derived_snapshot.receipts == source.receipts
        assert option.derived_snapshot.inventory == source.inventory
    assert source.model_dump_json() == before


def test_existing_started_order_returns_blocked_without_recreating_production():
    data = load_skf_snapshot(development=True).model_dump(exclude={"content_hash"})
    data["schema_version"] = "byof.snapshot/2"
    data["source"]["source_revision"] = "1"
    source = Snapshot.model_validate(data)
    baseline = solve(source, time_limit=2)
    source = evolve(source, active_plan_version="plan-1", active_plan_hash=baseline.content_hash)
    source = advance(source, baseline, minutes=15)
    assert source.actuals
    before = source.model_dump_json()
    result = evaluate_business_options(
        source,
        baseline,
        BusinessStudyRequest(
            kind="urgent_order",
            existing_order_id=source.orders[0].order_id,
            partial_delivery_allowed=True,
            total_time_limit=1,
        ),
    )
    assert all(option.status == "BLOCKED" for option in result.options)
    assert all(
        option.candidate is None and option.derived_snapshot is None for option in result.options
    )
    assert all(not option.searches for option in result.options)
    assert source.model_dump_json() == before


def test_existing_batch_delivery_commitments_are_not_overwritten_by_study():
    source = load_skf_snapshot(development=True)
    data = source.model_dump(exclude={"content_hash"})
    data["schema_version"] = "byof.snapshot/3"
    data["production_batches"] = tuple(
        batch.model_copy(update={"delivery_due_at": source.orders[0].due_at})
        for batch in materialize_batches(source)
    )
    source = Snapshot.model_validate(data)
    result = evaluate_business_options(
        source,
        None,
        BusinessStudyRequest(
            kind="urgent_order", existing_order_id=source.orders[0].order_id, total_time_limit=1
        ),
    )
    assert all(option.status == "BLOCKED" for option in result.options)
    assert all(
        "already has a split-delivery commitment" in option.summary for option in result.options
    )


def test_source_order_request_rejects_missing_order_and_earlier_final_date():
    source = load_skf_snapshot(development=True)
    with pytest.raises(ValueError, match="missing from the source"):
        evaluate_business_options(
            source, None, BusinessStudyRequest(kind="urgent_order", existing_order_id="missing")
        )
    with pytest.raises(ValueError, match="cannot precede"):
        evaluate_business_options(
            source,
            None,
            BusinessStudyRequest(
                kind="urgent_order",
                existing_order_id=source.orders[0].order_id,
                final_due_at=source.orders[0].due_at - timedelta(minutes=1),
            ),
        )


def test_existing_urgent_comparison_preserves_other_order_work_in_progress():
    data = load_skf_snapshot(development=True).model_dump(exclude={"content_hash"})
    data["schema_version"] = "byof.snapshot/2"
    data["source"]["source_revision"] = "1"
    source = Snapshot.model_validate(data)
    baseline = solve(source, time_limit=2)
    source = evolve(source, active_plan_version="plan-1", active_plan_hash=baseline.content_hash)
    source = advance(source, baseline, minutes=15)
    assert source.actuals
    target = proposed(source)
    data = source.model_dump(exclude={"content_hash"})
    data["schema_version"] = "byof.snapshot/3"
    data["scope_version"] += 1
    data["orders"] = (*data["orders"], target)
    source = Snapshot.model_validate(data)
    data["production_batches"] = materialize_batches(source)
    source = Snapshot.model_validate(data)
    before = source.model_dump_json()
    result = evaluate_business_options(
        source,
        baseline,
        BusinessStudyRequest(
            kind="urgent_order", existing_order_id=target.order_id, total_time_limit=4
        ),
    )
    normal = result.options[0]
    assert normal.status == "FEASIBLE"
    assert normal.derived_snapshot.actuals == source.actuals
    assert normal.derived_snapshot.reservations == source.reservations
    assert normal.derived_snapshot.production_batches == source.production_batches
    assert normal.derived_snapshot.active_plan_hash == baseline.content_hash
    assert (
        check_candidate(normal.derived_snapshot, normal.candidate, baseline=baseline).status
        == "PASS"
    )
    assert source.model_dump_json() == before
