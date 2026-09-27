"""BYOF owns contact configuration and send evidence, independently of human responses."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from packages.persistence import Base


class NotificationContact(Base):
    __tablename__ = "notification_contacts"
    __table_args__ = {"schema": "byof"}
    factory_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    role: Mapped[str] = mapped_column(String(30), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(254))
    version: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ContactAction(Base):
    __tablename__ = "notification_contact_actions"
    __table_args__ = {"schema": "byof"}
    factory_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(100))
    payload_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = {"schema": "byof"}
    notification_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    factory_id: Mapped[str] = mapped_column(String(160), index=True)
    case_id: Mapped[str] = mapped_column(String(160))
    task_id: Mapped[str] = mapped_column(String(160), index=True)
    task_version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(30))
    dedupe_key: Mapped[str] = mapped_column(String(240), unique=True)
    reminder_id: Mapped[str | None] = mapped_column(String(160))
    role: Mapped[str] = mapped_column(String(30))
    contact_version: Mapped[int | None] = mapped_column(Integer)
    recipient_id: Mapped[str | None] = mapped_column(String(100))
    recipient_email: Mapped[str | None] = mapped_column(String(254))
    message_id: Mapped[str] = mapped_column(String(240), unique=True)
    send_state: Mapped[str] = mapped_column(String(30))
    attempts: Mapped[int] = mapped_column(Integer)
    lease_token: Mapped[str | None] = mapped_column(String(160))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
