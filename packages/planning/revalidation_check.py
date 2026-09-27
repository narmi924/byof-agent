"""Recompute certificate evidence from original and current business facts on both sides."""

from collections.abc import Sequence
from datetime import UTC, datetime

from packages.domain.models import Approval, Candidate, Snapshot
from packages.domain.objectives import EffectiveObjective
from packages.domain.revalidation import ValidationCertificate
from packages.planning.checker import check_revalidated_plan
from packages.planning.progress_evidence import ProgressEvidenceError, validate_progress_chain


def verify_certificate(
    certificate: ValidationCertificate,
    original: Snapshot,
    current: Snapshot,
    candidate: Candidate,
    approvals: Sequence[Approval],
    batches: Sequence[dict],
    *,
    baseline: Candidate,
    objective: EffectiveObjective | None = None,
) -> None:
    cert = ValidationCertificate.model_validate(certificate)
    now = datetime.now(UTC)
    if not cert.issued_at <= now < cert.expires_at:
        raise ProgressEvidenceError("VALIDATION_EXPIRED")
    if not current.snapshot_clock < cert.business_expires_at == candidate.accept_before:
        raise ProgressEvidenceError("VALIDATION_BUSINESS_WINDOW_EXPIRED")
    if (
        cert.factory_id != current.factory_id
        or cert.run_id != current.run_id
        or cert.candidate_hash != candidate.content_hash
        or cert.original_binding != candidate.binding
        or cert.old_snapshot_id != original.snapshot_id
        or cert.old_snapshot_hash != original.content_hash
        or cert.old_source_revision != original.source.source_revision
        or cert.new_snapshot_id != current.snapshot_id
        or cert.new_snapshot_hash != current.content_hash
        or cert.new_source_revision != current.source.source_revision
        or cert.baseline_plan_version != current.active_plan_version
        or cert.baseline_plan_hash != current.active_plan_hash
        or set(cert.approval_ids) != {a.approval_id for a in approvals}
    ):
        raise ProgressEvidenceError("VALIDATION_BINDING_CHANGED")
    required = {"publish_plan", *candidate.required_consents}
    if len({a.approval_id for a in approvals}) != len(approvals) or not required <= {
        a.action_scope
        for a in approvals
        if a.factory_id == current.factory_id
        and a.candidate_hash == candidate.content_hash
        and a.binding == candidate.binding
        and a.decision == "APPROVED"
        and a.decided_at <= now < a.expires_at
    }:
        raise ProgressEvidenceError("APPROVAL_REQUIRED")
    evidence_hash = validate_progress_chain(
        original, current, batches, baseline=baseline, candidate=candidate
    )
    checked = check_revalidated_plan(
        original, current, candidate, baseline=baseline, objective=objective
    )
    if (
        checked.report.status != "PASS"
        or cert.source_evidence_hash != evidence_hash
        or cert.remaining_plan_hash != checked.remaining_plan_hash
        or cert.checker != checked.report
        or cert.metrics != checked.metrics
    ):
        raise ProgressEvidenceError("VALIDATION_CHECK_FAILED")
    # Reconstructing a long event chain can cross a real authorization deadline.
    now = datetime.now(UTC)
    if not cert.issued_at <= now < cert.expires_at:
        raise ProgressEvidenceError("VALIDATION_EXPIRED")
    if any(not a.decided_at <= now < a.expires_at for a in approvals):
        raise ProgressEvidenceError("APPROVAL_REQUIRED")
