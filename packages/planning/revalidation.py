"""Issue immutable progress certificates while retaining the original human decisions."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from packages.auth import AccessError, Principal
from packages.domain.models import Approval, Candidate, Snapshot, canonical_hash
from packages.domain.revalidation import ValidationCertificate
from packages.integrations.sync import SourceBatch
from packages.planning.checker import RevalidatedPlan, check_revalidated_plan
from packages.planning.preferences import require_current_objective
from packages.planning.progress_evidence import (
    MAX_PROGRESS_REVISIONS,
    ProgressEvidenceError,
    validate_progress_chain,
)
from packages.planning.revalidation_check import verify_certificate
from packages.planning.revalidation_store import ValidationRecord
from packages.planning.service import active_baseline, require_live
from packages.planning.store import CandidateRecord, FactoryState, SnapshotRecord

MAX_CHAIN_REVISIONS = MAX_PROGRESS_REVISIONS


@dataclass(frozen=True)
class CheckedProgress:
    original: Snapshot
    baseline: Candidate
    checked: RevalidatedPlan
    evidence_hash: str


def original_snapshot(db: Session, candidate: Candidate) -> Snapshot:
    record = db.scalar(
        select(SnapshotRecord).where(
            SnapshotRecord.factory_id == candidate.factory_id,
            SnapshotRecord.content_hash == candidate.binding.snapshot_hash,
        )
    )
    if record is None:
        raise AccessError(
            "VALIDATION_BASE_MISSING",
            "The fact snapshot behind the original approval is incomplete; rescheduling is needed.",
            409,
        )
    original = Snapshot.model_validate(record.document)
    if original.content_hash != record.content_hash:
        raise AccessError(
            "VALIDATION_BASE_INVALID", "The original fact evidence failed validation.", 409
        )
    return original


def source_batches(db: Session, original: Snapshot, current: Snapshot) -> list[dict]:
    first, last = int(original.source.source_revision), int(current.source.source_revision)
    if not 0 < last - first <= MAX_CHAIN_REVISIONS:
        raise AccessError(
            "VALIDATION_HISTORY_RANGE",
            "The production version did not advance or the history exceeds the range of one check; reschedule.",
            409,
        )
    rows = list(
        db.scalars(
            select(SourceBatch)
            .where(
                SourceBatch.factory_id == current.factory_id,
                SourceBatch.run_id == current.run_id,
                SourceBatch.revision > first,
                SourceBatch.revision <= last,
            )
            .order_by(SourceBatch.revision)
            .limit(MAX_CHAIN_REVISIONS + 1)
        )
    )
    if len(rows) != last - first or any(
        row.content_hash != canonical_hash(row.document) for row in rows
    ):
        raise AccessError(
            "VALIDATION_HISTORY_INCOMPLETE",
            "The production event chain is incomplete or failed validation; reschedule.",
            409,
        )
    return [row.document for row in rows]


def check_progress(db: Session, current: Snapshot, candidate: Candidate) -> CheckedProgress:
    """Prove unchanged intent; this check grants no approval or publication permission."""
    require_live(current)
    objective = require_current_objective(db, current, candidate.binding.objective_version)
    original = original_snapshot(db, candidate)
    baseline = active_baseline(db, current)
    if baseline is None:
        raise AccessError(
            "VALIDATION_BASELINE_REQUIRED",
            "The old plan accepted by the execution source is missing, so the normal prefix cannot be checked.",
            409,
        )
    try:
        evidence_hash = validate_progress_chain(
            original,
            current,
            source_batches(db, original, current),
            baseline=baseline,
            candidate=candidate,
        )
        checked = check_revalidated_plan(
            original, current, candidate, baseline=baseline, objective=objective
        )
    except ProgressEvidenceError as exc:
        raise AccessError(
            exc.code,
            "The change cannot be shown to be only normal progress of the old plan; rescheduling and approval are needed.",
            409,
        ) from exc
    if checked.report.status != "PASS" or checked.remaining_plan_hash is None:
        raise AccessError(
            "VALIDATION_CHECK_FAILED",
            "The remaining plan failed the full check under the latest facts; rescheduling is needed.",
            409,
        )
    return CheckedProgress(original, baseline, checked, evidence_hash)


def issue_certificate(
    engine: Engine,
    actor: Principal,
    factory_id: str,
    candidate_id: str,
    *,
    request_id: str,
    candidate_hash: str,
) -> ValidationCertificate:
    from packages.planning.publication import _approvals, _authorized

    actor.require(factory_id, {"planner"})
    with Session(engine) as db, db.begin():
        state = db.get(FactoryState, factory_id, with_for_update=True)
        if not _authorized(db, factory_id, actor.user_id, "planner"):
            raise AccessError("FORBIDDEN", "The check permission is no longer valid.")
        payload_hash = canonical_hash(
            {"candidate_id": candidate_id, "candidate_hash": candidate_hash}
        )
        prior = db.scalar(
            select(ValidationRecord).where(
                ValidationRecord.factory_id == factory_id, ValidationRecord.request_id == request_id
            )
        )
        if prior:
            if prior.requester_id != actor.user_id or prior.payload_hash != payload_hash:
                raise AccessError(
                    "IDEMPOTENCY_CONFLICT",
                    "The original check action ID was already used for other content.",
                    409,
                )
            return ValidationCertificate.model_validate(prior.document)
        record = db.get(CandidateRecord, candidate_id)
        saved = db.get(SnapshotRecord, state.snapshot_id) if state else None
        if state is None or saved is None or record is None or record.factory_id != factory_id:
            raise AccessError("NOT_FOUND", "The plan or current factory facts do not exist.", 404)
        current = Snapshot.model_validate(saved.document)
        if current.factory_id != factory_id or current.run_id != state.run_id:
            raise AccessError(
                "SOURCE_RUN_CHANGED",
                "The current facts do not belong to the selected factory run.",
                409,
            )
        require_live(current)
        if datetime.now(UTC) - state.last_synced_at > timedelta(seconds=30):
            raise AccessError("STALE_SOURCE", "The factory data is out of date; sync first.", 409)
        candidate = Candidate.model_validate(record.document)
        if candidate.content_hash != candidate_hash or record.content_hash != candidate_hash:
            raise AccessError(
                "CANDIDATE_CHANGED",
                "The plan has changed; the original approval cannot be reused.",
                409,
            )
        approvals = _approvals(db, candidate)
        proof = check_progress(db, current, candidate)
        original, checked = proof.original, proof.checked
        approvals = _approvals(db, candidate)
        now = datetime.now(UTC)
        if any(a.expires_at <= now for a in approvals):
            raise AccessError(
                "APPROVAL_REQUIRED",
                "The original approval has expired; rescheduling and approval are needed.",
                409,
            )
        certificate = ValidationCertificate(
            certificate_id=str(uuid4()),
            factory_id=factory_id,
            run_id=current.run_id,
            candidate_hash=candidate.content_hash,
            approval_ids=tuple(a.approval_id for a in approvals),
            original_binding=candidate.binding,
            old_snapshot_id=original.snapshot_id,
            old_snapshot_hash=str(original.content_hash),
            old_source_revision=original.source.source_revision,
            new_snapshot_id=current.snapshot_id,
            new_snapshot_hash=str(current.content_hash),
            new_source_revision=current.source.source_revision,
            baseline_plan_version=str(current.active_plan_version),
            baseline_plan_hash=str(current.active_plan_hash),
            remaining_plan_hash=str(checked.remaining_plan_hash),
            source_evidence_hash=proof.evidence_hash,
            checker=checked.report,
            metrics=checked.metrics,
            issued_at=now,
            expires_at=min(now + timedelta(seconds=30), *(a.expires_at for a in approvals)),
            business_expires_at=candidate.accept_before,
        )
        db.add(
            ValidationRecord(
                certificate_id=certificate.certificate_id,
                factory_id=factory_id,
                candidate_id=candidate_id,
                request_id=request_id,
                requester_id=actor.user_id,
                payload_hash=payload_hash,
                document=certificate.model_dump(mode="json"),
                created_at=now,
            )
        )
        return certificate


def require_certificate(
    db: Session,
    current: Snapshot,
    candidate: Candidate,
    approvals: tuple[Approval, ...],
    certificate_id: str,
) -> ValidationCertificate:
    row = db.get(ValidationRecord, certificate_id)
    if (
        row is None
        or row.factory_id != current.factory_id
        or row.candidate_id != candidate.candidate_id
    ):
        raise AccessError(
            "VALIDATION_NOT_FOUND",
            "The check certificate does not belong to this plan and factory.",
            409,
        )
    certificate = ValidationCertificate.model_validate(row.document)
    original = original_snapshot(db, candidate)
    baseline = active_baseline(db, current)
    objective = require_current_objective(db, current, candidate.binding.objective_version)
    if baseline is None:
        raise AccessError(
            "VALIDATION_BASELINE_REQUIRED", "The evidence of the executed old plan is missing.", 409
        )
    try:
        verify_certificate(
            certificate,
            original,
            current,
            candidate,
            approvals,
            source_batches(db, original, current),
            baseline=baseline,
            objective=objective,
        )
    except ProgressEvidenceError as exc:
        raise AccessError(
            exc.code,
            "The check certificate is invalid or its conditions changed; check the remaining plan again.",
            409,
        ) from exc
    return certificate


def certificates(db: Session, factory_id: str) -> list[ValidationCertificate]:
    return [
        ValidationCertificate.model_validate(row.document)
        for row in db.scalars(
            select(ValidationRecord)
            .where(ValidationRecord.factory_id == factory_id)
            .order_by(ValidationRecord.created_at.desc())
            .limit(30)
        )
    ]
