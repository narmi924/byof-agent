"""Case creation, durable wakeups and fact-based event coordination."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, literal, or_, select, union_all
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from packages.agent import risk_suggestions
from packages.agent.cases_store import (
    CaseCursor,
    CaseInput,
    CaseOperation,
    CaseRecord,
    CaseTurn,
    add_input,
)
from packages.agent.impact import impact_report
from packages.auth import AccessError, Principal, lock_memberships, lock_user
from packages.domain.models import Candidate, Event, Snapshot, batch_operations, canonical_hash
from packages.integrations.sync import SourceBatch
from packages.persistence import Membership, User
from packages.planning.service import active_baseline, require_live
from packages.planning.store import CandidateRecord, FactoryState, SnapshotRecord, SolveJob

TERMINAL = ("RESOLVED", "HANDED_OFF", "CANCELLED")
READ_ROLES = {"planner", "manager", "maintainer", "warehouse", "team_lead", "admin"}


def live_actor(
    db: Session, actor: Principal, factory_id: str, roles: set[str], *, lock: bool = False
) -> None:
    actor.require(factory_id, roles)
    user = lock_user(db, actor.user_id) if lock else db.get(User, actor.user_id)
    statement = select(Membership).where(
        Membership.user_id == actor.user_id,
        Membership.factory_id == factory_id,
        Membership.role.in_(roles),
    )
    grant = (
        next(iter(lock_memberships(db, actor.user_id, factory_id, roles)), None)
        if lock
        else db.scalar(statement)
    )
    if user is None or not user.active or grant is None:
        raise AccessError(
            "AUTHORIZATION_REVOKED", "This account may no longer handle tasks of this factory.", 403
        )


def _open_case(
    db: Session, snapshot: Snapshot, owner_id: str, title: str, *, force_new: bool = False
) -> CaseRecord:
    case = (
        db.scalar(
            select(CaseRecord)
            .where(
                CaseRecord.factory_id == snapshot.factory_id,
                CaseRecord.run_id == snapshot.run_id,
                CaseRecord.state.not_in(TERMINAL),
            )
            .order_by(CaseRecord.created_at, CaseRecord.case_id)
            .limit(1)
            .with_for_update()
        )
        if not force_new
        else None
    )
    if case is None:
        now = datetime.now(UTC)
        case = CaseRecord(
            case_id=str(uuid4()),
            factory_id=snapshot.factory_id,
            run_id=snapshot.run_id,
            owner_id=owner_id,
            title=title[:240],
            state="OPEN",
            version=1,
            created_at=now,
            updated_at=now,
            snapshot_id=snapshot.snapshot_id,
            context={"candidate_ids": [], "assumptions": [], "unknowns": []},
        )
        db.add(case)
        db.flush()
    return case


def _recover_user_input(
    db: Session,
    actor: Principal,
    factory_id: str,
    request_id: str,
    payload: dict,
    case: CaseRecord | None = None,
) -> dict | None:
    previous = db.scalar(
        select(CaseInput).where(
            CaseInput.factory_id == factory_id, CaseInput.input_key == "user:" + request_id
        )
    )
    if previous is None:
        return None
    if case is None:
        case = db.get(CaseRecord, previous.case_id, with_for_update=True)
    if case is None or case.factory_id != factory_id:
        raise AccessError("NOT_FOUND", "The task does not exist.", 404)
    live_actor(db, actor, factory_id, {"planner"}, lock=True)
    if "suggestion_id" in payload and isinstance(previous.payload.get("source_event_ids"), list):
        payload = {**payload, "source_event_ids": previous.payload["source_event_ids"]}
    expected = canonical_hash({"kind": "USER", "payload": payload, "case_id": case.case_id})
    if (
        previous.case_id != case.case_id
        or previous.kind != "USER"
        or previous.payload_hash != expected
    ):
        raise AccessError(
            "IDEMPOTENCY_CONFLICT",
            "The input ID was already used for other content or another identity.",
            409,
        )
    return case_view(case)


def recover_case_input(
    engine: Engine,
    actor: Principal,
    factory_id: str,
    request_id: str,
    message: str,
    *,
    case_id: str | None = None,
    start_new: bool = False,
    suggestion_id: str | None = None,
) -> dict | None:
    """Recover a saved request under current authorization without requiring live source facts."""
    if not message.strip() or len(message) > 8000 or type(start_new) is not bool:
        raise AccessError(
            "INVALID_INPUT", "Enter valid case information, up to 8000 characters.", 422
        )
    payload: dict = {"actor_id": actor.user_id, "message": message}
    if start_new:
        payload["start_new"] = True
    if suggestion_id:
        payload["suggestion_id"] = suggestion_id
    with Session(engine) as db, db.begin():
        live_actor(db, actor, factory_id, {"planner"})
        db.get(FactoryState, factory_id, with_for_update=True)
        case = db.get(CaseRecord, case_id, with_for_update=True) if case_id else None
        if case_id and (case is None or case.factory_id != factory_id):
            raise AccessError("NOT_FOUND", "The task does not exist.", 404)
        return _recover_user_input(db, actor, factory_id, request_id, payload, case)


def create_case(
    engine: Engine,
    actor: Principal,
    factory_id: str,
    request_id: str,
    message: str,
    start_new: bool = False,
    suggestion_id: str | None = None,
) -> dict:
    if not message.strip() or len(message) > 8000:
        raise AccessError("INVALID_INPUT", "Enter the issue to handle, up to 8000 characters.", 422)
    if type(start_new) is not bool:
        raise AccessError("INVALID_INPUT", "The new case options are malformed.", 422)
    with Session(engine) as db, db.begin():
        live_actor(db, actor, factory_id, {"planner"})
        state = db.get(FactoryState, factory_id, with_for_update=True)
        payload: dict = {"actor_id": actor.user_id, "message": message}
        if start_new:
            payload["start_new"] = True
        if suggestion_id:
            payload["suggestion_id"] = suggestion_id
        recovered = _recover_user_input(db, actor, factory_id, request_id, payload)
        if recovered is not None:
            return recovered
        if state is None:
            raise AccessError("SNAPSHOT_REQUIRED", "Sync the factory data first.", 409)
        saved = db.get(SnapshotRecord, state.snapshot_id)
        assert saved is not None
        snapshot = Snapshot.model_validate(saved.document)
        require_live(snapshot)
        if suggestion_id:
            suggestion = risk_suggestions.match_suggestion(
                db,
                snapshot,
                active_baseline(db, snapshot) if snapshot.active_plan_hash else None,
                suggestion_id,
            )
            if message.strip() != suggestion["prompt"]:
                raise AccessError(
                    "SUGGESTION_OUTDATED",
                    "The shop floor suggestion has changed; refresh and try again.",
                    409,
                )
            payload["source_event_ids"] = suggestion["source_event_ids"]
        case = _open_case(db, snapshot, actor.user_id, message, force_new=start_new)
        live_actor(db, actor, factory_id, {"planner"}, lock=True)
        add_input(db, case, "user:" + request_id, "USER", payload)
        return case_view(case)


def message_case(
    engine: Engine,
    actor: Principal,
    factory_id: str,
    case_id: str,
    request_id: str,
    message: str,
    suggestion_id: str | None = None,
) -> dict:
    if not message.strip() or len(message) > 8000:
        raise AccessError("INVALID_INPUT", "Enter valid information, up to 8000 characters.", 422)
    with Session(engine) as db, db.begin():
        live_actor(db, actor, factory_id, {"planner"})
        state = db.get(FactoryState, factory_id, with_for_update=True)
        case = db.get(CaseRecord, case_id, with_for_update=True)
        if case is None or case.factory_id != factory_id:
            raise AccessError("NOT_FOUND", "The task does not exist.", 404)
        payload = {"actor_id": actor.user_id, "message": message}
        if suggestion_id:
            payload["suggestion_id"] = suggestion_id
        recovered = _recover_user_input(db, actor, factory_id, request_id, payload, case)
        if recovered is not None:
            return recovered
        saved = db.get(SnapshotRecord, state.snapshot_id) if state else None
        if saved is None or state is None or state.run_id != case.run_id:
            raise AccessError(
                "SOURCE_RUN_CHANGED", "The source run has changed; check the task again.", 409
            )
        snapshot = Snapshot.model_validate(saved.document)
        require_live(snapshot)
        if suggestion_id:
            suggestion = risk_suggestions.match_suggestion(
                db,
                snapshot,
                active_baseline(db, snapshot) if snapshot.active_plan_hash else None,
                suggestion_id,
            )
            if message.strip() != suggestion["prompt"]:
                raise AccessError(
                    "SUGGESTION_OUTDATED",
                    "The shop floor suggestion has changed; refresh and try again.",
                    409,
                )
            payload["source_event_ids"] = suggestion["source_event_ids"]
        live_actor(db, actor, factory_id, {"planner"}, lock=True)
        if case.state in TERMINAL:
            raise AccessError("CASE_CLOSED", "This task is closed; create a new one.", 409)
        add_input(db, case, "user:" + request_id, "USER", payload)
        return case_view(case)


def list_risk_suggestions(engine: Engine, actor: Principal, factory_id: str) -> dict:
    """Current, verified source projection; reading it never creates a Case or model call."""
    with Session(engine) as db:
        live_actor(db, actor, factory_id, {"manager"})
        state = db.get(FactoryState, factory_id)
        saved = db.get(SnapshotRecord, state.snapshot_id) if state else None
        if state is None or saved is None:
            return {"run_id": None, "freshness": "UNKNOWN", "suggestions": []}
        snapshot = Snapshot.model_validate(saved.document)
        age = (datetime.now(UTC) - state.last_synced_at).total_seconds()
        if age < 0 or age > 30 or snapshot.source.freshness != "CURRENT":
            return {"run_id": snapshot.run_id, "freshness": "STALE", "suggestions": []}
        baseline = active_baseline(db, snapshot) if snapshot.active_plan_hash else None
        suggestions = risk_suggestions.current_suggestions(db, snapshot, baseline)
        return {
            "run_id": snapshot.run_id,
            "freshness": "CURRENT",
            "suggestions": [
                {key: value for key, value in suggestion.items() if key != "source_event_ids"}
                for suggestion in suggestions
            ],
        }


def stop_case_turn(
    engine: Engine,
    actor: Principal,
    factory_id: str,
    case_id: str,
    request_id: str,
    expected_target: str,
) -> dict:
    """Fence analysis and continuation; already dispatched business effects are preserved."""
    with Session(engine) as db, db.begin():
        live_actor(db, actor, factory_id, {"planner"})
        case = db.get(CaseRecord, case_id, with_for_update=True)
        if case is None or case.factory_id != factory_id:
            raise AccessError("NOT_FOUND", "The conversation does not exist.", 404)
        previous_stop = case.context.get("last_stop")
        if isinstance(previous_stop, dict) and previous_stop.get("request_id") == request_id:
            if previous_stop.get("target") != expected_target:
                raise AccessError(
                    "IDEMPOTENCY_CONFLICT",
                    "The stop request ID was already used for another analysis turn.",
                    409,
                )
            return case_view(case)
        state = db.get(FactoryState, factory_id)
        if state is None or state.run_id != case.run_id:
            raise AccessError(
                "SOURCE_RUN_CHANGED",
                "The shop floor run has switched; see the current conversation.",
                409,
            )
        live_actor(db, actor, factory_id, {"planner"}, lock=True)
        now = datetime.now(UTC)
        turn = (
            db.get(CaseTurn, case.active_turn_id, with_for_update=True)
            if case.active_turn_id
            else None
        )
        if turn and turn.state == "RUNNING":
            if expected_target != turn.turn_id:
                linked = db.get(CaseInput, expected_target)
                if linked is None or linked.case_id != case_id or linked.turn_id != turn.turn_id:
                    raise AccessError(
                        "CASE_ACTIVITY_CHANGED",
                        "The Agent has moved on to the next stage; refresh and try again.",
                        409,
                    )
            dispatched = db.scalar(
                select(CaseOperation.operation_id)
                .where(
                    CaseOperation.turn_id == turn.turn_id,
                    CaseOperation.action.in_(
                        (
                            "request_approval",
                            "request_information",
                            "reply",
                            "finish",
                            "handoff",
                        )
                    ),
                    CaseOperation.state.in_(("STARTED", "DONE")),
                )
                .limit(1)
            )
            if dispatched:
                raise AccessError(
                    "CASE_ACTION_DISPATCHED",
                    "The result or action of this turn was already submitted and cannot be stopped; progress keeps showing.",
                    409,
                )
            turn.state, turn.error_code, turn.lease_until = "CANCELLED", "USER_STOPPED", None
            case.active_turn_id = None
        elif case.state == "PLANNING" and expected_target == f"planning:{case.version}":
            # The solver may finish safely, but its result must not restart this case.
            case.active_turn_id = None
        else:
            pending = db.get(CaseInput, expected_target, with_for_update=True)
            if (
                pending is None
                or pending.case_id != case_id
                or pending.kind != "USER"
                or pending.turn_id is not None
                or pending.cancelled_at is not None
            ):
                raise AccessError(
                    "CASE_ACTIVITY_CHANGED",
                    "The Agent has moved on to the next stage; refresh and try again.",
                    409,
                )
            # USER inputs are immutable evidence; the database only permits
            # cancellation fields on TIMER inputs. Consume the queued input
            # into a cancelled turn so the worker cannot claim it later.
            stopped_turn = CaseTurn(
                turn_id=str(uuid4()),
                case_id=case_id,
                factory_id=factory_id,
                state="CANCELLED",
                created_at=now,
                deadline=now,
                model_requests=0,
                solver_requests=0,
                next_step=0,
                attempts=0,
                model_pending=False,
                error_code="USER_STOPPED",
            )
            db.add(stopped_turn)
            pending.turn_id = stopped_turn.turn_id
        # Consume outstanding events without rewriting their immutable payloads.
        cancelled = CaseTurn(
            turn_id=str(uuid4()),
            case_id=case_id,
            factory_id=factory_id,
            state="CANCELLED",
            created_at=now,
            deadline=now,
            model_requests=0,
            solver_requests=0,
            next_step=0,
            attempts=0,
            model_pending=False,
            error_code="USER_STOPPED",
        )
        db.add(cancelled)
        for queued in db.scalars(
            select(CaseInput).where(
                CaseInput.case_id == case_id,
                CaseInput.turn_id.is_(None),
                CaseInput.cancelled_at.is_(None),
            )
        ):
            queued.turn_id = cancelled.turn_id
        for queued_job in db.scalars(
            select(SolveJob)
            .where(SolveJob.case_id == case_id, SolveJob.state == "QUEUED")
            .with_for_update()
        ):
            subscriber = db.scalar(
                select(SolveJob.job_id)
                .where(
                    SolveJob.reused_from_id == queued_job.job_id,
                    SolveJob.state == "QUEUED",
                )
                .limit(1)
            )
            if subscriber is None:
                queued_job.state, queued_job.error_code = "CANCELLED", "USER_STOPPED"
        case.context = {
            **case.context,
            "analysis_paused": True,
            "stopped_at": now.isoformat(),
            "last_stop": {
                "request_id": request_id,
                "target": expected_target,
                "version": case.version + 1,
            },
        }
        case.state, case.error_code, case.updated_at = "WAITING", None, now
        case.version += 1
        return case_view(case)


def case_view(case: CaseRecord) -> dict:
    return {
        name: getattr(case, name)
        for name in (
            "case_id",
            "factory_id",
            "run_id",
            "owner_id",
            "title",
            "state",
            "version",
            "created_at",
            "updated_at",
            "snapshot_id",
            "context",
            "closure",
            "error_code",
        )
    }


def list_cases(engine: Engine, actor: Principal, factory_id: str) -> list[dict]:
    with Session(engine) as db:
        live_actor(db, actor, factory_id, READ_ROLES)
        return [
            case_view(row)
            for row in db.scalars(
                select(CaseRecord)
                .where(CaseRecord.factory_id == factory_id)
                .order_by(CaseRecord.updated_at.desc())
                .limit(50)
            )
        ]


def get_case(engine: Engine, actor: Principal, factory_id: str, case_id: str) -> dict:
    with Session(engine) as db:
        live_actor(db, actor, factory_id, READ_ROLES)
        case = db.get(CaseRecord, case_id)
        if case is None or case.factory_id != factory_id:
            raise AccessError("NOT_FOUND", "The task does not exist.", 404)
        inputs = list(
            db.scalars(
                select(CaseInput)
                .where(CaseInput.case_id == case_id)
                .order_by(CaseInput.created_at.desc(), CaseInput.input_id.desc())
                .limit(100)
            )
        )
        operations = list(
            db.scalars(
                select(CaseOperation)
                .where(CaseOperation.case_id == case_id)
                .order_by(CaseOperation.created_at.desc(), CaseOperation.operation_id.desc())
                .limit(100)
            )
        )
        active = db.get(CaseTurn, case.active_turn_id) if case.active_turn_id else None
        pending = next(
            (
                row
                for row in reversed(inputs)
                if row.kind == "USER" and row.turn_id is None and row.cancelled_at is None
            ),
            None,
        )
        target: str | None
        if active and active.state == "RUNNING":
            phase = "ANALYZING"
            target = active.turn_id
            started_at = active.created_at
            latest = next((row for row in operations if row.turn_id == active.turn_id), None)
            if (
                latest
                and latest.action in {"reply", "finish", "handoff"}
                and latest.state in {"STARTED", "DONE"}
            ):
                phase, target = ("IDLE" if latest.state == "DONE" else "ANALYZING"), None
            elif (
                latest
                and latest.action in {"solve_scenario", "request_approval", "request_information"}
                and latest.state in {"STARTED", "DONE"}
            ):
                phase, target = (
                    "SOLVING",
                    active.turn_id if latest.action == "solve_scenario" else None,
                )
            elif active.model_pending:
                phase = "THINKING"
            elif latest and latest.action == "query":
                phase = "READING_FACTS"
        elif pending:
            phase, target, started_at = "QUEUED", pending.input_id, pending.created_at
        elif case.state == "PLANNING":
            phase, target, started_at = "SOLVING", f"planning:{case.version}", case.updated_at
        elif case.context.get("analysis_paused") or (
            isinstance(case.context.get("last_stop"), dict)
            and case.context["last_stop"].get("version") == case.version
        ):
            phase, target, started_at = "STOPPED", None, case.updated_at
        else:
            phase, target, started_at = "IDLE", None, None
        boundaries = []
        if len(inputs) == 100:
            boundaries.append((inputs[-1].created_at, inputs[-1].input_id))
        if len(operations) == 100:
            boundaries.append((operations[-1].created_at, operations[-1].operation_id))
        boundary = max(boundaries) if boundaries else None
        return {
            **case_view(case),
            "history_cursor": {"at": boundary[0], "id": boundary[1]} if boundary else None,
            "activity": {"phase": phase, "stop_target": target, "started_at": started_at},
            "inputs": [
                {
                    key: getattr(row, key)
                    for key in (
                        "input_id",
                        "input_key",
                        "kind",
                        "payload",
                        "created_at",
                        "available_at",
                        "turn_id",
                        "cancelled_at",
                        "cancellation_reason",
                    )
                }
                for row in reversed(inputs)
            ],
            "operations": [
                {
                    key: getattr(row, key)
                    for key in (
                        "operation_id",
                        "action",
                        "reason_summary",
                        "parameters",
                        "snapshot_id",
                        "state",
                        "result",
                        "created_at",
                    )
                }
                for row in reversed(operations)
            ],
        }


def case_history(
    engine: Engine,
    actor: Principal,
    factory_id: str,
    case_id: str,
    before_at: datetime,
    before_id: str,
) -> dict:
    """Stable backward cursor across inputs and tool operations, including timestamp ties."""
    with Session(engine) as db:
        live_actor(db, actor, factory_id, READ_ROLES)
        case = db.get(CaseRecord, case_id)
        if case is None or case.factory_id != factory_id:
            raise AccessError("NOT_FOUND", "The task does not exist.", 404)
        events = union_all(
            select(
                CaseInput.input_id.label("id"),
                CaseInput.created_at.label("at"),
                literal("input").label("kind"),
            ).where(CaseInput.case_id == case_id),
            select(
                CaseOperation.operation_id.label("id"),
                CaseOperation.created_at.label("at"),
                literal("operation").label("kind"),
            ).where(CaseOperation.case_id == case_id),
        ).subquery()
        rows = list(
            db.execute(
                select(events)
                .where(
                    or_(
                        events.c.at < before_at,
                        and_(events.c.at == before_at, events.c.id < before_id),
                    )
                )
                .order_by(events.c.at.desc(), events.c.id.desc())
                .limit(101)
            )
        )
        page = rows[:100]
        result = {
            **case_view(case),
            "inputs": [],
            "operations": [],
            "history_cursor": {"at": page[-1].at, "id": page[-1].id} if len(rows) > 100 else None,
        }
        for event in reversed(page):
            if event.kind == "input":
                row = db.get(CaseInput, event.id)
                fields = (
                    "input_id",
                    "input_key",
                    "kind",
                    "payload",
                    "created_at",
                    "available_at",
                    "turn_id",
                    "cancelled_at",
                )
                result["inputs"].append({key: getattr(row, key) for key in fields})
            else:
                operation = db.get(CaseOperation, event.id)
                fields = (
                    "operation_id",
                    "action",
                    "reason_summary",
                    "parameters",
                    "snapshot_id",
                    "state",
                    "result",
                    "created_at",
                )
                result["operations"].append({key: getattr(operation, key) for key in fields})
        return result


def _startup_report(snapshot: Snapshot, baseline: Candidate | None) -> dict | None:
    reasons = []
    if any(
        a.state == "BLOCKED" or a.quality_state in {"FAILED", "UNKNOWN"} for a in snapshot.actuals
    ):
        reasons.append("UNRESOLVED_EXECUTION_FACT")
    if any(r.status != "AVAILABLE" for r in snapshot.resources):
        reasons.append("UNAVAILABLE_RESOURCE")
    if any(w.status != "AVAILABLE" for w in snapshot.workers):
        reasons.append("UNAVAILABLE_WORKER")
    _, operations = batch_operations(snapshot)
    completed = {a.operation_id for a in snapshot.actuals if a.state == "COMPLETED"}
    planned = {a.operation_id for a in baseline.assignments} if baseline else set()
    if {o.operation_id for o in operations} - completed - planned:
        reasons.append("UNPLANNED_DEMAND")
    if not reasons:
        return None
    report = impact_report(snapshot, (), baseline)
    report["classification"] = {"material": True, "urgent": True, "reasons": reasons}
    report["possible"] = {
        "scope": "FACTORY",
        "orders": [o.order_id for o in snapshot.orders],
        "operations": [o.operation_id for o in operations],
        "resources": [r.resource_id for r in snapshot.resources],
        "workers": [w.worker_id for w in snapshot.workers],
        "materials": [m.material_id for m in snapshot.profile.materials],
    }
    report["expansion_reasons"] = ["CURRENT_FACT_RECONCILIATION"]
    return report


def _source_cases(db: Session, snapshot: Snapshot, owner_id: str) -> list[CaseRecord]:
    filters = [
        CaseRecord.factory_id == snapshot.factory_id,
        CaseRecord.run_id == snapshot.run_id,
        CaseRecord.state.not_in(TERMINAL),
    ]
    if db.get(Membership, (owner_id, snapshot.factory_id, "manager")) is not None:
        # Old planner conversations remain in history, but cannot absorb new
        # factory alerts that belong in the two-person manager workflow.
        filters.append(CaseRecord.owner_id == owner_id)
    cases = list(
        db.scalars(
            select(CaseRecord).where(*filters).order_by(CaseRecord.case_id).with_for_update()
        )
    )
    return cases or [
        _open_case(db, snapshot, owner_id, "Production change to handle", force_new=True)
    ]


def _source_input_key(prefix: str, run_id: str, revision: int, case_id: str) -> str:
    # Input keys are unique per factory; scope fanout by Case and keep long source IDs bounded.
    return (
        prefix
        + ":"
        + canonical_hash({"run_id": run_id, "source_revision": revision, "case_id": case_id})
    )


def _waits_for_work(job: SolveJob | None) -> bool:
    """A calculation that stopped only because interrupted work had no confirmed remainder."""
    if job is None:
        return False
    options = (job.business_result or {}).get("options", [])
    return job.error_code == "WIP_CONFIRMATION_REQUIRED" or (
        bool(options)
        and all(option["status"] != "FEASIBLE" for option in options)
        and any(
            search.get("error_code") == "WIP_CONFIRMATION_REQUIRED"
            for option in options
            for search in option.get("searches", [])
        )
    )


def wake_for_confirmed_work(db: Session, snapshot: Snapshot, since: int) -> int:
    """The shop floor recorded the remaining work a conversation was waiting for: continue that
    conversation instead of waiting for the manager to repeat the request."""
    revision = db.scalar(
        select(SourceBatch.revision)
        .where(
            SourceBatch.factory_id == snapshot.factory_id,
            SourceBatch.run_id == snapshot.run_id,
            SourceBatch.revision > since,
            SourceBatch.revision <= int(snapshot.source.source_revision),
            SourceBatch.document["cause"].as_string() == "execution.confirm_remaining",
        )
        .order_by(SourceBatch.revision.desc())
        .limit(1)
    )
    # Every calculation covers the whole shop floor and stops at any stopped operation without its
    # remaining work, so waiting conversations continue only once none is left unconfirmed.
    if revision is None or any(
        a.state == "BLOCKED" and a.remaining_minutes is None for a in snapshot.actuals
    ):
        return 0
    woken = 0
    for case in db.scalars(
        select(CaseRecord)
        .where(
            CaseRecord.factory_id == snapshot.factory_id,
            CaseRecord.run_id == snapshot.run_id,
            CaseRecord.state.not_in(TERMINAL),
        )
        .with_for_update()
    ):
        latest = db.scalar(
            select(SolveJob)
            .where(SolveJob.factory_id == case.factory_id, SolveJob.case_id == case.case_id)
            .order_by(SolveJob.created_at.desc())
            .limit(1)
        )
        if _waits_for_work(latest):
            add_input(
                db,
                case,
                f"work-confirmed:{snapshot.run_id}:{revision}:{case.case_id}",
                "SOURCE",
                {
                    "message": "The shop floor confirmed the remaining work of the blocked operation. "
                    "Continue the analysis from the latest facts and give the options.",
                    "source_revision": str(revision),
                },
            )
            woken += 1
    return woken


def ingest_sources(engine: Engine) -> int:
    """Deterministic reconciliation and bounded collection; no model or private source access."""
    with Session(engine) as db:
        factory_ids = list(
            db.scalars(select(FactoryState.factory_id).order_by(FactoryState.factory_id))
        )
    received = 0
    for factory_id in factory_ids:
        with Session(engine) as db, db.begin():
            state = db.get(FactoryState, factory_id, with_for_update=True)
            assert state is not None
            saved = db.get(SnapshotRecord, state.snapshot_id)
            if saved is None:
                continue
            snapshot = Snapshot.model_validate(saved.document)
            if snapshot.source.source_system == "factory-simulator-replay":
                continue
            cursor = db.get(CaseCursor, factory_id, with_for_update=True)
            # When a prior conversation has ended, keep new source alerts with
            # the manager who most recently handled this run. Otherwise an
            # arbitrary planner account can silently receive the new case.
            owner = None
            recent_owners = list(
                db.scalars(
                    select(CaseRecord.owner_id)
                    .where(
                        CaseRecord.factory_id == factory_id, CaseRecord.run_id == snapshot.run_id
                    )
                    .order_by(CaseRecord.updated_at.desc())
                    .limit(20)
                )
            )
            active_owners = set(
                db.scalars(
                    select(User.user_id).where(
                        User.user_id.in_(recent_owners), User.active.is_(True)
                    )
                )
            )
            recent_grants: dict[str, set[str]] = {}
            for user_id, role in db.execute(
                select(Membership.user_id, Membership.role).where(
                    Membership.user_id.in_(recent_owners), Membership.factory_id == factory_id
                )
            ):
                recent_grants.setdefault(user_id, set()).add(role)
            for required_roles in ({"planner", "manager"}, {"planner"}):
                for recent_id in recent_owners:
                    if recent_id in active_owners and required_roles <= recent_grants.get(
                        recent_id, set()
                    ):
                        owner = recent_id
                        break
                if owner is not None:
                    break
            if owner is None:
                owner = db.scalar(
                    select(User.user_id)
                    .join(Membership, Membership.user_id == User.user_id)
                    .where(
                        User.active.is_(True),
                        Membership.factory_id == factory_id,
                        Membership.role == "planner",
                        User.user_id.in_(
                            select(Membership.user_id).where(
                                Membership.factory_id == factory_id,
                                Membership.role == "manager",
                            )
                        ),
                    )
                    .order_by(User.user_id)
                    .limit(1)
                )
            if owner is None:
                owner = db.scalar(
                    select(User.user_id)
                    .join(Membership, Membership.user_id == User.user_id)
                    .where(
                        User.active.is_(True),
                        Membership.factory_id == factory_id,
                        Membership.role == "planner",
                    )
                    .order_by(User.user_id)
                    .limit(1)
                )
            if owner is None:
                continue
            if db.get(Membership, (owner, factory_id, "manager")) is not None:
                from packages.agent.recovery_followup import reconcile_recoveries

                received += reconcile_recoveries(db, snapshot)
                if cursor is not None and cursor.run_id == snapshot.run_id:
                    received += wake_for_confirmed_work(db, snapshot, cursor.source_revision)
                # Manager workflow: source batches remain durable and readable as
                # suggestions. Only manager messages or explicitly selected
                # recovery follow-ups start a model turn.
                if cursor is None:
                    cursor = CaseCursor(
                        factory_id=factory_id, run_id=snapshot.run_id, source_revision=0
                    )
                    db.add(cursor)
                cursor.run_id = snapshot.run_id
                cursor.source_revision = int(snapshot.source.source_revision)
                continue
            baseline = active_baseline(db, snapshot) if snapshot.active_plan_hash else None
            if cursor is None or cursor.run_id != snapshot.run_id:
                # Adoption starts from a verified current snapshot. Old feeds may not
                # contain reconstructible historical facts, so reconcile current risk.
                if cursor is None:
                    cursor = CaseCursor(
                        factory_id=factory_id, run_id=snapshot.run_id, source_revision=0
                    )
                    db.add(cursor)
                cursor.run_id = snapshot.run_id
                cursor.source_revision = int(snapshot.source.source_revision)
                report = _startup_report(snapshot, baseline)
                if report is not None:
                    for case in _source_cases(db, snapshot, owner):
                        case.context = {**case.context, "impact": report}
                        add_input(
                            db,
                            case,
                            _source_input_key(
                                "reconcile", snapshot.run_id, cursor.source_revision, case.case_id
                            ),
                            "SOURCE_RECONCILIATION",
                            {"impact": report},
                        )
                        received += 1
                continue
            batches = list(
                db.scalars(
                    select(SourceBatch)
                    .where(
                        SourceBatch.factory_id == factory_id,
                        SourceBatch.run_id == snapshot.run_id,
                        SourceBatch.revision > cursor.source_revision,
                        SourceBatch.revision <= int(snapshot.source.source_revision),
                    )
                    .order_by(SourceBatch.revision)
                    .limit(100)
                )
            )
            for batch in batches:
                events = tuple(Event.model_validate(raw) for raw in batch.document["events"])
                historical = db.scalar(
                    select(SnapshotRecord).where(
                        SnapshotRecord.factory_id == factory_id,
                        SnapshotRecord.content_hash == batch.document["snapshot_hash"],
                    )
                )
                evidence = Snapshot.model_validate(historical.document) if historical else snapshot
                historical_baseline = (
                    active_baseline(db, evidence) if evidence.active_plan_hash else None
                )
                report = impact_report(evidence, events, historical_baseline)
                report["current_snapshot_hash"] = snapshot.content_hash
                report["current_source_revision"] = snapshot.source.source_revision
                if historical is None:
                    report["classification"] = {
                        "material": True,
                        "urgent": True,
                        "reasons": ["HISTORICAL_FACTS_UNAVAILABLE"],
                    }
                if report["classification"]["material"]:
                    for case in _source_cases(db, snapshot, owner):
                        pending = db.scalar(
                            select(CaseInput.available_at)
                            .where(
                                CaseInput.case_id == case.case_id,
                                CaseInput.turn_id.is_(None),
                                CaseInput.cancelled_at.is_(None),
                            )
                            .order_by(CaseInput.available_at)
                            .limit(1)
                        )
                        now = datetime.now(UTC)
                        due = (
                            now
                            if report["classification"]["urgent"]
                            else min(
                                pending or now + timedelta(seconds=3), now + timedelta(seconds=3)
                            )
                        )
                        case.context = {**case.context, "impact": report}
                        add_input(
                            db,
                            case,
                            _source_input_key("source", batch.run_id, batch.revision, case.case_id),
                            "SOURCE",
                            {
                                "event_ids": [e.event_id for e in events],
                                "source_revision": str(batch.revision),
                                "impact": report,
                            },
                            available_at=due,
                        )
                        received += 1
                cursor.source_revision = batch.revision
    return received


def wake_completed_jobs(engine: Engine) -> int:
    from packages.planning.business_service import study_view
    from packages.planning.publication import Publication
    from packages.planning.store import ApprovalRecord

    with Session(engine) as db:
        ids = list(db.scalars(select(CaseRecord.case_id).where(CaseRecord.state.not_in(TERMINAL))))
    count = 0
    for case_id in ids:
        with Session(engine) as db, db.begin():
            case = db.get(CaseRecord, case_id, with_for_update=True)
            assert case is not None
            for operation in db.scalars(
                select(CaseOperation).where(
                    CaseOperation.case_id == case_id,
                    CaseOperation.action.in_(("solve_scenario", "evaluate_business_options")),
                    CaseOperation.state == "DONE",
                )
            ):
                job_id = (operation.result or {}).get("job_id")
                job = db.get(SolveJob, job_id) if job_id else None
                if job is None or job.state not in ("SUCCEEDED", "FAILED"):
                    continue
                stopped_at = case.context.get("stopped_at")
                if stopped_at and operation.created_at <= datetime.fromisoformat(stopped_at):
                    continue
                study_payload = {}
                if job.business_request is not None:
                    current_state = db.get(FactoryState, case.factory_id)
                    current_record = (
                        db.get(SnapshotRecord, current_state.snapshot_id) if current_state else None
                    )
                    current_snapshot = (
                        Snapshot.model_validate(current_record.document) if current_record else None
                    )
                    study_payload = {"business_study": study_view(job, current_snapshot)}
                key = "solver:" + job.job_id
                if db.scalar(
                    select(CaseInput.input_id).where(
                        CaseInput.factory_id == case.factory_id, CaseInput.input_key == key
                    )
                ):
                    continue
                solved_candidate = (
                    db.get(CandidateRecord, job.candidate_id) if job.candidate_id else None
                )
                add_input(
                    db,
                    case,
                    key,
                    "SOLVER_RESULT",
                    {
                        "job_id": job.job_id,
                        "state": job.state,
                        "candidate_id": job.candidate_id,
                        "error_code": job.error_code,
                        **study_payload,
                        "has_solution": solved_candidate.document["has_solution"]
                        if solved_candidate
                        else None,
                    },
                )
                if job.candidate_id:
                    case.context = {
                        **case.context,
                        "candidate_ids": sorted(
                            set([*case.context.get("candidate_ids", []), job.candidate_id])
                        ),
                    }
                count += 1
            candidate_ids = case.context.get("candidate_ids", [])
            approvals = db.scalars(
                select(ApprovalRecord)
                .where(
                    ApprovalRecord.factory_id == case.factory_id,
                    ApprovalRecord.candidate_id.in_(candidate_ids),
                )
                .order_by(ApprovalRecord.created_at)
                .limit(100)
            )
            for approval in approvals:
                key = "approval:" + approval.approval_id
                if not db.scalar(
                    select(CaseInput.input_id).where(
                        CaseInput.factory_id == case.factory_id, CaseInput.input_key == key
                    )
                ):
                    add_input(db, case, key, "APPROVAL", approval.document)
                    count += 1
            for release in db.scalars(
                select(Publication).where(
                    Publication.factory_id == case.factory_id,
                    Publication.candidate_id.in_(candidate_ids),
                )
            ):
                current = release.document
                key = f"release:{release.release_id}:{current['source_state']}:{current['execution_state']}"
                if not db.scalar(
                    select(CaseInput.input_id).where(
                        CaseInput.factory_id == case.factory_id, CaseInput.input_key == key
                    )
                ):
                    add_input(db, case, key, "PUBLICATION", current)
                    count += 1
    return count
