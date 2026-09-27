"""Missed new dispatches need a new accepted plan; physical WIP remains recoverable."""

from copy import deepcopy

import pytest
from test_checker import at, example_snapshot, make_candidate
from test_factory_execution_review import initial_plan

from packages.agent.impact import classify_events
from packages.domain.models import Assignment, Event, Snapshot
from packages.planning.checker import check_candidate
from services.factory_sim.engine import advance, evolve, inject, missed_dispatch_events
from services.factory_sim.service import _write
from services.factory_sim.storage import SourceChange, World


def accepted_plan(*, independent_order=False):
    data = example_snapshot().model_dump(mode="python", exclude={"content_hash"})
    if independent_order:
        order = deepcopy(data["orders"][0])
        order["order_id"] = "independent"
        data["orders"] = [*data["orders"], order]
        data["inventory"][0]["on_hand"] += order["quantity"]
        for collection, key, prefix in (
            ("resources", "resource_id", "r"),
            ("workers", "worker_id", "w"),
        ):
            copies = deepcopy(data[collection])
            for index, row in enumerate(copies, 4):
                row[key] = f"{prefix}{index}"
            data[collection] = [*data[collection], *copies]
    snapshot = Snapshot.model_validate(data)
    assignments = []
    for order_index, order in enumerate(snapshot.orders):
        for step_index, code in enumerate(("begin", "check", "finish")):
            start = order_index * 2 + step_index * 2
            resource_index = order_index * 3 + step_index + 1
            assignments.append(
                Assignment(
                    operation_id=f"{order.order_id}-R001-B001-{code}",
                    resource_id=f"r{resource_index}",
                    worker_id=f"w{resource_index}",
                    changeover_start=at(start),
                    start_at=at(start),
                    end_at=at(start + 2),
                )
            )
    candidate = make_candidate(snapshot, assignments=tuple(assignments))
    assert check_candidate(snapshot, candidate).status == "PASS"
    return evolve(
        snapshot, active_plan_version="accepted-dispatch", active_plan_hash=candidate.content_hash
    ), candidate


def test_missed_dispatch_never_auto_starts_after_resource_is_restored():
    snapshot, candidate = accepted_plan()
    down = inject(snapshot, event_id="down", kind="resource.down", payload={"resource_id": "r1"})
    missed = advance(down, candidate)
    assert missed.actuals == () and missed.reservations == ()
    restored = inject(
        missed, event_id="restored", kind="resource.restore", payload={"resource_id": "r1"}
    )
    later = advance(restored, candidate, minutes=10)
    assert later.actuals == () and later.reservations == ()
    assert later.inventory == snapshot.inventory
    assert later.orders[0].status == "CONFIRMED"
    assert all(r.last_operation_id is None for r in later.resources)


def test_missed_operation_does_not_block_independent_on_time_production():
    snapshot, candidate = accepted_plan(independent_order=True)
    down = inject(snapshot, event_id="down", kind="resource.down", payload={"resource_id": "r1"})
    missed = advance(down, candidate)
    restored = inject(
        missed, event_id="restored", kind="resource.restore", payload={"resource_id": "r1"}
    )
    result = advance(restored, candidate, minutes=8)
    assert len(result.actuals) == 3
    assert all(a.operation_id.startswith("independent-") for a in result.actuals)
    assert all(a.state == "COMPLETED" for a in result.actuals)
    assert {o.order_id: o.status for o in result.orders} == {
        "order-a": "CONFIRMED",
        "independent": "COMPLETED",
    }
    assert sum(c.quantity for a in result.actuals for c in a.consumed) == 2
    assert result.inventory[0].on_hand == 2 and result.inventory[0].reserved == 0
    assert all(r.quantity == 0 for r in result.reservations)


@pytest.mark.parametrize("started_minutes", [1, 2])
def test_existing_setup_and_production_resume_confirmed_remainder_after_old_dispatch(
    started_minutes,
):
    snapshot, candidate = initial_plan(consume_first=True)
    started = advance(snapshot, candidate, minutes=started_minutes)
    actual = started.actuals[0]
    assert actual.state == ("SETUP" if started_minutes == 1 else "IN_PROGRESS")
    down = inject(started, event_id="down", kind="resource.down", payload={"resource_id": "r1"})
    waiting = advance(down, candidate, minutes=3)
    stalled = next(a for a in waiting.actuals if a.operation_id == actual.operation_id)
    assert stalled.remaining_minutes is None and stalled.segments == actual.segments
    restored = inject(
        waiting, event_id="restored", kind="resource.restore", payload={"resource_id": "r1"}
    )
    confirmed = inject(
        restored,
        event_id="measured-remaining",
        kind="execution.confirm_remaining",
        payload={
            "operation_id": actual.operation_id,
            "remaining_minutes": actual.remaining_minutes,
            "remaining_setup_minutes": actual.remaining_setup_minutes,
        },
    )
    resumed = advance(confirmed, candidate, minutes=actual.remaining_minutes)
    completed = next(a for a in resumed.actuals if a.operation_id == actual.operation_id)
    assert completed.state == "COMPLETED" and completed.changeover_start == actual.changeover_start
    assert completed.actual_start == (actual.actual_start or confirmed.snapshot_clock)
    assert sum(c.quantity for c in completed.consumed) == 2
    if actual.actual_start is not None:
        assert completed.consumed == actual.consumed
    production_minutes = sum(
        (s.end_at - s.start_at).total_seconds() / 60
        for s in completed.segments
        if s.phase == "PRODUCTION"
    )
    assert production_minutes == 2
    assert not any(
        event.entity_id == actual.operation_id
        for event in missed_dispatch_events(confirmed, resumed, candidate)
    )


def test_missed_dispatch_is_one_deterministic_event_in_source_change_chain():
    snapshot, candidate = accepted_plan()
    before = inject(snapshot, event_id="down", kind="resource.down", payload={"resource_id": "r1"})
    after = advance(before, candidate)
    events = missed_dispatch_events(before, after, candidate)
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "execution.dispatch_missed"
    assert (
        event.entity_type == "operations"
        and event.entity_id == candidate.assignments[0].operation_id
    )
    assert event == missed_dispatch_events(before, after, candidate)[0]
    assert {c.field: c.after for c in event.changes} == {
        "dispatch_state": "MISSED",
        "candidate_hash": candidate.content_hash,
        "scheduled_dispatch_at": candidate.assignments[0].changeover_start.isoformat(),
    }
    assert classify_events(events, after)["material"]
    assert not before.actuals and not after.actuals
    assert missed_dispatch_events(after, after, candidate) == ()

    class ChangeSink:
        def __init__(self):
            self.rows = []

        def add(self, row):
            assert isinstance(row, SourceChange)
            self.rows.append(row)

    sink = ChangeSink()
    world = World(
        factory_id=before.factory_id,
        run_id=before.run_id,
        revision=int(before.source.source_revision),
        business_clock=before.snapshot_clock,
        document=before.model_dump(mode="json"),
        active_candidate=candidate.model_dump(mode="json"),
    )
    _write(sink, world, before, after, "clock.tick", plan=candidate)
    assert sink.rows[0].document["previous_snapshot_hash"] == before.content_hash
    assert sink.rows[0].document["snapshot_hash"] == after.content_hash
    assert Event.model_validate(sink.rows[0].document["events"][0]) == event
    for _ in range(6):
        following = advance(after, candidate)
        _write(sink, world, after, following, "clock.tick", plan=candidate)
        after = following
    all_events = [Event.model_validate(e) for row in sink.rows for e in row.document["events"]]
    assert sum(e.event_id == event.event_id for e in all_events) == 1
    missed_events = [e for e in all_events if e.event_type == "execution.dispatch_missed"]
    assert len(missed_events) == 3
    assert len({e.event_id for e in missed_events}) == 3
    assert world.document == after.model_dump(mode="json")
