"""Event batches and complete snapshots commit together in real PostgreSQL transactions."""

import os
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.orm import Session
from test_dynamic_factory_postgres import control
from test_dynamic_factory_postgres import dynamic_source as dynamic_source

from packages.auth import AccessError
from packages.integrations.factory_http import ConnectorError, FactoryHTTP
from packages.integrations.sync import SourceBatch, save_incremental
from packages.persistence import connect
from packages.planning.service import synchronize
from packages.planning.store import FactoryState, SnapshotRecord


@pytest.fixture
def syncing(dynamic_source):
    source = dynamic_source
    client, tokens, initial, engine, _ = source
    reader = FactoryHTTP(str(client.base_url), tokens["reader"])
    first = synchronize(engine, reader, initial.factory_id)
    try:
        yield source, reader, first
    finally:
        reader.close()
        owner = connect(os.environ["TEST_MIGRATION_DATABASE_URL"])
        assert owner.url.database == "byof_test"
        with owner.begin() as db:
            for table in (SourceBatch, FactoryState, SnapshotRecord):
                db.execute(delete(table).where(table.factory_id == initial.factory_id))
        owner.dispose()


class AdjustedSource:
    """Inject a feed fault after a real HTTP read, without editing source history."""

    def __init__(self, reader, adjust=None, snapshot=None):
        self.reader = reader
        self.adjust = adjust
        self.received = snapshot

    def capabilities(self):
        return self.reader.capabilities()

    def snapshot(self, factory_id):
        return self.received or self.reader.snapshot(factory_id)

    def _get(self, path, params=None):
        page = deepcopy(self.reader._get(path, params))
        if path == "/factory/v1/changes" and self.adjust:
            self.adjust(page)
        return page


def stored(engine, factory_id):
    with Session(engine) as db:
        state = db.get(FactoryState, factory_id)
        batches = db.scalars(
            select(SourceBatch)
            .where(SourceBatch.factory_id == factory_id)
            .order_by(SourceBatch.revision)
        ).all()
        snapshots = db.scalars(
            select(SnapshotRecord.snapshot_id)
            .where(SnapshotRecord.factory_id == factory_id)
            .order_by(SnapshotRecord.snapshot_id)
        ).all()
        return {
            "state": (state.snapshot_id, state.run_id, state.source_revision, state.last_synced_at),
            "batches": [(r.revision, r.content_hash, r.document, r.received_at) for r in batches],
            "snapshots": list(snapshots),
        }


def snapshot_revisions(engine, factory_id):
    with Session(engine) as db:
        return sorted(
            int(row.document["source"]["source_revision"])
            for row in db.scalars(
                select(SnapshotRecord).where(SnapshotRecord.factory_id == factory_id)
            )
        )


def test_duplicate_batches_are_idempotent_and_snapshot_reconciliation_advances_once(syncing):
    source, reader, initial = syncing
    engine, factory = source[3], initial.factory_id
    assert control(source, "two-ticks", "clock.step", {"minutes": 2}).status_code == 200
    received = reader.snapshot(factory)
    with Session(engine) as db, db.begin():
        save_incremental(db, reader, initial, received)
    first = stored(engine, factory)
    with Session(engine) as db, db.begin():
        save_incremental(db, reader, initial, received)
    assert stored(engine, factory) == first
    assert [r[0] for r in first["batches"]] == [2, 3]
    synchronized = synchronize(engine, reader, factory)
    after = stored(engine, factory)
    assert synchronized.content_hash == received.content_hash
    assert after["batches"] == first["batches"]
    assert after["state"][0:3] == (received.snapshot_id, initial.run_id, "3")
    assert snapshot_revisions(engine, factory) == [1, 2, 3]
    assert synchronize(engine, reader, factory) == synchronized
    repeated = stored(engine, factory)
    assert repeated["batches"] == after["batches"]
    assert repeated["snapshots"] == after["snapshots"]
    assert reader.snapshot(factory).snapshot_clock == initial.snapshot_clock + timedelta(minutes=2)


def test_duplicate_revision_with_changed_event_content_cannot_replace_saved_batch(syncing):
    source, reader, initial = syncing
    engine, factory = source[3], initial.factory_id
    assert control(source, "one-tick", "clock.step").status_code == 200
    assert (
        control(
            source, "fault", "resource.down", {"resource_id": initial.resources[0].resource_id}
        ).status_code
        == 200
    )
    received = reader.snapshot(factory)
    with Session(engine) as db, db.begin():
        save_incremental(db, reader, initial, received)
    before = stored(engine, factory)

    def replace_content(page):
        source_event = next(r for r in page["changes"] if int(r["revision"]) == 3)["events"][0]
        assert source_event["event_type"] == "resource.down"
        source_event["event_type"] = "resource.restore"

    with pytest.raises(ConnectorError, match="could not be reconciled"):
        synchronize(engine, AdjustedSource(reader, replace_content), factory)
    assert stored(engine, factory) == before
    assert synchronize(engine, reader, factory).content_hash == received.content_hash
    assert stored(engine, factory)["batches"] == before["batches"]


@pytest.mark.parametrize("fault", ["missing_revision", "previous_hash", "terminal_hash"])
def test_event_gap_or_hash_tampering_rolls_back_flushed_batches_and_factory_state(syncing, fault):
    source, reader, initial = syncing
    engine, factory = source[3], initial.factory_id
    assert control(source, "three-ticks", "clock.step", {"minutes": 3}).status_code == 200
    received = reader.snapshot(factory)
    before = stored(engine, factory)
    inserted = []

    def observe_insert(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("INSERT INTO byof.source_batches"):
            inserted.append(statement)

    def corrupt_last_revision(page):
        if fault == "missing_revision":
            page["changes"] = [r for r in page["changes"] if int(r["revision"]) != 4]
        else:
            for batch in page["changes"]:
                if int(batch["revision"]) == 4:
                    field = (
                        "previous_snapshot_hash" if fault == "previous_hash" else "snapshot_hash"
                    )
                    batch[field] = "0" * 64

    event.listen(engine, "after_cursor_execute", observe_insert)
    try:
        with pytest.raises(ConnectorError):
            synchronize(engine, AdjustedSource(reader, corrupt_last_revision), factory)
    finally:
        event.remove(engine, "after_cursor_execute", observe_insert)
    assert inserted, "The rejection must roll back an actual PostgreSQL INSERT"
    assert stored(engine, factory) == before
    assert before["batches"] == [] and before["snapshots"] == [initial.snapshot_id]
    assert synchronize(engine, reader, factory).content_hash == received.content_hash
    after = stored(engine, factory)
    assert [r[0] for r in after["batches"]] == [2, 3, 4]
    assert after["state"][0:3] == (received.snapshot_id, initial.run_id, "4")
    assert snapshot_revisions(engine, factory) == [1, 2, 3, 4]


def test_late_business_event_with_new_revision_is_kept_but_old_snapshot_never_overwrites(syncing):
    source, reader, initial = syncing
    engine, factory = source[3], initial.factory_id
    assert control(source, "ten-ticks", "clock.step", {"minutes": 10}).status_code == 200
    previous = synchronize(engine, reader, factory)
    resource_id = previous.resources[0].resource_id
    assert (
        control(
            source, "late-observed-fault", "resource.down", {"resource_id": resource_id}
        ).status_code
        == 200
    )
    event_time = previous.snapshot_clock - timedelta(minutes=5)

    def late_occurrence(page):
        for batch in page["changes"]:
            for source_event in batch["events"]:
                source_event["occurred_at"] = event_time.isoformat()
                source_event["effective_at"] = event_time.isoformat()

    latest = synchronize(engine, AdjustedSource(reader, late_occurrence), factory)
    assert latest.source.source_revision == "12"
    assert latest.snapshot_clock == previous.snapshot_clock
    assert next(r.status for r in latest.resources if r.resource_id == resource_id) == "DOWN"
    accepted = stored(engine, factory)
    saved_event = accepted["batches"][-1][2]["events"][0]
    assert saved_event["occurred_at"] == saved_event["effective_at"] == event_time.isoformat()
    assert saved_event["source_revision"] == "12"
    with pytest.raises(AccessError) as rejected:
        synchronize(engine, AdjustedSource(reader, snapshot=previous), factory)
    assert rejected.value.code == "LATE_SNAPSHOT"
    assert stored(engine, factory) == accepted
    assert accepted["state"][0:3] == (latest.snapshot_id, initial.run_id, "12")
    assert snapshot_revisions(engine, factory) == list(range(1, 13))
