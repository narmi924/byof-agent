"""One durable, authorized routing action; continuous conversations run in the case runtime."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, and_, or_, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column

from packages.agent.router import PROMPT, RouteError, parse_decision
from packages.auth import AccessError, Grant, Principal, lock_membership, lock_user
from packages.integrations.factory_http import ConnectorError, FactoryHTTP
from packages.persistence import Base, User
from packages.planning.service import request_solve, synchronize
from packages.planning.store import SnapshotRecord
from packages.providers.gateway import MAX_OUTPUT_TOKENS
from packages.providers.registry import TextModel

MODEL_REQUEST_LIMIT = 1
MODEL_TIMEOUT_SECONDS = 30
MODEL_MAX_OUTPUT_TOKENS = MAX_OUTPUT_TOKENS
LEASE_SECONDS = 120
ROUTING_PROMPT = PROMPT


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (UniqueConstraint("factory_id", "request_id"), {"schema": "byof"})
    run_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    factory_id: Mapped[str] = mapped_column(String(160), index=True)
    request_id: Mapped[str] = mapped_column(String(160))
    requester_id: Mapped[str] = mapped_column(String(100))
    message: Mapped[str] = mapped_column(String(8000))
    state: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(160))
    model_requests: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(80))


def _actor(db: Session, user_id: str, factory_id: str) -> Principal:
    user = lock_user(db, user_id)
    membership = lock_membership(db, user_id, factory_id, "planner")
    if user is None or not user.active or membership is None:
        raise AccessError(
            "AUTHORIZATION_REVOKED", "This account may no longer handle tasks of this factory.", 403
        )
    return Principal(
        user_id=user.user_id,
        username=user.username,
        grants=(Grant(factory_id=factory_id, role="planner"),),
    )


def enqueue(
    engine: Engine,
    actor: Principal,
    factory_id: str,
    request_id: str,
    message: str,
) -> AgentRun:
    actor.require(factory_id, {"planner"})
    if not isinstance(message, str) or not message.strip() or len(message) > 8000:
        raise AccessError("INVALID_INPUT", "The request must have 1 to 8000 characters.", 422)
    if (
        not isinstance(request_id, str)
        or not 1 <= len(request_id) <= 160
        or any(c.isspace() for c in request_id)
    ):
        raise AccessError("INVALID_INPUT", "The request ID format is invalid.", 422)
    with Session(engine, expire_on_commit=False) as db, db.begin():
        # This single-user write lock serializes the four-request queue limit and deduplication.
        db.get(User, actor.user_id, with_for_update=True)
        _actor(db, actor.user_id, factory_id)
        existing = db.scalar(
            select(AgentRun).where(
                AgentRun.factory_id == factory_id, AgentRun.request_id == request_id
            )
        )
        if existing is None:
            queued = db.scalars(
                select(AgentRun.run_id).where(
                    AgentRun.factory_id == factory_id,
                    AgentRun.requester_id == actor.user_id,
                    AgentRun.state.in_(["QUEUED", "RUNNING"]),
                )
            ).all()
            if len(queued) >= 4:
                raise AccessError(
                    "AGENT_BUSY", "A request is already being handled; wait for its result.", 429
                )
            db.execute(
                insert(AgentRun)
                .values(
                    run_id=str(uuid4()),
                    factory_id=factory_id,
                    request_id=request_id,
                    requester_id=actor.user_id,
                    message=message,
                    state="QUEUED",
                    created_at=datetime.now(UTC),
                    model_requests=0,
                )
                .on_conflict_do_nothing(index_elements=["factory_id", "request_id"])
            )
            existing = db.scalar(
                select(AgentRun).where(
                    AgentRun.factory_id == factory_id, AgentRun.request_id == request_id
                )
            )
        assert existing is not None
        if existing.requester_id != actor.user_id or existing.message != message:
            raise AccessError(
                "IDEMPOTENCY_CONFLICT",
                "The same request ID cannot have different content or actors.",
                409,
            )
        return existing


def view(run: AgentRun) -> dict:
    return {
        name: getattr(run, name)
        for name in ("run_id", "state", "created_at", "result", "error_code", "model_requests")
    }


def _claim(engine: Engine) -> AgentRun | None:
    now = datetime.now(UTC)
    with Session(engine, expire_on_commit=False) as db, db.begin():
        run = db.scalar(
            select(AgentRun)
            .where(
                or_(
                    AgentRun.state == "QUEUED",
                    and_(AgentRun.state == "RUNNING", AgentRun.lease_until < now),
                )
            )
            .order_by(AgentRun.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if run is None:
            return None
        if run.model_requests >= MODEL_REQUEST_LIMIT:
            run.state, run.error_code, run.lease_until = "FAILED", "MODEL_RESULT_UNKNOWN", None
            return run
        run.state = "RUNNING"
        run.lease_token = str(uuid4())
        run.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        return run


def _owned(db: Session, claim: AgentRun) -> AgentRun:
    run = db.get(AgentRun, claim.run_id, with_for_update=True)
    if (
        run is None
        or run.state != "RUNNING"
        or run.lease_token != claim.lease_token
        or run.lease_until is None
        or run.lease_until <= datetime.now(UTC)
    ):
        raise AccessError(
            "LEASE_LOST",
            "The handling lease of the request expired; the result was not written.",
            409,
        )
    return run


def _authorize(engine: Engine, claim: AgentRun, *, reserve_model: bool = False) -> Principal:
    with Session(engine) as db, db.begin():
        current = _owned(db, claim)
        actor = _actor(db, current.requester_id, current.factory_id)
        if reserve_model:
            if current.model_requests >= MODEL_REQUEST_LIMIT:
                raise AccessError(
                    "MODEL_BUDGET_EXHAUSTED",
                    "The model call budget of this request is used up.",
                    409,
                )
            # Reserve before network I/O; a crash cannot cause an unaccounted second request.
            current.model_requests += 1
        return actor


def _finish(
    engine: Engine,
    claim: AgentRun,
    *,
    state: str,
    result: dict | None = None,
    error_code: str | None = None,
) -> bool:
    with Session(engine) as db, db.begin():
        try:
            current = _owned(db, claim)
        except AccessError:
            return False
        current.state, current.result, current.error_code = state, result, error_code
        current.lease_until = None
        return True


def process_one(engine: Engine, connector: FactoryHTTP, model: TextModel) -> bool:
    claim = _claim(engine)
    if claim is None:
        return False
    if claim.state == "FAILED":
        return True
    try:
        _authorize(engine, claim, reserve_model=True)
    except AccessError as exc:
        _finish(engine, claim, state="FAILED", error_code=exc.code)
        return True
    try:
        response = model.complete(ROUTING_PROMPT + json.dumps(claim.message, ensure_ascii=False))
    except Exception:
        # Provider/network failure may have happened after acceptance. Never repeat it silently.
        _finish(engine, claim, state="FAILED", error_code="MODEL_RESULT_UNKNOWN")
        return True
    try:
        if not isinstance(response, str) or len(response) > 65536:
            raise RouteError("Invalid model response size")
        decision = parse_decision(response)
    except RouteError:
        _finish(engine, claim, state="FAILED", error_code="INVALID_MODEL_DECISION")
        return True
    try:
        _authorize(engine, claim)
        route = decision["route"]
        if route == "clarify":
            _finish(
                engine,
                claim,
                state="NEEDS_INPUT",
                result={
                    "tool": "clarify",
                    "status": "NEEDS_INPUT",
                    "summary": decision["question"],
                },
            )
            return True
        snapshot = synchronize(engine, connector, claim.factory_id)
        actor = _authorize(engine, claim)
        if route == "query":
            entity = decision["entity"]
            labels = {
                "orders": "orders",
                "inventory": "inventory",
                "resources": "machines",
                "workers": "staff",
            }
            count = len(getattr(snapshot, entity))
            result = {
                "tool": "query",
                "status": "OK",
                "entity": entity,
                "record_count": count,
                "summary": f"Queried the current factory {labels[entity]}: {count} records.",
                "snapshot_hash": snapshot.content_hash,
                "snapshot_id": snapshot.snapshot_id,
                "source_revision": snapshot.source.source_revision,
            }
        else:
            job = request_solve(
                engine,
                actor,
                claim.factory_id,
                request_id=f"agent:{claim.run_id}",
                allow_overtime=False,
                time_limit=30,
            )
            with Session(engine) as db:
                job_snapshot = db.get(SnapshotRecord, job.snapshot_id)
                assert job_snapshot is not None
                result = {
                    "tool": "solve_scenario",
                    "status": "OK",
                    "task": decision["task"],
                    "job_id": job.job_id,
                    "summary": "Scheduling calculation submitted; see the plan list for the result.",
                    "snapshot_hash": job_snapshot.content_hash,
                    "snapshot_id": job_snapshot.snapshot_id,
                    "source_revision": job_snapshot.document["source"]["source_revision"],
                }
        _finish(engine, claim, state="SUCCEEDED", result=result)
    except AccessError as exc:
        _finish(engine, claim, state="FAILED", error_code=exc.code)
    except ConnectorError:
        _finish(engine, claim, state="FAILED", error_code="FACTORY_SOURCE_UNAVAILABLE")
    except Exception:
        _finish(engine, claim, state="FAILED", error_code="AGENT_TOOL_FAILURE")
    return True
