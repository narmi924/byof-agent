"""Versioned candidate export is a document for human review, with no execution side effect."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from packages.auth import AccessError, Principal, lock_membership, lock_user
from packages.domain.models import Candidate, Snapshot, canonical_hash
from packages.planning.checker import check_candidate
from packages.planning.preferences import load_objective, require_current_objective
from packages.planning.revalidation import check_progress
from packages.planning.service import active_baseline
from packages.planning.store import CandidateRecord, FactoryState, SnapshotRecord


def export_candidate(
    engine: Engine, actor: Principal, factory_id: str, candidate_id: str, *, candidate_hash: str
) -> dict:
    actor.require(factory_id, {"planner"})
    with Session(engine) as db, db.begin():
        state = db.get(FactoryState, factory_id, with_for_update={"read": True})
        user = lock_user(db, actor.user_id)
        if (
            user is None
            or not user.active
            or lock_membership(db, actor.user_id, factory_id, "planner") is None
        ):
            raise AccessError("FORBIDDEN", "This account may not export plans of this factory.")
        record = db.get(CandidateRecord, candidate_id)
        if state is None or record is None or record.factory_id != factory_id:
            raise AccessError("NOT_FOUND", "The plan or factory facts do not exist.", 404)
        candidate = Candidate.model_validate(record.document)
        if candidate.content_hash != candidate_hash or record.content_hash != candidate_hash:
            raise AccessError("CANDIDATE_CHANGED", "The plan has changed; review it again.", 409)
        original_record = db.get(SnapshotRecord, record.snapshot_id)
        current_record = db.get(SnapshotRecord, state.snapshot_id)
        if original_record is None or current_record is None:
            raise AccessError(
                "SNAPSHOT_REQUIRED",
                "The original or current facts of the plan are missing, so it cannot be exported.",
                409,
            )
        original, current = (
            Snapshot.model_validate(r.document) for r in (original_record, current_record)
        )
        objective = load_objective(db, factory_id, candidate.binding.objective_version)
        original_check = check_candidate(
            original,
            candidate,
            baseline=active_baseline(db, original),
            allow_overtime="allow_overtime" in candidate.required_consents,
            objective=objective,
        )
        if not candidate.has_solution or original_check.status != "PASS":
            raise AccessError(
                "CHECK_FAILED",
                "The plan has no schedule that passed the full check, so it cannot be exported.",
                409,
            )
        current_check, current_error = None, None
        try:
            if original.run_id != current.run_id:
                raise AccessError("SOURCE_RUN_CHANGED", "This plan belongs to an old run.", 409)
            if datetime.now(UTC) - state.last_synced_at > timedelta(seconds=30):
                raise AccessError("STALE_SOURCE", "The current facts are out of date.", 409)
            require_current_objective(db, current, candidate.binding.objective_version)
            if current.content_hash == original.content_hash:
                current_check = original_check
            else:
                current_check = check_progress(db, current, candidate).checked.report
        except AccessError as exc:
            current_error = exc.code
        exported_at = datetime.now(UTC)
        if current_check is not None and exported_at - state.last_synced_at > timedelta(seconds=30):
            current_check, current_error = None, "STALE_SOURCE"
        document: dict[str, object] = {
            "schema_version": "byof.plan-export/1",
            "purpose": "MANUAL_REVIEW",
            "factory_id": factory_id,
            "run_id": original.run_id,
            "candidate": candidate.model_dump(mode="json"),
            "original_snapshot_id": original.snapshot_id,
            "original_snapshot_hash": original.content_hash,
            "current_snapshot_id": current.snapshot_id,
            "current_snapshot_hash": current.content_hash,
            "original_checker": original_check.model_dump(mode="json"),
            "current_checker": current_check.model_dump(mode="json") if current_check else None,
            "current_error_code": current_error,
            "exported_by": actor.user_id,
            "exported_at": exported_at.isoformat(),
            "notice": "For manual review and import; the latest factory facts and the approved plan take precedence.",
        }
        return {**document, "content_hash": canonical_hash(document)}
