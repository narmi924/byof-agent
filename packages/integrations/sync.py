"""Persist deduplicated source events while independently reconciling the complete HTTP snapshot."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from packages.domain.models import Event, Snapshot, canonical_hash
from packages.domain.snapshot_delta import apply_delta
from packages.integrations.factory_http import ConnectorError, FactoryHTTP
from packages.persistence import Base
from packages.planning.store import SnapshotRecord


class SourceBatch(Base):
    __tablename__ = "source_batches"
    __table_args__ = {"schema": "byof"}
    run_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    factory_id: Mapped[str] = mapped_column(String(160), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    document: Mapped[dict] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RunSwitch(Base):
    __tablename__ = "run_switches"
    __table_args__ = (UniqueConstraint("factory_id", "request_id"), {"schema": "byof"})
    switch_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    factory_id: Mapped[str] = mapped_column(String(160), index=True)
    request_id: Mapped[str] = mapped_column(String(160))
    actor_id: Mapped[str] = mapped_column(String(100))
    previous_run_id: Mapped[str] = mapped_column(String(160))
    run_id: Mapped[str] = mapped_column(String(160))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def save_incremental(
    db: Session, connector: FactoryHTTP, previous: Snapshot, received: Snapshot
) -> None:
    cursor, target = int(previous.source.source_revision), int(received.source.source_revision)
    anchor = previous.content_hash
    reconstructed: Snapshot | None = previous
    if (previous.factory_id, previous.run_id) != (received.factory_id, received.run_id):
        raise ConnectorError("Incremental snapshots are outside the requested scope")
    pages = 0
    while cursor < target:
        pages += 1
        if pages > 100:
            raise ConnectorError("Source backlog exceeds one bounded reconciliation")
        page = connector._get(
            "/factory/v1/changes",
            {
                "factory_id": received.factory_id,
                "run_id": received.run_id,
                "after": cursor,
                "limit": 100,
            },
        )
        if (
            page.get("factory_id") != received.factory_id
            or page.get("run_id") != received.run_id
            or not isinstance(page.get("changes"), list)
        ):
            raise ConnectorError("Source changes are outside the requested scope")
        try:
            watermark = int(page["watermark"])
            next_cursor = int(page["next_cursor"])
            if watermark < target or next_cursor > watermark:
                raise ValueError("Watermark conflict")
            advanced = False
            for batch in page["changes"]:
                revision = int(batch["revision"])
                if revision > target:
                    break
                if (
                    revision != cursor + 1
                    or batch["run_id"] != received.run_id
                    or batch["factory_id"] != received.factory_id
                    or batch["previous_snapshot_hash"] != anchor
                ):
                    raise ValueError("Event gap or inconsistent snapshot chain")
                for raw in batch["events"]:
                    event = Event.model_validate(raw)
                    if (event.factory_id, event.run_id, event.source_revision) != (
                        received.factory_id,
                        received.run_id,
                        str(revision),
                    ):
                        raise ValueError("Event scope/version mismatch")
                if "snapshot_delta" in batch:
                    if reconstructed is None:
                        base = db.scalar(
                            select(SnapshotRecord).where(
                                SnapshotRecord.factory_id == received.factory_id,
                                SnapshotRecord.content_hash == anchor,
                            )
                        )
                        if base is None:
                            raise ConnectorError(
                                "Legacy history has no verified delta base; a controlled snapshot checkpoint is required",
                                code="SNAPSHOT_DELTA_BASE_REQUIRED",
                                status=409,
                            )
                        reconstructed = Snapshot.model_validate(base.document)
                        if reconstructed.content_hash != base.content_hash:
                            raise ValueError("Saved delta base differs from its recorded hash")
                    after = apply_delta(reconstructed, batch["snapshot_delta"])
                    if (
                        after.content_hash != batch["snapshot_hash"]
                        or after.source.source_revision != str(revision)
                        or (after.factory_id, after.run_id)
                        != (received.factory_id, received.run_id)
                        or after.snapshot_clock != datetime.fromisoformat(batch["business_clock"])
                    ):
                        raise ValueError("Delta result differs from its source batch")
                    if revision < target:
                        if after.snapshot_id == received.snapshot_id:
                            raise ValueError("Intermediate snapshot reused the target identity")
                        existing = db.get(SnapshotRecord, after.snapshot_id)
                        if existing is not None and (
                            existing.factory_id != after.factory_id
                            or existing.content_hash != after.content_hash
                            or Snapshot.model_validate(existing.document).content_hash
                            != after.content_hash
                        ):
                            raise ValueError("Intermediate snapshot identity changed content")
                        if existing is None:
                            db.add(
                                SnapshotRecord(
                                    snapshot_id=after.snapshot_id,
                                    factory_id=after.factory_id,
                                    content_hash=after.content_hash,
                                    document=after.model_dump(mode="json"),
                                    created_at=datetime.now(UTC),
                                )
                            )
                    reconstructed = after
                else:
                    # Legacy hashes remain reconcilable, but cannot supply invented historical facts.
                    reconstructed = None
                digest = canonical_hash(batch)
                old = db.get(SourceBatch, (received.run_id, revision))
                if old is not None and old.content_hash != digest:
                    raise ValueError("Duplicate event version changed content")
                if old is None:
                    db.add(
                        SourceBatch(
                            run_id=received.run_id,
                            revision=revision,
                            factory_id=received.factory_id,
                            content_hash=digest,
                            document=batch,
                            received_at=datetime.now(UTC),
                        )
                    )
                cursor, anchor, advanced = revision, batch["snapshot_hash"], True
            if not advanced:
                raise ValueError("Missing incremental source facts")
        except (ValueError, KeyError, TypeError) as exc:
            raise ConnectorError("Source events could not be reconciled with the snapshot") from exc
    if anchor != received.content_hash:
        raise ConnectorError("Incremental event watermark differs from complete snapshot")
