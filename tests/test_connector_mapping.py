"""Independent wire examples prove exact normalization without HTTP, a database, or a model."""

from copy import deepcopy

import pytest

from packages.domain.models import ConnectorMapping, batch_operations
from packages.domain.skf import load_skf_snapshot
from packages.domain.snapshot_delta import make_delta
from packages.integrations.mapping import (
    MappingError,
    normalize_change_batch,
    normalize_entity,
    normalize_snapshot,
)
from services.factory_sim.engine import advance
from tests.test_case_impact import active_case
from tests.test_checker import at, change_snapshot, example_snapshot

FIELDS = {
    "order": {
        "order_id": "identity.number",
        "product_id": "identity.item",
        "quantity": "demand",
        "due_at": "deadline",
        "priority_weight": "priority",
        "hard_deadline": "hard_limit",
        "version": "revision",
        "split_revision": "split",
        "status": "state",
    },
    "inventory": {
        "material_id": "stock.code",
        "unit": "stock.uom",
        "on_hand": "stock.physical",
        "reserved": "stock.held",
        "version": "stock.revision",
    },
    "receipt": {
        "receipt_id": "arrival.number",
        "material_id": "arrival.code",
        "unit": "arrival.uom",
        "quantity": "arrival.amount",
        "eta": "arrival.expected_at",
        "status": "arrival.state",
        "received_at": "arrival.actual_at",
        "version": "arrival.revision",
    },
    "resource": {
        "resource_id": "asset.code",
        "resource_type": "asset.kind",
        "operation_codes": "asset.operations",
        "capacity": "asset.slots",
        "status": "asset.state",
        "calendar": "asset.shifts",
        "unavailable": "asset.exclusions",
        "version": "asset.revision",
        "last_operation_id": "asset.previous_operation",
        "last_product_id": "asset.previous_product",
    },
    "worker": {
        "worker_id": "person.code",
        "skills": "person.qualifications",
        "status": "person.state",
        "overtime_available": "person.overtime",
        "calendar": "person.shifts",
        "unavailable": "person.exclusions",
        "version": "person.revision",
    },
}
STATES = {
    "order": {"CONFIRMED": "10", "IN_PROGRESS": "20", "COMPLETED": "30", "CANCELLED": "90"},
    "receipt": {"EXPECTED": "10", "CONFIRMED": "20", "RECEIVED": "30", "CANCELLED": "90"},
    "resource": {"AVAILABLE": "10", "MAINTENANCE": "20", "DOWN": "30", "UNKNOWN": "99"},
    "worker": {"AVAILABLE": "10", "ABSENT": "20", "UNKNOWN": "99"},
}


def mapping_for(snapshot, *, alternate=False):
    return ConnectorMapping.model_validate(
        {
            "mapping_id": "second-format" if alternate else "canonical-format",
            "version": 1,
            "factory_id": snapshot.factory_id,
            "source_id": snapshot.source.source_system,
            "origin_id": "registered-enterprise",
            "endpoint_bindings": [
                {"operation": "read_snapshot", "endpoint_id": "snapshot"},
                {"operation": "read_changes", "endpoint_id": "changes"},
            ],
            "fields": [
                {
                    "entity": entity,
                    "target_field": target,
                    "source_field": path if alternate else target,
                }
                for entity, fields in FIELDS.items()
                for target, path in fields.items()
            ],
            "units": [
                {
                    "source_unit": "m" + unit if alternate else unit,
                    "target_unit": unit,
                    "multiplier_numerator": 1,
                    "multiplier_denominator": 1000 if alternate else 1,
                }
                for unit in ("EA", "SET", "GFU")
            ],
            "statuses": [
                {
                    "entity": entity,
                    "source_status": code if alternate else status,
                    "target_status": status,
                }
                for entity, states in STATES.items()
                for status, code in states.items()
            ],
            "inventory_reserved_semantics": "included_in_on_hand",
        }
    )


def changed_mapping(mapping, mutate):
    raw = mapping.model_dump(mode="json")
    mutate(raw)
    return ConnectorMapping.model_validate(raw)


def requested_due_mapping(snapshot, *, alternate=False):
    return changed_mapping(
        mapping_for(snapshot, alternate=alternate),
        lambda raw: raw["fields"].append(
            {
                "entity": "order",
                "target_field": "requested_due_at",
                "source_field": "requested_deadline" if alternate else "requested_due_at",
            }
        ),
    )


def wire_row(entity, row):
    # Source encoders deliberately do not read the mapping under test.
    if entity == "order":
        result = {
            "identity": {"number": row["order_id"], "item": row["product_id"]},
            "demand": row["quantity"],
            "deadline": row["due_at"],
            "priority": row["priority_weight"],
            "hard_limit": row["hard_deadline"],
            "revision": row["version"],
            "split": row["split_revision"],
            "state": {"CONFIRMED": "10", "IN_PROGRESS": "20", "COMPLETED": "30", "CANCELLED": "90"}[
                row["status"]
            ],
        }
        if "requested_due_at" in row:
            result["requested_deadline"] = row["requested_due_at"]
        return result
    if entity == "inventory":
        return {
            "stock": {
                "code": row["material_id"],
                "uom": {"EA": "mEA", "SET": "mSET", "GFU": "mGFU"}[row["unit"]],
                "physical": row["on_hand"] * 1000,
                "held": row["reserved"] * 1000,
                "revision": row["version"],
            }
        }
    if entity == "receipt":
        return {
            "arrival": {
                "number": row["receipt_id"],
                "code": row["material_id"],
                "uom": {"EA": "mEA", "SET": "mSET", "GFU": "mGFU"}[row["unit"]],
                "amount": row["quantity"] * 1000,
                "expected_at": row["eta"],
                "state": {"EXPECTED": "10", "CONFIRMED": "20", "RECEIVED": "30", "CANCELLED": "90"}[
                    row["status"]
                ],
                "actual_at": row["received_at"],
                "revision": row["version"],
            }
        }
    if entity == "resource":
        return {
            "asset": {
                "code": row["resource_id"],
                "kind": row["resource_type"],
                "operations": row["operation_codes"],
                "slots": row["capacity"],
                "state": {"AVAILABLE": "10", "MAINTENANCE": "20", "DOWN": "30", "UNKNOWN": "99"}[
                    row["status"]
                ],
                "shifts": row["calendar"],
                "exclusions": row["unavailable"],
                "revision": row["version"],
                "previous_operation": row["last_operation_id"],
                "previous_product": row["last_product_id"],
            }
        }
    if entity == "worker":
        return {
            "person": {
                "code": row["worker_id"],
                "qualifications": row["skills"],
                "state": {"AVAILABLE": "10", "ABSENT": "20", "UNKNOWN": "99"}[row["status"]],
                "overtime": row["overtime_available"],
                "shifts": row["calendar"],
                "exclusions": row["unavailable"],
                "revision": row["version"],
            }
        }
    raise AssertionError(entity)


def wire_snapshot(snapshot):
    raw = snapshot.model_dump(mode="json")
    for collection, entity in (
        ("orders", "order"),
        ("inventory", "inventory"),
        ("receipts", "receipt"),
        ("resources", "resource"),
        ("workers", "worker"),
    ):
        raw[collection] = [wire_row(entity, row) for row in raw[collection]]
    return raw


def change_batch(before, after, *, alternate=False):
    delta = make_delta(before, after)
    if alternate:
        for collection, entity in (
            ("orders", "order"),
            ("inventory", "inventory"),
            ("receipts", "receipt"),
            ("resources", "resource"),
            ("workers", "worker"),
        ):
            if collection in delta["collections"]:
                rows = delta["collections"][collection]
                rows["upsert"] = {key: wire_row(entity, row) for key, row in rows["upsert"].items()}
    return {
        "factory_id": after.factory_id,
        "run_id": after.run_id,
        "revision": after.source.source_revision,
        "previous_snapshot_hash": before.content_hash,
        "snapshot_hash": after.content_hash,
        "business_clock": after.snapshot_clock.isoformat(),
        "cause": "clock.tick",
        "events": [
            {
                "event_id": "event-" + after.source.source_revision,
                "factory_id": after.factory_id,
                "run_id": after.run_id,
                "source_event_id": "source-event-" + after.source.source_revision,
                "source_revision": after.source.source_revision,
                "entity_type": "factory",
                "entity_id": after.factory_id,
                "entity_version": after.planning_revision,
                "event_type": "clock.tick",
                "occurred_at": after.snapshot_clock.isoformat(),
                "observed_at": after.source.observed_at.isoformat(),
                "effective_at": after.snapshot_clock.isoformat(),
                "changes": [],
            }
        ],
        "snapshot_delta": delta,
    }


@pytest.mark.parametrize("development", [True, False])
def test_identity_and_independent_second_wire_format_preserve_all_skf_facts_and_hash(development):
    original = load_skf_snapshot(development=development)
    canonical = original.model_dump(mode="json")
    alternate = wire_snapshot(original)
    retained = deepcopy(alternate)
    assert alternate["orders"][0]["identity"]["number"] == original.orders[0].order_id
    assert alternate["inventory"][0]["stock"]["physical"] == original.inventory[0].on_hand * 1000
    assert alternate["resources"][0]["asset"]["state"] == "10"
    for raw, different in ((canonical, False), (alternate, True)):
        normalized = normalize_snapshot(raw, mapping_for(original, alternate=different))
        assert normalized.model_dump(mode="json") == canonical
        assert normalized.content_hash == original.content_hash
        assert normalized.source == original.source
        assert all("requested_due_at" not in row for row in normalized.model_dump()["orders"])
        assert batch_operations(normalized) == batch_operations(original)
    assert alternate == retained
    if not development:
        batches, operations = batch_operations(original)
        assert (
            len(original.orders),
            sum(row.quantity for row in original.orders),
            len(batches),
            len(operations),
        ) == (6, 5400, 108, 864)


@pytest.mark.parametrize("alternate", [False, True])
@pytest.mark.parametrize("value", [None, at(12).isoformat()])
def test_legacy_mapping_cannot_silently_discard_a_supplied_requested_due_date(alternate, value):
    original = example_snapshot()
    raw = wire_snapshot(original) if alternate else original.model_dump(mode="json")
    raw["orders"][0]["requested_deadline" if alternate else "requested_due_at"] = value
    with pytest.raises(MappingError, match="^UNMAPPED_SOURCE_FIELD$"):
        normalize_snapshot(raw, mapping_for(original, alternate=alternate))


@pytest.mark.parametrize("alternate", [False, True])
def test_explicit_requested_due_mapping_preserves_separate_dates_and_snapshot_hash(alternate):
    def change(raw):
        raw["schema_version"] = "byof.snapshot/3"
        raw["orders"][0]["requested_due_at"] = at(12)

    original = change_snapshot(example_snapshot(), change)
    raw = wire_snapshot(original) if alternate else original.model_dump(mode="json")
    retained = deepcopy(raw)
    normalized = normalize_snapshot(raw, requested_due_mapping(original, alternate=alternate))
    assert normalized == original
    assert normalized.content_hash == original.content_hash
    assert normalized.orders[0].requested_due_at == at(12)
    assert normalized.orders[0].due_at == at(20)
    assert raw == retained


@pytest.mark.parametrize("alternate", [False, True])
@pytest.mark.parametrize("value", ["2030-01-01T08:12:00", "not-a-date", True])
def test_explicit_requested_due_mapping_validates_timestamp(alternate, value):
    original = example_snapshot()
    raw = original.orders[0].model_dump(mode="json")
    raw["requested_due_at"] = value
    if alternate:
        raw = wire_row("order", raw)
    with pytest.raises(MappingError, match="^INVALID_NORMALIZED_ENTITY$"):
        normalize_entity("order", raw, requested_due_mapping(original, alternate=alternate))


@pytest.mark.parametrize("alternate", [False, True])
def test_explicit_requested_due_mapping_requires_source_field_even_when_nullable(alternate):
    original = example_snapshot()
    raw = original.orders[0].model_dump(mode="json")
    if alternate:
        raw = wire_row("order", raw)
    with pytest.raises(MappingError, match="^MISSING_SOURCE_FIELD$"):
        normalize_entity("order", raw, requested_due_mapping(original, alternate=alternate))


@pytest.mark.parametrize("alternate", [False, True])
@pytest.mark.parametrize("initial", [None, at(12)])
def test_mapped_delta_preserves_added_or_changed_requested_due_date(alternate, initial):
    def initial_facts(raw):
        raw["schema_version"] = "byof.snapshot/3"
        raw["orders"][0]["requested_due_at"] = initial

    before = change_snapshot(example_snapshot(), initial_facts)

    def changes(raw):
        raw["snapshot_id"] = "facts-2"
        raw["snapshot_clock"] = at(1)
        raw["source"].update(source_revision="2", observed_at=at(1), effective_at=at(1))
        raw["planning_revision"] += 1
        raw["scope_version"] += 1
        raw["orders"][0].update(requested_due_at=at(15), version=2)

    after = change_snapshot(before, changes)
    raw = change_batch(before, after, alternate=alternate)
    retained = deepcopy(raw)
    with pytest.raises(MappingError, match="^UNMAPPED_SOURCE_FIELD$"):
        normalize_change_batch(before, raw, mapping_for(before, alternate=alternate))
    normalized, reconstructed = normalize_change_batch(
        before, raw, requested_due_mapping(before, alternate=alternate)
    )
    assert reconstructed == after
    assert reconstructed.orders[0].requested_due_at == at(15)
    assert reconstructed.orders[0].due_at == before.orders[0].due_at
    assert reconstructed.content_hash == after.content_hash
    assert normalized["snapshot_delta"] == make_delta(before, after)
    assert raw == retained


def test_handwritten_fractional_unit_conversion_preserves_included_reservations():
    expected = {"material_id": "part", "unit": "EA", "on_hand": 9, "reserved": 3, "version": 4}
    raw = {"stock": {"code": "part", "uom": "mEA", "physical": 9000, "held": 3000, "revision": 4}}
    result = normalize_entity("inventory", raw, mapping_for(example_snapshot(), alternate=True))
    assert result == expected
    assert result["on_hand"] - result["reserved"] == 6
    assert raw["stock"]["physical"] == 9000


def test_handwritten_nonunit_numerator_converts_both_inventory_quantities():
    mapping = changed_mapping(
        mapping_for(example_snapshot(), alternate=True),
        lambda raw: raw["units"][0].update(multiplier_numerator=3, multiplier_denominator=2),
    )
    raw = {"stock": {"code": "part", "uom": "mEA", "physical": 6, "held": 2, "revision": 4}}
    assert normalize_entity("inventory", raw, mapping) == {
        "material_id": "part",
        "unit": "EA",
        "on_hand": 9,
        "reserved": 3,
        "version": 4,
    }


@pytest.mark.parametrize("quantity", [True, False, "1000", 1000.0, None])
def test_boolean_string_float_and_unknown_quantities_are_not_coerced(quantity):
    raw = {"stock": {"code": "part", "uom": "mEA", "physical": quantity, "held": 0, "revision": 1}}
    with pytest.raises(MappingError, match="^INVALID_SOURCE_QUANTITY$"):
        normalize_entity("inventory", raw, mapping_for(example_snapshot(), alternate=True))


@pytest.mark.parametrize("field,value", [("physical", 1001), ("held", 1)])
def test_nonintegral_unit_conversion_never_rounds_or_discards_remainder(field, value):
    raw = {"stock": {"code": "part", "uom": "mEA", "physical": 2000, "held": 0, "revision": 1}}
    raw["stock"][field] = value
    with pytest.raises(MappingError, match="^NONINTEGRAL_UNIT_CONVERSION$"):
        normalize_entity("inventory", raw, mapping_for(example_snapshot(), alternate=True))


@pytest.mark.parametrize("unit", ["EA", "kg", "unconfirmed", True, None])
def test_unknown_or_undeclared_even_canonical_unit_is_rejected(unit):
    raw = {"stock": {"code": "part", "uom": unit, "physical": 2000, "held": 0, "revision": 1}}
    with pytest.raises(MappingError, match="^UNMAPPED_SOURCE_UNIT$"):
        normalize_entity("inventory", raw, mapping_for(example_snapshot(), alternate=True))


@pytest.mark.parametrize("status", ["AVAILABLE", "free-text", 10, True, None])
def test_unknown_or_undeclared_even_canonical_status_is_rejected(status):
    original = example_snapshot()
    raw = wire_snapshot(original)["resources"][0]
    raw["asset"]["state"] = status
    with pytest.raises(MappingError, match="^UNMAPPED_SOURCE_STATUS$"):
        normalize_entity("resource", raw, mapping_for(original, alternate=True))


def test_explicit_unknown_status_and_unknown_previous_setup_stay_unknown():
    original = example_snapshot()
    raw = wire_snapshot(original)["resources"][0]
    raw["asset"]["state"] = "99"
    result = normalize_entity("resource", raw, mapping_for(original, alternate=True))
    assert result["status"] == "UNKNOWN"
    assert result["last_operation_id"] is None and result["last_product_id"] is None


@pytest.mark.parametrize("entity", ["order", "receipt"])
def test_boolean_demand_or_receipt_quantity_is_rejected(entity):
    original = load_skf_snapshot(development=True)
    canonical = original.model_dump(mode="json")
    raw = wire_row(entity, canonical["orders" if entity == "order" else "receipts"][0])
    if entity == "order":
        raw["demand"] = True
    else:
        raw["arrival"]["amount"] = True
    with pytest.raises(MappingError):
        normalize_entity(entity, raw, mapping_for(original, alternate=True))


@pytest.mark.parametrize("kind", ["canonical", "nested_extra", "missing", "wrong_container"])
def test_source_paths_are_exact_and_canonical_fields_cannot_bypass_mapping(kind):
    original = example_snapshot()
    raw = wire_snapshot(original)["orders"][0]
    if kind == "canonical":
        raw["quantity"] = 5000
    elif kind == "nested_extra":
        raw["identity"]["order_id"] = "other"
    elif kind == "missing":
        del raw["identity"]["number"]
    else:
        raw["identity"] = [{"number": "other"}]
    expected = {
        "canonical": "UNMAPPED_SOURCE_FIELD",
        "nested_extra": "UNMAPPED_SOURCE_FIELD",
        "missing": "MISSING_SOURCE_FIELD",
        "wrong_container": "INVALID_SOURCE_OBJECT",
    }[kind]
    with pytest.raises(MappingError, match="^" + expected + "$"):
        normalize_entity("order", raw, mapping_for(original, alternate=True))


@pytest.mark.parametrize("kind", ["missing_target", "overlap", "empty_segment", "too_deep"])
def test_incomplete_and_ambiguous_path_configuration_is_rejected(kind):
    original = example_snapshot()

    def mutate(raw):
        if kind == "missing_target":
            raw["fields"] = [
                row for row in raw["fields"] if row["target_field"] != "split_revision"
            ]
        else:
            raw["fields"][0]["source_field"] = {
                "overlap": "identity",
                "empty_segment": "identity..number",
                "too_deep": ".".join(["a"] * 17),
            }[kind]

    configured = changed_mapping(mapping_for(original, alternate=True), mutate)
    with pytest.raises(
        MappingError, match="INCOMPLETE_FIELD_MAPPING|AMBIGUOUS_MAPPING_PATH|INVALID_MAPPING_PATH"
    ):
        normalize_entity("order", wire_snapshot(original)["orders"][0], configured)


@pytest.mark.parametrize("delta", [False, True])
def test_empty_collection_does_not_hide_an_incomplete_mapping(delta):
    before = example_snapshot()
    assert not before.receipts
    mapping = changed_mapping(
        mapping_for(before),
        lambda value: value.update(
            fields=[row for row in value["fields"] if row["entity"] != "receipt"]
        ),
    )
    with pytest.raises(MappingError, match="^INCOMPLETE_FIELD_MAPPING$"):
        if delta:
            after = advance(before, None)
            normalize_change_batch(before, change_batch(before, after), mapping)
        else:
            normalize_snapshot(before.model_dump(mode="json"), mapping)


def test_constructed_mapping_cannot_bypass_contract_validation():
    original = example_snapshot()
    forged = mapping_for(original).model_copy(update={"inventory_reserved_semantics": "extra"})
    with pytest.raises(MappingError, match="^INVALID_MAPPING$"):
        normalize_snapshot(original.model_dump(mode="json"), forged)


@pytest.mark.parametrize(
    "kind",
    ["factory", "source", "hash", "missing_hash", "extra", "nonlot", "reserved", "missing_skill"],
)
def test_complete_snapshot_preserves_scope_hash_and_domain_refusals(kind):
    original = example_snapshot()
    mapping = mapping_for(original, alternate=True)
    raw = wire_snapshot(original)
    if kind == "factory":
        mapping = changed_mapping(mapping, lambda value: value.update(factory_id="other"))
    elif kind == "source":
        mapping = changed_mapping(mapping, lambda value: value.update(source_id="other"))
    elif kind == "hash":
        raw["content_hash"] = "0" * 64
    elif kind == "missing_hash":
        del raw["content_hash"]
    elif kind == "extra":
        raw["private_future_events"] = []
    elif kind == "nonlot":
        raw["orders"][0]["demand"] = 3
    elif kind == "reserved":
        raw["inventory"][0]["stock"]["held"] = 3000
    else:
        raw["workers"][0]["person"]["qualifications"] = []
    with pytest.raises(MappingError):
        normalize_snapshot(raw, mapping)


def test_unit_conversion_preserves_received_and_pending_supplies_without_double_counting():
    original = load_skf_snapshot(development=True)
    raw = wire_snapshot(original)
    normalized = normalize_snapshot(raw, mapping_for(original, alternate=True))
    assert normalized.receipts and normalized.receipts == original.receipts
    row = original.receipts[0].model_dump(mode="json")
    row.update(status="RECEIVED", received_at=row["eta"])
    result = normalize_entity(
        "receipt", wire_row("receipt", row), mapping_for(original, alternate=True)
    )
    assert result == row
    assert result["quantity"] == row["quantity"] and result["received_at"] == row["eta"]


@pytest.mark.parametrize("alternate", [False, True])
def test_actual_progress_delta_uses_same_mapping_and_retains_inventory_and_execution_history(
    alternate,
):
    before, candidate = active_case()
    mapping = mapping_for(before, alternate=alternate)
    for _ in range(6):
        after = advance(before, candidate)
        raw = change_batch(before, after, alternate=alternate)
        retained = deepcopy(raw)
        normalized, reconstructed = normalize_change_batch(before, raw, mapping)
        assert reconstructed == after
        assert normalized["snapshot_delta"] == make_delta(before, after)
        assert normalized["revision"] == after.source.source_revision
        assert normalized["snapshot_hash"] == after.content_hash
        assert normalized["events"] == raw["events"]
        assert reconstructed.actuals == after.actuals
        assert normalize_snapshot(wire_snapshot(after), mapping_for(after, alternate=True)) == after
        assert raw == retained
        before = reconstructed
    assert before.inventory[0].on_hand == before.inventory[0].reserved == 0
    assert sum(c.quantity for row in before.actuals for c in row.consumed) == 2


def test_delta_add_update_remove_and_reorder_use_complete_mapped_rows():
    before = example_snapshot(second_product=True)

    def changes(raw):
        raw["snapshot_id"] = "facts-2"
        raw["snapshot_clock"] = at(1)
        raw["source"].update(source_revision="2", observed_at=at(1), effective_at=at(1))
        raw["planning_revision"] += 1
        raw["scope_version"] += 1
        row = deepcopy(raw["orders"][0])
        row.update(order_id="new-order", priority_weight=8)
        raw["orders"] = [row, raw["orders"][0]]
        raw["resources"][0].update(status="DOWN", version=2)

    after = change_snapshot(before, changes)
    normalized, rebuilt = normalize_change_batch(
        before, change_batch(before, after, alternate=True), mapping_for(before, alternate=True)
    )
    assert rebuilt == after
    assert normalized["snapshot_delta"]["collections"]["orders"]["remove"] == ["order-b"]
    assert [row.order_id for row in rebuilt.orders] == ["new-order", "order-a"]
    assert rebuilt.resources[0].status == "DOWN"


@pytest.mark.parametrize(
    "kind",
    [
        "hash",
        "previous_hash",
        "scope",
        "clock",
        "revision",
        "upsert_id",
        "partial_upsert",
        "canonical_bypass",
        "set_bypass",
        "event_scope",
        "bad_event",
        "extra_batch",
        "extra_delta",
    ],
)
def test_mapped_delta_refuses_chain_tampering_and_unmapped_updates_without_mutating_inputs(kind):
    before, plan = active_case()
    after = advance(before, plan)
    raw = change_batch(before, after, alternate=True)
    delta = raw["snapshot_delta"]
    if kind == "hash":
        delta["snapshot_hash"] = "0" * 64
    elif kind == "previous_hash":
        raw["previous_snapshot_hash"] = "0" * 64
    elif kind == "scope":
        raw["factory_id"] = "other"
    elif kind == "clock":
        raw["business_clock"] = at(10).isoformat()
    elif kind == "revision":
        raw["revision"] = "999"
    elif kind == "upsert_id":
        row = next(iter(delta["collections"]["inventory"]["upsert"].values()))
        row["stock"]["code"] = "other"
    elif kind == "partial_upsert":
        row = next(iter(delta["collections"]["inventory"]["upsert"].values()))
        del row["stock"]["physical"]
    elif kind == "canonical_bypass":
        row = next(iter(delta["collections"]["inventory"]["upsert"].values()))
        row["on_hand"] = 10000
    elif kind == "set_bypass":
        delta["set"]["inventory"] = after.model_dump(mode="json")["inventory"]
    elif kind == "event_scope":
        raw["events"][0]["factory_id"] = "other"
    elif kind == "bad_event":
        raw["events"][0]["entity_version"] = True
    elif kind == "extra_batch":
        raw["private_answer"] = "hidden"
    else:
        delta["private_answer"] = "hidden"
    retained = deepcopy(raw)
    original = before.model_dump(mode="json")
    with pytest.raises(MappingError):
        normalize_change_batch(before, raw, mapping_for(before, alternate=True))
    assert raw == retained and before.model_dump(mode="json") == original


def test_valid_hashed_delta_cannot_skip_a_source_revision():
    before = example_snapshot()
    after = change_snapshot(
        before,
        lambda raw: raw.update(
            snapshot_id="facts-3", source={**raw["source"], "source_revision": "3"}
        ),
    )
    with pytest.raises(MappingError, match="^CHANGE_REVISION_GAP$"):
        normalize_change_batch(before, change_batch(before, after), mapping_for(before))


def test_returned_nested_values_do_not_alias_wire_or_original_snapshot():
    before = example_snapshot()
    raw = wire_snapshot(before)["resources"][0]
    result = normalize_entity("resource", raw, mapping_for(before, alternate=True))
    result["calendar"][0]["kind"] = "OVERTIME"
    assert raw["asset"]["shifts"][0]["kind"] == "NORMAL"
    assert before.resources[0].calendar[0].kind == "NORMAL"


@pytest.mark.parametrize("entity", ["actual", "profile", "sql", "../orders"])
def test_undeclared_entity_types_are_rejected(entity):
    with pytest.raises(MappingError, match="^UNSUPPORTED_MAPPING_ENTITY$"):
        normalize_entity(entity, {}, mapping_for(example_snapshot()))
