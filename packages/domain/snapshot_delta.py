"""Hash-anchored public fact deltas; they carry no simulator control or future state."""

from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from packages.domain.models import Digest, Identifier, Snapshot

COLLECTION_KEYS = {
    "orders": "order_id",
    "inventory": "material_id",
    "receipts": "receipt_id",
    "resources": "resource_id",
    "workers": "worker_id",
    "actuals": "operation_id",
    "reservations": "reservation_id",
}
_SET_FIELDS = (
    set(Snapshot.model_fields) - set(COLLECTION_KEYS) - {"factory_id", "run_id", "content_hash"}
)


class SnapshotDeltaError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _Rows(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    upsert: dict[Identifier, dict[str, Any]] = Field(default_factory=dict)
    remove: list[Identifier] = Field(default_factory=list)
    order: list[Identifier] | None = None


class _Delta(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["byof.snapshot-delta/1"] = "byof.snapshot-delta/1"
    factory_id: Identifier
    run_id: Identifier
    previous_snapshot_hash: Digest
    snapshot_hash: Digest
    previous_source_revision: Identifier
    source_revision: Identifier
    set: dict[str, Any]
    collections: dict[str, _Rows]


def _snapshot(value: Snapshot) -> Snapshot:
    try:
        return Snapshot.model_validate(value.model_dump(mode="json"))
    except ValidationError as exc:
        raise SnapshotDeltaError("INVALID_SNAPSHOT") from exc


def _scope(before: Snapshot, after: Snapshot) -> None:
    if (before.factory_id, before.run_id) != (after.factory_id, after.run_id):
        raise SnapshotDeltaError("DELTA_SCOPE_MISMATCH")
    if before.snapshot_id == after.snapshot_id or (
        before.source.source_revision == after.source.source_revision
    ):
        raise SnapshotDeltaError("DELTA_VERSION_CONFLICT")


def make_delta(before: Snapshot, after: Snapshot) -> dict[str, Any]:
    """Store only changed top-level metadata and complete changed rows, preserving row order."""
    before, after = _snapshot(before), _snapshot(after)
    _scope(before, after)
    old, new = before.model_dump(mode="json"), after.model_dump(mode="json")
    collections: dict[str, Any] = {}
    for collection, identity in COLLECTION_KEYS.items():
        prior = {row[identity]: row for row in old[collection]}
        current = {row[identity]: row for row in new[collection]}
        upsert = {key: row for key, row in current.items() if prior.get(key) != row}
        remove = [key for key in prior if key not in current]
        natural_order = [key for key in prior if key in current] + [
            key for key in current if key not in prior
        ]
        # JSONB objects do not retain insertion order. New IDs need an explicit array order.
        order = (
            list(current) if natural_order != list(current) or set(current) - set(prior) else None
        )
        if upsert or remove or order is not None:
            rows: dict[str, Any] = {"upsert": upsert, "remove": remove}
            if order is not None:
                rows["order"] = order
            collections[collection] = rows
    return {
        "schema_version": "byof.snapshot-delta/1",
        "factory_id": after.factory_id,
        "run_id": after.run_id,
        "previous_snapshot_hash": before.content_hash,
        "snapshot_hash": after.content_hash,
        "previous_source_revision": before.source.source_revision,
        "source_revision": after.source.source_revision,
        # Optional v3 ledgers/terms stay absent in legacy snapshots. Whole-ledger
        # replacement preserves tombstones and its exact source ordering.
        "set": {key: new.get(key) for key in sorted(_SET_FIELDS) if old.get(key) != new.get(key)},
        "collections": collections,
    }


def apply_delta(before: Snapshot, delta: object) -> Snapshot:
    """Rebuild one immutable snapshot or reject the entire delta; never mutate the base."""
    before = _snapshot(before)
    try:
        change = _Delta.model_validate(delta)
    except ValidationError as exc:
        raise SnapshotDeltaError("INVALID_DELTA") from exc
    if (change.factory_id, change.run_id) != (before.factory_id, before.run_id):
        raise SnapshotDeltaError("DELTA_SCOPE_MISMATCH")
    if (
        change.previous_snapshot_hash != before.content_hash
        or change.previous_source_revision != before.source.source_revision
    ):
        raise SnapshotDeltaError("DELTA_ANCHOR_MISMATCH")
    if set(change.set) - _SET_FIELDS or set(change.collections) - set(COLLECTION_KEYS):
        raise SnapshotDeltaError("UNKNOWN_DELTA_FIELD")
    document = before.model_dump(mode="json", exclude={"content_hash"})
    document.update(deepcopy(change.set))
    for collection, changes in change.collections.items():
        identity = COLLECTION_KEYS[collection]
        rows = {row[identity]: row for row in document[collection]}
        if (
            len(changes.remove) != len(set(changes.remove))
            or set(changes.remove) & set(changes.upsert)
            or set(changes.remove) - set(rows)
        ):
            raise SnapshotDeltaError("DELTA_ROW_IDENTITY_CONFLICT")
        for key in changes.remove:
            del rows[key]
        for key, row in changes.upsert.items():
            if row.get(identity) != key:
                raise SnapshotDeltaError("DELTA_ROW_IDENTITY_CONFLICT")
            rows[key] = deepcopy(row)
        order = changes.order if changes.order is not None else list(rows)
        if len(order) != len(set(order)) or set(order) != set(rows):
            raise SnapshotDeltaError("DELTA_ROW_ORDER_CONFLICT")
        document[collection] = [rows[key] for key in order]
    document["content_hash"] = change.snapshot_hash
    try:
        after = Snapshot.model_validate(document)
    except ValidationError as exc:
        raise SnapshotDeltaError("DELTA_SNAPSHOT_INVALID") from exc
    _scope(before, after)
    if after.source.source_revision != change.source_revision:
        raise SnapshotDeltaError("DELTA_VERSION_CONFLICT")
    return after
