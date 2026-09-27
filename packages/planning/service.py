"""Transactional planning actions with immutable evidence and real-clock worker leases."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from packages.auth import AccessError, Principal, lock_membership, lock_user
from packages.domain.models import Approval, Candidate, Snapshot
from packages.integrations.capabilities import execution_support
from packages.integrations.factory_http import ConnectorError, FactoryHTTP
from packages.integrations.sync import RunSwitch, save_incremental
from packages.planning.store import (
    ApprovalRecord,
    CandidateRecord,
    FactoryState,
    SnapshotRecord,
    SolveJob,
)


def active_baseline(db: Session, snapshot: Snapshot) -> Candidate | None:
    if snapshot.active_plan_version is None:
        return None
    record = db.scalar(
        select(CandidateRecord).where(
            CandidateRecord.factory_id == snapshot.factory_id,
            CandidateRecord.content_hash == snapshot.active_plan_hash,
        )
    )
    if record is None:
        raise AccessError(
            "BASELINE_NOT_AVAILABLE",
            "The full content of the execution source's current plan is missing; check the plan version first.",
            409,
        )
    return Candidate.model_validate(record.document)


def require_live(snapshot: Snapshot) -> None:
    if snapshot.source.source_system == "factory-simulator-replay":
        raise AccessError(
            "REPLAY_READ_ONLY",
            "The current run is a replay; official plans cannot be approved, released or calculated.",
            409,
        )


def synchronize_due(engine: Engine, connector: FactoryHTTP) -> int:
    with Session(engine) as db:
        factories = list(
            db.scalars(
                select(FactoryState.factory_id)
                .where(FactoryState.last_synced_at <= datetime.now(UTC) - timedelta(seconds=3))
                .order_by(FactoryState.last_synced_at)
                .limit(20)
            )
        )
    completed = 0
    for factory_id in factories:
        try:
            synchronize(engine, connector, factory_id)
            completed += 1
        except (ConnectorError, AccessError):
            # Keep the last verified snapshot; stale real-clock age remains visible in the workspace.
            continue
    return completed


def synchronize(
    engine: Engine,
    connector: FactoryHTTP,
    factory_id: str,
    *,
    run_switch: tuple[Principal, str, str] | None = None,
) -> Snapshot:
    capabilities = connector.capabilities()
    if not capabilities.read_snapshot or capabilities.snapshot_consistency == "UNVERIFIED":
        raise AccessError(
            "SOURCE_INCOMPLETE", "The source cannot provide a consistent business snapshot.", 409
        )
    received = connector.snapshot(factory_id)
    now = datetime.now(UTC)
    with Session(engine) as db, db.begin():
        db.execute(
            insert(FactoryState)
            .values(
                factory_id=factory_id,
                snapshot_id=received.snapshot_id,
                run_id=received.run_id,
                source_revision=received.source.source_revision,
                last_synced_at=now,
            )
            .on_conflict_do_nothing(index_elements=["factory_id"])
        )
        state = db.scalar(
            select(FactoryState).where(FactoryState.factory_id == factory_id).with_for_update()
        )
        assert state is not None
        state.connector_capabilities = capabilities.model_dump(mode="json")
        state.capabilities_observed_at = now
        previous = db.get(SnapshotRecord, state.snapshot_id)
        switched = False
        from packages.agent.human_tasks import lock_review_cases

        lock_review_cases(db, factory_id)
        if run_switch is not None:
            actor, request_id, expected_new_run = run_switch
            actor.require(factory_id, {"sim_admin"})
            user = lock_user(db, actor.user_id)
            member = lock_membership(db, actor.user_id, factory_id, "sim_admin")
            if (
                user is None
                or not user.active
                or member is None
                or received.run_id != expected_new_run
            ):
                raise AccessError(
                    "SOURCE_RUN_CHANGED",
                    "The source run or administrator permission has changed; check again.",
                    409,
                )
        if previous and state.run_id != received.run_id:
            if run_switch is None:
                raise AccessError(
                    "SOURCE_RUN_CHANGED",
                    "The source run batch has changed; an administrator must confirm the switch.",
                    409,
                )
            prior_switch = db.scalar(
                select(RunSwitch).where(
                    RunSwitch.factory_id == factory_id, RunSwitch.request_id == request_id
                )
            )
            if prior_switch is not None:
                raise AccessError(
                    "STALE_RUN_SWITCH",
                    "This switch was already handled; the old run cannot be restored.",
                    409,
                )
            db.add(
                RunSwitch(
                    switch_id=str(uuid4()),
                    factory_id=factory_id,
                    request_id=request_id,
                    actor_id=actor.user_id,
                    previous_run_id=state.run_id,
                    run_id=received.run_id,
                    changed_at=now,
                )
            )
            switched = True
        if previous and state.run_id == received.run_id:
            if state.source_revision == received.source.source_revision:
                if previous.content_hash != received.content_hash:
                    raise AccessError(
                        "SOURCE_REVISION_CONFLICT",
                        "The source returned different facts for the same version; check the interface.",
                        409,
                    )
                state.last_synced_at = now
                from packages.planning.publication import reconcile_execution

                unchanged = Snapshot.model_validate(previous.document)
                reconcile_execution(db, unchanged)
                from packages.agent.human_tasks import reconcile_reviews

                db.flush()
                reconcile_reviews(db, unchanged)
                return unchanged
            # This connector's source contract uses monotonic integer watermarks.
            try:
                if int(received.source.source_revision) <= int(state.source_revision):
                    raise AccessError(
                        "LATE_SNAPSHOT",
                        "An old snapshot version arrived and did not overwrite the current facts.",
                        409,
                    )
            except ValueError:
                raise AccessError(
                    "INVALID_WATERMARK", "The order of source versions cannot be checked.", 409
                ) from None
        existing = db.get(SnapshotRecord, received.snapshot_id)
        if existing and (
            existing.content_hash != received.content_hash or existing.factory_id != factory_id
        ):
            raise AccessError(
                "SNAPSHOT_COLLISION", "The snapshot ID conflicts with saved content.", 409
            )
        if not existing:
            if previous and not switched and capabilities.read_changes:
                save_incremental(
                    db, connector, Snapshot.model_validate(previous.document), received
                )
            db.add(
                SnapshotRecord(
                    snapshot_id=received.snapshot_id,
                    factory_id=factory_id,
                    content_hash=received.content_hash,
                    document=received.model_dump(mode="json"),
                    created_at=now,
                )
            )
        state.snapshot_id = received.snapshot_id
        state.run_id = received.run_id
        state.source_revision = received.source.source_revision
        state.last_synced_at = now
        from packages.planning.publication import reconcile_execution

        reconcile_execution(db, received)
        from packages.agent.human_tasks import reconcile_reviews

        db.flush()
        reconcile_reviews(db, received)
    return received


def request_solve(
    engine: Engine,
    actor: Principal,
    factory_id: str,
    *,
    request_id: str,
    allow_overtime: bool,
    time_limit: int,
    case_id: str | None = None,
    new_actions_not_before: datetime | None = None,
) -> SolveJob:
    actor.require(factory_id, {"planner"})
    with Session(engine, expire_on_commit=False) as db, db.begin():
        state = db.scalar(
            select(FactoryState).where(FactoryState.factory_id == factory_id).with_for_update()
        )
        if not state:
            raise AccessError("SNAPSHOT_REQUIRED", "Sync the factory data first.", 409)
        # Factory state precedes identity locks; a concurrent revocation serializes with commit.
        user = lock_user(db, actor.user_id)
        member = lock_membership(db, actor.user_id, factory_id, "planner")
        if user is None or not user.active or member is None:
            raise AccessError(
                "AUTHORIZATION_REVOKED",
                "This account may no longer request scheduling for this factory.",
            )
        saved = db.get(SnapshotRecord, state.snapshot_id)
        assert saved is not None
        require_live(Snapshot.model_validate(saved.document))
        if new_actions_not_before is not None and (
            not isinstance(new_actions_not_before, datetime)
            or new_actions_not_before.utcoffset() is None
            or new_actions_not_before.second != 0
            or new_actions_not_before.microsecond != 0
        ):
            raise AccessError(
                "INVALID_NEW_ACTIONS_TIME",
                "The earliest time for new actions must be a whole-minute business time with a time zone.",
                422,
            )
        existing = db.scalar(
            select(SolveJob).where(
                SolveJob.factory_id == factory_id, SolveJob.request_id == request_id
            )
        )
        if existing:
            if (
                existing.requester_id != actor.user_id
                or existing.business_request is not None
                or existing.allow_overtime != allow_overtime
                or existing.time_limit != time_limit
                or existing.case_id != case_id
                or existing.new_actions_not_before != new_actions_not_before
            ):
                raise AccessError(
                    "IDEMPOTENCY_CONFLICT",
                    "The same request ID cannot be used for different actions.",
                    409,
                )
            return existing
        from packages.agent.cases_store import CaseRecord
        from packages.planning.preferences import resolve_objective

        snapshot = Snapshot.model_validate(saved.document)
        if new_actions_not_before is not None and not (
            snapshot.snapshot_clock <= new_actions_not_before < snapshot.horizon.end_at
        ):
            raise AccessError(
                "INVALID_NEW_ACTIONS_TIME",
                "The earliest time for new actions must not be before the current factory time and must be before the end of the scheduling window.",
                422,
            )
        if case_id is not None:
            case = db.get(CaseRecord, case_id)
            if (
                case is None
                or case.factory_id != factory_id
                or case.run_id != snapshot.run_id
                or case.state in {"RESOLVED", "HANDED_OFF", "CANCELLED"}
            ):
                raise AccessError(
                    "INVALID_CASE_SCOPE",
                    "The solve case does not belong to the current factory run.",
                    409,
                )
        objective = resolve_objective(db, snapshot, save=True)
        objective_version = objective.objective_version if objective else "delivery-v1"
        # Factory lock serializes lookup and creation. Each caller retains its own
        # request/owner/case record; only identical immutable inputs share CPU work.
        shared = (
            db.scalar(
                select(SolveJob)
                .where(
                    SolveJob.factory_id == factory_id,
                    SolveJob.snapshot_id == snapshot.snapshot_id,
                    SolveJob.reused_from_id.is_(None),
                    SolveJob.business_request.is_(None),
                    SolveJob.objective_version == objective_version,
                    SolveJob.allow_overtime == allow_overtime,
                    SolveJob.time_limit == time_limit,
                    SolveJob.new_actions_not_before == new_actions_not_before,
                    SolveJob.case_id.is_not(None),
                    SolveJob.case_id != case_id,
                    SolveJob.state.in_(("QUEUED", "RUNNING", "SUCCEEDED")),
                )
                .order_by(SolveJob.created_at.desc())
                .limit(1)
                .with_for_update()
            )
            if case_id
            else None
        )
        active_count = len(
            db.scalars(
                select(SolveJob.job_id).where(
                    SolveJob.factory_id == factory_id,
                    SolveJob.state.in_(["QUEUED", "RUNNING"]),
                    SolveJob.reused_from_id.is_(None),
                )
            ).all()
        )
        if shared is None and active_count >= 4:
            raise AccessError(
                "PLANNING_BUSY",
                "A calculation is already queued; wait for the current result.",
                429,
            )
        job = SolveJob(
            job_id=str(uuid4()),
            factory_id=factory_id,
            request_id=request_id,
            requester_id=actor.user_id,
            snapshot_id=state.snapshot_id,
            allow_overtime=allow_overtime,
            time_limit=time_limit,
            state="SUCCEEDED" if shared and shared.state == "SUCCEEDED" else "QUEUED",
            created_at=datetime.now(UTC),
            attempts=0,
            objective_version=objective_version,
            case_id=case_id,
            new_actions_not_before=new_actions_not_before,
            reused_from_id=shared.job_id if shared else None,
            candidate_id=shared.candidate_id if shared and shared.state == "SUCCEEDED" else None,
        )
        db.add(job)
        return job


def claim_job(engine: Engine) -> SolveJob | None:
    now = datetime.now(UTC)
    with Session(engine, expire_on_commit=False) as db, db.begin():
        job = db.scalar(
            select(SolveJob)
            .where(
                SolveJob.reused_from_id.is_(None),
                or_(
                    SolveJob.state == "QUEUED",
                    and_(SolveJob.state == "RUNNING", SolveJob.lease_until < now),
                ),
            )
            .order_by(SolveJob.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None
        if job.attempts >= 3:
            job.state, job.error_code = "FAILED", "WORKER_RETRY_EXHAUSTED"
            _complete_shared(db, job)
            return None
        job.state, job.lease_token = "RUNNING", str(uuid4())
        job.lease_until = now + timedelta(seconds=job.time_limit + 300)
        job.attempts += 1
        return job


def complete_job(
    engine: Engine, claimed: SolveJob, result: Candidate | None, error: str | None = None
) -> bool:
    now = datetime.now(UTC)
    with Session(engine) as db, db.begin():
        job = db.scalar(select(SolveJob).where(SolveJob.job_id == claimed.job_id).with_for_update())
        if (
            not job
            or job.state != "RUNNING"
            or job.lease_token != claimed.lease_token
            or job.lease_until is None
            or job.lease_until <= now
        ):
            return False
        if result is not None:
            if job.business_request is not None:
                raise ValueError("Advisory business studies cannot create executable candidates")
            result = Candidate.model_validate(result.model_dump())
            snapshot = db.get(SnapshotRecord, job.snapshot_id)
            if (
                snapshot is None
                or result.factory_id != job.factory_id
                or result.binding.snapshot_hash != snapshot.content_hash
                or result.binding.objective_version != job.objective_version
                or result.new_actions_not_before != job.new_actions_not_before
            ):
                raise ValueError("Worker result is outside claimed snapshot")
            db.add(
                CandidateRecord(
                    candidate_id=result.candidate_id,
                    factory_id=job.factory_id,
                    snapshot_id=job.snapshot_id,
                    content_hash=result.content_hash,
                    document=result.model_dump(mode="json"),
                    created_at=now,
                )
            )
            job.candidate_id = result.candidate_id
            job.state = "SUCCEEDED"
        else:
            job.state, job.error_code = "FAILED", error or "WORKER_FAILURE"
        job.lease_until = None
        _complete_shared(db, job)
        return True


def _complete_shared(db: Session, job: SolveJob) -> None:
    db.execute(
        update(SolveJob)
        .where(SolveJob.reused_from_id == job.job_id, SolveJob.state == "QUEUED")
        .values(state=job.state, candidate_id=job.candidate_id, error_code=job.error_code)
    )


def approve(
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
    role = "manager" if action_scope == "allow_overtime" else "planner"
    actor.require(factory_id, {role})
    from packages.planning.checker import check_candidate

    with Session(engine) as db, db.begin():
        state = db.scalar(
            select(FactoryState).where(FactoryState.factory_id == factory_id).with_for_update()
        )
        from packages.agent.human_tasks import lock_review_cases

        lock_review_cases(db, factory_id)
        user = lock_user(db, actor.user_id)
        membership = lock_membership(db, actor.user_id, factory_id, role)
        if not user or not user.active or membership is None:
            raise AccessError(
                "FORBIDDEN", "The approval permission is no longer valid; sign in again to check."
            )
        previous = db.scalar(
            select(ApprovalRecord).where(
                ApprovalRecord.factory_id == factory_id, ApprovalRecord.request_id == request_id
            )
        )
        if previous:
            from packages.planning.review_store import ApprovalReviewRecord

            old = Approval.model_validate(previous.document)
            if (
                db.scalar(
                    select(ApprovalReviewRecord).where(
                        ApprovalReviewRecord.approval_id == previous.approval_id
                    )
                )
                is not None
                or previous.candidate_id != candidate_id
                or old.candidate_hash != candidate_hash
                or old.action_scope != action_scope
                or old.decision != decision
                or old.approver_id != actor.user_id
            ):
                raise AccessError(
                    "IDEMPOTENCY_CONFLICT",
                    "The same approval ID cannot be used for different decisions.",
                    409,
                )
            return old
        record = db.get(CandidateRecord, candidate_id)
        if not state or not record or record.factory_id != factory_id:
            raise AccessError("NOT_FOUND", "The plan does not exist.", 404)
        candidate = Candidate.model_validate(record.document)
        if candidate.content_hash != candidate_hash:
            raise AccessError("CANDIDATE_CHANGED", "The plan has changed; review it again.", 409)
        snapshot_record = db.get(SnapshotRecord, state.snapshot_id)
        assert snapshot_record is not None
        snapshot = Snapshot.model_validate(snapshot_record.document)
        require_live(snapshot)
        from packages.planning.preferences import require_current_objective

        # A later fact must not prevent the authorized person from revoking an old approval.
        if decision != "REJECTED":
            objective = require_current_objective(db, snapshot, candidate.binding.objective_version)
            if (
                snapshot.content_hash != candidate.binding.snapshot_hash
                or snapshot.snapshot_clock >= candidate.accept_before
                or snapshot.snapshot_clock > candidate.effective_not_before
            ):
                raise AccessError(
                    "STALE_CANDIDATE",
                    "The factory facts or executable time have changed; recalculate.",
                    409,
                )
            if (
                not candidate.has_solution
                or check_candidate(
                    snapshot,
                    candidate,
                    baseline=active_baseline(db, snapshot),
                    allow_overtime="allow_overtime" in candidate.required_consents,
                    objective=objective,
                ).status
                != "PASS"
            ):
                raise AccessError(
                    "CHECK_FAILED",
                    "The plan has not passed the independent check and cannot be approved.",
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
                "decision": decision,
                "decided_at": datetime.now(UTC),
                "expires_at": datetime.now(UTC) + timedelta(hours=1),
            }
        )
        db.add(
            ApprovalRecord(
                approval_id=approval.approval_id,
                request_id=request_id,
                factory_id=factory_id,
                candidate_id=candidate_id,
                document=approval.model_dump(mode="json"),
                created_at=approval.decided_at,
            )
        )
        from packages.agent.human_tasks import reconcile_reviews

        db.flush()
        reconcile_reviews(db, snapshot)
    return approval


def job_view(job: SolveJob) -> dict:
    return {
        name: getattr(job, name)
        for name in (
            "job_id",
            "state",
            "candidate_id",
            "error_code",
            "created_at",
            "allow_overtime",
            "new_actions_not_before",
        )
    }


def workspace(
    engine: Engine,
    factory_id: str,
    *,
    case_id: str | None = None,
    candidate_id: str | None = None,
) -> dict:
    from packages.domain.demand import finished_goods
    from packages.domain.production_facts import order_facts, plan_review
    from packages.planning.preferences import effective_view, load_objective, objective_view
    from packages.planning.publication import _authorized
    from packages.planning.revalidation import certificates
    from packages.planning.reviews import reviews

    with Session(engine) as db:
        state = db.get(FactoryState, factory_id)
        snapshot_record = db.get(SnapshotRecord, state.snapshot_id) if state else None
        snapshot = Snapshot.model_validate(snapshot_record.document) if snapshot_record else None
        objective_state = (
            effective_view(db, snapshot)
            if snapshot
            else {
                "status": "INVALID",
                "objective_version": None,
                "definition": None,
                "sources": [],
                "context_hash": None,
                "reason": "Sync the factory facts first.",
            }
        )
        candidate_filters = []
        job_filters = []
        if case_id:
            from packages.agent.cases_store import CaseRecord

            case = db.get(CaseRecord, case_id)
            if case is None or case.factory_id != factory_id:
                raise AccessError(
                    "CASE_NOT_FOUND", "The current factory has no such conversation.", 404
                )
            job_filters.append(SolveJob.case_id == case_id)
            related = select(SolveJob.candidate_id).where(
                SolveJob.factory_id == factory_id,
                SolveJob.case_id == case_id,
                SolveJob.candidate_id.is_not(None),
            )
            candidate_filters.append(
                or_(
                    CandidateRecord.candidate_id.in_(related),
                    CandidateRecord.candidate_id.in_(case.context.get("candidate_ids", [])),
                )
            )
        if candidate_id:
            candidate_filters.append(CandidateRecord.candidate_id == candidate_id)
            job_filters.append(SolveJob.candidate_id == candidate_id)
        objective_contracts: dict[str, dict] = {"delivery-v1": objective_view(db, None)}
        candidates = []
        for record in db.scalars(
            select(CandidateRecord)
            .where(CandidateRecord.factory_id == factory_id, *candidate_filters)
            .order_by(CandidateRecord.created_at.desc())
            .limit(200 if case_id else 30)
        ):
            candidate = Candidate.model_validate(record.document)
            try:
                objective = load_objective(db, factory_id, candidate.binding.objective_version)
                if objective is not None:
                    objective_contracts[objective.objective_version] = objective_view(db, objective)
            except AccessError:
                objective = None
            approvals = [
                Approval.model_validate(a.document)
                for a in db.scalars(
                    select(ApprovalRecord)
                    .where(ApprovalRecord.candidate_id == candidate.candidate_id)
                    .order_by(ApprovalRecord.created_at)
                )
            ]
            latest = {a.action_scope: a for a in approvals}
            required = {"publish_plan", *candidate.required_consents}
            valid = {
                scope
                for scope, a in latest.items()
                if a.decision == "APPROVED"
                and a.decided_at <= datetime.now(UTC) < a.expires_at
                and a.candidate_hash == candidate.content_hash
                and a.binding == candidate.binding
                and _authorized(db, factory_id, a.approver_id, a.approver_role)
            }
            status = "APPROVED" if required <= valid else "CANDIDATE"
            if not candidate.has_solution:
                status = "NO_SOLUTION"
            elif candidate.checker.status != "PASS":
                status = "CHECK_FAILED"
            elif (
                snapshot is None
                or snapshot.content_hash != candidate.binding.snapshot_hash
                or snapshot.snapshot_clock >= candidate.accept_before
                or snapshot.snapshot_clock > candidate.effective_not_before
                or objective_state["status"] != "READY"
                or objective_state["objective_version"] != candidate.binding.objective_version
            ):
                status = "STALE"
            original = db.get(SnapshotRecord, record.snapshot_id)
            origin = Snapshot.model_validate(original.document) if original else None
            candidates.append(
                {
                    "candidate": candidate,
                    "review": plan_review(origin, candidate, active_baseline(db, origin))
                    if origin and candidate.has_solution and candidate.checker.status == "PASS"
                    else None,
                    "snapshot_id": record.snapshot_id,
                    "created_at": record.created_at,
                    "run_id": (
                        original.document.get("run_id")
                        if (original := db.get(SnapshotRecord, record.snapshot_id))
                        else None
                    ),
                    "state": status,
                    "approvals": approvals,
                    # The conversation that produced the plan, for returning to it from the timeline.
                    "case_id": db.scalar(
                        select(SolveJob.case_id)
                        .where(SolveJob.candidate_id == candidate.candidate_id)
                        .limit(1)
                    ),
                }
            )
        jobs = db.scalars(
            select(SolveJob)
            .where(
                SolveJob.factory_id == factory_id, SolveJob.business_request.is_(None), *job_filters
            )
            .order_by(SolveJob.created_at.desc())
            .limit(200 if case_id else 30)
        ).all()
        return {
            "snapshot": snapshot,
            "order_facts": order_facts(snapshot, active_baseline(db, snapshot)) if snapshot else [],
            "finished_goods": (
                [lot.model_dump(mode="json") for lot in finished_goods(snapshot)]
                if snapshot is not None
                else None
            ),
            "execution_support": execution_support(
                state.connector_capabilities if state else None,
                state.capabilities_observed_at if state else None,
            ),
            "last_synced_at": state.last_synced_at if state else None,
            "freshness": (
                "UNKNOWN"
                if snapshot is None or state is None
                else "CURRENT"
                if datetime.now(UTC) - state.last_synced_at <= timedelta(seconds=30)
                else "STALE"
            ),
            "jobs": [job_view(j) for j in jobs],
            "candidates": candidates,
            "objective_state": objective_state,
            "objective_contracts": objective_contracts,
            "validation_certificates": certificates(db, factory_id),
            "approval_reviews": reviews(db, factory_id),
        }
