"""Certificate decisions are immutable BYOF records, separate from enterprise facts."""

from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.persistence import Base


class ValidationRecord(Base):
    __tablename__ = "validation_certificates"
    __table_args__ = (UniqueConstraint("factory_id", "request_id"), {"schema": "byof"})
    certificate_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    factory_id: Mapped[str] = mapped_column(String(160), index=True)
    candidate_id: Mapped[str] = mapped_column(String(160))
    request_id: Mapped[str] = mapped_column(String(160))
    requester_id: Mapped[str] = mapped_column(String(100))
    payload_hash: Mapped[str] = mapped_column(String(64))
    document: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
