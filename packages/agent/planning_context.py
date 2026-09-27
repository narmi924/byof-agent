"""Compact solver outcomes and a substantive-fact key for avoiding identical failed solves."""

from datetime import datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from packages.agent.cases_store import CaseInput
from packages.domain.models import Snapshot, canonical_hash
from packages.domain.production_facts import material_shortfalls as material_shortfalls
from packages.planning.store import CandidateRecord, SnapshotRecord, SolveJob


def current_tool_result(
    action: str, result: dict | None, snapshot_hash: str | None, same_turn: bool = False
) -> dict | None:
    """Keep old observations auditable without presenting their numbers as current facts.

    A delivery report made in this turn stays readable for the answer it was made for.
    """
    if (
        result
        and action in {"query", "reply", "report_production"}
        and not (same_turn and action == "report_production")
        and result.get("snapshot_hash") is not None
        and result.get("snapshot_hash") != snapshot_hash
    ):
        return {
            "status": "HISTORICAL",
            "snapshot_hash": result.get("snapshot_hash"),
            "summary": "The shop floor has been updated; this historical query or reply is not a current fact. Use the current figures or query again.",
        }
    return result


def execution_status(db: Session, snapshot: Snapshot) -> dict:
    record = (
        db.scalar(
            select(CandidateRecord).where(
                CandidateRecord.factory_id == snapshot.factory_id,
                CandidateRecord.content_hash == snapshot.active_plan_hash,
            )
        )
        if snapshot.active_plan_hash
        else None
    )
    actuals = {a.operation_id: a for a in snapshot.actuals}
    starts = [
        a.get("resume_changeover_start") or a["changeover_start"]
        for a in (record.document["assignments"] if record else [])
        if a["operation_id"] not in actuals
    ]
    future = [datetime.fromisoformat(s.replace("Z", "+00:00")) for s in starts]
    return {
        "next_planned_start": min(
            (s for s in future if s >= snapshot.snapshot_clock), default=None
        ),
        "blocked_operations": [a.operation_id for a in snapshot.actuals if a.state == "BLOCKED"],
        "completed_operations": sum(a.state == "COMPLETED" for a in snapshot.actuals),
        "instruction": "Before the planned start, wait for source events; do not keep querying whether production started.",
    }


def planning_key(snapshot: Snapshot) -> str:
    data = snapshot.model_dump(
        mode="json",
        exclude={
            "content_hash",
            "schema_version",  # Wire-format upgrades alone are not business changes.
            "snapshot_id",
            "snapshot_clock",
            "source",
            "planning_revision",
            "events",
        },
    )
    for field in ("orders", "resources", "workers", "actuals", "inventory", "receipts"):
        for row in data.get(field, []):
            row.pop("version", None)
            row.pop("remaining_confirmed_by", None)
    return canonical_hash(data)


def decision_key(snapshot: Snapshot) -> str:
    """Facts behind a manager's decision; the active plan's normal progress leaves it unchanged.

    While the factory runs, steps advance, kits are reserved and consumed, receipts arrive on
    time and orders start or finish. A decision still stands unless demand, supply commitments,
    equipment, people, stopped or failed work, the active plan or the missing material change.
    """
    return canonical_hash(
        {
            "run_id": snapshot.run_id,
            "scope_version": snapshot.scope_version,
            "active_plan_hash": snapshot.active_plan_hash,
            "profile": snapshot.profile.model_dump(mode="json"),
            "business_terms": snapshot.business_terms.model_dump(mode="json")
            if snapshot.business_terms
            else None,
            "production_batches": [
                b.model_dump(mode="json") for b in snapshot.production_batches or ()
            ],
            "orders": [
                o.model_dump(mode="json", exclude={"version", "status"})
                | {"cancelled": o.status == "CANCELLED"}
                for o in snapshot.orders
            ],
            "receipts": [
                r.model_dump(mode="json", include={"receipt_id", "material_id", "quantity", "eta"})
                | {"cancelled": r.status == "CANCELLED"}
                for r in snapshot.receipts
            ],
            "resources": [
                r.model_dump(
                    mode="json", exclude={"version", "last_operation_id", "last_product_id"}
                )
                for r in snapshot.resources
            ],
            "workers": [w.model_dump(mode="json", exclude={"version"}) for w in snapshot.workers],
            # Waiting for a predecessor is normal; unknown remaining work follows a disruption.
            "stopped": sorted(
                a.operation_id
                for a in snapshot.actuals
                if a.state == "BLOCKED" and a.remaining_minutes is None
            ),
            "failed": sorted(
                a.operation_id for a in snapshot.actuals if a.quality_state == "FAILED"
            ),
            "shortfalls": material_shortfalls(snapshot),
        }
    )


def adds_lateness(db: Session, snapshot: Snapshot, candidate: dict) -> bool:
    """Whether a plan delivers later than the plan in effect (any delay on the first plan)."""
    from packages.planning.service import active_baseline

    def tardiness(document: dict) -> int:
        return next(
            (
                m.get("value") or 0
                for m in document.get("objective", ())
                if m.get("name") == "weighted_tardiness"
            ),
            0,
        )

    baseline = active_baseline(db, snapshot)
    before = tardiness(baseline.model_dump(mode="json")) if baseline is not None else 0
    return tardiness(candidate) > before


def outcomes(db: Session, case_id: str) -> list[dict]:
    result = []
    for job in db.scalars(
        select(SolveJob)
        .where(SolveJob.case_id == case_id, SolveJob.business_request.is_(None))
        .order_by(SolveJob.created_at.desc())
        .limit(5)
    ):
        row = db.get(CandidateRecord, job.candidate_id) if job.candidate_id else None
        candidate = row.document if row else {}
        result.append(
            {
                "job_id": job.job_id,
                "snapshot_id": job.snapshot_id,
                "state": job.state,
                "allow_overtime": job.allow_overtime,
                "candidate_id": job.candidate_id,
                "error_code": job.error_code,
                "has_solution": candidate.get("has_solution"),
                "native_status": candidate.get("native_status"),
                "termination_reason": candidate.get("termination_reason"),
                "checker": candidate.get("checker"),
                "objective": candidate.get("objective"),
                "summary": "A feasible schedule was found and still needs review."
                if candidate.get("has_solution")
                else "No feasible schedule exists under the current constraints (proven); it cannot be submitted for approval."
                if candidate.get("native_status") == "INFEASIBLE"
                else "This calculation ended without finding a feasible plan and without proving there is none; it cannot be submitted for approval. Analyze it with the termination reason and the verified shop floor facts."
                if row
                else "Calculating; wait for the solve result.",
            }
        )
    return result


def latest_solver_state(
    db: Session, snapshot: Snapshot, case_id: str
) -> Literal["INFEASIBLE", "UNKNOWN", "FAILED"] | None:
    latest = db.scalar(
        select(SolveJob)
        .where(
            SolveJob.factory_id == snapshot.factory_id,
            SolveJob.case_id == case_id,
            SolveJob.business_request.is_(None),
            SolveJob.state.in_(("SUCCEEDED", "FAILED")),
        )
        .order_by(SolveJob.created_at.desc())
        .limit(1)
    )
    prior = db.get(SnapshotRecord, latest.snapshot_id) if latest else None
    if (
        not latest
        or not prior
        or planning_key(Snapshot.model_validate(prior.document)) != planning_key(snapshot)
    ):
        return None
    if latest.state == "FAILED":
        return "FAILED"
    candidate = db.get(CandidateRecord, latest.candidate_id) if latest.candidate_id else None
    if candidate and not candidate.document.get("has_solution"):
        return (
            "INFEASIBLE" if candidate.document.get("native_status") == "INFEASIBLE" else "UNKNOWN"
        )
    return None


# Searches on one unchanged problem between two manager messages: the first try plus one
# automatic retry. Anything more waits for the manager instead of recomputing silently.
AUTOMATIC_SEARCHES = 2


def _found_plan(db: Session, job: SolveJob) -> bool:
    candidate = db.get(CandidateRecord, job.candidate_id) if job.candidate_id else None
    return job.state == "SUCCEEDED" and bool(candidate and candidate.document.get("has_solution"))


def solve_limit_reason(
    db: Session, case_id: str, snapshot: Snapshot, parameters: dict
) -> str | None:
    """Bound one unchanged problem across solver-result wakeups and worker restarts."""
    key = planning_key(snapshot)
    epoch = db.scalar(
        select(func.max(CaseInput.created_at)).where(
            CaseInput.case_id == case_id,
            CaseInput.kind == "USER",
            CaseInput.cancelled_at.is_(None),
        )
    )
    attempts = 0
    for job, record in db.execute(
        select(SolveJob, SnapshotRecord)
        .join(SnapshotRecord, SolveJob.snapshot_id == SnapshotRecord.snapshot_id)
        .where(SolveJob.case_id == case_id, SolveJob.business_request.is_(None))
    ):
        if planning_key(Snapshot.model_validate(record.document)) != key:
            continue
        attempts += int(epoch is None or job.created_at >= epoch)
        if _found_plan(db, job):
            # A found plan may expire as time passes; re-solving it is not a repeated failure.
            continue
        boundary = parameters.get("new_actions_not_before")
        previous_boundary = (
            job.new_actions_not_before.isoformat() if job.new_actions_not_before else None
        )
        # JSON timestamps can use Z or +00:00. Compare instants, not their spelling.
        same_boundary = (
            datetime.fromisoformat(boundary) == job.new_actions_not_before
            if boundary is not None
            else previous_boundary is None
        )
        if (
            job.allow_overtime == parameters["allow_overtime"]
            and same_boundary
            and job.time_limit >= parameters["time_limit"]
        ):
            return "UNCHANGED_SEARCH"
    return "PROBLEM_SEARCH_LIMIT" if attempts >= AUTOMATIC_SEARCHES else None


def study_limit_reason(
    db: Session, case_id: str, snapshot: Snapshot, parameters: dict
) -> str | None:
    key = planning_key(snapshot)
    shape = {k: v for k, v in parameters.items() if k != "total_time_limit"}
    epoch = db.scalar(
        select(func.max(CaseInput.created_at)).where(
            CaseInput.case_id == case_id,
            CaseInput.kind == "USER",
            CaseInput.cancelled_at.is_(None),
        )
    )
    attempts = 0
    for job, record in db.execute(
        select(SolveJob, SnapshotRecord)
        .join(SnapshotRecord, SolveJob.snapshot_id == SnapshotRecord.snapshot_id)
        .where(SolveJob.case_id == case_id, SolveJob.business_request.is_not(None))
    ):
        if planning_key(Snapshot.model_validate(record.document)) != key:
            continue
        attempts += int(epoch is None or job.created_at >= epoch)
        previous = {k: v for k, v in job.business_request.items() if k != "total_time_limit"}
        if previous == shape and job.time_limit >= parameters.get("total_time_limit", 30):
            return "UNCHANGED_SEARCH"
    return "PROBLEM_SEARCH_LIMIT" if attempts >= AUTOMATIC_SEARCHES else None


def repeated_infeasible(
    db: Session, case_id: str, snapshot: Snapshot, overtime: bool, review_timing: bool
) -> bool:
    job = db.scalar(
        select(SolveJob)
        .where(
            SolveJob.case_id == case_id,
            SolveJob.allow_overtime == overtime,
            SolveJob.state == "SUCCEEDED",
            SolveJob.business_request.is_(None),
            SolveJob.new_actions_not_before.is_not(None)
            if review_timing
            else SolveJob.new_actions_not_before.is_(None),
        )
        .order_by(SolveJob.created_at.desc())
        .limit(1)
    )
    if job is None or job.candidate_id is None:
        return False
    candidate = db.get(CandidateRecord, job.candidate_id)
    prior = db.get(SnapshotRecord, job.snapshot_id)
    return bool(
        candidate
        and prior
        and candidate.document["native_status"] == "INFEASIBLE"
        and planning_key(Snapshot.model_validate(prior.document)) == planning_key(snapshot)
    )


def recent_business_studies(db: Session, snapshot: Snapshot, case_id: str) -> list[dict]:
    """Expose this conversation's advisory outcomes, never another case's decisions."""
    from packages.planning.business_service import study_view

    result = []
    jobs = db.scalars(
        select(SolveJob)
        .join(SnapshotRecord, SolveJob.snapshot_id == SnapshotRecord.snapshot_id)
        .where(
            SolveJob.factory_id == snapshot.factory_id,
            SolveJob.case_id == case_id,
            SnapshotRecord.factory_id == snapshot.factory_id,
            SnapshotRecord.document["run_id"].astext == snapshot.run_id,
            SolveJob.business_request.is_not(None),
        )
        .order_by(SolveJob.created_at.desc())
        .limit(3)
    )
    from packages.agent.assistant_store import AssistantAction

    for job in jobs:
        view = study_view(job, snapshot)
        study = view["study"]
        # A manager's approval of one option ends the choice; the card is history from then on.
        approved = db.scalar(
            select(AssistantAction)
            .where(
                AssistantAction.factory_id == snapshot.factory_id,
                AssistantAction.kind == "treatment_execute",
                AssistantAction.payload["approval"]["job_id"].astext == job.job_id,
                AssistantAction.state.not_in(("CANCELLED", "FAILED")),
            )
            .order_by(AssistantAction.created_at.desc())
            .limit(1)
        )
        result.append(
            {
                "job_id": view["job_id"],
                "case_id": case_id,
                "state": view["state"],
                "current": view["current"] and approved is None,
                "approved_option_id": approved.payload["approval"]["option_id"]
                if approved
                else None,
                "execution_state": approved.state if approved else None,
                "advisory_only": True,
                "request": view["request"],
                "options": [
                    {
                        key: option[key]
                        for key in (
                            "option_id",
                            "title",
                            "status",
                            "summary",
                            "allow_overtime",
                            "diagnostic_only",
                            "protects_existing_commitments",
                            "quote_id",
                            "cost_minor",
                            "currency",
                            "on_time_quantity",
                            "completion_at",
                            "deliveries",
                            "earliest_completion_proven",
                            "maximum_on_time_quantity_proven",
                            "economics",
                            "actions",
                        )
                    }
                    for option in study["options"]
                ]
                if study
                else [],
            }
        )
    return result
