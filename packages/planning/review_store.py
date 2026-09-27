"""Immutable evidence is attached one-to-one to an actual human approval."""

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.persistence import Base


class ApprovalReviewRecord(Base):
    __tablename__ = "approval_reviews"
    __table_args__ = {"schema": "byof"}
    review_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    factory_id: Mapped[str] = mapped_column(String(160), index=True)
    candidate_id: Mapped[str] = mapped_column(String(160), index=True)
    approval_id: Mapped[str] = mapped_column(String(160), unique=True)
    document: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
