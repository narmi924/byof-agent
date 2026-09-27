"""Public source transitions and hidden adverse deltas challenge approval evidence."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from packages.domain.models import CheckReport
from packages.domain.revalidation import ValidationCertificate
from packages.planning.checker import calculate_metrics
from packages.planning.progress_evidence import ProgressEvidenceError, validate_progress_chain
from services.factory_sim.engine import advance, evolve, inject
from services.factory_sim.service import _write
from services.factory_sim.storage import SourceChange, World
from tests.test_checker import (
    at,
    change_snapshot,
    example_assignments,
    example_snapshot,
    make_candidate,
)


class Collector:
    def __init__(self):
        self.rows: list[SourceChange] = []

    def add(self, row: SourceChange):
        self.rows.append(row)


def batch(before, after, cause="clock.tick"):
    collector = Collector()
    _write(cast(Session, collector), World(factory_id=before.factory_id), before, after, cause)
    assert len(collector.rows) == 1
    return collector.rows[0].document


def example(*, ticks=1, prefix=0, batches=1, second_product=False, shift=0):
    initial = example_snapshot(batches=batches, second_product=second_product)
    assignments = example_assignments(batches=batches, second_product=second_product, shift=shift)
    baseline = make_candidate(initial, assignments)
    original = evolve(
        initial, active_plan_version="source-plan-1", active_plan_hash=baseline.content_hash
    )
    for _ in range(prefix):
        original = advance(original, baseline)
    candidate = make_candidate(original, assignments, candidate_id="candidate-for-approval")
    current = original
    changes = []
    for _ in range(ticks):
        after = advance(current, baseline)
        changes.append(batch(current, after))
        current = after
    return original, current, changes, baseline, candidate


def validate(data):
    original, current, changes, baseline, candidate = data
    return validate_progress_chain(
        original, current, changes, baseline=baseline, candidate=candidate
    )


@pytest.mark.parametrize(
    "options",
    [
        {"ticks": 1},
        {"ticks": 6},
        {"ticks": 2, "prefix": 2},
        {"ticks": 1, "prefix": 6},
        {"ticks": 3, "shift": 5},
        {"ticks": 11, "batches": 2},
        {"ticks": 23, "second_product": True},
    ],
)
def test_source_produced_setup_progress_completion_and_idle_clocks_preserve_exact_evidence(options):
    data = example(**options)
    original, current, changes, baseline, candidate = data
    unchanged = [value.model_dump_json() for value in (original, current, baseline, candidate)]
    result = validate(data)
    assert len(result) == 64 and int(result, 16) >= 0
    assert validate(data) == result
    assert [
        value.model_dump_json() for value in (original, current, baseline, candidate)
    ] == unchanged
    assert int(current.source.source_revision) - int(original.source.source_revision) == len(
        changes
    )
    assert all(
        change["snapshot_delta"]["snapshot_hash"] == change["snapshot_hash"] for change in changes
    )


def test_complete_prefix_accounts_for_exact_consumption_without_replenishing_stock():
    data = example(ticks=11, batches=2)
    original, current, *_ = data
    validate(data)
    assert original.inventory[0].on_hand == 4 and original.inventory[0].reserved == 0
    assert current.inventory[0].on_hand == current.inventory[0].reserved == 0
    assert (
        sum(consumption.quantity for actual in current.actuals for consumption in actual.consumed)
        == 4
    )
    assert len(current.actuals) == 6 and all(
        actual.state == "COMPLETED" for actual in current.actuals
    )


@pytest.mark.parametrize(
    "variant",
    ["missing", "reordered", "duplicate", "hash", "foreign", "delta_missing", "extra_field"],
)
def test_chain_gaps_reordering_identity_changes_and_missing_readset_are_rejected(variant):
    original, current, changes, baseline, candidate = example(ticks=3)
    changes = deepcopy(changes)
    if variant == "missing":
        changes.pop(1)
    elif variant == "reordered":
        changes.reverse()
    elif variant == "duplicate":
        changes[1] = changes[0]
    elif variant == "hash":
        changes[1]["previous_snapshot_hash"] = "f" * 64
    elif variant == "foreign":
        changes[1]["factory_id"] = "other-factory"
    elif variant == "delta_missing":
        changes[1].pop("snapshot_delta")
    else:
        changes[1]["confirmed"] = True
    with pytest.raises(ProgressEvidenceError):
        validate_progress_chain(original, current, changes, baseline=baseline, candidate=candidate)


@pytest.mark.parametrize(
    "variant",
    [
        "missing",
        "duplicate",
        "wrong_before",
        "wrong_after",
        "correction",
        "wrong_revision",
        "fake_kind",
    ],
)
def test_scalar_source_events_must_cover_the_actual_delta_and_cannot_grant_confirmation(variant):
    original, current, changes, baseline, candidate = example()
    changes = deepcopy(changes)
    events = changes[0]["events"]
    assert events
    if variant == "missing":
        events.pop()
    elif variant == "duplicate":
        events.append(deepcopy(events[0]))
    elif variant == "wrong_before":
        events[0]["changes"][0]["before"] = "fabricated-prior-fact"
    elif variant == "wrong_after":
        events[0]["changes"][0]["after"] = "fabricated-new-fact"
    elif variant == "correction":
        events[0]["corrects_event_id"] = "older-event"
    elif variant == "wrong_revision":
        events[0]["source_revision"] = "900"
    else:
        events[0]["event_type"] = "quality.failed"
    with pytest.raises(ProgressEvidenceError):
        validate_progress_chain(original, current, changes, baseline=baseline, candidate=candidate)


@pytest.mark.parametrize(
    "variant",
    [
        "worker_calendar",
        "worker_skill",
        "resource_calendar",
        "profile",
        "scope",
        "inventory",
        "reservation_id",
        "reservation_source",
        "segment_start",
        "segment_event",
    ],
)
def test_hidden_nested_or_unexplained_changes_are_rejected_even_with_source_emitted_events(variant):
    original, current, _, baseline, candidate = example(prefix=1)

    def mutate(data):
        if variant == "worker_calendar":
            data["workers"][0]["calendar"][0]["end_at"] = at(29).isoformat()
        elif variant == "worker_skill":
            data["workers"][0]["skills"].append("unapproved-skill")
        elif variant == "resource_calendar":
            data["resources"][0]["calendar"][0]["end_at"] = at(29).isoformat()
        elif variant == "profile":
            data["profile"]["policy"]["freeze_window_min"] = 0
        elif variant == "scope":
            data["scope_version"] += 1
        elif variant == "inventory":
            data["inventory"][0]["on_hand"] += 1
        elif variant == "reservation_id":
            data["reservations"][0]["reservation_id"] = "rewritten-reservation"
        elif variant == "reservation_source":
            data["reservations"][0]["source_event_id"] = "rewritten-source"
        elif variant == "segment_start":
            data["actuals"][0]["segments"][0]["start_at"] = (
                at(0) + timedelta(seconds=30)
            ).isoformat()
        else:
            data["actuals"][0]["segments"][0]["source_event_id"] = "rewritten-history"

    current = change_snapshot(current, mutate)
    with pytest.raises(ProgressEvidenceError):
        validate_progress_chain(
            original, current, [batch(original, current)], baseline=baseline, candidate=candidate
        )


@pytest.mark.parametrize("field", ["due_at", "priority_weight", "hard_deadline", "status"])
def test_unchanged_operation_scope_does_not_hide_changed_order_commitments(field):
    original, current, _, baseline, candidate = example()
    values = {
        "due_at": at(30).isoformat(),
        "priority_weight": 10,
        "hard_deadline": True,
        "status": "CANCELLED",
    }
    current = change_snapshot(
        current, lambda data: data["orders"][0].update({field: values[field]})
    )
    with pytest.raises(ProgressEvidenceError):
        validate_progress_chain(
            original, current, [batch(original, current)], baseline=baseline, candidate=candidate
        )


def test_new_order_that_did_not_exist_in_original_scope_is_rejected():
    original, current, _, baseline, candidate = example()

    def add(data):
        order = deepcopy(data["orders"][0])
        order.update(order_id="new-rush-order", status="CONFIRMED", priority_weight=10)
        data["orders"].append(order)

    current = change_snapshot(current, add)
    with pytest.raises(ProgressEvidenceError) as failure:
        validate_progress_chain(
            original, current, [batch(original, current)], baseline=baseline, candidate=candidate
        )
    assert failure.value.code == "ORDER_SCOPE_CHANGED"


def test_partial_batch_output_is_not_mistaken_for_normal_remaining_work():
    original, current, _, baseline, candidate = example()
    current = change_snapshot(current, lambda data: data["actuals"][0].update(completed_quantity=1))
    with pytest.raises(ProgressEvidenceError) as failure:
        validate_progress_chain(
            original, current, [batch(original, current)], baseline=baseline, candidate=candidate
        )
    assert failure.value.code == "UNPROVEN_PROGRESS_STATE"


def test_empty_setup_cannot_be_inserted_at_the_next_dispatch_boundary():
    original, current, _, baseline, candidate = example(shift=1)
    assignment = baseline.assignments[0]

    def insert(data):
        data["actuals"].append(
            {
                "operation_id": assignment.operation_id,
                "batch_id": "order-a-R001-B001",
                "route_version": "r1",
                "state": "SETUP",
                "actual_start": None,
                "resource_id": assignment.resource_id,
                "worker_id": assignment.worker_id,
                "completed_quantity": 0,
                "quality_state": "PENDING",
                "remaining_minutes": 2,
                "remaining_confirmed_by": "unobserved-start",
                "remaining_setup_minutes": 0,
                "changeover_start": assignment.changeover_start,
                "version": 2,
            }
        )
        data["resources"][0].update(
            last_operation_id=assignment.operation_id, last_product_id="item-a", version=2
        )

    current = change_snapshot(current, insert)
    with pytest.raises(ProgressEvidenceError) as failure:
        validate_progress_chain(
            original, current, [batch(original, current)], baseline=baseline, candidate=candidate
        )
    assert failure.value.code == "UNPROVEN_EXECUTION_START"


def test_fault_then_restore_is_not_erased_by_final_resource_availability():
    original, _, _, baseline, candidate = example()
    failed = inject(
        original, event_id="failure", kind="resource.down", payload={"resource_id": "r1"}
    )
    restored = inject(
        failed, event_id="repair", kind="resource.restore", payload={"resource_id": "r1"}
    )
    current = advance(restored, baseline)
    changes = [
        batch(original, failed, "resource.down"),
        batch(failed, restored, "resource.restore"),
        batch(restored, current),
    ]
    assert current.resources[0].status == original.resources[0].status == "AVAILABLE"
    with pytest.raises(ProgressEvidenceError) as failure:
        validate_progress_chain(original, current, changes, baseline=baseline, candidate=candidate)
    assert failure.value.code == "MATERIAL_PROGRESS_EVENT"


def test_actual_receipt_is_conservatively_rejected_even_at_exact_confirmed_eta():
    initial = change_snapshot(
        example_snapshot(),
        lambda data: data["receipts"].append(
            {
                "receipt_id": "scheduled-receipt",
                "material_id": "shared-part",
                "unit": "EA",
                "quantity": 2,
                "eta": at(1).isoformat(),
                "status": "CONFIRMED",
            }
        ),
    )
    baseline = make_candidate(initial)
    original = evolve(
        initial, active_plan_version="source-plan-1", active_plan_hash=baseline.content_hash
    )
    current = advance(original, baseline)
    candidate = make_candidate(original)
    assert current.receipts[0].status == "RECEIVED"
    with pytest.raises(ProgressEvidenceError) as failure:
        validate_progress_chain(
            original, current, [batch(original, current)], baseline=baseline, candidate=candidate
        )
    assert failure.value.code == "MATERIAL_PROGRESS_CHANGE"


@pytest.mark.parametrize(
    "variant",
    [
        "baseline",
        "candidate",
        "remaining",
        "unknown",
        "replay",
        "stale",
        "incomplete",
        "final_hash",
    ],
)
def test_mismatched_or_unresolved_original_and_current_facts_fail_closed(variant):
    original, current, changes, baseline, candidate = example(prefix=1)
    if variant == "baseline":
        baseline = make_candidate(example_snapshot(), candidate_id="not-source-plan")
    elif variant == "candidate":
        candidate = make_candidate(current)
    elif variant == "remaining":
        original = change_snapshot(
            original, lambda data: data["actuals"][0].update(remaining_minutes=7)
        )
        candidate = make_candidate(original)
    elif variant == "unknown":
        original = change_snapshot(
            original, lambda data: data["actuals"][0].update(quality_state="UNKNOWN")
        )
    elif variant == "replay":
        original = change_snapshot(
            original, lambda data: data["source"].update(source_system="factory-simulator-replay")
        )
    elif variant == "stale":
        original = change_snapshot(original, lambda data: data["source"].update(freshness="STALE"))
    elif variant == "incomplete":
        original = change_snapshot(original, lambda data: data["source"].update(complete=False))
    else:
        current = change_snapshot(
            current, lambda data: data["source"].update(observed_at="2030-01-02T00:00:00Z")
        )
    with pytest.raises(ProgressEvidenceError):
        validate_progress_chain(original, current, changes, baseline=baseline, candidate=candidate)


def certificate_data():
    data = example()
    original, current, _, baseline, candidate = data
    issued = datetime(2026, 9, 18, tzinfo=UTC)
    return {
        "certificate_id": "certificate-1",
        "factory_id": original.factory_id,
        "run_id": original.run_id,
        "candidate_hash": candidate.content_hash,
        "approval_ids": ["approval-1"],
        "original_binding": candidate.binding,
        "old_snapshot_id": original.snapshot_id,
        "old_snapshot_hash": original.content_hash,
        "old_source_revision": original.source.source_revision,
        "new_snapshot_id": current.snapshot_id,
        "new_snapshot_hash": current.content_hash,
        "new_source_revision": current.source.source_revision,
        "baseline_plan_version": original.active_plan_version,
        "baseline_plan_hash": baseline.content_hash,
        "remaining_plan_hash": "1" * 64,
        "source_evidence_hash": validate(data),
        "checker": CheckReport(
            checker_version="controlled-complete-check",
            snapshot_hash=current.content_hash,
            status="PASS",
        ),
        "metrics": calculate_metrics(current, baseline.assignments, baseline=baseline),
        "issued_at": issued,
        "expires_at": issued + timedelta(seconds=60),
        "business_expires_at": candidate.accept_before,
    }


def test_certificate_is_immutable_and_separates_real_expiry_from_business_clock():
    certificate = ValidationCertificate(**certificate_data())
    assert certificate.expires_at - certificate.issued_at == timedelta(seconds=60)
    assert certificate.business_expires_at.year == 2030 and certificate.issued_at.year == 2026
    assert certificate.checker.snapshot_hash == certificate.new_snapshot_hash
    assert all(metric.lower_bound is None for metric in certificate.metrics)
    assert ValidationCertificate.model_validate_json(certificate.model_dump_json()) == certificate
    with pytest.raises(ValidationError, match="frozen"):
        certificate.new_source_revision = "90"
    modified = certificate.model_dump(mode="json")
    modified["approval_ids"] = ["different-approval"]
    with pytest.raises(ValidationError) as failure:
        ValidationCertificate.model_validate(modified)
    assert failure.value.errors()[0]["type"] == "HASH_MISMATCH"


@pytest.mark.parametrize(
    "variant",
    [
        "no_approval",
        "duplicate_approval",
        "old_binding",
        "baseline_version",
        "same_revision",
        "numeric_alias",
        "same_snapshot",
        "wrong_check",
        "not_pass",
        "missing_metric",
        "duplicate_metric",
        "old_proof",
        "unknown_metric",
        "wrong_unit",
        "naive_time",
        "expired",
        "too_long",
        "confirmed",
    ],
)
def test_certificate_rejects_unbound_expired_or_fabricated_evidence(variant):
    data = ValidationCertificate(**certificate_data()).model_dump(
        mode="json", exclude={"content_hash"}
    )
    if variant == "no_approval":
        data["approval_ids"] = []
    elif variant == "duplicate_approval":
        data["approval_ids"] *= 2
    elif variant == "old_binding":
        data["original_binding"]["snapshot_hash"] = "f" * 64
    elif variant == "baseline_version":
        data["baseline_plan_version"] = "different-baseline"
    elif variant == "same_revision":
        data["new_source_revision"] = data["old_source_revision"]
    elif variant == "numeric_alias":
        data["new_source_revision"] = "0" + data["new_source_revision"]
    elif variant == "same_snapshot":
        data["new_snapshot_id"] = data["old_snapshot_id"]
    elif variant == "wrong_check":
        data["checker"]["snapshot_hash"] = data["old_snapshot_hash"]
    elif variant == "not_pass":
        data["checker"]["status"] = "NOT_RUN"
    elif variant == "missing_metric":
        data["metrics"].pop()
    elif variant == "duplicate_metric":
        data["metrics"][1] = data["metrics"][0]
    elif variant == "old_proof":
        data["metrics"][0]["lower_bound"] = 0
    elif variant == "unknown_metric":
        data["metrics"][0].update(value=None, unknown_reason="not checked")
    elif variant == "wrong_unit":
        data["metrics"][0]["unit"] = "hours"
    elif variant == "naive_time":
        data["issued_at"] = "2026-09-18T00:00:00"
    elif variant == "expired":
        data["expires_at"] = data["issued_at"]
    elif variant == "too_long":
        data["expires_at"] = "2026-09-18T00:01:01Z"
    else:
        data["confirmed"] = True
    with pytest.raises(ValidationError):
        ValidationCertificate.model_validate(data)


def test_json_roundtrip_preserves_source_batch_hash_and_certificate_evidence():
    original, current, changes, baseline, candidate = example(ticks=6)
    before = validate_progress_chain(
        original, current, changes, baseline=baseline, candidate=candidate
    )
    after = validate_progress_chain(
        original, current, json.loads(json.dumps(changes)), baseline=baseline, candidate=candidate
    )
    assert before == after
