"""One factory reader with declared row mappings and a fixed endpoint registry."""

from __future__ import annotations

from typing import Any, NoReturn
from urllib.parse import quote

from pydantic import ValidationError

from packages.domain.execution import ActionReceipt
from packages.domain.models import (
    ActualExecution,
    ConnectorCapabilities,
    ConnectorMapping,
    Snapshot,
    SourceEnvelope,
)
from packages.integrations.factory_http import ConnectorError, FactoryHTTP
from packages.integrations.mapping import (
    COLLECTION_ENTITIES,
    MappingError,
    _complete,
    normalize_change_batch,
    normalize_entity,
    normalize_snapshot,
)

ENDPOINTS = {
    f"{prefix}_{name}": (operation, f"/factory/{version}/{path}")
    for prefix, version in (("v1", "v1"), ("alternate", "v2"))
    for name, operation, path in (
        ("snapshot", "read_snapshot", "snapshot"),
        ("changes", "read_changes", "changes"),
        ("detail", "query_detail", "objects"),
        ("action", "query_action", "actions"),
        ("plans", "accept_plan", "plans"),
    )
}
DETAIL_KEYS = {
    "orders": "order_id",
    "inventory": "material_id",
    "receipts": "receipt_id",
    "resources": "resource_id",
    "workers": "worker_id",
    "actuals": "operation_id",
}
PAGE_FIELDS = {"factory_id", "run_id", "watermark", "next_cursor", "has_more", "changes"}


def _fail(code: str) -> NoReturn:
    raise ConnectorError("Mapped source contract could not be verified", code=code)


def _identity(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 160
        or value in {".", ".."}
        or any(character.isspace() or not character.isprintable() for character in value)
        or any(character in value for character in "/\\%?#")
    ):
        _fail("INVALID_SOURCE_IDENTITY")
    return value


def _revision(value: object) -> int:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 160
        or not value.isascii()
        or not value.isdecimal()
        or str(int(value)) != value
    ):
        _fail("INVALID_SOURCE_CURSOR")
    return int(value)


class MappedFactoryHTTP(FactoryHTTP):
    def __init__(
        self,
        origin: str,
        token: str,
        *,
        factory_id: str,
        origin_id: str,
        mapping: ConnectorMapping,
        transport=None,
    ):
        self.factory_id = _identity(factory_id)
        _identity(origin_id)
        try:
            self.mapping = ConnectorMapping.model_validate(mapping)
            _complete(self.mapping)
        except ValidationError:
            _fail("INVALID_MAPPING")
        except MappingError as exc:
            _fail(exc.code)
        if self.mapping.factory_id != factory_id:
            _fail("MAPPING_FACTORY_MISMATCH")
        if self.mapping.origin_id != origin_id:
            _fail("MAPPING_ORIGIN_MISMATCH")
        self.endpoints: dict[str, str] = {}
        for binding in self.mapping.endpoint_bindings:
            registered = ENDPOINTS.get(binding.endpoint_id)
            if registered is None or registered[0] != binding.operation:
                _fail("UNREGISTERED_MAPPING_ENDPOINT")
            self.endpoints[binding.operation] = registered[1]
        if "read_snapshot" not in self.endpoints:
            _fail("SNAPSHOT_ENDPOINT_REQUIRED")
        super().__init__(origin, token, transport=transport)

    def _request(self, *args, **kwargs) -> dict | None:
        _fail("MAPPED_OPERATION_REQUIRED")

    def _get(self, path: str, params: dict | None = None) -> dict:
        # Legacy incremental sync lacks the canonical before-snapshot needed to map deltas.
        _fail("MAPPED_OPERATION_REQUIRED")

    def _factory(self, factory_id: object) -> None:
        if _identity(factory_id) != self.factory_id:
            _fail("MAPPING_FACTORY_MISMATCH")

    def _read(
        self,
        operation: str,
        params: dict,
        *,
        identities: tuple[str, ...] = (),
        missing_ok: bool = False,
    ) -> dict | None:
        path = self.endpoints.get(operation)
        if path is None or operation == "accept_plan":
            _fail("MAPPED_OPERATION_UNAVAILABLE")
        self._factory(params.get("factory_id"))
        for identity in identities:
            path += "/" + quote(_identity(identity), safe="")
        return super()._request("GET", path, params=params, missing_ok=missing_ok)

    def _source(self, source: SourceEnvelope) -> None:
        if source.source_system != self.mapping.source_id:
            _fail("MAPPING_SOURCE_MISMATCH")
        if (
            not source.complete
            or source.consistency == "UNVERIFIED"
            or source.freshness != "CURRENT"
        ):
            _fail("INCOMPLETE_SOURCE_FACTS")
        revision = _revision(source.source_revision)
        if source.cursor is not None and _revision(source.cursor) != revision:
            _fail("SOURCE_CURSOR_MISMATCH")

    def _snapshot(self, snapshot: Snapshot) -> None:
        self._factory(snapshot.factory_id)
        self._source(snapshot.source)

    def snapshot(self, factory_id: str) -> Snapshot:
        self._factory(factory_id)
        try:
            result = normalize_snapshot(
                self._read("read_snapshot", {"factory_id": factory_id}), self.mapping
            )
        except MappingError as exc:
            _fail(exc.code)
        self._snapshot(result)
        return result

    def changes(self, before: Snapshot, limit: int = 100) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            _fail("INVALID_CHANGE_LIMIT")
        try:
            before = Snapshot.model_validate(before)
        except ValidationError:
            _fail("INVALID_CHANGE_ANCHOR")
        self._snapshot(before)
        _identity(before.run_id)
        cursor = _revision(before.source.source_revision)
        page = self._read(
            "read_changes",
            {
                "factory_id": self.factory_id,
                "run_id": before.run_id,
                "after": cursor,
                "limit": limit,
            },
        )
        if page is None or set(page) != PAGE_FIELDS:
            _fail("INVALID_CHANGE_PAGE")
        if (page["factory_id"], page["run_id"]) != (self.factory_id, before.run_id):
            _fail("CHANGE_PAGE_SCOPE_MISMATCH")
        watermark, next_cursor = _revision(page["watermark"]), _revision(page["next_cursor"])
        changes = page["changes"]
        if (
            not isinstance(changes, list)
            or len(changes) > limit
            or type(page["has_more"]) is not bool
            or not cursor <= next_cursor <= watermark
        ):
            _fail("INVALID_CHANGE_PAGE")
        anchor, normalized = before, []
        for raw in changes:
            try:
                batch, after = normalize_change_batch(anchor, raw, self.mapping)
            except MappingError as exc:
                _fail(exc.code)
            self._snapshot(after)
            if after.snapshot_clock < anchor.snapshot_clock:
                _fail("SOURCE_CLOCK_REWOUND")
            normalized.append(batch)
            anchor = after
        if (
            next_cursor != _revision(anchor.source.source_revision)
            or page["has_more"] != (next_cursor < watermark)
            or (not changes and watermark != cursor)
        ):
            _fail("CHANGE_PAGE_CURSOR_MISMATCH")
        return {**page, "changes": normalized}

    def capabilities(self) -> ConnectorCapabilities:
        try:
            declared = ConnectorCapabilities.model_validate(
                super()._request("GET", "/factory/v1/capabilities")
            )
        except ValidationError:
            _fail("INVALID_SOURCE_CAPABILITIES")
        supported = declared.model_dump()
        for operation in ("read_snapshot", "read_changes", "query_detail", "query_action"):
            supported[operation] = supported[operation] and operation in self.endpoints
        # A read credential and an endpoint declaration never certify an execution writer.
        supported.update(accept_plan=False, conditional_acceptance=False, idempotency=False)
        if not supported["read_snapshot"]:
            supported["snapshot_consistency"] = "UNVERIFIED"
        return ConnectorCapabilities.model_validate(supported)

    def action(self, factory_id: str, run_id: str, operation_id: str) -> ActionReceipt | None:
        self._factory(factory_id)
        raw = self._read(
            "query_action",
            {"factory_id": factory_id, "run_id": _identity(run_id)},
            identities=(operation_id,),
            missing_ok=True,
        )
        if raw is None:
            return None
        try:
            receipt = ActionReceipt.model_validate(raw)
        except ValidationError:
            _fail("INVALID_ACTION_RECEIPT")
        if (receipt.factory_id, receipt.run_id, receipt.operation_id) != (
            factory_id,
            run_id,
            operation_id,
        ):
            _fail("ACTION_RECEIPT_SCOPE_MISMATCH")
        return receipt

    def query_detail(self, factory_id: str, run_id: str, entity: str, identity: str) -> dict:
        self._factory(factory_id)
        _identity(run_id)
        if not isinstance(entity, str) or entity not in DETAIL_KEYS:
            _fail("UNSUPPORTED_DETAIL_ENTITY")
        raw = self._read("query_detail", {"factory_id": factory_id}, identities=(entity, identity))
        if raw is None or set(raw) != {"record", "source", "run_id"}:
            _fail("INVALID_DETAIL_RESPONSE")
        if raw["run_id"] != run_id:
            _fail("DETAIL_RUN_MISMATCH")
        try:
            source = SourceEnvelope.model_validate(raw["source"])
            record = (
                ActualExecution.model_validate(raw["record"]).model_dump(mode="json")
                if entity == "actuals"
                else normalize_entity(COLLECTION_ENTITIES[entity], raw["record"], self.mapping)
            )
        except ValidationError:
            _fail("INVALID_DETAIL_RESPONSE")
        except MappingError as exc:
            _fail(exc.code)
        self._source(source)
        if record[DETAIL_KEYS[entity]] != identity:
            _fail("DETAIL_IDENTITY_MISMATCH")
        return {"record": record, "source": source.model_dump(mode="json"), "run_id": run_id}
