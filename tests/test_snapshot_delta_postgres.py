"""Rebuilt intermediate facts and their rejected deltas use real HTTP and PostgreSQL."""

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session
from test_dynamic_factory_postgres import control
from test_dynamic_factory_postgres import dynamic_source as dynamic_source
from test_incremental_sync_postgres import AdjustedSource, stored
from test_incremental_sync_postgres import syncing as syncing

from packages.domain.models import Snapshot
from packages.domain.snapshot_delta import apply_delta
from packages.integrations.factory_http import ConnectorError
from packages.integrations.sync import SourceBatch
from packages.planning.service import synchronize
from packages.planning.store import SnapshotRecord


def test_incremental_retains_exact_snapshot_for_every_new_delta_revision(syncing):
    source, reader, initial = syncing
    engine, factory = source[3], initial.factory_id
    assert control(source, "three-ticks", "clock.step", {"minutes": 3}).status_code == 200
    latest = synchronize(engine, reader, factory)
    with Session(engine) as db:
        batches = list(
            db.scalars(
                select(SourceBatch)
                .where(SourceBatch.factory_id == factory)
                .order_by(SourceBatch.revision)
            )
        )
        records = list(
            db.scalars(select(SnapshotRecord).where(SnapshotRecord.factory_id == factory))
        )
        assert len(records) == 4
        by_hash = {row.content_hash: row for row in records}
        prior = initial
        for row in batches:
            expected = apply_delta(prior, row.document["snapshot_delta"])
            saved = Snapshot.model_validate(by_hash[expected.content_hash].document)
            assert saved == expected
            assert saved.source.source_revision == str(row.revision)
            prior = saved
        assert prior == latest
    assert synchronize(engine, reader, factory) == latest
    assert len(stored(engine, factory)["snapshots"]) == 4


@pytest.mark.parametrize("fault", ["scope", "row_identity", "content_hash", "private_field"])
def test_bad_later_delta_rolls_back_flushed_intermediate_snapshots_events_and_watermark(
    syncing, fault
):
    source, reader, initial = syncing
    engine, factory = source[3], initial.factory_id
    assert control(source, "three-ticks", "clock.step", {"minutes": 3}).status_code == 200
    before = stored(engine, factory)
    inserted = set()

    def observe(connection, cursor, statement, parameters, context, executemany):
        for table in ("snapshots", "source_batches"):
            if statement.lstrip().startswith("INSERT INTO byof." + table):
                inserted.add(table)

    def corrupt(page):
        delta = next(row for row in page["changes"] if int(row["revision"]) == 4)["snapshot_delta"]
        if fault == "scope":
            delta["run_id"] = "other-run"
        elif fault == "row_identity":
            delta["collections"]["orders"] = {"upsert": {"wrong": {"order_id": "different"}}}
        elif fault == "content_hash":
            delta["snapshot_hash"] = "0" * 64
        else:
            delta["set"]["private_future_events"] = []

    event.listen(engine, "after_cursor_execute", observe)
    try:
        with pytest.raises(ConnectorError):
            synchronize(engine, AdjustedSource(reader, corrupt), factory)
    finally:
        event.remove(engine, "after_cursor_execute", observe)
    assert inserted == {"snapshots", "source_batches"}
    assert stored(engine, factory) == before
    latest = synchronize(engine, reader, factory)
    assert latest.source.source_revision == "4"
    assert len(stored(engine, factory)["snapshots"]) == 4


def test_caught_up_legacy_source_can_upgrade_without_inventing_missing_history(syncing):
    source, reader, initial = syncing
    engine, factory = source[3], initial.factory_id
    assert control(source, "legacy-two-ticks", "clock.step", {"minutes": 2}).status_code == 200

    def legacy(page):
        for row in page["changes"]:
            row.pop("snapshot_delta")

    checkpoint = synchronize(engine, AdjustedSource(reader, legacy), factory)
    history = stored(engine, factory)
    assert history["snapshots"] == sorted([initial.snapshot_id, checkpoint.snapshot_id])
    assert all("snapshot_delta" not in row[2] for row in history["batches"])
    assert control(source, "upgraded-two-ticks", "clock.step", {"minutes": 2}).status_code == 200
    latest = synchronize(engine, reader, factory)
    with Session(engine) as db:
        versions = sorted(
            int(Snapshot.model_validate(row.document).source.source_revision)
            for row in db.scalars(
                select(SnapshotRecord).where(SnapshotRecord.factory_id == factory)
            )
        )
    assert versions == [1, 3, 4, 5]
    assert latest.source.source_revision == "5"


def test_mixed_legacy_gap_requires_checkpoint_and_keeps_previous_verified_state(syncing):
    source, reader, initial = syncing
    engine, factory = source[3], initial.factory_id
    assert control(source, "two-ticks", "clock.step", {"minutes": 2}).status_code == 200
    before = stored(engine, factory)

    def missing_legacy_base(page):
        next(row for row in page["changes"] if int(row["revision"]) == 2).pop("snapshot_delta")

    with pytest.raises(ConnectorError) as caught:
        synchronize(engine, AdjustedSource(reader, missing_legacy_base), factory)
    assert caught.value.code == "SNAPSHOT_DELTA_BASE_REQUIRED"
    assert stored(engine, factory) == before
