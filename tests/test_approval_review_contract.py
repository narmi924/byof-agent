"""Immutable human review evidence cannot carry stale facts or old optimality bounds."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from test_progress_checker import initial_case

from packages.domain.approval_review import ApprovalReview
from packages.domain.models import canonical_hash
from packages.planning.checker import check_revalidated_plan
from services.factory_sim.engine import advance


@pytest.fixture(scope="module")
def document():
    original, baseline, candidate = initial_case()
    latest = advance(original, baseline, minutes=2)
    proof = check_revalidated_plan(original, latest, candidate, baseline=baseline)
    assert proof.report.status == "PASS"
    return ApprovalReview(
        review_id="explicit-human-review",
        factory_id=original.factory_id,
        run_id=original.run_id,
        candidate_id=candidate.candidate_id,
        candidate_hash=candidate.content_hash,
        approval_id="actual-approval",
        approver_id="authenticated-planner",
        approver_role="planner",
        action_scope="publish_plan",
        original_binding=candidate.binding,
        old_snapshot_id=original.snapshot_id,
        old_snapshot_hash=original.content_hash,
        old_source_revision=original.source.source_revision,
        new_snapshot_id=latest.snapshot_id,
        new_snapshot_hash=latest.content_hash,
        new_source_revision=latest.source.source_revision,
        baseline_plan_version=latest.active_plan_version,
        baseline_plan_hash=latest.active_plan_hash,
        remaining_plan_hash=proof.remaining_plan_hash,
        source_evidence_hash=canonical_hash(
            {"from": original.content_hash, "to": latest.content_hash}
        ),
        checker=proof.report,
        metrics=proof.metrics,
        reviewed_at=datetime.now(UTC),
    ).model_dump(mode="json", exclude={"content_hash"})


@pytest.mark.parametrize(
    "change",
    [
        {"approver_role": "manager"},
        {"action_scope": "allow_overtime"},
        {"new_source_revision": "01"},
        {"new_source_revision": "1"},
        {"new_source_revision": "three"},
        {"baseline_plan_version": "other-baseline"},
        {"confirmed": True},
        {"content_hash": "0" * 64},
    ],
)
def test_review_rejects_wrong_role_intent_versions_or_forged_hash(document, change):
    with pytest.raises(ValueError):
        ApprovalReview.model_validate({**deepcopy(document), **change})


@pytest.mark.parametrize(
    "change",
    [
        {"lower_bound": 0},
        {"value": None, "unknown_reason": "not-confirmed"},
        {"value": -1},
        {"unit": "guessed-unit"},
    ],
)
def test_latest_metrics_cannot_reuse_proof_unknown_values_or_units(document, change):
    data = deepcopy(document)
    data["metrics"][0].update(change)
    with pytest.raises(ValueError):
        ApprovalReview.model_validate(data)


def test_review_requires_latest_check_and_all_metrics(document):
    for key in ("snapshot_hash", "status"):
        data = deepcopy(document)
        data["checker"][key] = data["old_snapshot_hash"] if key == "snapshot_hash" else "FAIL"
        with pytest.raises(ValueError):
            ApprovalReview.model_validate(data)
    data = deepcopy(document)
    data["metrics"].pop()
    with pytest.raises(ValueError):
        ApprovalReview.model_validate(data)
    original = ApprovalReview.model_validate(document)
    assert ApprovalReview.model_validate_json(original.model_dump_json()) == original
