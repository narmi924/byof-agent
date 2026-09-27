"""The execution source verifies certificates against its own immutable public history."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.domain.execution import PlanSubmission
from packages.domain.models import Candidate, Snapshot
from packages.domain.snapshot_delta import apply_delta
from packages.planning.progress_evidence import ProgressEvidenceError
from packages.planning.revalidation_check import verify_certificate
from services.factory_sim.storage import SourceChange, SourceRun

MAX_SOURCE_HISTORY = 10_000


def check_source_certificate(
    db: Session, submission: PlanSubmission, current: Snapshot, baseline: Candidate | None
) -> None:
    certificate = submission.certificate
    if certificate is None or baseline is None:
        raise ProgressEvidenceError("VALIDATION_BASELINE_REQUIRED")
    run = db.get(SourceRun, current.run_id)
    if run is None or run.factory_id != current.factory_id:
        raise ProgressEvidenceError("VALIDATION_HISTORY_INCOMPLETE")
    anchor = Snapshot.model_validate(run.initial_snapshot)
    first = int(anchor.source.source_revision)
    old = int(certificate.old_source_revision)
    last = int(current.source.source_revision)
    if not first <= old < last or last - first > MAX_SOURCE_HISTORY:
        raise ProgressEvidenceError("VALIDATION_HISTORY_RANGE")
    changes = list(
        db.scalars(
            select(SourceChange)
            .where(
                SourceChange.factory_id == current.factory_id,
                SourceChange.run_id == current.run_id,
                SourceChange.revision > first,
                SourceChange.revision <= last,
            )
            .order_by(SourceChange.revision)
            .limit(MAX_SOURCE_HISTORY + 1)
        )
    )
    if len(changes) != last - first:
        raise ProgressEvidenceError("VALIDATION_HISTORY_INCOMPLETE")
    original = anchor if first == old else None
    batches = []
    for revision, change in enumerate(changes, start=first + 1):
        if revision != change.revision:
            raise ProgressEvidenceError("VALIDATION_HISTORY_INCOMPLETE")
        if revision <= old:
            document = change.document
            anchor = apply_delta(anchor, document["snapshot_delta"])
            if anchor.content_hash != document["snapshot_hash"]:
                raise ProgressEvidenceError("VALIDATION_HISTORY_INVALID")
            if revision == old:
                original = anchor
        else:
            batches.append(change.document)
    if original is None:
        raise ProgressEvidenceError("VALIDATION_BASE_MISSING")
    verify_certificate(
        certificate,
        original,
        current,
        submission.candidate,
        submission.approvals,
        batches,
        baseline=baseline,
        objective=submission.objective,
    )
