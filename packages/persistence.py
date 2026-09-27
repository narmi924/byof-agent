"""PostgreSQL connections and authentication records owned by BYOF."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"schema": "byof"}
    user_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = {"schema": "byof"}
    user_id: Mapped[str] = mapped_column(ForeignKey("byof.users.user_id"), primary_key=True)
    factory_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    role: Mapped[str] = mapped_column(String(30), primary_key=True)


class LoginSession(Base):
    __tablename__ = "sessions"
    __table_args__ = {"schema": "byof"}
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("byof.users.user_id"))
    csrf_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def connect(url: str) -> Engine:
    if not url.startswith("postgresql+psycopg://"):
        raise ValueError("PostgreSQL configuration is required")
    return create_engine(
        url, pool_pre_ping=True, hide_parameters=True, connect_args={"connect_timeout": 3}
    )
