"""Local publication and conditional external acceptance retain separate durable records."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column

from packages.auth import AccessError, Principal, lock_membership, lock_user
from packages.domain.execution import ActionReceipt, PlanSubmission
from packages.domain.models import Approval, Candidate, Release, Snapshot, canonical_hash
from packages.integrations.capabilities import require_execution_support
from packages.integrations.factory_http import ConnectorError, FactoryExecution, FactoryHTTP
from packages.persistence import Base
from packages.planning.checker import check_candidate
from packages.planning.service import active_baseline, require_live
from packages.planning.store import ApprovalRecord, CandidateRecord, FactoryState, SnapshotRecord


class Publication(Base):
    __tablename__ = "publications"
    __table_args__ = (UniqueConstraint("factory_id", "request_id"), {"schema": "byof"})
    release_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    factory_id: Mapped[str] = mapped_column(String(160), index=True)
    request_id: Mapped[str] = mapped_column(String(160))
    requester_id: Mapped[str] = mapped_column(String(100))
    candidate_id: Mapped[str] = mapped_column(String(160))
    payload: Mapped[dict] = mapped_column(JSONB)
    document: Mapped[dict] = mapped_column(JSONB)
    source_receipt: Mapped[dict | None] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(160))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(80))


def _authorized(db: Session, factory_id: str, user_id: str, role: str) -> bool:
    user = lock_user(db, user_id)
    return bool(user and user.active and lock_membership(db, user_id, factory_id, role))


def _approvals(db: Session, candidate: Candidate) -> tuple[Approval, ...]:
    rows = db.scalars(
        select(ApprovalRecord)
        .where(
            ApprovalRecord.factory_id == candidate.factory_id,
            ApprovalRecord.candidate_id == candidate.candidate_id,
        )
        .order_by(ApprovalRecord.created_at)
    ).all()
    latest: dict[str, Approval] = {
        a.action_scope: a for a in (Approval.model_validate(row.document) for row in rows)
    }
    required = {"publish_plan", *candidate.required_consents}
    now = datetime.now(UTC)
    result = []
    for scope in sorted(required):
        approval = latest.get(scope)
        if (
            approval is None
            or approval.decision != "APPROVED"
            or not approval.decided_at <= now < approval.expires_at
            or approval.candidate_hash != candidate.content_hash
            or approval.binding != candidate.binding
            or not _authorized(
                db, candidate.factory_id, approval.approver_id, approval.approver_role
            )
        ):
            raise AccessError(
                "APPROVAL_REQUIRED",
                "The current plan has no valid approval, or the approval permission has changed.",
                409,
            )
        result.append(approval)
    return tuple(result)


def commit_publication(
    engine: Engine,
    actor: Principal,
    factory_id: str,
    candidate_id: str,
    *,
    request_id: str,
    candidate_hash: str,
    certificate_id: str | None = None,
) -> Release:
    actor.require(factory_id, {"planner"})
    with Session(engine) as db, db.begin():
        state = db.scalar(
            select(FactoryState).where(FactoryState.factory_id == factory_id).with_for_update()
        )
        if not _authorized(db, factory_id, actor.user_id, "planner"):
            raise AccessError("FORBIDDEN", "The release permission is no longer valid.")
        prior = db.scalar(
            select(Publication).where(
                Publication.factory_id == factory_id, Publication.request_id == request_id
            )
        )
        if prior:
            release = Release.model_validate(prior.document)
            previous_payload = PlanSubmission.model_validate(prior.payload)
            if (
                prior.requester_id != actor.user_id
                or prior.candidate_id != candidate_id
                or release.candidate_hash != candidate_hash
                or (
                    previous_payload.certificate.certificate_id
                    if previous_payload.certificate
                    else None
                )
                != certificate_id
            ):
                raise AccessError(
                    "IDEMPOTENCY_CONFLICT",
                    "The release ID was already used for other content.",
                    409,
                )
            return release
        record = db.get(CandidateRecord, candidate_id)
        if state is None or record is None or record.factory_id != factory_id:
            raise AccessError("NOT_FOUND", "The plan does not exist.", 404)
        saved = db.get(SnapshotRecord, state.snapshot_id)
        assert saved is not None
        snapshot = Snapshot.model_validate(saved.document)
        require_live(snapshot)
        require_execution_support(state.connector_capabilities, state.capabilities_observed_at)
        unresolved = db.scalar(
            select(Publication)
            .where(
                Publication.factory_id == factory_id,
                Publication.payload["run_id"].astext == snapshot.run_id,
                Publication.state.in_(("QUEUED", "DELIVERING", "UNKNOWN")),
            )
            .order_by(Publication.created_at)
            .limit(1)
        )
        if unresolved:
            raise AccessError(
                "UNRESOLVED_PUBLICATION",
                f"The execution source result of release {unresolved.release_id} is not settled yet; check the original action first.",
                409,
            )
        if datetime.now(UTC) - state.last_synced_at > timedelta(seconds=30):
            raise AccessError("STALE_SOURCE", "The factory data is out of date; sync first.", 409)
        candidate = Candidate.model_validate(record.document)
        if (
            candidate.content_hash != candidate_hash
            or (certificate_id is None and candidate.binding.snapshot_hash != snapshot.content_hash)
            or not candidate.effective_not_before
            <= snapshot.snapshot_clock
            < candidate.accept_before
        ):
            raise AccessError(
                "STALE_CANDIDATE",
                "The plan content, factory facts or execution time have changed; recalculate.",
                409,
            )
        approvals = _approvals(db, candidate)
        from packages.planning.preferences import require_current_objective

        objective = require_current_objective(db, snapshot, candidate.binding.objective_version)
        certificate = None
        if certificate_id is not None:
            from packages.planning.revalidation import require_certificate

            certificate = require_certificate(db, snapshot, candidate, approvals, certificate_id)
        elif (
            check_candidate(
                snapshot,
                candidate,
                baseline=active_baseline(db, snapshot),
                allow_overtime="allow_overtime" in candidate.required_consents,
                objective=objective,
            ).status
            != "PASS"
        ):
            raise AccessError(
                "CHECK_FAILED", "The independent check failed; the plan cannot be released.", 409
            )
        identity = str(uuid4())
        payload = PlanSubmission(
            operation_id=identity,
            factory_id=factory_id,
            run_id=snapshot.run_id,
            expected_source_revision=snapshot.source.source_revision,
            expected_snapshot_hash=str(snapshot.content_hash),
            expected_active_plan_version=snapshot.active_plan_version,
            candidate=candidate,
            approvals=approvals,
            objective=objective,
            certificate=certificate,
        )
        now = datetime.now(UTC)
        release = Release(
            release_id=identity,
            operation_id=identity,
            factory_id=factory_id,
            candidate_hash=candidate_hash,
            payload_hash=canonical_hash(payload),
            approval_ids=tuple(a.approval_id for a in approvals),
            expected_source_revision=snapshot.source.source_revision,
            expected_active_plan_version=snapshot.active_plan_version,
            local_state="LOCAL_COMMITTED",
            source_state="PENDING_SOURCE",
            committed_at=now,
        )
        db.add(
            Publication(
                release_id=identity,
                factory_id=factory_id,
                request_id=request_id,
                requester_id=actor.user_id,
                candidate_id=candidate_id,
                payload=payload.model_dump(mode="json"),
                document=release.model_dump(mode="json"),
                state="QUEUED",
                created_at=now,
                next_attempt_at=now,
                attempts=0,
            )
        )
        return release


def deliver_one(engine: Engine, reader: FactoryHTTP, writer: FactoryExecution) -> bool:
    now = datetime.now(UTC)
    with Session(engine, expire_on_commit=False) as db, db.begin():
        record = db.scalar(
            select(Publication)
            .where(
                Publication.state.in_(("QUEUED", "DELIVERING", "UNKNOWN")),
                Publication.next_attempt_at <= now,
                or_(Publication.lease_until.is_(None), Publication.lease_until <= now),
            )
            .order_by(Publication.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if record is None:
            return False
        # A prior claim may have reached the source, including a crash before its receipt.
        # Local invalidation cannot establish a remote rejection for that original action.
        may_have_sent = record.attempts > 0
        record.lease_token = str(uuid4())
        record.lease_until = now + timedelta(seconds=60)
        record.state = "DELIVERING"
        may_send = record.attempts < 6
        if may_send:
            record.attempts += 1
        submission = PlanSubmission.model_validate(record.payload)
        claimed_id, fence = record.release_id, record.lease_token
        requester = record.requester_id
    receipt: ActionReceipt | None = None
    error = None
    try:
        # Reconcile the original action first, including after a crash or lost HTTP receipt.
        receipt = reader.action(submission.factory_id, submission.run_id, submission.operation_id)
        if receipt is None and not may_send:
            # Exhausting the send budget never proves that an uncertain source action failed.
            error = "SOURCE_RESULT_UNKNOWN"
        elif receipt is None:
            with Session(engine) as db, db.begin():
                factory_state = db.scalar(
                    select(FactoryState)
                    .where(FactoryState.factory_id == submission.factory_id)
                    .with_for_update()
                )
                guarded = db.get(Publication, claimed_id, with_for_update=True)
                if (
                    guarded is None
                    or guarded.lease_token != fence
                    or guarded.lease_until is None
                    or guarded.lease_until <= datetime.now(UTC)
                ):
                    return True
                if canonical_hash(submission) != Release.model_validate(
                    guarded.document
                ).payload_hash or guarded.payload != submission.model_dump(mode="json"):
                    raise AccessError(
                        "PUBLICATION_CONTENT_CHANGED",
                        "The local release content has changed; the release was blocked.",
                        409,
                    )
                authorized = _authorized(db, submission.factory_id, requester, "planner")
                current_approvals = _approvals(db, submission.candidate)
                from packages.planning.preferences import require_current_objective

                current_snapshot = (
                    db.get(SnapshotRecord, factory_state.snapshot_id) if factory_state else None
                )
                if current_snapshot is None:
                    raise AccessError(
                        "SNAPSHOT_REQUIRED", "The current factory snapshot is missing.", 409
                    )
                require_current_objective(
                    db,
                    Snapshot.model_validate(current_snapshot.document),
                    submission.candidate.binding.objective_version,
                )
                if submission.certificate is not None:
                    from packages.planning.revalidation import require_certificate

                    verified = require_certificate(
                        db,
                        Snapshot.model_validate(current_snapshot.document),
                        submission.candidate,
                        current_approvals,
                        submission.certificate.certificate_id,
                    )
                    if verified != submission.certificate:
                        raise AccessError(
                            "VALIDATION_CONTENT_CHANGED",
                            "The check certificate content does not match the release record.",
                            409,
                        )
                if not authorized or {a.approval_id for a in current_approvals} != {
                    a.approval_id for a in submission.approvals
                }:
                    raise AccessError(
                        "APPROVAL_CHANGED",
                        "The approval or permission changed before release.",
                        409,
                    )
                assert factory_state is not None
                require_execution_support(
                    factory_state.connector_capabilities, factory_state.capabilities_observed_at
                )
                # Reconcile existing actions before this check; a lost capability cannot justify resending.
                current_capabilities = reader.capabilities()
                sending_at = datetime.now(UTC)
                # The capability request may consume the remaining lease or approval lifetime.
                if guarded.lease_token != fence or guarded.lease_until <= sending_at:
                    return True
                if any(
                    not approval.decided_at <= sending_at < approval.expires_at
                    for approval in current_approvals
                ):
                    raise AccessError(
                        "APPROVAL_REQUIRED",
                        "The approval expired before release; check again.",
                        409,
                    )
                if submission.certificate is not None and not (
                    submission.certificate.issued_at
                    <= sending_at
                    < submission.certificate.expires_at
                ):
                    raise AccessError(
                        "VALIDATION_EXPIRED",
                        "The check certificate expired before release; check again.",
                        409,
                    )
                require_execution_support(
                    factory_state.connector_capabilities,
                    factory_state.capabilities_observed_at,
                    now=sending_at,
                )
                require_execution_support(
                    current_capabilities.model_dump(mode="json"), sending_at, now=sending_at
                )
                receipt = writer.submit(submission)
        if receipt is not None and receipt.candidate_hash != submission.candidate.content_hash:
            raise ConnectorError("Receipt candidate content mismatch")
    except AccessError as exc:
        error = exc.code
    except ConnectorError:
        receipt = None
        error = "SOURCE_RESULT_UNKNOWN"
    with Session(engine) as db, db.begin():
        row = db.get(Publication, claimed_id, with_for_update=True)
        if (
            row is None
            or row.lease_token != fence
            or row.lease_until is None
            or row.lease_until <= datetime.now(UTC)
        ):
            return True
        data = Release.model_validate(row.document).model_dump(mode="json")
        if receipt is not None:
            data.update(
                source_state=receipt.source_state,
                source_receipt_id=receipt.receipt_id,
                effective_at=receipt.effective_at,
            )
            row.state, row.error_code = "DONE", receipt.error_code
            row.source_receipt = receipt.model_dump(mode="json")
        elif error == "SOURCE_RESULT_UNKNOWN" or may_have_sent:
            data["source_state"] = "UNKNOWN"
            row.state, row.error_code = "UNKNOWN", "SOURCE_RESULT_UNKNOWN"
            row.next_attempt_at = datetime.now(UTC) + timedelta(
                seconds=min(60, 2 ** min(row.attempts, 6))
            )
        else:
            data["source_state"] = "REJECTED"
            row.state, row.error_code = "DONE", error
        row.document = Release.model_validate(data).model_dump(mode="json")
        row.lease_until = None
    return True


def publications(
    engine: Engine, factory_id: str, *, candidate_ids: list[str] | None = None
) -> list[dict]:
    with Session(engine) as db:
        rows = db.scalars(
            select(Publication)
            .where(
                Publication.factory_id == factory_id,
                *(
                    [Publication.candidate_id.in_(candidate_ids)]
                    if candidate_ids is not None
                    else []
                ),
            )
            .order_by(Publication.created_at.desc())
            .limit(200 if candidate_ids is not None else 30)
        ).all()
        return [
            {
                "release": Release.model_validate(row.document),
                "candidate_id": row.candidate_id,
                "error_code": row.error_code,
            }
            for row in rows
        ]


def reconcile_execution(db: Session, snapshot: Snapshot) -> None:
    actuals = {a.operation_id: a for a in snapshot.actuals}
    for row in db.scalars(
        select(Publication)
        .where(Publication.factory_id == snapshot.factory_id, Publication.state == "DONE")
        .with_for_update()
    ):
        release = Release.model_validate(row.document)
        if release.source_state != "ACTIVE":
            continue
        payload = PlanSubmission.model_validate(row.payload)
        if payload.run_id != snapshot.run_id:
            continue
        expected = {a.operation_id for a in payload.candidate.assignments}
        known = [a for identity, a in actuals.items() if identity in expected]
        if any(a.state == "BLOCKED" or a.quality_state == "FAILED" for a in known):
            state = "BLOCKED"
        elif len(known) == len(expected) and all(
            a.state == "COMPLETED" and a.quality_state == "PASSED" for a in known
        ):
            state = "COMPLETED"
        else:
            state = "IN_PROGRESS" if known else "NOT_STARTED"
        if release.execution_state != state:
            data = release.model_dump(mode="json")
            data["execution_state"] = state
            row.document = Release.model_validate(data).model_dump(mode="json")
