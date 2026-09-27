"""Declarative row mappings preserve the canonical snapshot and source change chain."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from packages.domain.models import (
    ConnectorMapping,
    Contract,
    Event,
    Inventory,
    Order,
    Receipt,
    Resource,
    Snapshot,
    Worker,
)
from packages.domain.snapshot_delta import SnapshotDeltaError, apply_delta

ENTITY_TYPES: dict[str, type[Contract]] = {
    "order": Order,
    "inventory": Inventory,
    "receipt": Receipt,
    "resource": Resource,
    "worker": Worker,
}
COLLECTION_ENTITIES = {
    "orders": "order",
    "inventory": "inventory",
    "receipts": "receipt",
    "resources": "resource",
    "workers": "worker",
}
QUANTITIES = {"inventory": ("on_hand", "reserved"), "receipt": ("quantity",)}
CHANGE_FIELDS = {
    "factory_id",
    "run_id",
    "revision",
    "previous_snapshot_hash",
    "snapshot_hash",
    "business_clock",
    "cause",
    "events",
    "snapshot_delta",
}


class MappingError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _object(value: object, code: str = "INVALID_SOURCE_OBJECT") -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise MappingError(code)
    return value


def _mapping(value: ConnectorMapping) -> ConnectorMapping:
    try:
        return ConnectorMapping.model_validate(value)
    except ValidationError:
        raise MappingError("INVALID_MAPPING") from None


def _paths(
    entity: str, mapping: ConnectorMapping
) -> tuple[type[Contract], dict[tuple[str, ...], str]]:
    model = ENTITY_TYPES.get(entity)
    if model is None:
        raise MappingError("UNSUPPORTED_MAPPING_ENTITY")
    fields = [field for field in mapping.fields if field.entity == entity]
    # Only this new, omitted-when-None field may be absent from legacy mappings.
    # _row still rejects it if the source supplies an undeclared value (including null).
    targets = {field.target_field for field in fields}
    expected = set(model.model_fields)
    legacy_optional = {"requested_due_at"} if entity == "order" else set()
    if targets - expected or (expected - targets) - legacy_optional:
        raise MappingError("INCOMPLETE_FIELD_MAPPING")
    paths: dict[tuple[str, ...], str] = {}
    for field in fields:
        path = tuple(field.source_field.split("."))
        if any(not part for part in path) or len(path) > 16:
            raise MappingError("INVALID_MAPPING_PATH")
        if any(path[: len(old)] == old or old[: len(path)] == path for old in paths):
            raise MappingError("AMBIGUOUS_MAPPING_PATH")
        paths[path] = field.target_field
    return model, paths


def _complete(mapping: ConnectorMapping) -> None:
    for entity in ENTITY_TYPES:
        _paths(entity, mapping)


def _row(entity: str, raw: object, mapping: ConnectorMapping) -> dict[str, Any]:
    model, paths = _paths(entity, mapping)

    result: dict[str, Any] = {}

    def read(node: object, prefix: tuple[str, ...]) -> None:
        document = _object(node)
        expected = {path[len(prefix)] for path in paths if path[: len(prefix)] == prefix}
        if set(document) - expected:
            raise MappingError("UNMAPPED_SOURCE_FIELD")
        if expected - set(document):
            raise MappingError("MISSING_SOURCE_FIELD")
        for name, value in document.items():
            path = (*prefix, name)
            if path in paths:
                result[paths[path]] = deepcopy(value)
            else:
                read(value, path)

    read(raw, ())
    if "status" in model.model_fields:
        source_status = result["status"]
        if not isinstance(source_status, str):
            raise MappingError("UNMAPPED_SOURCE_STATUS")
        status = next(
            (
                item.target_status
                for item in mapping.statuses
                if item.entity == entity and item.source_status == source_status
            ),
            None,
        )
        if status is None:
            raise MappingError("UNMAPPED_SOURCE_STATUS")
        result["status"] = status
    if entity in QUANTITIES:
        source_unit = result["unit"]
        unit = next((item for item in mapping.units if item.source_unit == source_unit), None)
        if not isinstance(source_unit, str) or unit is None:
            raise MappingError("UNMAPPED_SOURCE_UNIT")
        for quantity_field in QUANTITIES[entity]:
            value = result[quantity_field]
            if type(value) is not int:
                raise MappingError("INVALID_SOURCE_QUANTITY")
            quotient, remainder = divmod(
                value * unit.multiplier_numerator, unit.multiplier_denominator
            )
            if remainder:
                raise MappingError("NONINTEGRAL_UNIT_CONVERSION")
            result[quantity_field] = quotient
        result["unit"] = unit.target_unit
    try:
        return model.model_validate(result).model_dump(mode="json")
    except ValidationError:
        raise MappingError("INVALID_NORMALIZED_ENTITY") from None


def normalize_entity(entity: str, raw: object, mapping: ConnectorMapping) -> dict[str, Any]:
    """Map one complete row; undeclared source fields cannot bypass the mapping."""
    return _row(entity, raw, _mapping(mapping))


def _scope(snapshot: Snapshot, mapping: ConnectorMapping) -> None:
    if snapshot.factory_id != mapping.factory_id:
        raise MappingError("MAPPING_FACTORY_MISMATCH")
    if snapshot.source.source_system != mapping.source_id:
        raise MappingError("MAPPING_SOURCE_MISMATCH")


def normalize_snapshot(raw: object, mapping: ConnectorMapping) -> Snapshot:
    """Keep the standard envelope/profile/actuals; map the five declared fact collections."""
    mapping = _mapping(mapping)
    _complete(mapping)
    document = deepcopy(_object(raw))
    if not isinstance(document.get("content_hash"), str):
        raise MappingError("CANONICAL_SNAPSHOT_HASH_REQUIRED")
    for collection, entity in COLLECTION_ENTITIES.items():
        rows = document.get(collection)
        if not isinstance(rows, list):
            raise MappingError("INVALID_SOURCE_COLLECTION")
        document[collection] = [_row(entity, row, mapping) for row in rows]
    try:
        snapshot = Snapshot.model_validate(document)
    except ValidationError:
        raise MappingError("INVALID_NORMALIZED_SNAPSHOT") from None
    _scope(snapshot, mapping)
    return snapshot


def normalize_change_batch(
    before: Snapshot, raw: object, mapping: ConnectorMapping
) -> tuple[dict[str, Any], Snapshot]:
    """Map full delta upserts and verify the reconstructed canonical snapshot and batch."""
    mapping = _mapping(mapping)
    _complete(mapping)
    _scope(before, mapping)
    document = deepcopy(_object(raw))
    if set(document) != CHANGE_FIELDS:
        raise MappingError("INVALID_CHANGE_BATCH")
    delta = _object(document["snapshot_delta"], "INVALID_CHANGE_DELTA")
    collections = _object(delta.get("collections"), "INVALID_CHANGE_DELTA")
    for collection, entity in COLLECTION_ENTITIES.items():
        if collection not in collections:
            continue
        rows = _object(collections[collection], "INVALID_CHANGE_DELTA")
        upsert = _object(rows.get("upsert", {}), "INVALID_CHANGE_DELTA")
        rows["upsert"] = {key: _row(entity, value, mapping) for key, value in upsert.items()}
    try:
        after = apply_delta(before, delta)
    except SnapshotDeltaError as exc:
        raise MappingError(exc.code) from None
    _scope(after, mapping)
    if (
        document["factory_id"] != after.factory_id
        or document["run_id"] != after.run_id
        or document["revision"] != after.source.source_revision
        or document["previous_snapshot_hash"] != before.content_hash
        or document["snapshot_hash"] != after.content_hash
    ):
        raise MappingError("CHANGE_BATCH_BINDING_MISMATCH")
    revision, previous_revision = after.source.source_revision, before.source.source_revision
    if (
        not revision.isascii()
        or not revision.isdecimal()
        or not previous_revision.isascii()
        or not previous_revision.isdecimal()
        or int(revision) != int(previous_revision) + 1
    ):
        raise MappingError("CHANGE_REVISION_GAP")
    try:
        business_clock = datetime.fromisoformat(document["business_clock"])
    except (TypeError, ValueError):
        raise MappingError("INVALID_CHANGE_CLOCK") from None
    if business_clock != after.snapshot_clock:
        raise MappingError("CHANGE_CLOCK_MISMATCH")
    if not isinstance(document["cause"], str) or not document["cause"].strip():
        raise MappingError("INVALID_CHANGE_CAUSE")
    if not isinstance(document["events"], list):
        raise MappingError("INVALID_CHANGE_EVENTS")
    # Events keep the existing canonical contract; the mapping transforms complete fact rows.
    for value in document["events"]:
        try:
            event = Event.model_validate(value)
        except ValidationError:
            raise MappingError("INVALID_CHANGE_EVENT") from None
        if (event.factory_id, event.run_id, event.source_revision) != (
            after.factory_id,
            after.run_id,
            after.source.source_revision,
        ):
            raise MappingError("CHANGE_EVENT_SCOPE_MISMATCH")
    return document, after
