"""Human-authorized workbench actions; model output cannot create these records."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.persistence import Base


class AssistantAction(Base):
    __tablename__ = "assistant_actions"
    __table_args__ = (UniqueConstraint("factory_id", "request_id"), {"schema": "byof"})
    action_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    factory_id: Mapped[str] = mapped_column(String(160))
    user_id: Mapped[str] = mapped_column(String(100))
    request_id: Mapped[str] = mapped_column(String(160))
    run_id: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(30))
    result: Mapped[dict | None] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
