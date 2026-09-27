"""Authenticated information tasks; human answers are evidence, never source fact updates."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import Field, StrictBool, StrictStr, TypeAdapter, ValidationError
from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint, and_, or_, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column, object_session

from packages.agent.cases_store import CaseOperation, CaseRecord, CaseTurn, add_input, cancel_timers
from packages.auth import AccessError, Principal, lock_membership, lock_memberships, lock_user
from packages.domain.models import (
    Contract,
    Digest,
    Identifier,
    Positive,
    Snapshot,
    Timestamp,
    canonical_hash,
)
from packages.integrations.notification_store import Notification
from packages.persistence import Base, Membership, User
from packages.planning.service import require_live
from packages.planning.store import FactoryState, SnapshotRecord

TaskRole = Literal["maintainer", "warehouse", "team_lead", "planner", "manager"]
TaskField = Literal[
    "repair_eta", "remaining_minutes", "remaining_setup_minutes", "receipt_eta", "comment"
]
LIVE_STATES = {"OPEN", "ESCALATED"}
TERMINAL_CASE_STATES = {"RESOLVED", "HANDED_OFF", "CANCELLED"}
ROLES = {"maintainer", "warehouse", "team_lead", "planner", "manager"}


class HumanTaskRecord(Base):
    __tablename__ = "human_tasks"
    __table_args__ = (
        UniqueConstraint("factory_id", "operation_id"),
        Index(
            "human_tasks_open_question",
            "case_id",
            "question_hash",
            unique=True,
            postgresql_where=text("state IN ('OPEN','ESCALATED')"),
        ),
        {"schema": "byof"},
    )
    task_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    factory_id: Mapped[str] = mapped_column(String(160), index=True)
    case_id: Mapped[str] = mapped_column(String(160), index=True)
    operation_id: Mapped[str] = mapped_column(String(160))
    question: Mapped[str] = mapped_column(String(2000))
    subject_id: Mapped[str] = mapped_column(String(160))
    owner_role: Mapped[str] = mapped_column(String(30))
    owner_id: Mapped[str | None] = mapped_column(String(100))
    requested_fields: Mapped[list] = mapped_column(JSONB)
    creation_hash: Mapped[str] = mapped_column(String(64))
    question_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(30))
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminders_count: Mapped[int] = mapped_column(Integer)
    response: Mapped[dict | None] = mapped_column(JSONB)


class TaskAction(Base):
    __tablename__ = "task_actions"
    __table_args__ = (UniqueConstraint("factory_id", "request_id"), {"schema": "byof"})
    action_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(160), index=True)
    case_id: Mapped[str] = mapped_column(String(160), index=True)
    factory_id: Mapped[str] = mapped_column(String(160), index=True)
    request_id: Mapped[str] = mapped_column(String(160))
    actor_id: Mapped[str | None] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(30))
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TaskReminder(Base):
    __tablename__ = "task_reminders"
    __table_args__ = (UniqueConstraint("task_id", "ordinal"), {"schema": "byof"})
    reminder_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(160), index=True)
    case_id: Mapped[str] = mapped_column(String(160), index=True)
    factory_id: Mapped[str] = mapped_column(String(160), index=True)
    task_version: Mapped[int] = mapped_column(Integer)
    ordinal: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TaskCreation(Contract):
    case_id: Identifier
    factory_id: Identifier
    operation_id: Identifier
    question: Annotated[StrictStr, Field(min_length=1, max_length=500)]
    role: TaskRole
    subject_id: Identifier
    fields: tuple[TaskField, ...] = Field(min_length=1, max_length=5)
    deadline_minutes: Positive = Field(le=1440)


class ResponseInput(Contract):
    request_id: Identifier
    expected_task_version: Positive
    answer: dict[str, Any]


class TransferInput(Contract):
    request_id: Identifier
    expected_task_version: Positive
    target_role: TaskRole
    target_owner_id: Identifier | None = None
    reason: Annotated[StrictStr, Field(min_length=1, max_length=500, pattern=r"\S")]


class CancelInput(Contract):
    request_id: Identifier
    expected_task_version: Positive
    reason: Annotated[StrictStr, Field(min_length=1, max_length=500, pattern=r"\S")]


class HandoffInput(Contract):
    request_id: Identifier
    expected_task_version: Positive
    expected_case_version: Positive
    expected_snapshot_hash: Digest
    accept_responsibility: StrictBool
    accept_risks: StrictBool
    responsibility_summary: Annotated[
        StrictStr, Field(min_length=1, max_length=2000, pattern=r"\S")
    ]
    risk_summary: Annotated[StrictStr, Field(min_length=1, max_length=2000, pattern=r"\S")]


@contextmanager
def _transaction(engine: Engine):
    try:
        with Session(engine) as db, db.begin():
            yield db
    except IntegrityError as exc:
        raise AccessError(
            "IDEMPOTENCY_CONFLICT",
            "The action ID or open question already exists; reload the task.",
            409,
        ) from exc


def _parse(model, payload):
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise AccessError(
            "INVALID_TASK_INPUT",
            "The task parameters are incomplete or malformed; check the input.",
            422,
        ) from exc


def _lock_case(
    db: Session, factory_id: str, case_id: str
) -> tuple[FactoryState | None, CaseRecord]:
    state = db.get(FactoryState, factory_id, with_for_update=True)
    case = db.get(CaseRecord, case_id, with_for_update=True)
    if case is None or case.factory_id != factory_id:
        raise AccessError(
            "NOT_FOUND", "The case does not exist or does not belong to the current factory.", 404
        )
    return state, case


def _current_run(db: Session, state: FactoryState | None, case: CaseRecord) -> None:
    saved = db.get(SnapshotRecord, state.snapshot_id) if state is not None else None
    if state is None or saved is None or saved.factory_id != case.factory_id:
        raise AccessError(
            "SNAPSHOT_REQUIRED", "This factory has no current snapshot; sync the data first.", 409
        )
    try:
        snapshot = Snapshot.model_validate(saved.document)
    except ValidationError as exc:
        raise AccessError(
            "INVALID_SOURCE_SNAPSHOT",
            "The current snapshot does not meet the business contract; check the source.",
            409,
        ) from exc
    if (
        snapshot.factory_id != case.factory_id
        or snapshot.snapshot_id != saved.snapshot_id
        or snapshot.content_hash != saved.content_hash
        or snapshot.source.source_revision != state.source_revision
    ):
        raise AccessError(
            "INVALID_SOURCE_SNAPSHOT", "The current snapshot does not match the source record.", 409
        )
    if snapshot.run_id != case.run_id or state.run_id != case.run_id:
        raise AccessError(
            "SOURCE_RUN_CHANGED",
            "The source run has changed; manual tasks of the old run cannot be handled.",
            409,
        )
    require_live(snapshot)


def _case(db: Session, factory_id: str, case_id: str) -> CaseRecord:
    state, case = _lock_case(db, factory_id, case_id)
    _current_run(db, state, case)
    return case


def _locked_task(db: Session, factory_id: str, task_id: str) -> tuple[CaseRecord, HumanTaskRecord]:
    case_id = db.scalar(
        select(HumanTaskRecord.case_id).where(
            HumanTaskRecord.task_id == task_id, HumanTaskRecord.factory_id == factory_id
        )
    )
    if case_id is None:
        raise AccessError("NOT_FOUND", "The manual task does not exist.", 404)
    case = _case(db, factory_id, case_id)
    task = db.get(HumanTaskRecord, task_id, with_for_update=True)
    assert task is not None
    return case, task


def _roles(db: Session, actor: Principal, factory_id: str, *, lock: bool = False) -> set[str]:
    if lock:
        user = lock_user(db, actor.user_id)
        roles = {row.role for row in lock_memberships(db, actor.user_id, factory_id)}
    else:
        user = db.get(User, actor.user_id, populate_existing=True)
        roles = set(
            db.scalars(
                select(Membership.role).where(
                    Membership.user_id == actor.user_id, Membership.factory_id == factory_id
                )
            )
        )
    if user is None or not user.active or not roles.intersection(ROLES):
        raise AccessError("FORBIDDEN", "This account may not handle manual tasks of this factory.")
    return roles


def _assigned(task: HumanTaskRecord, actor: Principal, roles: set[str]) -> bool:
    return task.owner_role in roles and (task.owner_id is None or task.owner_id == actor.user_id)


def _origin(db: Session, task: HumanTaskRecord) -> CaseOperation | None:
    operation = db.get(CaseOperation, task.operation_id)
    if operation is not None and (
        operation.factory_id != task.factory_id
        or operation.case_id != task.case_id
        or operation.parameter_hash
        != canonical_hash({"action": operation.action, "parameters": operation.parameters})
        or operation.action == "request_approval"
        and operation.parameters.get("candidate_id") != task.subject_id
        or operation.action == "handoff"
        and task.subject_id != task.case_id
    ):
        raise AccessError(
            "TASK_ORIGIN_MISMATCH",
            "The manual task does not match the evidence of the original action.",
            409,
        )
    return operation


def _kind(db: Session | None, task: HumanTaskRecord) -> str:
    operation = _origin(db, task) if db is not None else None
    return {
        "request_approval": "APPROVAL",
        "handoff": "HANDOFF",
    }.get(operation.action if operation else "", "INFORMATION")


def view(task: HumanTaskRecord) -> dict:
    db = object_session(task)
    kind = _kind(db, task)
    case = db.get(CaseRecord, task.case_id) if db is not None else None
    state = db.get(FactoryState, task.factory_id) if db is not None else None
    saved = db.get(SnapshotRecord, state.snapshot_id) if db is not None and state else None
    review = None
    if kind == "APPROVAL" and db is not None:
        from packages.domain.models import Candidate
        from packages.planning.store import CandidateRecord

        record = db.get(CandidateRecord, task.subject_id)
        candidate = (
            Candidate.model_validate(record.document)
            if record is not None and record.factory_id == task.factory_id
            else None
        )
        review = {
            "candidate_id": task.subject_id,
            "required_scopes": sorted(
                {"publish_plan", *(candidate.required_consents if candidate else ())}
            ),
            "outcome": (task.response or {}).get("outcome", "PENDING"),
        }
    notification = (
        db.scalar(
            select(Notification)
            .where(Notification.task_id == task.task_id)
            .order_by(Notification.created_at.desc())
            .limit(1)
        )
        if db is not None
        else None
    )
    send_state = notification.send_state if notification else "NOT_ENABLED"
    return {
        "task_id": task.task_id,
        "factory_id": task.factory_id,
        "case_id": task.case_id,
        "task_type": kind,
        "case_version": case.version if case is not None else None,
        "snapshot_hash": saved.content_hash if saved is not None else None,
        "review": review,
        "version": task.version,
        "question": task.question,
        "subject_id": task.subject_id,
        "owner_id": task.owner_id,
        "owner_role": task.owner_role,
        "fields": task.requested_fields,
        "state": task.state,
        "response": task.response,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
        "due_at": task.due_at.isoformat(),
        "clock": "real",
        "reminders_count": task.reminders_count,
        "send_state": "QUEUED" if send_state == "CLAIMED" else send_state,
        "delivery_state": "UNAVAILABLE",
    }


def _prior(
    db: Session, factory_id: str, request_id: str, kind: str, payload: dict, actor_id: str | None
) -> TaskAction | None:
    old = db.scalar(
        select(TaskAction).where(
            TaskAction.factory_id == factory_id, TaskAction.request_id == request_id
        )
    )
    try:
        digest = canonical_hash(payload)
    except (ValueError, TypeError) as exc:
        raise AccessError(
            "INVALID_TASK_INPUT", "The task parameters contain unrecognized values.", 422
        ) from exc
    if old is not None and (
        old.kind != kind or old.payload_hash != digest or old.actor_id != actor_id
    ):
        raise AccessError(
            "IDEMPOTENCY_CONFLICT",
            "The action ID was already used for other content or another identity.",
            409,
        )
    return old


def _record(
    db: Session,
    task: HumanTaskRecord,
    request_id: str,
    kind: str,
    payload: dict,
    actor_id: str | None,
) -> dict:
    result = view(task)
    db.add(
        TaskAction(
            action_id=str(uuid4()),
            task_id=task.task_id,
            case_id=task.case_id,
            factory_id=task.factory_id,
            request_id=request_id,
            actor_id=actor_id,
            kind=kind,
            payload_hash=canonical_hash(payload),
            payload=payload,
            result=result,
            created_at=datetime.now(UTC),
        )
    )
    return result


def create_task(
    engine: Engine,
    *,
    case_id: str,
    factory_id: str,
    operation_id: str,
    question: str,
    role: str,
    subject_id: str,
    fields: list[str],
    deadline_minutes: int,
    actor: Principal | None = None,
) -> dict:
    request = _parse(
        TaskCreation,
        {
            "case_id": case_id,
            "factory_id": factory_id,
            "operation_id": operation_id,
            "question": question,
            "role": role,
            "subject_id": subject_id,
            "fields": fields,
            "deadline_minutes": deadline_minutes,
        },
    )
    if not request.question.strip() or len(set(request.fields)) != len(request.fields):
        raise AccessError(
            "INVALID_TASK_INPUT",
            "The question cannot be empty and required fields cannot repeat.",
            422,
        )
    payload = request.model_dump(mode="json")
    question_hash = canonical_hash(
        {
            "question": question.strip(),
            "role": role,
            "subject_id": subject_id,
            "fields": sorted(fields),
        }
    )
    with _transaction(engine) as db:
        case = _case(db, factory_id, case_id)
        operation = db.get(CaseOperation, operation_id)
        if operation is not None and operation.action in {"request_approval", "handoff"}:
            if (
                operation.factory_id != factory_id
                or operation.case_id != case_id
                or operation.parameter_hash
                != canonical_hash({"action": operation.action, "parameters": operation.parameters})
                or operation.action == "request_approval"
                and operation.parameters.get("candidate_id") != subject_id
                or operation.action == "handoff"
                and subject_id != case_id
            ):
                raise AccessError(
                    "TASK_ORIGIN_MISMATCH",
                    "The manual task does not match the evidence of the original action.",
                    409,
                )
            question_hash = canonical_hash(
                {"task_type": operation.action, "question_hash": question_hash}
            )
        if actor is not None:
            actor.require(factory_id, {"planner"})
        requester_id = actor.user_id if actor is not None else case.owner_id
        # Keep Case before identity locks, matching other task writers. The locks cover commit.
        requester = lock_user(db, requester_id)
        membership = lock_membership(db, requester_id, factory_id, "planner")
        if requester is None or not requester.active or membership is None:
            raise AccessError(
                "AUTHORIZATION_REVOKED",
                "This account may no longer create manual tasks for this factory.",
            )
        prior = _prior(db, factory_id, operation_id, "CREATE", payload, requester_id)
        if prior is not None:
            task = db.get(HumanTaskRecord, prior.task_id)
            assert task is not None
            return view(task)
        if case.state in TERMINAL_CASE_STATES:
            raise AccessError(
                "CASE_CLOSED", "The case is closed; no new information request can be created.", 409
            )
        task = db.scalar(
            select(HumanTaskRecord)
            .where(
                HumanTaskRecord.case_id == case_id,
                HumanTaskRecord.question_hash == question_hash,
                HumanTaskRecord.state.in_(LIVE_STATES),
            )
            .with_for_update()
        )
        if task is None:
            now = datetime.now(UTC)
            task = HumanTaskRecord(
                task_id=str(uuid4()),
                factory_id=factory_id,
                case_id=case_id,
                operation_id=operation_id,
                question=question.strip(),
                subject_id=subject_id,
                owner_role=role,
                owner_id=None,
                requested_fields=list(request.fields),
                creation_hash=canonical_hash(payload),
                question_hash=question_hash,
                state="OPEN",
                version=1,
                created_at=now,
                updated_at=now,
                due_at=now + timedelta(minutes=deadline_minutes),
                next_reminder_at=now + timedelta(minutes=deadline_minutes),
                reminders_count=0,
            )
            db.add(task)
        return _record(db, task, operation_id, "CREATE", payload, requester_id)


def get_task(engine: Engine, actor: Principal, factory_id: str, task_id: str) -> dict:
    with Session(engine) as db:
        roles = _roles(db, actor, factory_id)
        task = db.get(HumanTaskRecord, task_id)
        if task is None or task.factory_id != factory_id:
            raise AccessError("NOT_FOUND", "The manual task does not exist.", 404)
        if not roles.intersection({"planner", "manager"}) and not _assigned(task, actor, roles):
            raise AccessError("FORBIDDEN", "This account is not the owner of this manual task.")
        return view(task)


def list_tasks(
    engine: Engine,
    actor: Principal,
    factory_id: str,
    *,
    case_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise AccessError("INVALID_TASK_INPUT", "The query limit must be 1 to 100.", 422)
    with Session(engine) as db:
        roles = _roles(db, actor, factory_id)
        statement = select(HumanTaskRecord).where(HumanTaskRecord.factory_id == factory_id)
        if case_id is not None:
            statement = statement.where(HumanTaskRecord.case_id == case_id)
        if not roles.intersection({"planner", "manager"}):
            statement = statement.where(
                HumanTaskRecord.owner_role.in_(roles),
                or_(HumanTaskRecord.owner_id.is_(None), HumanTaskRecord.owner_id == actor.user_id),
            )
        return [
            view(task)
            for task in db.scalars(
                statement.order_by(HumanTaskRecord.created_at.desc()).limit(limit)
            )
        ]


def _cancel_reminders(db: Session, task: HumanTaskRecord) -> None:
    for notification in db.scalars(
        select(Notification)
        .where(
            Notification.task_id == task.task_id,
            Notification.send_state.in_({"QUEUED", "CLAIMED", "NOT_ENABLED", "NOT_CONFIGURED"}),
        )
        .with_for_update()
    ):
        notification.send_state, notification.error_code = "CANCELLED", "TASK_CHANGED"
        notification.lease_token = notification.lease_until = None
        notification.updated_at = datetime.now(UTC)
    for reminder in db.scalars(
        select(TaskReminder)
        .where(TaskReminder.task_id == task.task_id, TaskReminder.state == "QUEUED")
        .with_for_update()
    ):
        reminder.state = "CANCELLED"


def _editable(case: CaseRecord, task: HumanTaskRecord, version: int) -> None:
    if task.version != version:
        raise AccessError(
            "TASK_VERSION_CHANGED", "The task has changed; refresh before replying or acting.", 409
        )
    if task.state not in LIVE_STATES or case.state in TERMINAL_CASE_STATES:
        raise AccessError(
            "TASK_CLOSED", "The task or case is closed and cannot be handled again.", 409
        )


def _answer(task: HumanTaskRecord, answer: dict) -> dict:
    if set(answer) != set(task.requested_fields):
        raise AccessError(
            "INVALID_TASK_ANSWER",
            "The reply must contain the required fields and no confirmations or extra fields.",
            422,
        )
    result: dict[str, Any] = {}
    for field, value in answer.items():
        if field in {"repair_eta", "receipt_eta"}:
            try:
                result[field] = TypeAdapter(Timestamp).validate_python(value).isoformat()
            except ValidationError as exc:
                raise AccessError(
                    "INVALID_TASK_ANSWER", "Expected times must include an explicit time zone.", 422
                ) from exc
        elif field in {"remaining_minutes", "remaining_setup_minutes"}:
            if type(value) is not int or value < 0:
                raise AccessError(
                    "INVALID_TASK_ANSWER",
                    "Remaining work must be an explicit non-negative whole number.",
                    422,
                )
            result[field] = value
        elif field == "comment" and isinstance(value, str) and 0 < len(value.strip()) <= 4000:
            result[field] = value.strip()
        else:
            raise AccessError(
                "INVALID_TASK_ANSWER",
                "The reply does not match the format of the required fields.",
                422,
            )
    return result


def _wake(
    db: Session, case: CaseRecord, task: HumanTaskRecord, request_id: str, kind: str, payload: dict
) -> None:
    add_input(
        db,
        case,
        f"human-task:{task.task_id}:{request_id}",
        kind,
        {"task_id": task.task_id, "task_version": task.version, **payload},
    )


def respond(
    engine: Engine,
    actor: Principal,
    factory_id: str,
    task_id: str,
    *,
    request_id: str,
    expected_task_version: int,
    answer: dict,
) -> dict:
    request = _parse(
        ResponseInput,
        {
            "request_id": request_id,
            "expected_task_version": expected_task_version,
            "answer": answer,
        },
    )
    with _transaction(engine) as db:
        case, task = _locked_task(db, factory_id, task_id)
        roles = _roles(db, actor, factory_id, lock=True)
        if not _assigned(task, actor, roles):
            raise AccessError(
                "FORBIDDEN", "Only the current owner can reply to this information request."
            )
        if _kind(db, task) != "INFORMATION":
            raise AccessError(
                "EXPLICIT_TASK_ACTION_REQUIRED",
                "Use plan approval or an explicit takeover; an ordinary reply does not close this task.",
                409,
            )
        validated_answer = _answer(task, answer)
        payload = {"task_id": task_id, **request.model_dump(mode="json")}
        old = _prior(db, factory_id, request_id, "RESPOND", payload, actor.user_id)
        if old is not None:
            return old.result
        _editable(case, task, expected_task_version)
        answer = validated_answer
        now = datetime.now(UTC)
        task.response = {
            "answer": answer,
            "actor_id": actor.user_id,
            "actor_role": task.owner_role,
            "received_at": now.isoformat(),
            "source": "authenticated_human_information",
        }
        task.state, task.version, task.updated_at = "RESPONDED", task.version + 1, now
        task.next_reminder_at = None
        _cancel_reminders(db, task)
        _wake(db, case, task, request_id, "human_task.responded", {"response": task.response})
        return _record(db, task, request_id, "RESPOND", payload, actor.user_id)


def transfer(
    engine: Engine,
    actor: Principal,
    factory_id: str,
    task_id: str,
    *,
    request_id: str,
    expected_task_version: int,
    target_role: str,
    reason: str,
    target_owner_id: str | None = None,
) -> dict:
    request = _parse(
        TransferInput,
        {
            "request_id": request_id,
            "expected_task_version": expected_task_version,
            "target_role": target_role,
            "target_owner_id": target_owner_id,
            "reason": reason,
        },
    )
    with _transaction(engine) as db:
        case, task = _locked_task(db, factory_id, task_id)
        roles = _roles(db, actor, factory_id, lock=True)
        payload = {"task_id": task_id, **request.model_dump(mode="json")}
        old = _prior(db, factory_id, request_id, "TRANSFER", payload, actor.user_id)
        if old is not None:
            return old.result
        if (
            actor.user_id != case.owner_id
            and "manager" not in roles
            and not _assigned(task, actor, roles)
        ):
            raise AccessError(
                "FORBIDDEN", "Only the case owner, task owner or manager can transfer it."
            )
        _editable(case, task, expected_task_version)
        if _kind(db, task) in {"APPROVAL", "HANDOFF"} and target_role not in {"planner", "manager"}:
            raise AccessError(
                "INVALID_TASK_OWNER",
                "Review and takeover tasks can only be transferred to a planner or manager.",
                409,
            )
        if target_owner_id is not None:
            user = lock_user(db, target_owner_id)
            member = lock_membership(db, target_owner_id, factory_id, target_role)
            if user is None or not user.active or member is None:
                raise AccessError(
                    "INVALID_TASK_OWNER",
                    "The recipient has no valid role required by this factory.",
                    409,
                )
        previous = {"owner_role": task.owner_role, "owner_id": task.owner_id}
        now = datetime.now(UTC)
        task.owner_role, task.owner_id = target_role, target_owner_id
        task.version, task.updated_at = task.version + 1, now
        _cancel_reminders(db, task)
        if task.state == "OPEN":
            task.next_reminder_at = max(task.due_at, now + timedelta(minutes=15))
        _wake(
            db,
            case,
            task,
            request_id,
            "human_task.transferred",
            {
                "previous": previous,
                "owner_role": target_role,
                "owner_id": target_owner_id,
                "actor_id": actor.user_id,
                "reason": reason,
            },
        )
        return _record(db, task, request_id, "TRANSFER", payload, actor.user_id)


def cancel(
    engine: Engine,
    actor: Principal,
    factory_id: str,
    task_id: str,
    *,
    request_id: str,
    expected_task_version: int,
    reason: str,
) -> dict:
    request = _parse(
        CancelInput,
        {
            "request_id": request_id,
            "expected_task_version": expected_task_version,
            "reason": reason,
        },
    )
    with _transaction(engine) as db:
        case, task = _locked_task(db, factory_id, task_id)
        roles = _roles(db, actor, factory_id, lock=True)
        payload = {"task_id": task_id, **request.model_dump(mode="json")}
        old = _prior(db, factory_id, request_id, "CANCEL", payload, actor.user_id)
        if old is not None:
            return old.result
        if (
            actor.user_id != case.owner_id
            and "manager" not in roles
            and not _assigned(task, actor, roles)
        ):
            raise AccessError(
                "FORBIDDEN", "Only the case owner, task owner or manager can cancel it."
            )
        _editable(case, task, expected_task_version)
        task.state, task.version, task.updated_at = "CANCELLED", task.version + 1, datetime.now(UTC)
        task.next_reminder_at = None
        _cancel_reminders(db, task)
        _wake(
            db,
            case,
            task,
            request_id,
            "human_task.cancelled",
            {"actor_id": actor.user_id, "reason": reason},
        )
        return _record(db, task, request_id, "CANCEL", payload, actor.user_id)


def accept_handoff(
    engine: Engine,
    actor: Principal,
    factory_id: str,
    task_id: str,
    **payload_fields,
) -> dict:
    request = _parse(HandoffInput, payload_fields)
    if not request.accept_responsibility or not request.accept_risks:
        raise AccessError(
            "HANDOFF_CONFIRMATION_REQUIRED",
            "Before taking over, explicitly accept the responsibility and remaining risks.",
            422,
        )
    with _transaction(engine) as db:
        case, task = _locked_task(db, factory_id, task_id)
        roles = _roles(db, actor, factory_id, lock=True)
        if task.owner_role not in {"planner", "manager"} or not _assigned(task, actor, roles):
            raise AccessError(
                "FORBIDDEN",
                "Only the currently assigned planner or manager can explicitly take over this case.",
            )
        if _kind(db, task) != "HANDOFF":
            raise AccessError(
                "HANDOFF_TASK_REQUIRED", "This manual task has no real takeover request.", 409
            )
        payload = {"task_id": task_id, **request.model_dump(mode="json")}
        old = _prior(db, factory_id, request.request_id, "ACCEPT_HANDOFF", payload, actor.user_id)
        if old is not None:
            return old.result
        _editable(case, task, request.expected_task_version)
        if case.version != request.expected_case_version:
            raise AccessError(
                "CASE_VERSION_CHANGED",
                "The case received new information; review it again before taking over.",
                409,
            )
        state = db.get(FactoryState, factory_id)
        saved = db.get(SnapshotRecord, state.snapshot_id) if state else None
        assert state is not None and saved is not None
        if saved.content_hash != request.expected_snapshot_hash:
            raise AccessError(
                "SNAPSHOT_CHANGED",
                "The factory facts have changed; check the latest risks before taking over.",
                409,
            )
        now = datetime.now(UTC)
        task.owner_id = actor.user_id
        task.response = {
            "outcome": "HANDED_OFF",
            "actor_id": actor.user_id,
            "actor_role": task.owner_role,
            "received_at": now.isoformat(),
            "source": "authenticated_human_handoff",
            "responsibility_summary": request.responsibility_summary.strip(),
            "risk_summary": request.risk_summary.strip(),
            "accept_responsibility": True,
            "accept_risks": True,
            "snapshot_hash": saved.content_hash,
            "snapshot_id": saved.snapshot_id,
            "run_id": case.run_id,
            "source_revision": state.source_revision,
            "accepted_case_version": case.version,
            "context_hash": canonical_hash(case.context),
        }
        task.state, task.version, task.updated_at = "ACCEPTED", task.version + 1, now
        task.next_reminder_at = None
        _cancel_reminders(db, task)
        for other in db.scalars(
            select(HumanTaskRecord)
            .where(
                HumanTaskRecord.factory_id == factory_id,
                HumanTaskRecord.case_id == case.case_id,
                HumanTaskRecord.task_id != task.task_id,
                HumanTaskRecord.state.in_(LIVE_STATES),
            )
            .order_by(HumanTaskRecord.task_id)
            .with_for_update()
        ):
            other.state, other.version, other.updated_at = "CANCELLED", other.version + 1, now
            other.next_reminder_at = None
            other.response = {"outcome": "HANDED_OFF", "handoff_task_id": task.task_id}
            _cancel_reminders(db, other)
        for turn in db.scalars(
            select(CaseTurn)
            .where(CaseTurn.case_id == case.case_id, CaseTurn.state == "RUNNING")
            .order_by(CaseTurn.turn_id)
            .with_for_update()
        ):
            turn.state, turn.error_code = "CANCELLED", "CASE_HANDED_OFF"
            turn.lease_token = turn.lease_until = None
            turn.model_pending = False
        _wake(
            db, case, task, request.request_id, "human_task.handed_off", {"response": task.response}
        )
        case.state, case.owner_id, case.active_turn_id = "HANDED_OFF", actor.user_id, None
        cancel_timers(db, case, "CASE_HANDED_OFF")
        case.error_code = None
        case.closure = {
            "status": "HANDED_OFF",
            "summary": "The owner explicitly took over the responsibility and remaining risks.",
            "closing_evidence": {"task_id": task.task_id, **task.response},
        }
        return _record(db, task, request.request_id, "ACCEPT_HANDOFF", payload, actor.user_id)


def _review_invalid_reason(db: Session, snapshot: Snapshot, candidate) -> str | None:
    from packages.planning.checker import check_candidate
    from packages.planning.preferences import require_current_objective
    from packages.planning.revalidation import check_progress
    from packages.planning.service import active_baseline

    try:
        require_live(snapshot)
        objective = require_current_objective(db, snapshot, candidate.binding.objective_version)
        if snapshot.content_hash == candidate.binding.snapshot_hash:
            if snapshot.snapshot_clock > candidate.effective_not_before:
                return "STALE_TIME"
            report = check_candidate(
                snapshot,
                candidate,
                baseline=active_baseline(db, snapshot),
                allow_overtime="allow_overtime" in candidate.required_consents,
                objective=objective,
            )
        else:
            if not snapshot.profile.policy.progress_revalidation_enabled:
                return "STALE_CANDIDATE"
            report = check_progress(db, snapshot, candidate).checked.report
        return None if report.status == "PASS" else "CHECK_FAILED"
    except AccessError as exc:
        return exc.code
    except ValueError:
        return "INVALID_REVIEW_EVIDENCE"


def lock_review_cases(db: Session, factory_id: str) -> None:
    """Caller holds FactoryState; acquire all Case locks before any shared identity locks."""
    list(
        db.scalars(
            select(CaseRecord)
            .where(CaseRecord.factory_id == factory_id)
            .order_by(CaseRecord.case_id)
            .with_for_update()
        )
    )


def reconcile_reviews(db: Session, snapshot: Snapshot) -> int:
    """Caller keeps FactoryState locked through commit; ordinary progress needs its full proof."""
    from packages.domain.models import Approval, Candidate
    from packages.planning.publication import _approvals
    from packages.planning.store import ApprovalRecord, CandidateRecord

    state = db.get(FactoryState, snapshot.factory_id, with_for_update=True)
    saved = db.get(SnapshotRecord, state.snapshot_id) if state else None
    if (
        state is None
        or saved is None
        or state.snapshot_id != snapshot.snapshot_id
        or saved.content_hash != snapshot.content_hash
        or state.run_id != snapshot.run_id
    ):
        raise AccessError(
            "INVALID_SOURCE_SNAPSHOT",
            "The review loop must rely on the current saved factory snapshot.",
            409,
        )
    lock_review_cases(db, snapshot.factory_id)
    identities = list(
        db.execute(
            select(HumanTaskRecord.case_id, HumanTaskRecord.task_id)
            .join(CaseOperation, CaseOperation.operation_id == HumanTaskRecord.operation_id)
            .where(
                HumanTaskRecord.factory_id == snapshot.factory_id,
                HumanTaskRecord.state.in_(LIVE_STATES),
                CaseOperation.action == "request_approval",
            )
            .order_by(HumanTaskRecord.case_id, HumanTaskRecord.task_id)
        )
    )
    changed = 0
    for case_id, task_id in identities:
        _, case = _lock_case(db, snapshot.factory_id, case_id)
        task = db.get(HumanTaskRecord, task_id, with_for_update=True)
        if task is None or task.state not in LIVE_STATES or _kind(db, task) != "APPROVAL":
            continue
        record = db.get(CandidateRecord, task.subject_id)
        outcome, reason, approval_ids = "PENDING", None, []
        if case.run_id != snapshot.run_id or case.state in TERMINAL_CASE_STATES:
            outcome, reason = "STALE", "CASE_CLOSED_OR_RUN_CHANGED"
        elif record is None or record.factory_id != snapshot.factory_id:
            outcome, reason = "STALE", "CANDIDATE_NOT_FOUND"
        else:
            try:
                candidate = Candidate.model_validate(record.document)
                if record.content_hash != candidate.content_hash:
                    raise ValueError("Candidate storage hash differs")
                latest = {}
                for row in db.scalars(
                    select(ApprovalRecord)
                    .where(
                        ApprovalRecord.factory_id == snapshot.factory_id,
                        ApprovalRecord.candidate_id == candidate.candidate_id,
                    )
                    .order_by(ApprovalRecord.created_at, ApprovalRecord.approval_id)
                ):
                    approval = Approval.model_validate(row.document)
                    if (
                        approval.candidate_hash == candidate.content_hash
                        and approval.binding == candidate.binding
                    ):
                        latest[approval.action_scope] = approval
                rejected = [
                    latest[scope]
                    for scope in {"publish_plan", *candidate.required_consents}
                    if scope in latest and latest[scope].decision == "REJECTED"
                ]
                if rejected:
                    outcome, approval_ids = "REJECTED", [a.approval_id for a in rejected]
                else:
                    reason = _review_invalid_reason(db, snapshot, candidate)
                    if reason is not None:
                        outcome = "STALE"
                    else:
                        try:
                            approvals = _approvals(db, candidate)
                            outcome, approval_ids = "APPROVED", [a.approval_id for a in approvals]
                        except AccessError:
                            pass
            except ValueError:
                outcome, reason = "STALE", "INVALID_REVIEW_EVIDENCE"
        if outcome == "PENDING":
            continue
        now = datetime.now(UTC)
        task.response = {
            "outcome": outcome,
            "reason": reason,
            "candidate_id": task.subject_id,
            "approval_ids": approval_ids,
            "source": "verified_approval_records",
            "snapshot_hash": snapshot.content_hash,
            "received_at": now.isoformat(),
        }
        request_id = "review:" + canonical_hash({"task_id": task_id, "version": task.version})
        task.state = "CANCELLED" if outcome == "STALE" else "REVIEWED"
        task.version, task.updated_at, task.next_reminder_at = task.version + 1, now, None
        _cancel_reminders(db, task)
        _wake(db, case, task, request_id, "human_task.reviewed", {"response": task.response})
        _record(db, task, request_id, "RECONCILE_REVIEW", task.response, None)
        changed += 1
    return changed


def tick_reminders(engine: Engine, *, limit: int = 50) -> int:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Reminder batch size must be between 1 and 100")
    now = datetime.now(UTC)
    with Session(engine) as db:
        due = list(
            db.execute(
                select(HumanTaskRecord.factory_id, HumanTaskRecord.task_id)
                .join(CaseRecord, HumanTaskRecord.case_id == CaseRecord.case_id)
                .outerjoin(FactoryState, FactoryState.factory_id == HumanTaskRecord.factory_id)
                .outerjoin(SnapshotRecord, SnapshotRecord.snapshot_id == FactoryState.snapshot_id)
                .where(
                    HumanTaskRecord.state.in_(LIVE_STATES),
                    or_(
                        CaseRecord.state.in_(TERMINAL_CASE_STATES),
                        FactoryState.factory_id.is_(None),
                        FactoryState.run_id != CaseRecord.run_id,
                        SnapshotRecord.snapshot_id.is_(None),
                        SnapshotRecord.factory_id != HumanTaskRecord.factory_id,
                        SnapshotRecord.document["run_id"].astext != CaseRecord.run_id,
                        SnapshotRecord.document["source"]["source_system"].astext
                        == "factory-simulator-replay",
                        and_(
                            HumanTaskRecord.state == "OPEN", HumanTaskRecord.next_reminder_at <= now
                        ),
                    ),
                )
                .order_by(HumanTaskRecord.next_reminder_at.asc().nulls_first())
                .limit(limit)
            )
        )
    changed = 0
    for factory_id, task_id in due:
        with _transaction(engine) as db:
            case_id = db.scalar(
                select(HumanTaskRecord.case_id).where(
                    HumanTaskRecord.factory_id == factory_id,
                    HumanTaskRecord.task_id == task_id,
                )
            )
            if case_id is None:
                continue
            state, case = _lock_case(db, factory_id, case_id)
            task = db.get(HumanTaskRecord, task_id, with_for_update=True)
            if task is None:
                continue
            if task.state not in LIVE_STATES:
                continue
            invalid_source = None
            try:
                _current_run(db, state, case)
            except AccessError as exc:
                invalid_source = exc.code
            if invalid_source is not None:
                task.state, task.next_reminder_at = "CANCELLED", None
                task.version, task.updated_at = task.version + 1, now
                _cancel_reminders(db, task)
                _record(
                    db, task, f"source-ended:{task_id}", "CANCEL", {"reason": invalid_source}, None
                )
            elif case.state in TERMINAL_CASE_STATES:
                task.state, task.next_reminder_at = "CANCELLED", None
                task.version, task.updated_at = task.version + 1, now
                _cancel_reminders(db, task)
                _record(
                    db, task, f"case-ended:{task_id}", "CANCEL", {"reason": "CASE_CLOSED"}, None
                )
                _wake(
                    db,
                    case,
                    task,
                    f"case-ended:{task_id}",
                    "human_task.cancelled",
                    {"reason": "CASE_CLOSED"},
                )
            elif (
                task.state != "OPEN" or task.next_reminder_at is None or task.next_reminder_at > now
            ):
                continue
            elif task.reminders_count < 2:
                task.reminders_count += 1
                db.add(
                    TaskReminder(
                        reminder_id=str(uuid4()),
                        task_id=task_id,
                        case_id=case.case_id,
                        factory_id=factory_id,
                        task_version=task.version,
                        ordinal=task.reminders_count,
                        state="QUEUED",
                        scheduled_at=now,
                        created_at=now,
                    )
                )
                task.next_reminder_at = now + timedelta(minutes=15)
                task.updated_at = now
            else:
                task.state, task.next_reminder_at = "ESCALATED", None
                task.version, task.updated_at = task.version + 1, now
                request_id = f"escalate:{task_id}"
                _cancel_reminders(db, task)
                _wake(
                    db,
                    case,
                    task,
                    request_id,
                    "human_task.escalated",
                    {"reason": "RESPONSE_OVERDUE", "escalation_role": "manager"},
                )
                _record(db, task, request_id, "ESCALATE", {"reason": "RESPONSE_OVERDUE"}, None)
            changed += 1
    return changed
