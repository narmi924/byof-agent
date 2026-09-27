"""A manager's decision survives normal production progress but not a new disruption."""

from datetime import timedelta

from test_solver_wip import initial

from packages.agent.planning_context import decision_key, planning_key
from services.factory_sim.engine import advance, inject


def test_normal_progress_keeps_the_decision_while_the_plan_advances():
    source, baseline = initial()
    later = advance(source, baseline, minutes=20)
    assert planning_key(later) != planning_key(source)
    assert decision_key(later) == decision_key(source)


def test_disruptions_and_demand_changes_invalidate_the_decision():
    source, baseline = initial()
    running = advance(source, baseline, minutes=12)
    key = decision_key(running)
    busy = next(a for a in running.actuals if a.state == "IN_PROGRESS")
    order = running.orders[0]
    receipt = next(r for r in running.receipts if r.status in {"CONFIRMED", "EXPECTED"})
    changed = [
        inject(
            running,
            event_id="down",
            kind="resource.down",
            payload={"resource_id": busy.resource_id},
        ),
        inject(
            running,
            event_id="leave",
            kind="worker.leave",
            payload={"worker_id": busy.worker_id, "minutes": 30},
        ),
        inject(
            running,
            event_id="more",
            kind="order.revise",
            payload={
                "order_id": order.order_id,
                "expected_version": order.version,
                "quantity": order.quantity + 50,
                "due_at": order.due_at.isoformat(),
                "priority_weight": order.priority_weight,
                "hard_deadline": order.hard_deadline,
            },
        ),
        inject(
            running,
            event_id="late",
            kind="receipt.delay",
            payload={
                "receipt_id": receipt.receipt_id,
                "eta": (receipt.eta + timedelta(hours=2)).isoformat(),
            },
        ),
    ]
    assert all(decision_key(snapshot) != key for snapshot in changed)
