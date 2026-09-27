"""Physical execution regressions from independent hand-written accepted plans."""

from copy import deepcopy
from datetime import timedelta

from test_checker import at, example_snapshot, make_candidate

from packages.domain.models import Assignment, Snapshot
from packages.planning.checker import check_candidate
from services.factory_sim.engine import advance, evolve, inject, missed_dispatch_events


def initial_plan(*, consume_first=False):
    data = example_snapshot().model_dump(mode="python", exclude={"content_hash"})
    data["profile"]["policy"]["first_changeover_min"] = 1
    if consume_first:
        data["profile"]["bom"][0]["consume_step_id"] = "a-begin"
    snapshot = Snapshot.model_validate(data)
    assignments = tuple(
        Assignment(
            operation_id=f"order-a-R001-B001-{code}",
            resource_id=f"r{index + 1}",
            worker_id=f"w{index + 1}",
            changeover_start=at(2 * index),
            start_at=at(2 * index + 1),
            end_at=at(2 * index + 3),
        )
        for index, code in enumerate(("begin", "check", "finish"))
    )
    candidate = make_candidate(snapshot, assignments=assignments)
    assert check_candidate(snapshot, candidate).status == "PASS"
    snapshot = evolve(
        snapshot, active_plan_version="accepted-initial", active_plan_hash=candidate.content_hash
    )
    return snapshot, candidate


def test_setup_may_overlap_predecessor_but_production_starts_at_its_actual_completion():
    snapshot, candidate = initial_plan()
    before_predecessor_finishes = advance(snapshot, candidate, minutes=2)
    assert before_predecessor_finishes.actuals[0].state == "IN_PROGRESS"
    at_predecessor_completion = advance(before_predecessor_finishes, candidate)
    first, second = at_predecessor_completion.actuals
    assert first.actual_end == at(3)
    assert second.changeover_start == at(2)
    assert second.state == "SETUP"
    assert second.actual_start is None
    assert second.consumed == ()
    assert [(s.phase, s.start_at, s.end_at) for s in second.segments] == [("SETUP", at(2), at(3))]
    result = advance(at_predecessor_completion, candidate, minutes=4)
    assert result.orders[0].status == "COMPLETED"
    by_id = {actual.operation_id: actual for actual in result.actuals}
    for assignment in candidate.assignments:
        actual = by_id[assignment.operation_id]
        assert actual.changeover_start == assignment.changeover_start
        assert actual.actual_start == assignment.start_at
        assert actual.actual_end == assignment.end_at
        assert actual.state == "COMPLETED"
        assert sum(
            (
                segment.end_at - segment.start_at
                for segment in actual.segments
                if segment.phase == "PRODUCTION"
            ),
            start=timedelta(),
        ) == timedelta(minutes=2)


def test_confirmed_work_waits_for_accepted_resume_time_and_never_consumes_twice():
    snapshot, initial = initial_plan(consume_first=True)
    started = advance(snapshot, initial, minutes=2)
    original = started.actuals[0]
    assert original.actual_start == at(1)
    assert sum(item.quantity for item in original.consumed) == 2
    paused = inject(started, event_id="fault", kind="resource.down", payload={"resource_id": "r1"})
    repaired = inject(
        paused, event_id="repair", kind="resource.restore", payload={"resource_id": "r1"}
    )
    confirmed = inject(
        repaired,
        event_id="confirmed-remainder",
        kind="execution.confirm_remaining",
        payload={
            "operation_id": original.operation_id,
            "remaining_minutes": 1,
            "remaining_setup_minutes": 0,
        },
    )
    assignments = []
    for index, assignment in enumerate(initial.assignments):
        data = assignment.model_dump(mode="python")
        if index == 0:
            data.update(resume_changeover_start=at(15), resume_at=at(15), end_at=at(16))
        else:
            data.update(
                changeover_start=at(15 + (index - 1) * 2),
                start_at=at(16 + (index - 1) * 2),
                end_at=at(18 + (index - 1) * 2),
            )
        assignments.append(Assignment.model_validate(data))
    continuation = make_candidate(
        confirmed, assignments=tuple(assignments), candidate_id="accepted-continuation"
    )
    accepted = evolve(
        confirmed,
        active_plan_version="accepted-continuation",
        active_plan_hash=continuation.content_hash,
    )
    waiting = advance(accepted, continuation, minutes=13)
    assert waiting.snapshot_clock == at(15)
    pending = next(a for a in waiting.actuals if a.operation_id == original.operation_id)
    assert pending.state == "BLOCKED"
    assert pending.remaining_minutes == 1
    assert pending.actual_end is None
    assert pending.segments == original.segments
    assert pending.consumed == original.consumed
    assert waiting.inventory == accepted.inventory
    resumed = advance(waiting, continuation)
    finished = next(a for a in resumed.actuals if a.operation_id == original.operation_id)
    assert finished.state == "COMPLETED"
    assert finished.actual_start == at(1)
    assert finished.actual_end == at(16)
    assert finished.consumed == original.consumed
    assert resumed.inventory == accepted.inventory
    assert [(s.start_at, s.end_at) for s in finished.segments if s.phase == "PRODUCTION"] == [
        (at(1), at(2)),
        (at(15), at(16)),
    ]


def test_dependency_wait_cannot_resume_over_another_batchs_physical_setup():
    data = example_snapshot(second_product=True).model_dump(mode="python", exclude={"content_hash"})
    data["profile"]["policy"]["first_changeover_min"] = 1
    second_preparation = deepcopy(data["resources"][0])
    second_preparation["resource_id"] = "r4"
    data["resources"] = (*data["resources"], second_preparation)
    second_worker = deepcopy(data["workers"][0])
    second_worker.update(worker_id="w4", skills=("PREP",))
    data["workers"] = (*data["workers"], second_worker)
    snapshot = Snapshot.model_validate(data)
    timings = {
        "a": [(0, 1, 3, "r1", "w1"), (2, 3, 5, "r2", "w2"), (4, 5, 7, "r3", "w3")],
        "b": [(0, 1, 3, "r4", "w4"), (10, 15, 17, "r2", "w2"), (14, 19, 21, "r3", "w3")],
    }
    assignments = tuple(
        Assignment(
            operation_id=f"order-{product}-R001-B001-{code}",
            resource_id=resource,
            worker_id=worker,
            changeover_start=at(setup),
            start_at=at(start),
            end_at=at(end),
        )
        for product, schedule in timings.items()
        for code, (setup, start, end, resource, worker) in zip(
            ("begin", "check", "finish"), schedule
        )
    )
    candidate = make_candidate(snapshot, assignments=assignments)
    assert check_candidate(snapshot, candidate).status == "PASS"
    snapshot = evolve(
        snapshot,
        active_plan_version="accepted-two-products",
        active_plan_hash=candidate.content_hash,
    )
    snapshot = advance(snapshot, candidate, minutes=2)
    interrupted = inject(
        snapshot, event_id="a-preparation-down", kind="resource.down", payload={"resource_id": "r1"}
    )
    waiting = advance(interrupted, candidate, minutes=8)
    independent = next(a for a in waiting.actuals if a.operation_id == "order-b-R001-B001-begin")
    assert independent.state == "COMPLETED" and independent.actual_end == at(3)
    # No crew prepares a step behind a stopped predecessor; that dispatch goes back to planning.
    assert all(a.operation_id != "order-a-R001-B001-check" for a in waiting.actuals)
    assert "order-a-R001-B001-check" in {
        event.entity_id for event in missed_dispatch_events(interrupted, waiting, candidate)
    }
    repaired = inject(
        waiting, event_id="a-repair", kind="resource.restore", payload={"resource_id": "r1"}
    )
    confirmed = inject(
        repaired,
        event_id="a-remainder",
        kind="execution.confirm_remaining",
        payload={
            "operation_id": "order-a-R001-B001-begin",
            "remaining_minutes": 1,
            "remaining_setup_minutes": 0,
        },
    )
    result = advance(confirmed, candidate, minutes=2)
    second = next(a for a in result.actuals if a.operation_id == "order-b-R001-B001-check")
    assert second.resource_id == "r2" and second.changeover_start == at(10)
    assert second.segments[0].phase == "SETUP" and second.segments[0].start_at == at(10)
    assert all(a.operation_id != "order-a-R001-B001-check" for a in result.actuals), (
        "A predecessor recovery must not resume work over another batch's setup on r2/w2"
    )
