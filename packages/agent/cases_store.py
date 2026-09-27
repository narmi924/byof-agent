"""Durable case coordination records; checkpoints only reference this business ledger."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from packages.auth import AccessError
from packages.domain.models import canonical_hash
from packages.persistence import Base


class CaseRecord(Base):
    __tablename__ = "cases"
    __table_args__ = {"schema": "byof"}
    case_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    factory_id: Mapped[str] = mapped_column(String(160), index=True)
    run_id: Mapped[str] = mapped_column(String(160))
    owner_id: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(40))
    version: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(240))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    active_turn_id: Mapped[str | None] = mapped_column(String(160))
    snapshot_id: Mapped[str | None] = mapped_column(String(160))
    context: Mapped[dict] = mapped_column(JSONB, default=dict)
    closure: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(80))


class CaseInput(Base):
    __tablename__ = "case_inputs"
    __table_args__ = (UniqueConstraint("factory_id", "input_key"), {"schema": "byof"})
    input_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    factory_id: Mapped[str] = mapped_column(String(160), index=True)
    case_id: Mapped[str] = mapped_column(String(160), index=True)
    input_key: Mapped[str] = mapped_column(String(250))
    kind: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict] = mapped_column(JSONB)
    payload_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    turn_id: Mapped[str | None] = mapped_column(String(160))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_reason: Mapped[str | None] = mapped_column(String(80))


class CaseTurn(Base):
    __tablename__ = "case_turns"
    __table_args__ = {"schema": "byof"}
    turn_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(160), index=True)
    factory_id: Mapped[str] = mapped_column(String(160), index=True)
    state: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(160))
    model_requests: Mapped[int] = mapped_column(Integer, default=0)
    solver_requests: Mapped[int] = mapped_column(Integer, default=0)
    next_step: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    model_pending: Mapped[bool] = mapped_column(default=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    model_id: Mapped[str | None] = mapped_column(String(80))


class CaseOperation(Base):
    __tablename__ = "case_operations"
    __table_args__ = (UniqueConstraint("turn_id", "step"), {"schema": "byof"})
    operation_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(160), index=True)
    factory_id: Mapped[str] = mapped_column(String(160), index=True)
    turn_id: Mapped[str] = mapped_column(String(160))
    step: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(40))
    reason_summary: Mapped[str | None] = mapped_column(String(500))
    parameters: Mapped[dict] = mapped_column(JSONB)
    parameter_hash: Mapped[str] = mapped_column(String(64))
    expected_case_version: Mapped[int] = mapped_column(Integer)
    snapshot_id: Mapped[str] = mapped_column(String(160))
    state: Mapped[str] = mapped_column(String(40))
    result: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CaseCursor(Base):
    __tablename__ = "case_cursors"
    __table_args__ = {"schema": "byof"}
    factory_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(160))
    source_revision: Mapped[int] = mapped_column(Integer)


def cancel_timers(
    db: Session, case: CaseRecord, reason: str, *, keep_input_id: str | None = None
) -> None:
    """Caller holds the Case lock; cancellation never impersonates a processed input."""
    rows = list(
        db.scalars(
            select(CaseInput).where(
                CaseInput.case_id == case.case_id,
                CaseInput.kind == "TIMER",
                CaseInput.turn_id.is_(None),
                CaseInput.cancelled_at.is_(None),
            )
        )
    )
    now = datetime.now(UTC)
    changed = False
    for row in rows:
        if row.input_id != keep_input_id:
            row.cancelled_at, row.cancellation_reason = now, reason
            changed = True
    if changed:
        case.version += 1
        case.updated_at = now


def add_input(
    db: Session,
    case: CaseRecord,
    input_key: str,
    kind: str,
    payload: dict,
    *,
    available_at: datetime | None = None,
) -> CaseInput:
    """Caller holds the Case row lock; persistent input survives worker downtime."""
    digest = canonical_hash({"kind": kind, "payload": payload, "case_id": case.case_id})
    old = db.scalar(
        select(CaseInput).where(
            CaseInput.factory_id == case.factory_id, CaseInput.input_key == input_key
        )
    )
    if old is not None:
        if old.payload_hash != digest:
            raise AccessError(
                "IDEMPOTENCY_CONFLICT", "The input ID was already used for other content.", 409
            )
        return old
    now = datetime.now(UTC)
    row = CaseInput(
        input_id=str(uuid4()),
        factory_id=case.factory_id,
        case_id=case.case_id,
        input_key=input_key,
        kind=kind,
        payload=payload,
        payload_hash=digest,
        created_at=now,
        available_at=available_at or now,
    )
    db.add(row)
    case.version += 1
    case.updated_at = now
    return row
