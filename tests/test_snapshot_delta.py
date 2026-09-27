"""Round-trip public source facts and reject malformed delta chains without a database."""

import json
from copy import deepcopy
from typing import cast

import pytest
from sqlalchemy.orm import Session

from packages.domain.models import Snapshot
from packages.domain.snapshot_delta import SnapshotDeltaError, apply_delta, make_delta
from packages.integrations.factory_http import ConnectorError, FactoryHTTP
from packages.integrations.sync import save_incremental
from packages.planning.store import SnapshotRecord
from services.factory_sim.engine import advance, evolve, inject
from services.factory_sim.service import _write
from services.factory_sim.storage import World
from tests.test_case_impact import ChangeCollector, active_case
from tests.test_checker import at, change_snapshot, example_snapshot


def batch(before, after):
    collector = ChangeCollector()
    _write(
        cast(Session, collector), World(factory_id=before.factory_id), before, after, "clock.tick"
    )
    return collector.rows[0].document


def test_every_actual_progress_delta_rebuilds_identical_snapshot_and_preserves_ledger():
    before, plan = active_case()
    original = before.model_dump(mode="json")
    for _ in range(6):
        after = advance(before, plan)
        document = batch(before, after)
        delta = document["snapshot_delta"]
        assert "profile" not in delta["set"]
        assert "workers" not in delta["collections"]
        rebuilt = apply_delta(before, json.loads(json.dumps(delta)))
        assert rebuilt.model_dump(mode="json") == after.model_dump(mode="json")
        assert rebuilt.content_hash == document["snapshot_hash"]
        assert len(json.dumps(delta)) < len(json.dumps(after.model_dump(mode="json")))
        before = rebuilt
    assert before.inventory[0].on_hand == before.inventory[0].reserved == 0
    assert sum(c.quantity for a in before.actuals for c in a.consumed) == 2
    assert all(a.segments and a.state == "COMPLETED" for a in before.actuals)
    assert original["actuals"] == [] and original["reservations"] == []


def test_clock_only_delta_has_no_fact_collections_or_profile_copy():
    before = example_snapshot(batches=100)
    after = advance(before, None)
    delta = make_delta(before, after)
    assert delta["collections"] == {}
    assert set(delta["set"]) == {
        "schema_version",
        "snapshot_id",
        "snapshot_clock",
        "source",
        "planning_revision",
    }
    assert apply_delta(before, delta) == after


def test_new_order_and_nested_resource_unavailability_are_reconstructed_without_special_ids():
    before = example_snapshot()
    order = before.orders[0].model_dump(mode="json")
    order.update(order_id="urgent", quantity=4, priority_weight=10)
    after = inject(before, event_id="new", kind="order.add", payload=order)
    delta = make_delta(before, after)
    assert set(delta["collections"]) == {"orders"}
    assert set(delta["collections"]["orders"]["upsert"]) == {"urgent"}
    assert apply_delta(before, delta) == after
    changed_resources = [r.model_dump(mode="json") for r in after.resources]
    changed_resources[0].update(
        unavailable=[{"start_at": at(5).isoformat(), "end_at": at(10).isoformat()}], version=2
    )
    changed = evolve(after, resources=changed_resources)
    delta = make_delta(after, changed)
    assert delta["collections"]["resources"]["upsert"]["r1"]["unavailable"]
    assert apply_delta(after, delta) == changed


def test_row_order_and_removal_are_explicit_and_hash_significant():
    before = example_snapshot(second_product=True)
    reordered = evolve(before, orders=tuple(reversed(before.orders)))
    delta = make_delta(before, reordered)
    assert delta["collections"]["orders"] == {
        "upsert": {},
        "remove": [],
        "order": ["order-b", "order-a"],
    }
    assert apply_delta(before, delta) == reordered
    removed = evolve(reordered, orders=(reordered.orders[0],))
    delta = make_delta(reordered, removed)
    assert delta["collections"]["orders"]["remove"] == ["order-a"]
    assert apply_delta(reordered, delta) == removed


def test_jsonb_object_key_order_cannot_reorder_new_rows_or_change_snapshot_hash():
    before = example_snapshot()
    orders = [o.model_dump(mode="json") for o in before.orders]
    for identity in ("urgent-z", "urgent-a", "urgent-m"):
        orders.append({**orders[0], "order_id": identity})
    after = evolve(before, orders=orders)
    delta = make_delta(before, after)
    reordered_json_objects = json.loads(json.dumps(delta, sort_keys=True))
    assert list(reordered_json_objects["collections"]["orders"]["upsert"]) != [
        "urgent-z",
        "urgent-a",
        "urgent-m",
    ]
    assert apply_delta(before, reordered_json_objects).content_hash == after.content_hash
    assert apply_delta(before, reordered_json_objects).orders == after.orders


def test_delta_application_does_not_alias_or_mutate_snapshot_or_transport_document():
    before, plan = active_case()
    after = advance(before, plan)
    delta = make_delta(before, after)
    original = deepcopy(delta)
    rebuilt = apply_delta(before, delta)
    assert delta == original
    delta["collections"]["actuals"]["upsert"][after.actuals[0].operation_id]["segments"][0][
        "phase"
    ] = "SETUP"
    assert rebuilt.actuals[0].segments[0].phase == "PRODUCTION"
    assert before.actuals == ()


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("schema_version", "future", "INVALID_DELTA"),
        ("factory_id", "other", "DELTA_SCOPE_MISMATCH"),
        ("run_id", "other-run", "DELTA_SCOPE_MISMATCH"),
        ("previous_snapshot_hash", "0" * 64, "DELTA_ANCHOR_MISMATCH"),
        ("previous_source_revision", "999", "DELTA_ANCHOR_MISMATCH"),
        ("snapshot_hash", "0" * 64, "DELTA_SNAPSHOT_INVALID"),
        ("source_revision", "999", "DELTA_VERSION_CONFLICT"),
    ],
)
def test_anchor_scope_version_and_final_hash_tampering_rejected(field, value, code):
    before = example_snapshot()
    delta = make_delta(before, advance(before, None))
    delta[field] = value
    with pytest.raises(SnapshotDeltaError, match=code):
        apply_delta(before, delta)


@pytest.mark.parametrize(
    "name,value",
    [
        ("factory_id", "other"),
        ("content_hash", "0" * 64),
        ("inventory", []),
        ("future_events", []),
        ("private_seed", 123),
    ],
)
def test_set_cannot_override_scope_collections_or_include_private_fields(name, value):
    before = example_snapshot()
    delta = make_delta(before, advance(before, None))
    delta["set"][name] = value
    with pytest.raises(SnapshotDeltaError, match="UNKNOWN_DELTA_FIELD"):
        apply_delta(before, delta)


@pytest.mark.parametrize(
    "fault", ["key", "missing_id", "missing_required", "private_field", "bad_nested_ledger"]
)
def test_changed_rows_need_exact_identity_complete_schema_and_valid_nested_ledger(fault):
    before, plan = active_case()
    after = advance(before, plan)
    delta = make_delta(before, after)
    actual_id = after.actuals[0].operation_id
    row = delta["collections"]["actuals"]["upsert"][actual_id]
    expected = "DELTA_SNAPSHOT_INVALID"
    if fault == "key":
        row["operation_id"] = "unknown"
        expected = "DELTA_ROW_IDENTITY_CONFLICT"
    elif fault == "missing_id":
        del row["operation_id"]
        expected = "DELTA_ROW_IDENTITY_CONFLICT"
    elif fault == "missing_required":
        del row["worker_id"]
    elif fault == "private_field":
        row["future_failure"] = "tomorrow"
    else:
        row["segments"][0]["end_at"] = at(100).isoformat()
    with pytest.raises(SnapshotDeltaError, match=expected):
        apply_delta(before, delta)


@pytest.mark.parametrize(
    "changes",
    [
        {"remove": ["absent"]},
        {"remove": ["order-a", "order-a"]},
        {"remove": ["order-a"], "upsert": {"order-a": {"order_id": "order-a"}}},
        {"order": ["order-a", "order-a"]},
        {"order": []},
        {"order": ["missing"]},
    ],
)
def test_ambiguous_collection_edits_fail(changes):
    before = example_snapshot()
    delta = make_delta(before, advance(before, None))
    delta["collections"]["orders"] = changes
    with pytest.raises(SnapshotDeltaError, match="DELTA_ROW_(IDENTITY|ORDER)_CONFLICT"):
        apply_delta(before, delta)


def test_unknown_collection_and_top_level_fields_fail():
    before = example_snapshot()
    delta = make_delta(before, advance(before, None))
    delta["collections"]["private_timers"] = {}
    with pytest.raises(SnapshotDeltaError, match="UNKNOWN_DELTA_FIELD"):
        apply_delta(before, delta)
    del delta["collections"]["private_timers"]
    delta["future_events"] = []
    with pytest.raises(SnapshotDeltaError, match="INVALID_DELTA"):
        apply_delta(before, delta)


def test_make_delta_rejects_same_identity_and_cross_run():
    before = example_snapshot()
    with pytest.raises(SnapshotDeltaError, match="DELTA_VERSION_CONFLICT"):
        make_delta(before, before)
    after = evolve(before)
    after = change_snapshot(after, lambda data: data.update(run_id="replay"))
    with pytest.raises(SnapshotDeltaError, match="DELTA_SCOPE_MISMATCH"):
        make_delta(before, after)


class MemoryRecords:
    """Unit-test record routing only; transaction rollback is reserved for PostgreSQL tests."""

    def __init__(self):
        self.records = {}

    def get(self, cls, key):
        return self.records.get((cls, key))

    def add(self, row):
        key = row.snapshot_id if isinstance(row, SnapshotRecord) else (row.run_id, row.revision)
        assert (type(row), key) not in self.records
        self.records[type(row), key] = row

    def scalar(self, statement):
        return None


class Feed:
    def __init__(self, final, batches):
        self.final, self.batches = final, batches

    def _get(self, path, params):
        assert path == "/factory/v1/changes"
        assert params["run_id"] == self.final.run_id
        return {
            "factory_id": self.final.factory_id,
            "run_id": self.final.run_id,
            "watermark": int(self.final.source.source_revision),
            "next_cursor": int(self.final.source.source_revision),
            "changes": self.batches,
        }


def chain():
    first = example_snapshot()
    second, third = advance(first, None), None
    third = advance(second, None)
    return first, second, third, [batch(first, second), batch(second, third)]


def save(records, first, last, batches):
    save_incremental(cast(Session, records), cast(FactoryHTTP, Feed(last, batches)), first, last)


def test_incremental_saves_only_proven_intermediate_and_is_idempotent():
    first, second, last, batches = chain()
    records = MemoryRecords()
    save(records, first, last, batches)
    stored = records.get(SnapshotRecord, second.snapshot_id)
    assert Snapshot.model_validate(stored.document) == second
    assert stored.content_hash == second.content_hash
    assert records.get(SnapshotRecord, last.snapshot_id) is None
    assert len(records.records) == 3
    original = dict(records.records)
    save(records, first, last, batches)
    assert records.records == original


def test_legacy_batches_remain_hash_reconcilable_without_invented_snapshots():
    first, second, last, batches = chain()
    for row in batches:
        del row["snapshot_delta"]
    records = MemoryRecords()
    save(records, first, last, batches)
    assert records.get(SnapshotRecord, second.snapshot_id) is None
    assert records.get(SnapshotRecord, last.snapshot_id) is None
    assert len(records.records) == 2


@pytest.mark.parametrize(
    "fault",
    [
        "before_hash",
        "after_hash",
        "revision",
        "business_clock",
        "delta_row",
        "saved_collision",
        "legacy_gap",
    ],
)
def test_incremental_delta_disagreement_rejects_source_reconciliation(fault):
    first, second, last, batches = chain()
    records = MemoryRecords()
    if fault == "before_hash":
        batches[0]["snapshot_delta"]["previous_snapshot_hash"] = "0" * 64
    elif fault == "after_hash":
        batches[0]["snapshot_delta"]["snapshot_hash"] = "0" * 64
    elif fault == "revision":
        batches[0]["snapshot_delta"]["source_revision"] = "999"
    elif fault == "business_clock":
        batches[0]["business_clock"] = at(50).isoformat()
    elif fault == "delta_row":
        batches[0]["snapshot_delta"]["collections"]["orders"] = {
            "upsert": {"wrong": {"order_id": "other"}}
        }
    elif fault == "saved_collision":
        records.add(
            SnapshotRecord(
                snapshot_id=second.snapshot_id,
                factory_id="other",
                content_hash=second.content_hash,
                document=second.model_dump(mode="json"),
            )
        )
    else:
        del batches[0]["snapshot_delta"]
    with pytest.raises(ConnectorError) as caught:
        save(records, first, last, batches)
    if fault == "legacy_gap":
        assert caught.value.code == "SNAPSHOT_DELTA_BASE_REQUIRED"
    else:
        assert "could not be reconciled" in str(caught.value)
