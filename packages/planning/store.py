"""BYOF projections and work records; contains no access to simulator-owned tables."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.persistence import Base


class FactoryState(Base):
    __tablename__ = "factory_states"
    __table_args__ = {"schema": "byof"}
    factory_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(160))
    run_id: Mapped[str] = mapped_column(String(160))
    source_revision: Mapped[str] = mapped_column(String(160))
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    connector_capabilities: Mapped[dict | None] = mapped_column(JSONB)
    capabilities_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SnapshotRecord(Base):
    __tablename__ = "snapshots"
    __table_args__ = {"schema": "byof"}
    snapshot_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    factory_id: Mapped[str] = mapped_column(String(160), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    document: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SolveJob(Base):
    __tablename__ = "solve_jobs"
    __table_args__ = (UniqueConstraint("factory_id", "request_id"), {"schema": "byof"})
    job_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    factory_id: Mapped[str] = mapped_column(String(160), index=True)
    request_id: Mapped[str] = mapped_column(String(160))
    requester_id: Mapped[str] = mapped_column(String(100))
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("byof.snapshots.snapshot_id"))
    allow_overtime: Mapped[bool] = mapped_column(Boolean)
    time_limit: Mapped[int] = mapped_column(Integer)
    new_actions_not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(160))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    candidate_id: Mapped[str | None] = mapped_column(String(160))
    error_code: Mapped[str | None] = mapped_column(String(80))
    objective_version: Mapped[str] = mapped_column(String(160), default="delivery-v1")
    case_id: Mapped[str | None] = mapped_column(String(160))
    business_request: Mapped[dict | None] = mapped_column(JSONB)
    business_result: Mapped[dict | None] = mapped_column(JSONB)
    reused_from_id: Mapped[str | None] = mapped_column(String(160), index=True)


class CandidateRecord(Base):
    __tablename__ = "candidates"
    __table_args__ = {"schema": "byof"}
    candidate_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    factory_id: Mapped[str] = mapped_column(String(160), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("byof.snapshots.snapshot_id"))
    content_hash: Mapped[str] = mapped_column(String(64))
    document: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ApprovalRecord(Base):
    __tablename__ = "approvals"
    __table_args__ = (UniqueConstraint("factory_id", "request_id"), {"schema": "byof"})
    approval_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(160))
    factory_id: Mapped[str] = mapped_column(String(160), index=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("byof.candidates.candidate_id"))
    document: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
