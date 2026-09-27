"""Priced hypothetical changes preserve facts and qualifications until approval."""

from datetime import timedelta

import pytest

from packages.domain.business_options import BusinessStudyRequest
from packages.domain.models import Snapshot
from packages.domain.skf import load_skf_snapshot
from packages.domain.treatment import TreatmentAction, action_cost, project_treatment
from packages.planning.business_options import evaluate_business_options
from services.factory_sim.engine import advance, evolve, inject


def test_immediate_and_scheduled_supply_have_distinct_cash_and_inventory_effects():
    source = fixture_snapshot()
    stock = source.inventory[0]
    base = dict(
        kind="supply",
        target_id=stock.material_id,
        quantity=50,
        action_id="test-purchase",
        expected_version=stock.version,
    )
    immediate = TreatmentAction(**base, mode="immediate", ready_at=source.snapshot_clock)
    future = TreatmentAction(
        **base, mode="standard", ready_at=source.snapshot_clock + timedelta(minutes=135)
    )
    now = project_treatment(source, (immediate,), "test-immediate")
    later = project_treatment(source, (future,), "test-scheduled")
    assert now.inventory[0].on_hand == stock.on_hand + 50
    assert later.inventory[0].on_hand == stock.on_hand
    assert now.receipts[-1].status == "RECEIVED" and later.receipts[-1].status == "CONFIRMED"
    assert action_cost(immediate)[0] > action_cost(future)[0]
    assert action_cost(immediate)[1] > action_cost(immediate)[0]
    with pytest.raises(ValueError, match="already exists|changed"):
        project_treatment(now, (immediate,), "duplicate")
    assert source.inventory[0] == stock
    arrived = advance(later, None, minutes=135)
    assert arrived.receipts[-1].status == "RECEIVED"
    assert arrived.inventory[0].on_hand == stock.on_hand + 50
    after = advance(arrived, None, minutes=1)
    assert after.inventory[0].on_hand == arrived.inventory[0].on_hand


def test_replacement_does_not_mark_absent_person_present_or_invent_skills():
    source = fixture_snapshot()
    worker = source.workers[0]
    absent = inject(
        source, kind="worker.absent", payload={"worker_id": worker.worker_id}, event_id="absence"
    )
    action = TreatmentAction(
        kind="staff",
        target_id=worker.worker_id,
        action_id="test-agency",
        expected_version=absent.workers[0].version,
        ready_at=source.snapshot_clock + timedelta(minutes=45),
    )
    result = project_treatment(absent, (action,), "test-cover")
    assert result.workers[0].status == "ABSENT"
    assert result.workers[-1].skills == worker.skills
    assert result.workers[-1].unavailable[0].end_at == action.ready_at


@pytest.mark.parametrize("named", ["worker", "operation"])
def test_cover_takes_over_the_work_an_absent_person_had_to_stop(named):
    from test_solver_wip import initial

    from packages.planning.business_service import validate_request

    source, baseline = initial()
    source = advance(source, baseline, minutes=12)
    running = next(a for a in source.actuals if a.state == "IN_PROGRESS")
    gone = inject(
        source, event_id="gone", kind="worker.absent", payload={"worker_id": running.worker_id}
    )
    confirmed = inject(
        gone,
        event_id="remaining",
        kind="execution.confirm_remaining",
        payload={
            "operation_id": running.operation_id,
            "remaining_minutes": 5,
            "remaining_setup_minutes": 0,
        },
    )
    # The interrupted operation may name the disruption as well as the absent person.
    request = BusinessStudyRequest(
        kind="production_exception",
        subject_id=running.worker_id if named == "worker" else running.operation_id,
        total_time_limit=20,
    )
    validate_request(confirmed, request)
    study = evaluate_business_options(confirmed, baseline, request)
    cover = next(
        o
        for o in study.options
        if o.status == "FEASIBLE" and any(a.kind == "staff" for a in o.actions)
    )
    staff = next(a for a in cover.actions if a.kind == "staff")
    handed = next(
        a for a in cover.derived_snapshot.actuals if a.operation_id == running.operation_id
    )
    assert (
        handed.worker_id == staff.action_id
        and handed.segments
        == next(a for a in confirmed.actuals if a.operation_id == running.operation_id).segments
    )
    resumed = next(a for a in cover.candidate.assignments if a.operation_id == running.operation_id)
    assert resumed.worker_id == staff.action_id and resumed.resume_at >= staff.ready_at


def test_repair_preserves_equipment_capability_and_requires_time():
    source = fixture_snapshot()
    resource = source.resources[0]
    broken = inject(
        source,
        kind="resource.down",
        payload={"resource_id": resource.resource_id},
        event_id="outage",
    )
    action = TreatmentAction(
        kind="repair",
        target_id=resource.resource_id,
        action_id="test-repair",
        expected_version=broken.resources[0].version,
        ready_at=source.snapshot_clock + timedelta(minutes=75),
    )
    result = project_treatment(broken, (action,), "test-fixed")
    assert result.resources[0].operation_codes == resource.operation_codes
    assert result.resources[0].unavailable[-1].end_at == action.ready_at
    with pytest.raises(ValueError, match="sixty"):
        project_treatment(
            broken, (action.model_copy(update={"ready_at": source.snapshot_clock}),), "too-fast"
        )


def test_true_shortage_produces_priced_supply_actions_and_checked_options():
    source = fixture_snapshot()
    material = source.inventory[0].material_id
    short = evolve(
        source,
        inventory=tuple(
            s.model_copy(update={"on_hand": 0}) if s.material_id == material else s
            for s in source.inventory
        ),
        receipts=tuple(r for r in source.receipts if r.material_id != material),
    )
    study = evaluate_business_options(
        short, None, BusinessStudyRequest(kind="production_exception", total_time_limit=6)
    )
    supply = [o for o in study.options if any(a.kind == "supply" for a in o.actions)]
    assert {a.mode for option in supply for a in option.actions if a.kind == "supply"} == {
        "standard",
        "express",
        "immediate",
    }
    assert any(o.status == "FEASIBLE" and o.economics.status == "ESTIMATED" for o in supply)
    assert short.inventory[0].on_hand == 0


def fixture_snapshot():
    original = load_skf_snapshot(development=True)
    data = original.model_dump(mode="python", exclude={"content_hash"})
    data["source"]["source_revision"] = "0"
    return Snapshot.model_validate(data)


def test_ranking_respects_cash_ceiling_and_exposes_financial_sensitivity():
    from packages.planning.business_economics import rank_treatments

    source = fixture_snapshot()
    study = evaluate_business_options(
        source, None, BusinessStudyRequest(kind="production_exception", total_time_limit=3)
    )
    feasible = next(option for option in study.options if option.status == "FEASIBLE")
    economics = feasible.economics
    assert economics.adverse.net_contribution_minor <= economics.net_contribution_minor
    assert "not rescheduled" in economics.adverse.basis
    expensive = feasible.model_copy(
        update={
            "option_id": "expensive",
            "economics": economics.model_copy(
                update={"net_contribution_minor": 90000, "incremental_cash_outlay_minor": 50000}
            ),
        }
    )
    cheap = feasible.model_copy(
        update={
            "option_id": "cheap",
            "economics": economics.model_copy(
                update={"net_contribution_minor": 70000, "incremental_cash_outlay_minor": 20000}
            ),
        }
    )
    assert (
        rank_treatments([cheap, expensive], BusinessStudyRequest(kind="production_exception"))[
            0
        ].option_id
        == "expensive"
    )
    assert (
        rank_treatments(
            [expensive, cheap],
            BusinessStudyRequest(kind="production_exception", economic_priority="cash"),
        )[0].option_id
        == "cheap"
    )
    limited = rank_treatments(
        [expensive, cheap],
        BusinessStudyRequest(kind="production_exception", max_cash_outlay_minor=25000),
    )
    assert limited[0].option_id == "cheap" and limited[1].status == "BLOCKED"
    worse = cheap.model_copy(
        update={
            "option_id": "worse",
            "economics": cheap.economics.model_copy(update={"net_contribution_minor": 60000}),
        }
    )
    ranked = rank_treatments([worse, cheap], BusinessStudyRequest(kind="production_exception"))
    assert ranked[0].option_id == "cheap" and not ranked[0].dominated
    assert ranked[1].dominated


def test_all_workshop_materials_have_explicit_simulation_rates():
    from packages.domain.treatment import material_rate
    from scripts.setup_team import workshop_snapshot

    assert all(
        material_rate(material.material_id) is not None
        for material in workshop_snapshot().profile.materials
    )


def test_due_extension_applies_to_an_order_already_in_production():
    snapshot = load_skf_snapshot(development=True)
    order = snapshot.orders[0]
    data = snapshot.model_dump(mode="json", exclude={"content_hash"})
    data["orders"][0]["status"] = "IN_PROGRESS"
    started = Snapshot.model_validate(data)
    later = order.due_at + timedelta(days=1)
    due = TreatmentAction(
        kind="order_due",
        target_id=order.order_id,
        action_id="due-in-production",
        expected_version=order.version,
        ready_at=later,
    )
    projected = project_treatment(started, (due,), "due-in-production")
    assert projected.orders[0].due_at == later
    assert projected.orders[0].status == "IN_PROGRESS"
    fewer = TreatmentAction(
        kind="order_quantity",
        target_id=order.order_id,
        action_id="fewer-in-production",
        expected_version=order.version,
        ready_at=started.snapshot_clock,
        quantity=max(1, order.quantity - 50),
    )
    with pytest.raises(ValueError):
        project_treatment(started, (fewer,), "fewer-in-production")


def test_delayed_receipt_offers_buying_again_and_a_date_for_the_earliest_affected_order():
    source = fixture_snapshot()
    used = {b.material_id for b in source.profile.bom}
    receipt = next(
        r
        for r in source.receipts
        if r.material_id in used and r.status in {"CONFIRMED", "EXPECTED"}
    )
    later = source.horizon.end_at - timedelta(hours=2)
    if later <= receipt.eta:
        pytest.skip("fixture receipt already arrives at the end of the horizon")
    delayed = inject(
        source,
        event_id="delay",
        kind="receipt.delay",
        payload={"receipt_id": receipt.receipt_id, "eta": later.isoformat()},
    )
    study = evaluate_business_options(
        delayed,
        None,
        BusinessStudyRequest(
            kind="production_exception", subject_id=receipt.receipt_id, total_time_limit=6
        ),
    )
    supplied = {
        (a.target_id, a.mode) for o in study.options for a in o.actions if a.kind == "supply"
    }
    assert {
        (receipt.material_id, mode) for mode in ("standard", "express", "immediate")
    } <= supplied
    # Buying again keeps every promise, so no customer date is put up for negotiation.
    assert any(
        o.status == "FEASIBLE" and not {a.kind for a in o.actions} & {"order_due", "order_quantity"}
        for o in study.options
    )
    assert not any(a.kind == "order_due" for o in study.options for a in o.actions)


def test_a_date_no_measure_can_keep_is_proposed_while_other_promises_hold():
    source = fixture_snapshot()
    order = min((o for o in source.orders if o.status == "CONFIRMED"), key=lambda o: o.due_at)
    rushed = inject(
        source,
        event_id="rush",
        kind="order.revise",
        payload={
            "order_id": order.order_id,
            "expected_version": order.version,
            "quantity": order.quantity,
            "due_at": (source.snapshot_clock + timedelta(hours=1)).isoformat(),
            "priority_weight": order.priority_weight,
            "hard_deadline": False,
        },
    )
    requested = next(o for o in rushed.orders if o.order_id == order.order_id).due_at
    study = evaluate_business_options(
        rushed,
        None,
        BusinessStudyRequest(
            kind="production_exception", existing_order_id=order.order_id, total_time_limit=20
        ),
    )
    # Nothing delivers the whole order within the hour, yet the manager still gets a plan.
    assert not any(
        o.status == "FEASIBLE" and not {a.kind for a in o.actions} & {"order_due", "order_quantity"}
        for o in study.options
    )
    dated = [
        o
        for o in study.options
        if o.status == "FEASIBLE" and any(a.kind == "order_due" for a in o.actions)
    ]
    assert dated
    for option in dated:
        change = next(a for a in option.actions if a.kind == "order_due")
        impact = next(i for i in option.impacts if i.order_id == order.order_id)
        assert change.target_id == order.order_id
        assert requested < impact.completion_at <= change.ready_at
        assert change.ready_at.minute == 0 and option.deliveries[0].ready_at == change.ready_at
        assert all(
            i.tardiness_minutes == 0
            for i in option.impacts
            if i.order_id != order.order_id and i.existing_commitment
        )
        project_treatment(rushed, option.actions, "agreed-date")


@pytest.mark.parametrize("named", [False, True])
def test_a_firm_date_no_measure_can_keep_can_still_be_negotiated(named):
    source = fixture_snapshot()
    order = min((o for o in source.orders if o.status == "CONFIRMED"), key=lambda o: o.due_at)
    rushed = inject(
        source,
        event_id="firm-rush",
        kind="order.revise",
        payload={
            "order_id": order.order_id,
            "expected_version": order.version,
            "quantity": order.quantity,
            "due_at": (source.snapshot_clock + timedelta(hours=1)).isoformat(),
            "priority_weight": order.priority_weight,
            "hard_deadline": True,
        },
    )
    requested = next(o for o in rushed.orders if o.order_id == order.order_id).due_at
    study = evaluate_business_options(
        rushed,
        None,
        BusinessStudyRequest(
            kind="production_exception",
            existing_order_id=order.order_id if named else None,
            total_time_limit=20,
        ),
    )
    # The customer called the date firm, but asking for a later one is still a plan to offer.
    dated = [
        (o, a)
        for o in study.options
        if o.status == "FEASIBLE"
        for a in o.actions
        if a.kind == "order_due" and a.target_id == order.order_id
    ]
    assert dated
    for option, change in dated:
        impact = next(i for i in option.impacts if i.order_id == order.order_id)
        assert requested < impact.completion_at <= change.ready_at
        project_treatment(rushed, option.actions, "agreed-firm-date")


def test_an_approved_date_extends_the_delivery_scope_of_execution():
    from packages.agent.treatment_execution import _within_scope
    from packages.auth import AccessError

    source = fixture_snapshot()
    study = evaluate_business_options(
        source, None, BusinessStudyRequest(kind="production_exception", total_time_limit=3)
    )
    option = next(o for o in study.options if o.status == "FEASIBLE")
    impact = max(option.impacts, key=lambda i: i.completion_at)
    # A schedule finishing an hour after the checked plan exceeds the approved scope ...
    earlier = option.model_copy(
        update={
            "impacts": tuple(
                i.model_copy(
                    update={
                        "completion_at": i.completion_at - timedelta(hours=1),
                        "requested_due_at": i.completion_at - timedelta(hours=1),
                    }
                )
                if i.order_id == impact.order_id
                else i
                for i in option.impacts
            )
        }
    )
    with pytest.raises(AccessError, match="exceed the approved delivery impact"):
        _within_scope(option.derived_snapshot, option.candidate, earlier)
    # ... unless the customer agreed to a date that covers it.
    agreed = earlier.model_copy(
        update={
            "actions": (
                TreatmentAction(
                    kind="order_due",
                    target_id=impact.order_id,
                    action_id="agreed",
                    expected_version=1,
                    ready_at=impact.completion_at,
                ),
            )
        }
    )
    _within_scope(option.derived_snapshot, option.candidate, agreed)


def test_a_reduced_order_never_dominates_a_full_delivery():
    from packages.planning.business_economics import rank_treatments

    source = fixture_snapshot()
    study = evaluate_business_options(
        source, None, BusinessStudyRequest(kind="production_exception", total_time_limit=3)
    )
    full = next(option for option in study.options if option.status == "FEASIBLE")
    order = source.orders[0].model_copy(update={"quantity": 100})
    source = evolve(source, orders=(order, *source.orders[1:]))
    reduced = full.model_copy(
        update={
            "option_id": "reduced",
            "actions": (
                TreatmentAction(
                    kind="order_quantity",
                    target_id=order.order_id,
                    action_id="fewer",
                    expected_version=order.version,
                    ready_at=source.snapshot_clock,
                    quantity=order.quantity - 50,
                ),
            ),
            "economics": full.economics.model_copy(
                update={"net_contribution_minor": full.economics.net_contribution_minor + 1}
            ),
        }
    )
    ranked = rank_treatments(
        [reduced, full.model_copy(update={"option_id": "full"})],
        BusinessStudyRequest(kind="production_exception", economic_priority="delivery"),
        source,
    )
    assert [o.option_id for o in ranked] == ["full", "reduced"]
    assert not any(o.dominated for o in ranked)


def test_timing_gaps_find_material_the_plan_needs_before_a_delayed_delivery():
    from test_solver_wip import initial

    from packages.domain.models import Receipt
    from packages.planning.business_options import timing_gaps
    from packages.planning.checker import _required_operations

    source, baseline = initial()
    assert timing_gaps(source, baseline) == {}
    starts = {a.operation_id: a.start_at for a in baseline.assignments}
    root_id, root = min(
        ((i, o) for i, o in _required_operations(source).items() if not o.step.predecessors),
        key=lambda pair: starts[pair[0]],
    )
    item = next(b for b in source.profile.bom if b.product_id == root.product_id)
    need = item.quantity_per_unit * root.quantity
    stock = next(s for s in source.inventory if s.material_id == item.material_id)
    # No free stock: the whole first kit depends on one confirmed delivery.
    arriving = Receipt(
        receipt_id="LATE-1",
        material_id=item.material_id,
        unit=stock.unit,
        quantity=need,
        eta=starts[root_id],
        status="CONFIRMED",
        received_at=None,
        version=1,
    )
    others = tuple(r for r in source.receipts if r.material_id != item.material_id)
    on_time = source.model_copy(
        update={
            "inventory": tuple(
                s.model_copy(update={"on_hand": s.reserved}) if s is stock else s
                for s in source.inventory
            ),
            "receipts": (*others, arriving),
        }
    )
    assert item.material_id not in timing_gaps(on_time, baseline)
    late = on_time.model_copy(
        update={
            "receipts": (
                *others,
                arriving.model_copy(update={"eta": starts[root_id] + timedelta(hours=1)}),
            )
        }
    )
    assert timing_gaps(late, baseline)[item.material_id] >= need
