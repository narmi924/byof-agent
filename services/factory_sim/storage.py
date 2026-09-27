"""Simulator-owned facts. BYOF application code must access these through HTTP."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.persistence import Base


class World(Base):
    __tablename__ = "worlds"
    __table_args__ = {"schema": "factory_sim"}
    factory_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(160))
    revision: Mapped[int] = mapped_column(Integer)
    business_clock: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    document: Mapped[dict] = mapped_column(JSONB)
    active_candidate: Mapped[dict | None] = mapped_column(JSONB)
    mode: Mapped[str] = mapped_column(String(20), default="PAUSED", server_default="PAUSED")
    interval_ms: Mapped[int] = mapped_column(Integer, default=1000, server_default="1000")
    next_tick_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replay_state: Mapped[dict | None] = mapped_column(JSONB)
    scenario_state: Mapped[dict | None] = mapped_column(JSONB)


class SourceChange(Base):
    __tablename__ = "changes"
    __table_args__ = {"schema": "factory_sim"}
    run_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    factory_id: Mapped[str] = mapped_column(String(160), index=True)
    document: Mapped[dict] = mapped_column(JSONB)


class SourceAction(Base):
    __tablename__ = "actions"
    __table_args__ = (
        UniqueConstraint("factory_id", "run_id", "operation_id"),
        {"schema": "factory_sim"},
    )
    action_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    factory_id: Mapped[str] = mapped_column(String(160), index=True)
    run_id: Mapped[str] = mapped_column(String(160))
    operation_id: Mapped[str] = mapped_column(String(160))
    payload_hash: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(50))
    request: Mapped[dict] = mapped_column(JSONB)
    result: Mapped[dict] = mapped_column(JSONB)


class SourceRun(Base):
    __tablename__ = "runs"
    __table_args__ = {"schema": "factory_sim"}
    run_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    factory_id: Mapped[str] = mapped_column(String(160), index=True)
    initial_snapshot: Mapped[dict] = mapped_column(JSONB)
    replay_of: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
