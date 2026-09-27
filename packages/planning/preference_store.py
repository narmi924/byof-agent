"""Preference proposals, human confirmation and effective objective evidence belong to BYOF."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.persistence import Base


class PreferenceState(Base):
    __tablename__ = "preference_states"
    __table_args__ = {"schema": "byof"}
    factory_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    coordination_id: Mapped[str | None] = mapped_column(String(160))


class PreferenceProposal(Base):
    __tablename__ = "preference_proposals"
    __table_args__ = {"schema": "byof"}
    proposal_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    factory_id: Mapped[str] = mapped_column(String(160), index=True)
    scope_type: Mapped[str] = mapped_column(String(20))
    scope_id: Mapped[str] = mapped_column(String(160))
    proposer_id: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(20))
    document: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PreferenceRevision(Base):
    __tablename__ = "preference_revisions"
    __table_args__ = (
        UniqueConstraint("factory_id", "scope_type", "scope_id", "version"),
        {"schema": "byof"},
    )
    preference_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    factory_id: Mapped[str] = mapped_column(String(160), index=True)
    scope_type: Mapped[str] = mapped_column(String(20))
    scope_id: Mapped[str] = mapped_column(String(160))
    version: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PreferenceHead(Base):
    __tablename__ = "preference_heads"
    __table_args__ = {"schema": "byof"}
    factory_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    scope_type: Mapped[str] = mapped_column(String(20), primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    preference_id: Mapped[str] = mapped_column(String(160))
    active: Mapped[bool] = mapped_column(Boolean)


class PreferenceAction(Base):
    __tablename__ = "preference_actions"
    __table_args__ = {"schema": "byof"}
    factory_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(30))
    payload_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PreferenceCoordination(Base):
    __tablename__ = "preference_coordinations"
    __table_args__ = {"schema": "byof"}
    coordination_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    factory_id: Mapped[str] = mapped_column(String(160), index=True)
    context_hash: Mapped[str] = mapped_column(String(64))
    document: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ObjectiveRecord(Base):
    __tablename__ = "objective_contracts"
    __table_args__ = {"schema": "byof"}
    objective_version: Mapped[str] = mapped_column(String(160), primary_key=True)
    factory_id: Mapped[str] = mapped_column(String(160), index=True)
    document: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
