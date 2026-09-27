"""Hand-authored execution facts challenge the Checker without Solver-generated plans."""

import pytest
from test_checker import (
    at,
    change_assignment,
    change_snapshot,
    example_assignments,
    example_snapshot,
    make_candidate,
)

from packages.domain.models import Assignment, Candidate, Snapshot
from packages.planning.checker import calculate_metrics, check_candidate

BATCH = "order-a-R001-B001"


def candidate_for(snapshot, assignments, baseline):
    return make_candidate(
        snapshot, assignments, objective=calculate_metrics(snapshot, assignments, baseline=baseline)
    )


def wip_case(*, blocked=False, freeze=0):
    initial = change_snapshot(
        example_snapshot(), lambda data: data["profile"]["policy"].update(freeze_window_min=freeze)
    )
    baseline = make_candidate(initial)
    verified = check_candidate(initial, baseline)
    assert verified.status == "PASS"
    baseline = make_candidate(initial, checker=verified)
    now = 5 if blocked else 3
    data = initial.model_dump(mode="python", exclude={"content_hash"})
    data.update(
        schema_version="byof.snapshot/2",
        snapshot_clock=at(now),
        planning_revision=2,
        active_plan_version="source-version-2",
        active_plan_hash=baseline.content_hash,
    )
    data["source"].update(source_revision="2", observed_at=at(now), effective_at=at(now))
    data["orders"][0].update(status="IN_PROGRESS", version=2)
    data["inventory"][0]["reserved"] = 2
    actuals = []
    for index, code in enumerate(("begin", "check"), 1):
        completed = code == "begin"
        actuals.append(
            {
                "operation_id": BATCH + "-" + code,
                "batch_id": BATCH,
                "route_version": "r1",
                "state": "COMPLETED" if completed else ("BLOCKED" if blocked else "IN_PROGRESS"),
                "actual_start": at(0 if completed else 2),
                "actual_end": at(2) if completed else None,
                "resource_id": f"r{index}",
                "worker_id": f"w{index}",
                "completed_quantity": 2 if completed else 0,
                "consumed": [],
                "quality_state": "UNKNOWN" if completed else "PENDING",
                "remaining_minutes": 0 if completed else 1,
                "remaining_setup_minutes": 0,
                "remaining_confirmed_by": "source:progress-2",
                "version": 2,
                "changeover_start": at(0 if completed else 2),
                "segments": [
                    {
                        "phase": "PRODUCTION",
                        "source_event_id": "segment-" + code,
                        "start_at": at(0 if completed else 2),
                        "end_at": at(2 if completed else 3),
                    }
                ],
            }
        )
        data["resources"][index - 1].update(
            last_operation_id=BATCH + "-" + code, last_product_id="item-a"
        )
    data["actuals"] = actuals
    data["reservations"] = [
        {
            "reservation_id": "reserved-kit",
            "batch_id": BATCH,
            "material_id": "shared-part",
            "quantity": 2,
            "unit": "EA",
            "plan_version": "source-version-2",
            "source_event_id": "kit-reservation",
            "created_at": at(0),
        }
    ]
    assignments = list(example_assignments())
    assignments = change_assignment(
        assignments, 1, resume_changeover_start=at(now), resume_at=at(now), end_at=at(now + 1)
    )
    assignments = change_assignment(
        assignments, 2, changeover_start=at(now + 1), start_at=at(now + 1), end_at=at(now + 3)
    )
    return Snapshot.model_validate(data), baseline, assignments


def rejected(snapshot, assignments, baseline, code):
    report = check_candidate(
        snapshot, candidate_for(snapshot, assignments, baseline), baseline=baseline
    )
    assert report.status == "FAIL"
    assert code in {issue.code for issue in report.issues}, report


def test_running_work_preserves_history_and_reuses_the_existing_physical_kit():
    snapshot, baseline, assignments = wip_case()
    report = check_candidate(
        snapshot, candidate_for(snapshot, assignments, baseline), baseline=baseline
    )
    assert report.status == "PASS", report
    assert snapshot.inventory[0].on_hand - snapshot.inventory[0].reserved == 0
    assert assignments[0] == baseline.assignments[0]
    assert assignments[1].start_at == at(2) and assignments[1].resume_at == at(3)
    assert [m.value for m in calculate_metrics(snapshot, assignments, baseline=baseline)] == [
        0,
        0,
        0,
        0,
        6,
    ]


def test_blocked_confirmed_work_resumes_without_counting_the_downtime_as_work():
    snapshot, baseline, assignments = wip_case(blocked=True)
    report = check_candidate(
        snapshot, candidate_for(snapshot, assignments, baseline), baseline=baseline
    )
    assert report.status == "PASS", report
    assert assignments[1].start_at == at(2) and assignments[1].end_at == at(6)
    assert snapshot.actuals[1].remaining_minutes == 1
    assert [m.value for m in calculate_metrics(snapshot, assignments, baseline=baseline)] == [
        0,
        0,
        2,
        2,
        8,
    ]
    wrong = change_assignment(assignments, 1, end_at=at(7))
    rejected(snapshot, wrong, baseline, "WRONG_REMAINING_DURATION")


def test_unknown_remaining_work_and_unknown_setup_require_confirmation():
    snapshot, baseline, assignments = wip_case(blocked=True)
    for changes in (
        {"remaining_minutes": None, "remaining_confirmed_by": None},
        {"remaining_setup_minutes": None},
    ):
        changed = change_snapshot(snapshot, lambda data: data["actuals"][1].update(changes))
        rejected(changed, assignments, baseline, "CONFIRMATION_REQUIRED")


def test_fully_completed_scope_keeps_consumption_and_terminal_quality_gates():
    snapshot, baseline, _ = wip_case()

    def complete(data):
        data["snapshot_clock"] = at(6)
        data["orders"][0]["status"] = "COMPLETED"
        inspected = data["actuals"][1]
        inspected.update(
            state="COMPLETED",
            actual_end=at(4),
            completed_quantity=2,
            quality_state="PASSED",
            remaining_minutes=0,
        )
        inspected["segments"][0]["end_at"] = at(4)
        finished = dict(
            inspected,
            operation_id=BATCH + "-finish",
            resource_id="r3",
            worker_id="w3",
            actual_start=at(4),
            actual_end=at(6),
            changeover_start=at(4),
            segments=[
                {
                    "phase": "PRODUCTION",
                    "source_event_id": "finished-work",
                    "start_at": at(4),
                    "end_at": at(6),
                }
            ],
            consumed=[
                {
                    "material_id": "shared-part",
                    "quantity": 2,
                    "unit": "EA",
                    "event_id": "consume-kit",
                }
            ],
        )
        data["actuals"].append(finished)
        data["resources"][2].update(last_operation_id=BATCH + "-finish", last_product_id="item-a")
        data["inventory"][0].update(on_hand=0, reserved=0)
        data["reservations"][0]["quantity"] = 0

    snapshot = change_snapshot(snapshot, complete)
    assignments = example_assignments()
    report = check_candidate(
        snapshot, candidate_for(snapshot, assignments, baseline), baseline=baseline
    )
    assert report.status == "PASS", report
    assert snapshot.inventory[0].on_hand == 0
    assert [m.value for m in calculate_metrics(snapshot, assignments, baseline=baseline)] == [
        0,
        0,
        0,
        0,
        6,
    ]

    def unresolved_terminal_gate(data):
        data["profile"]["routes"][-1]["quality_gate"] = True
        data["actuals"][-1]["quality_state"] = "UNKNOWN"

    unknown = change_snapshot(snapshot, unresolved_terminal_gate)
    rejected(unknown, assignments, baseline, "QUALITY_CONFIRMATION_REQUIRED")


def test_completed_history_is_not_rechecked_against_todays_downtime_or_absence():
    snapshot, baseline, assignments = wip_case()

    def change(data):
        data["resources"][0].update(
            status="DOWN", unavailable=[{"start_at": at(0), "end_at": at(60)}]
        )
        data["workers"][0].update(
            status="ABSENT", unavailable=[{"start_at": at(0), "end_at": at(60)}]
        )

    snapshot = change_snapshot(snapshot, change)
    report = check_candidate(
        snapshot, candidate_for(snapshot, assignments, baseline), baseline=baseline
    )
    assert report.status == "PASS", report
    changed = change_assignment(assignments, 0, end_at=at(1))
    rejected(snapshot, changed, baseline, "ACTUAL_HISTORY_CHANGED")


def test_actual_completion_replaces_old_predictions_without_counting_completed_work_as_a_change():
    snapshot, baseline, assignments = wip_case()

    def earlier_completion(data):
        data["actuals"][0]["actual_end"] = at(1)
        data["actuals"][0]["segments"][0]["end_at"] = at(1)

    snapshot = change_snapshot(snapshot, earlier_completion)
    assignments = change_assignment(assignments, 0, end_at=at(1))
    report = check_candidate(
        snapshot, candidate_for(snapshot, assignments, baseline), baseline=baseline
    )
    assert report.status == "PASS", report
    assert [m.value for m in calculate_metrics(snapshot, assignments, baseline=baseline)][2:4] == [
        0,
        0,
    ]
    rejected(snapshot, baseline.assignments, baseline, "ACTUAL_HISTORY_CHANGED")


def test_in_progress_future_cannot_move_change_employee_or_ignore_a_calendar_gap():
    snapshot, baseline, assignments = wip_case()
    moved = change_assignment(
        assignments, 1, resume_at=at(4), resume_changeover_start=at(4), end_at=at(5)
    )
    rejected(snapshot, moved, baseline, "ACTIVE_WORK_MOVED")
    worker_changed = change_assignment(assignments, 1, worker_id="w1")
    rejected(snapshot, worker_changed, baseline, "ACTUAL_HISTORY_CHANGED")
    gap = change_snapshot(
        snapshot,
        lambda data: data["resources"][1].update(
            unavailable=[{"start_at": at(3), "end_at": at(4)}]
        ),
    )
    rejected(gap, assignments, baseline, "RESOURCE_UNAVAILABLE_INTERVAL")
    down = change_snapshot(snapshot, lambda data: data["resources"][1].update(status="DOWN"))
    rejected(down, assignments, baseline, "RESOURCE_UNAVAILABLE")


@pytest.mark.parametrize("quality", ["UNKNOWN", "PENDING", "FAILED"])
def test_completed_quality_gate_without_pass_blocks_its_successor(quality):
    snapshot, baseline, assignments = wip_case()

    def complete(data):
        data["snapshot_clock"] = at(4)
        actual = data["actuals"][1]
        actual.update(
            state="COMPLETED",
            actual_end=at(4),
            completed_quantity=2,
            remaining_minutes=0,
            quality_state=quality,
        )
        actual["segments"][0]["end_at"] = at(4)

    snapshot = change_snapshot(snapshot, complete)
    assignments = change_assignment(assignments, 1, resume_at=None, resume_changeover_start=None)
    rejected(snapshot, assignments, baseline, "QUALITY_CONFIRMATION_REQUIRED")
    passed = change_snapshot(
        snapshot, lambda data: data["actuals"][1].update(quality_state="PASSED")
    )
    report = check_candidate(
        passed, candidate_for(passed, assignments, baseline), baseline=baseline
    )
    assert report.status == "PASS", report


def test_replanning_requires_the_actual_source_accepted_baseline():
    snapshot, baseline, assignments = wip_case()
    candidate = candidate_for(snapshot, assignments, baseline)
    assert "UNSUPPORTED_BASELINE" in {i.code for i in check_candidate(snapshot, candidate).issues}
    wrong = Candidate.model_validate(
        {**baseline.model_dump(), "content_hash": None, "candidate_id": "different-candidate"}
    )
    rejected(snapshot, assignments, wrong, "BASELINE_MISMATCH")
    wrong_factory = Candidate.model_validate(
        {**baseline.model_dump(), "content_hash": None, "factory_id": "other-factory"}
    )
    rejected(snapshot, assignments, wrong_factory, "BASELINE_MISMATCH")


def test_old_batches_cannot_disappear_by_shrinking_an_order_without_reconciliation():
    initial = example_snapshot(batches=2)
    baseline = make_candidate(initial, example_assignments(batches=2))
    assert check_candidate(initial, baseline).status == "PASS"
    data = initial.model_dump(mode="python", exclude={"content_hash"})
    data.update(
        schema_version="byof.snapshot/2",
        active_plan_version="source-active",
        active_plan_hash=baseline.content_hash,
    )
    data["orders"][0]["quantity"] = 2
    snapshot = Snapshot.model_validate(data)
    rejected(snapshot, example_assignments(), baseline, "BASELINE_SCOPE_MISMATCH")


def test_freeze_window_is_half_open_and_never_overrides_physical_failure():
    snapshot, baseline, assignments = wip_case(freeze=2)
    moved = change_assignment(assignments, 2, changeover_start=at(5), start_at=at(5), end_at=at(7))
    rejected(snapshot, moved, baseline, "FROZEN_OPERATION_CHANGED")
    upper_edge = change_snapshot(
        snapshot, lambda data: data["profile"]["policy"].update(freeze_window_min=1)
    )
    report = check_candidate(
        upper_edge, candidate_for(upper_edge, moved, baseline), baseline=baseline
    )
    assert report.status == "PASS", report
    down = change_snapshot(snapshot, lambda data: data["resources"][2].update(status="DOWN"))
    rejected(down, assignments, baseline, "RESOURCE_UNAVAILABLE")


def test_setup_execution_keeps_actual_setup_but_does_not_invent_an_actual_production_start():
    snapshot, baseline, assignments = wip_case()

    def setup(data):
        actual = data["actuals"][1]
        actual.update(
            state="SETUP", actual_start=None, remaining_setup_minutes=1, remaining_minutes=2
        )
        actual["segments"][0]["phase"] = "SETUP"

    snapshot = change_snapshot(snapshot, setup)
    assignments = change_assignment(
        assignments, 1, start_at=at(4), resume_changeover_start=at(3), resume_at=at(4), end_at=at(6)
    )
    assignments = change_assignment(
        assignments, 2, changeover_start=at(6), start_at=at(6), end_at=at(8)
    )
    report = check_candidate(
        snapshot, candidate_for(snapshot, assignments, baseline), baseline=baseline
    )
    assert report.status == "PASS", report
    assert snapshot.actuals[1].actual_start is None
    missing_setup = change_assignment(assignments, 1, start_at=at(3), resume_at=at(3), end_at=at(5))
    rejected(snapshot, missing_setup, baseline, "CHANGEOVER")


def test_new_first_task_uses_equipment_setup_state_even_without_future_predecessor():
    snapshot, baseline, assignments = wip_case()
    snapshot = change_snapshot(
        snapshot,
        lambda data: data["resources"][2].update(
            last_operation_id="earlier-run-operation", last_product_id="item-a"
        ),
    )
    rejected(snapshot, assignments, baseline, "CHANGEOVER")
    correct = change_assignment(
        assignments, 2, changeover_start=at(3), start_at=at(4), end_at=at(6)
    )
    report = check_candidate(
        snapshot, candidate_for(snapshot, correct, baseline), baseline=baseline
    )
    assert report.status == "PASS", report
    absent_setup = change_snapshot(
        snapshot,
        lambda data: data["resources"][1].update(last_operation_id=None, last_product_id=None),
    )
    rejected(absent_setup, correct, baseline, "SOURCE_SETUP_STATE")


def test_future_setup_occupies_the_employee_even_when_original_start_is_in_the_past():
    snapshot, baseline, assignments = wip_case(blocked=True)

    def setup(data):
        data["resources"][2].update(last_operation_id="previous", last_product_id="item-a")
        data["workers"][1]["skills"].append("PACK")

    snapshot = change_snapshot(snapshot, setup)
    finish = change_assignment(
        assignments, 2, worker_id="w2", changeover_start=at(5), start_at=at(6), end_at=at(8)
    )
    rejected(snapshot, finish, baseline, "WORKER_CONFLICT")


def test_resuming_after_another_product_requires_a_new_full_changeover():
    snapshot, baseline, assignments = wip_case(blocked=True)
    snapshot = change_snapshot(
        snapshot,
        lambda data: data["resources"][1].update(
            last_operation_id="intervening-operation", last_product_id="other-product"
        ),
    )
    rejected(snapshot, assignments, baseline, "CHANGEOVER")
    assignments = change_assignment(
        assignments, 1, resume_changeover_start=at(5), resume_at=at(10), end_at=at(11)
    )
    assignments = change_assignment(
        assignments, 2, changeover_start=at(11), start_at=at(11), end_at=at(13)
    )
    report = check_candidate(
        snapshot, candidate_for(snapshot, assignments, baseline), baseline=baseline
    )
    assert report.status == "PASS", report
    assert assignments[1].changeover_start == at(2)
    assert assignments[1].resume_at - assignments[1].resume_changeover_start == at(5) - at(0)


def test_new_order_cannot_borrow_an_existing_wip_kit_and_same_instant_receipt_can_fund_it():
    snapshot, baseline, assignments = wip_case()
    data = snapshot.model_dump(mode="python", exclude={"content_hash"})
    order = dict(data["orders"][0], order_id="new-order", status="CONFIRMED", version=1)
    data["orders"] = [*data["orders"], order]
    data["scope_version"] += 1
    snapshot = Snapshot.model_validate(data)
    additions = []
    for code, resource, worker, start in (
        ("begin", "r1", "w1", 4),
        ("check", "r2", "w2", 7),
        ("finish", "r3", "w3", 10),
    ):
        additions.append(
            {
                "operation_id": "new-order-R001-B001-" + code,
                "resource_id": resource,
                "worker_id": worker,
                "changeover_start": at(start - 1),
                "start_at": at(start),
                "end_at": at(start + 2),
            }
        )
    combined = (*assignments, *(Assignment.model_validate(row) for row in additions))
    rejected(snapshot, combined, baseline, "MATERIAL_SHORTAGE")
    omitted = make_candidate(snapshot, assignments, objective=())
    assert "MISSING_OPERATION" in {
        i.code for i in check_candidate(snapshot, omitted, baseline=baseline).issues
    }
    funded = change_snapshot(
        snapshot,
        lambda facts: facts["receipts"].append(
            {
                "receipt_id": "new-kit",
                "material_id": "shared-part",
                "unit": "EA",
                "quantity": 2,
                "eta": at(4),
                "status": "CONFIRMED",
            }
        ),
    )
    report = check_candidate(funded, candidate_for(funded, combined, baseline), baseline=baseline)
    assert report.status == "PASS", report
    late = change_snapshot(funded, lambda facts: facts["receipts"][0].update(eta=at(5)))
    rejected(late, combined, baseline, "MATERIAL_SHORTAGE")


def test_incremental_overtime_uses_segments_not_elapsed_downtime_and_ignores_completed_changes():
    snapshot, baseline, assignments = wip_case(blocked=True)
    overtime_calendar = [{"start_at": at(0), "end_at": at(60), "kind": "OVERTIME"}]

    def overtime(data):
        for worker in data["workers"]:
            worker["calendar"] = overtime_calendar

    snapshot = change_snapshot(snapshot, overtime)
    baseline_data = baseline.model_dump()
    baseline_data["content_hash"] = None
    baseline_data["assignments"][0].update(start_at=at(1), changeover_start=at(1), end_at=at(2))
    prior = Candidate.model_validate(baseline_data)
    metrics = calculate_metrics(snapshot, assignments, baseline=prior)
    # Same three actual minutes cancel; candidate future is 3 min, old future is only 1 min.
    assert metrics[1].value == 2
    assert metrics[2].value == 2 and metrics[3].value == 2


def test_actual_historical_segments_cannot_overlap_on_one_employee():
    snapshot, baseline, assignments = wip_case()

    def overlap(data):
        data["actuals"][1].update(worker_id="w1", actual_start=at(1), changeover_start=at(1))
        data["actuals"][1]["segments"][0].update(start_at=at(1), end_at=at(2))

    snapshot = change_snapshot(snapshot, overlap)
    assignments = change_assignment(
        assignments, 1, worker_id="w1", start_at=at(1), changeover_start=at(1)
    )
    rejected(snapshot, assignments, baseline, "ACTUAL_WORKER_CONFLICT")


def test_unstarted_task_cannot_claim_resume_and_skipped_history_cannot_pass():
    snapshot, baseline, assignments = wip_case()
    fake_resume = change_assignment(assignments, 2, resume_at=at(4), resume_changeover_start=at(4))
    rejected(snapshot, fake_resume, baseline, "UNEXPECTED_CONTINUATION")
    incomplete = make_candidate(snapshot, assignments[1:], objective=())
    report = check_candidate(snapshot, incomplete, baseline=baseline)
    assert "MISSING_OPERATION" in {issue.code for issue in report.issues}
