"""An explicit human review can approve unchanged work against proven newer source facts."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from packages.auth import AccessError, Principal
from packages.domain.approval_review import ApprovalReview
from packages.domain.models import Approval, Candidate, Snapshot
from packages.planning.publication import _authorized
from packages.planning.revalidation import check_progress
from packages.planning.review_store import ApprovalReviewRecord
from packages.planning.store import ApprovalRecord, CandidateRecord, FactoryState, SnapshotRecord


def approve_progress(
    engine: Engine,
    actor: Principal,
    factory_id: str,
    candidate_id: str,
    *,
    request_id: str,
    candidate_hash: str,
    action_scope: str,
    decision: str,
) -> Approval:
    if decision != "APPROVED" or action_scope not in {"publish_plan", "allow_overtime"}:
        raise AccessError("INVALID_INPUT", "Choose the scope that needs explicit approval.", 422)
    role = "manager" if action_scope == "allow_overtime" else "planner"
    actor.require(factory_id, {role})
    with Session(engine) as db, db.begin():
        state = db.get(FactoryState, factory_id, with_for_update=True)
        from packages.agent.human_tasks import lock_review_cases

        lock_review_cases(db, factory_id)
        if not _authorized(db, factory_id, actor.user_id, role):
            raise AccessError("FORBIDDEN", "This account may no longer make this approval.")
        previous = db.scalar(
            select(ApprovalRecord).where(
                ApprovalRecord.factory_id == factory_id, ApprovalRecord.request_id == request_id
            )
        )
        if previous:
            old = Approval.model_validate(previous.document)
            evidence = db.scalar(
                select(ApprovalReviewRecord).where(
                    ApprovalReviewRecord.approval_id == previous.approval_id
                )
            )
            if (
                evidence is None
                or previous.candidate_id != candidate_id
                or old.candidate_hash != candidate_hash
                or old.approver_id != actor.user_id
                or old.action_scope != action_scope
                or old.decision != decision
            ):
                raise AccessError(
                    "IDEMPOTENCY_CONFLICT",
                    "The original request was used for another review mode or decision.",
                    409,
                )
            return old
        record = db.get(CandidateRecord, candidate_id)
        saved = db.get(SnapshotRecord, state.snapshot_id) if state else None
        if state is None or saved is None or record is None or record.factory_id != factory_id:
            raise AccessError("NOT_FOUND", "The plan or current factory facts do not exist.", 404)
        current = Snapshot.model_validate(saved.document)
        candidate = Candidate.model_validate(record.document)
        if current.run_id != state.run_id or current.factory_id != factory_id:
            raise AccessError(
                "SOURCE_RUN_CHANGED", "The factory run has changed; check again.", 409
            )
        if record.content_hash != candidate_hash or candidate.content_hash != candidate_hash:
            raise AccessError("CANDIDATE_CHANGED", "The plan has changed; review it again.", 409)
        if datetime.now(UTC) - state.last_synced_at > timedelta(seconds=30):
            raise AccessError("STALE_SOURCE", "The factory data is out of date; sync first.", 409)
        if action_scope == "allow_overtime" and action_scope not in candidate.required_consents:
            raise AccessError(
                "CONSENT_NOT_REQUIRED", "This plan requests no extra overtime permission.", 409
            )
        proof = check_progress(db, current, candidate)
        if not _authorized(db, factory_id, actor.user_id, role):
            raise AccessError(
                "FORBIDDEN", "The approval permission expired when the check finished."
            )
        now = datetime.now(UTC)
        if now - state.last_synced_at > timedelta(seconds=30):
            raise AccessError(
                "STALE_SOURCE",
                "The factory data went out of date during the check; sync again.",
                409,
            )
        approval = Approval.model_validate(
            {
                "approval_id": str(uuid4()),
                "factory_id": factory_id,
                "candidate_hash": candidate_hash,
                "binding": candidate.binding,
                "approver_id": actor.user_id,
                "approver_role": role,
                "action_scope": action_scope,
                "decision": "APPROVED",
                "decided_at": now,
                "expires_at": now + timedelta(hours=1),
            }
        )
        review = ApprovalReview.model_validate(
            {
                "review_id": str(uuid4()),
                "factory_id": factory_id,
                "run_id": current.run_id,
                "candidate_id": candidate_id,
                "candidate_hash": candidate_hash,
                "approval_id": approval.approval_id,
                "approver_id": actor.user_id,
                "approver_role": role,
                "action_scope": action_scope,
                "original_binding": candidate.binding,
                "old_snapshot_id": proof.original.snapshot_id,
                "old_snapshot_hash": proof.original.content_hash,
                "old_source_revision": proof.original.source.source_revision,
                "new_snapshot_id": current.snapshot_id,
                "new_snapshot_hash": current.content_hash,
                "new_source_revision": current.source.source_revision,
                "baseline_plan_version": current.active_plan_version,
                "baseline_plan_hash": current.active_plan_hash,
                "remaining_plan_hash": proof.checked.remaining_plan_hash,
                "source_evidence_hash": proof.evidence_hash,
                "checker": proof.checked.report,
                "metrics": proof.checked.metrics,
                "reviewed_at": now,
            }
        )
        db.add(
            ApprovalRecord(
                approval_id=approval.approval_id,
                request_id=request_id,
                factory_id=factory_id,
                candidate_id=candidate_id,
                document=approval.model_dump(mode="json"),
                created_at=now,
            )
        )
        db.add(
            ApprovalReviewRecord(
                review_id=review.review_id,
                factory_id=factory_id,
                candidate_id=candidate_id,
                approval_id=approval.approval_id,
                document=review.model_dump(mode="json"),
                created_at=now,
            )
        )
        from packages.agent.human_tasks import reconcile_reviews

        db.flush()
        reconcile_reviews(db, current)
        return approval


def reviews(db: Session, factory_id: str) -> list[ApprovalReview]:
    return [
        ApprovalReview.model_validate(row.document)
        for row in db.scalars(
            select(ApprovalReviewRecord)
            .where(ApprovalReviewRecord.factory_id == factory_id)
            .order_by(ApprovalReviewRecord.created_at.desc())
            .limit(60)
        )
    ]
