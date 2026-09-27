"""User-scoped preference with optimistic writes; each Agent turn pins its model."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column

from packages.auth import AccessError, Principal
from packages.persistence import Base, Membership, User
from packages.providers.catalog import ModelCatalog
from packages.providers.registry import ProviderUnavailable, TextModel


class UserModelSelection(Base):
    __tablename__ = "user_model_selections"
    __table_args__ = {"schema": "byof"}
    user_id: Mapped[str] = mapped_column(ForeignKey("byof.users.user_id"), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(80))
    version: Mapped[int] = mapped_column(default=1)
    request_id: Mapped[str] = mapped_column(String(160))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def selected(db: Session, user_id: str, catalog: ModelCatalog) -> str:
    row = db.get(UserModelSelection, user_id)
    return row.model_id if row else catalog.default_id


def view(db: Session, user_id: str, catalog: ModelCatalog) -> dict:
    row = db.get(UserModelSelection, user_id)
    return {
        "selected_model_id": row.model_id if row else catalog.default_id,
        "version": row.version if row else 0,
        "models": catalog.public(),
        "scope": "CURRENT_USER",
        "takes_effect": "NEXT_TURN",
    }


def choose(
    engine: Engine,
    actor: Principal,
    catalog: ModelCatalog,
    *,
    model_id: str,
    expected_version: int,
    request_id: str,
) -> dict:
    if not any(g.role in {"manager", "planner"} for g in actor.grants):
        raise AccessError("FORBIDDEN", "This account may not choose the Production Agent model.")
    try:
        catalog.model(model_id)
    except ProviderUnavailable:
        raise AccessError(
            "MODEL_UNAVAILABLE",
            "This model is not configured or has been retired; choose an available model.",
            409,
        ) from None
    with Session(engine) as db, db.begin():
        user = db.get(User, actor.user_id, with_for_update=True)
        if user is None or not user.active:
            raise AccessError("UNAUTHENTICATED", "The signed-in identity has expired.", 401)
        grants = db.scalars(
            select(Membership)
            .where(
                Membership.user_id == actor.user_id,
                Membership.role.in_(("manager", "planner")),
            )
            .with_for_update(read=True)
        ).all()
        if not grants:
            raise AccessError(
                "FORBIDDEN", "This account may not choose the Production Agent model."
            )
        row = db.get(UserModelSelection, actor.user_id)
        if row and row.request_id == request_id:
            if row.model_id != model_id or row.version != expected_version + 1:
                raise AccessError(
                    "IDEMPOTENCY_CONFLICT",
                    "This switch request was already used for another choice.",
                    409,
                )
            return view(db, actor.user_id, catalog)
        if (row.version if row else 0) != expected_version:
            raise AccessError(
                "MODEL_SELECTION_CHANGED",
                "Another window switched the model; refresh and choose again.",
                409,
            )
        if row is None:
            row = UserModelSelection(user_id=actor.user_id)
            db.add(row)
        row.model_id, row.version, row.request_id = model_id, expected_version + 1, request_id
        row.updated_at = datetime.now(UTC)
        db.flush()
        return view(db, actor.user_id, catalog)


class UserModelRouter:
    def __init__(self, catalog: ModelCatalog, default_model: TextModel):
        self.catalog, self.default_model = catalog, default_model

    def complete(self, prompt: str) -> str:
        return self.default_model.complete(prompt)

    def for_user(self, db: Session, user_id: str, pinned_id: str | None) -> tuple[str, TextModel]:
        model_id = pinned_id or selected(db, user_id, self.catalog)
        return model_id, self.catalog.model(model_id)
