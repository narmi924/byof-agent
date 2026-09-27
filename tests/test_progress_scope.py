"""A candidate covers its current scope while the executing baseline may predate new orders."""

from copy import deepcopy
from datetime import timedelta

import pytest

from packages.domain.models import Candidate, Snapshot, batch_operations
from packages.domain.skf import load_skf_snapshot, verify_skf_baseline
from packages.planning.checker import check_candidate, check_revalidated_plan
from packages.planning.progress_evidence import ProgressEvidenceError, validate_progress_chain
from packages.planning.solver import solve
from services.factory_sim.engine import advance, evolve, inject
from tests.test_checker import change_snapshot, example_snapshot, make_candidate
from tests.test_progress_evidence import batch
from tests.test_progress_recovery import progress


@pytest.fixture(scope="module")
def expanded_skf_scope():
    baseline_digest = verify_skf_baseline()
    reference = load_skf_snapshot(development=True)
    data = reference.model_dump(mode="python", exclude={"content_hash"})
    data["orders"][0]["quantity"] = 150
    data["source"].update(source_revision="1", cursor="1")
    data["profile"]["version"] = "scope-progress-example/1"
    data["profile"]["policy"].update(
        policy_version="scope-progress-example/1", progress_revalidation_enabled=True
    )
    initial = Snapshot.model_validate(data)
    assert initial.horizon == reference.horizon
    assert initial.inventory == reference.inventory
    assert initial.resources == reference.resources and initial.workers == reference.workers
    assert initial.profile.model_dump(
        exclude={"version", "policy"}
    ) == reference.profile.model_dump(exclude={"version", "policy"})
    assert initial.profile.policy.model_dump(
        exclude={"policy_version", "progress_revalidation_enabled"}
    ) == reference.profile.policy.model_dump(
        exclude={"policy_version", "progress_revalidation_enabled"}
    )
    assert initial.profile.policy.freeze_window_min == 60
    baseline = solve(initial, time_limit=5)
    assert baseline.has_solution and baseline.checker.status == "PASS", baseline.checker
    assert len(baseline.assignments) == 24
    source = evolve(
        initial, active_plan_hash=baseline.content_hash, active_plan_version="active-three-batches"
    )
    source = advance(source, baseline, minutes=13)
    urgent = initial.orders[0].model_dump(mode="python")
    urgent.update(
        order_id="urgent-before-solving",
        quantity=50,
        priority_weight=5,
        due_at=initial.snapshot_clock + timedelta(hours=8),
    )
    original = inject(source, event_id="urgent-received", kind="order.add", payload=urgent)
    candidate = solve(
        original,
        baseline=baseline,
        time_limit=5,
        new_actions_not_before=original.snapshot_clock + timedelta(minutes=10),
    )
    assert candidate.has_solution and candidate.checker.status == "PASS", candidate.checker
    assert len(candidate.assignments) == 32
    current, changes = progress(original, baseline, 2)
    assert verify_skf_baseline() == baseline_digest
    return original, current, changes, baseline, candidate


def test_order_received_before_solving_is_in_candidate_scope_but_not_executing_baseline(
    expanded_skf_scope,
):
    original, current, changes, baseline, candidate = expanded_skf_scope
    saved = deepcopy(candidate.model_dump(mode="json"))
    batches, operations = batch_operations(original)
    assert len(batches) == 4 and len(operations) == 32
    assert sorted(order.quantity for order in original.orders) == [50, 150]
    old_ids = {row.operation_id for row in baseline.assignments}
    required = {row.operation_id for row in operations}
    new_ids = required - old_ids
    assert len(new_ids) == 8
    assert all(identity.startswith("urgent-before-solving-") for identity in new_ids)
    assert len(changes) == 2
    assert current.snapshot_clock - original.snapshot_clock == timedelta(minutes=2)
    assert check_candidate(original, candidate, baseline=baseline).status == "PASS"
    evidence = validate_progress_chain(
        original, current, changes, baseline=baseline, candidate=candidate
    )
    assert len(evidence) == 64
    checked = check_revalidated_plan(original, current, candidate, baseline=baseline)
    assert checked.report.status == "PASS", checked.report
    assert {row.operation_id for row in checked.assignments} == required
    assert tuple(row for row in checked.assignments if row.operation_id in new_ids) == tuple(
        row for row in candidate.assignments if row.operation_id in new_ids
    )
    assert candidate.model_dump(mode="json") == saved
    assert current.active_plan_hash == baseline.content_hash
    assert not (new_ids & {row.operation_id for row in current.actuals})
    assert current.actuals != original.actuals
    assert current.profile == original.profile and current.horizon == original.horizon


@pytest.mark.parametrize("disguise_as_tick", [False, True])
def test_another_order_arriving_inside_progress_chain_invalidates_earlier_candidate(
    expanded_skf_scope, disguise_as_tick
):
    original, _, _, baseline, candidate = expanded_skf_scope
    before, changes = progress(original, baseline, 1)
    payload = original.orders[-1].model_dump(mode="python")
    payload["order_id"] = "arrived-after-solving"
    if disguise_as_tick:
        after = advance(before, baseline)

        def forge(data):
            data["orders"].append(payload)
            data["scope_version"] += 1

        after = change_snapshot(after, forge)
        changes.append(batch(before, after))
        expected = "MATERIAL_PROGRESS_CHANGE"
    else:
        after = inject(before, event_id="late-urgent", kind="order.add", payload=payload)
        changes.append(batch(before, after, cause="order.add"))
        expected = "MATERIAL_PROGRESS_EVENT"
    with pytest.raises(ProgressEvidenceError, match=expected):
        validate_progress_chain(original, after, changes, baseline=baseline, candidate=candidate)
    checked = check_revalidated_plan(original, after, candidate, baseline=baseline)
    assert checked.report.status == "FAIL"
    assert checked.assignments == () and checked.remaining_plan_hash is None


def test_removed_old_baseline_order_cannot_be_disguised_as_an_expanded_scope():
    initial = example_snapshot()
    baseline = make_candidate(initial)
    original = evolve(
        initial, active_plan_hash=baseline.content_hash, active_plan_version="existing-plan"
    )

    def replace_order(data):
        data["orders"][0]["order_id"] = "replacement-order"
        data["scope_version"] += 1

    original = change_snapshot(original, replace_order)
    assignments = tuple(
        row.model_copy(
            update={"operation_id": row.operation_id.replace("order-a", "replacement-order")}
        )
        for row in baseline.assignments
    )
    candidate = make_candidate(original, assignments)
    current = evolve(original, snapshot_clock=original.snapshot_clock + timedelta(minutes=1))
    with pytest.raises(ProgressEvidenceError, match="BASELINE_SCOPE_MISMATCH"):
        validate_progress_chain(
            original, current, [batch(original, current)], baseline=baseline, candidate=candidate
        )


@pytest.mark.parametrize("omit_all_new_operations", [False, True])
def test_candidate_must_cover_new_order_even_when_old_baseline_did_not(
    expanded_skf_scope, omit_all_new_operations
):
    original, current, changes, baseline, candidate = expanded_skf_scope
    old_ids = {row.operation_id for row in baseline.assignments}
    new_ids = [row.operation_id for row in candidate.assignments if row.operation_id not in old_ids]
    omitted = set(new_ids if omit_all_new_operations else new_ids[:1])
    data = candidate.model_dump(mode="python", exclude={"content_hash"})
    data["assignments"] = [row for row in data["assignments"] if row["operation_id"] not in omitted]
    incomplete = Candidate.model_validate(data)
    assert len(incomplete.assignments) == 32 - len(omitted)
    with pytest.raises(ProgressEvidenceError, match="CANDIDATE_SCOPE_MISMATCH"):
        validate_progress_chain(original, current, changes, baseline=baseline, candidate=incomplete)
    report = check_candidate(original, incomplete, baseline=baseline)
    assert report.status == "FAIL"
    assert {issue.code for issue in report.issues} == {"MISSING_OPERATION"}
    assert {issue.object_id for issue in report.issues} == omitted
