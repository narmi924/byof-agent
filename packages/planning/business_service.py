"""Durable what-if requests using the existing solver queue, with no source writes."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, object_session

from packages.agent.cases import live_actor
from packages.auth import AccessError, Principal
from packages.domain.business_options import BusinessStudy, BusinessStudyRequest
from packages.domain.models import Snapshot, canonical_hash
from packages.planning.service import active_baseline, require_live
from packages.planning.store import FactoryState, SnapshotRecord, SolveJob


def validate_request(snapshot: Snapshot, request: BusinessStudyRequest) -> None:
    require_live(snapshot)
    terms = snapshot.business_terms
    if request.kind == "production_exception":
        known = (
            {o.order_id for o in snapshot.orders}
            | {r.resource_id for r in snapshot.resources}
            | {w.worker_id for w in snapshot.workers}
            | {m.material_id for m in snapshot.profile.materials}
            | {r.receipt_id for r in snapshot.receipts}
            | {a.operation_id for a in snapshot.actuals}
        )
        if request.subject_id is not None and request.subject_id not in known:
            raise AccessError(
                "SUBJECT_NOT_FOUND",
                "The selected disruption object is not in the current factory facts.",
                422,
            )
        if request.existing_order_id is not None and request.existing_order_id not in {
            o.order_id for o in snapshot.orders
        }:
            raise AccessError(
                "ORDER_NOT_FOUND", "The selected order is not in the current factory facts.", 422
            )
        return
    if request.kind == "urgent_order":
        order = request.order
        if request.existing_order_id is not None:
            order = next(
                (item for item in snapshot.orders if item.order_id == request.existing_order_id),
                None,
            )
            if order is None:
                raise AccessError(
                    "ORDER_NOT_FOUND",
                    "The selected order is not in the currently synced factory facts.",
                    422,
                )
        assert order is not None
        if request.order is not None and any(
            existing.order_id == order.order_id for existing in snapshot.orders
        ):
            raise AccessError(
                "ORDER_EXISTS",
                "A pre-commitment trial needs a new order ID that is not in the source yet.",
                409,
            )
        product = next(
            (p for p in snapshot.profile.products if p.product_id == order.product_id), None
        )
        if product is None or order.quantity % product.batch_size:
            raise AccessError(
                "INVALID_INPUT",
                "Choose an existing product and enter a quantity in production batches.",
                422,
            )
        if request.order is not None and (
            order.status != "CONFIRMED" or order.version != 1 or order.split_revision != 1
        ):
            raise AccessError(
                "INVALID_INPUT", "A new trial order must use the initial version.", 422
            )
        if request.final_due_at is not None and not (
            order.due_at <= request.final_due_at <= snapshot.horizon.end_at
        ):
            raise AccessError(
                "INVALID_INPUT",
                "The final delivery date cannot be earlier than the original request or outside the scheduling window.",
                422,
            )
        rule = (
            next((r for r in terms.delivery_rules if r.product_id == order.product_id), None)
            if terms is not None
            else None
        )
        if request.partial_delivery_allowed and (
            rule is None or not rule.partial_delivery_allowed or rule.max_deliveries < 2
        ):
            raise AccessError(
                "PARTIAL_DELIVERY_NOT_ALLOWED",
                "The current source rule does not allow split delivery.",
                422,
            )
        if request.partial_delivery_allowed and rule is not None:
            minimum = request.minimum_partial_quantity or product.batch_size
            if minimum < rule.minimum_partial_quantity or minimum % product.batch_size:
                raise AccessError(
                    "INVALID_INPUT",
                    "The first delivery quantity does not match the source rule or production batch.",
                    422,
                )
    else:
        known = {quote.quote_id for quote in terms.expedite_quotes} if terms is not None else set()
        if not set(request.expedite_quote_ids) <= known:
            raise AccessError(
                "QUOTE_NOT_FOUND",
                "The resupply quote does not exist; improvised quotes cannot be used.",
                422,
            )


def request_business_study(
    engine: Engine,
    actor: Principal,
    factory_id: str,
    *,
    request_id: str,
    request: BusinessStudyRequest,
    expected_snapshot_hash: str | None = None,
    expected_run_id: str | None = None,
    case_id: str | None = None,
) -> SolveJob:
    if case_id is not None and request.order is not None:
        raise AccessError(
            "SOURCE_ORDER_REQUIRED",
            "Reference an order that the shop floor entered and synced; orders are not entered in the manager conversation.",
            422,
        )
    payload = request.model_dump(mode="json")
    with Session(engine, expire_on_commit=False) as db, db.begin():
        state = db.get(FactoryState, factory_id, with_for_update=True)
        live_actor(db, actor, factory_id, {"planner"}, lock=True)
        previous = db.scalar(
            select(SolveJob).where(
                SolveJob.factory_id == factory_id, SolveJob.request_id == request_id
            )
        )
        if previous is not None:
            if (
                previous.requester_id != actor.user_id
                or previous.business_request != payload
                or previous.case_id != case_id
            ):
                raise AccessError(
                    "IDEMPOTENCY_CONFLICT", "The same trial request cannot change its content.", 409
                )
            return previous
        saved = db.get(SnapshotRecord, state.snapshot_id) if state else None
        if saved is None:
            raise AccessError("SNAPSHOT_REQUIRED", "Sync the factory data first.", 409)
        snapshot = Snapshot.model_validate(saved.document)
        if (
            expected_snapshot_hash is not None and expected_snapshot_hash != snapshot.content_hash
        ) or (expected_run_id is not None and expected_run_id != snapshot.run_id):
            raise AccessError(
                "CASE_FACTS_CHANGED",
                "The factory facts have changed; refresh and run the trial again.",
                409,
            )
        assert state is not None
        if (datetime.now(UTC) - state.last_synced_at).total_seconds() > 30:
            raise AccessError(
                "SOURCE_STALE",
                "The factory data is not synced to the latest yet; try again shortly.",
                409,
            )
        if case_id is not None:
            from packages.agent.cases import TERMINAL
            from packages.agent.cases_store import CaseRecord

            case = db.get(CaseRecord, case_id)
            if (
                case is None
                or case.factory_id != factory_id
                or case.run_id != snapshot.run_id
                or case.state in TERMINAL
            ):
                raise AccessError(
                    "CASE_FACTS_CHANGED",
                    "The trial does not belong to the current factory task.",
                    409,
                )
        validate_request(snapshot, request)
        active_baseline(db, snapshot)
        active = (
            db.scalar(
                select(func.count())
                .select_from(SolveJob)
                .where(SolveJob.factory_id == factory_id, SolveJob.state.in_(("QUEUED", "RUNNING")))
            )
            or 0
        )
        if active >= 4:
            raise AccessError(
                "SOLVER_BUSY",
                "A calculation is already running; wait for its result and try again.",
                409,
            )
        job = SolveJob(
            job_id=str(uuid4()),
            factory_id=factory_id,
            request_id=request_id,
            requester_id=actor.user_id,
            snapshot_id=snapshot.snapshot_id,
            allow_overtime=False,
            time_limit=request.total_time_limit,
            state="QUEUED",
            created_at=datetime.now(UTC),
            attempts=0,
            objective_version="business-study-v1",
            case_id=case_id,
            business_request=payload,
        )
        db.add(job)
        return job


def complete_business_study(engine: Engine, claimed: SolveJob, result: BusinessStudy) -> bool:
    with Session(engine) as db, db.begin():
        job = db.get(SolveJob, claimed.job_id, with_for_update=True)
        if (
            job is None
            or job.state != "RUNNING"
            or job.lease_token != claimed.lease_token
            or job.lease_until is None
            or job.lease_until <= datetime.now(UTC)
        ):
            return False
        saved = db.get(SnapshotRecord, job.snapshot_id)
        if (
            saved is None
            or result.origin_snapshot_id != saved.snapshot_id
            or result.origin_snapshot_hash != saved.content_hash
            or result.baseline_hash != saved.document.get("active_plan_hash")
            or result.factory_id != job.factory_id
            or result.run_id != saved.document.get("run_id")
            or result.request.model_dump(mode="json") != job.business_request
        ):
            raise ValueError("Business result differs from the claimed immutable input")
        job.business_result = result.model_dump(mode="json")
        job.state, job.lease_until = "SUCCEEDED", None
        return True


def study_view(job: SolveJob, current: Snapshot | None = None) -> dict:
    """Return JSON-safe evidence for both HTTP responses and durable Agent inputs."""
    document = job.business_result
    public = None
    if document is not None:
        public = BusinessStudy.model_validate(document).public_view()
    is_current = bool(
        document and current and document["origin_snapshot_hash"] == current.content_hash
    )
    if document and current and document["request"]["kind"] == "production_exception":
        from packages.agent.planning_context import decision_key

        db = object_session(job)
        origin = db.get(SnapshotRecord, job.snapshot_id) if db is not None else None
        if origin:
            source = Snapshot.model_validate(origin.document)
            is_current = (
                source.run_id == current.run_id
                and decision_key(source) == decision_key(current)
                and current.snapshot_clock <= source.snapshot_clock + timedelta(minutes=15)
            )
    return {
        "job_id": job.job_id,
        "case_id": job.case_id,
        "state": job.state,
        "request": job.business_request,
        "error_code": job.error_code,
        "study": public,
        "study_hash": canonical_hash(document) if document else None,
        "current": is_current,
        "created_at": job.created_at.isoformat(),
        "advisory_only": True,
    }
