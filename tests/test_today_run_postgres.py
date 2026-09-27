"""A dated demonstration run shifts the calendar without altering factory facts."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session
from test_dynamic_factory_postgres import dynamic_source as dynamic_source

from packages.domain.execution import TodayRunStart
from packages.domain.models import Snapshot
from packages.integrations.factory_http import FactoryControls
from services.factory_sim.engine import SimulationError
from services.factory_sim.storage import SourceRun, World
from services.factory_sim.today_run import dated_initial, start_today_run


def test_whole_initial_calendar_moves_to_requested_business_day(dynamic_source):
    original = dynamic_source[2]
    target = datetime(2026, 9, 23, 6, 0, tzinfo=UTC)
    shifted = dated_initial(original, target, "new-demo-run")
    zone = ZoneInfo(original.profile.timezone)
    days = (target.astimezone(zone).date() - original.snapshot_clock.astimezone(zone).date()).days
    assert shifted.snapshot_clock.astimezone(zone).date() == target.astimezone(zone).date()
    assert (
        shifted.horizon.start_at - original.horizon.start_at
        == shifted.horizon.end_at - original.horizon.end_at
    )
    assert (shifted.horizon.start_at - original.horizon.start_at).days == days
    assert (
        shifted.orders[0].due_at - original.orders[0].due_at
        == shifted.horizon.start_at - original.horizon.start_at
    )
    assert (
        shifted.resources[0].calendar[0].start_at - original.resources[0].calendar[0].start_at
        == shifted.horizon.start_at - original.horizon.start_at
    )
    assert shifted.inventory == original.inventory
    assert [r.quantity for r in shifted.receipts] == [r.quantity for r in original.receipts]
    assert shifted.run_id == "new-demo-run" and shifted.source.source_revision == "1"
    assert original.run_id != shifted.run_id


def test_explicit_today_run_preserves_origin_and_retries_safely(dynamic_source):
    client, tokens, initial, _, engine = dynamic_source
    factory = initial.factory_id
    controls = FactoryControls(str(client.base_url), tokens["controller"])
    try:
        result = controls.start_today_run(
            factory, TodayRunStart(request_id="today-1", expected_run_id=initial.run_id)
        )
        assert (
            controls.start_today_run(
                factory, TodayRunStart(request_id="today-1", expected_run_id=initial.run_id)
            )
            == result
        )
    finally:
        controls.close()
    assert result["origin_run_id"] == initial.run_id and result["run_id"] != initial.run_id
    with Session(engine) as db:
        original = db.get(SourceRun, initial.run_id)
        current = db.get(World, factory)
        assert original is not None and original.initial_snapshot["run_id"] == initial.run_id
        assert (
            current is not None and current.run_id == result["run_id"] and current.mode == "PAUSED"
        )
    response = client.get("/factory/v1/snapshot", params={"factory_id": factory})
    response.raise_for_status()
    fresh = Snapshot.model_validate(response.json())
    assert fresh.run_id == result["run_id"]
    assert (
        fresh.snapshot_clock.astimezone(ZoneInfo(initial.profile.timezone)).date()
        == datetime.now(UTC).astimezone(ZoneInfo(initial.profile.timezone)).date()
    )
    assert (
        fresh.orders[0].due_at - initial.orders[0].due_at
        == fresh.horizon.start_at - initial.horizon.start_at
    )
    assert fresh.inventory == initial.inventory
    # Starting again on the same local day is an explicit clean rehearsal, not a no-op.
    again = start_today_run(engine, factory, "today-again", fresh.run_id)
    assert again["run_id"] not in {initial.run_id, fresh.run_id}
    with Session(engine) as db:
        assert db.get(SourceRun, fresh.run_id) is not None
        assert db.get(World, factory).run_id == again["run_id"]
    second = client.get("/factory/v1/snapshot", params={"factory_id": factory})
    second.raise_for_status()
    restarted = Snapshot.model_validate(second.json())
    assert restarted.orders == fresh.orders
    assert restarted.inventory == fresh.inventory
    assert restarted.actuals == () and restarted.active_plan_version is None
    assert restarted.snapshot_clock == fresh.snapshot_clock
    with pytest.raises(SimulationError, match="SOURCE_RUN_CHANGED"):
        start_today_run(engine, factory, "today-stale", initial.run_id)


def test_explicit_versioned_demo_keeps_previous_run_and_changes_only_new_initial(dynamic_source):
    source = dynamic_source
    initial, engine = source[2], source[4]
    result = start_today_run(
        engine, initial.factory_id, "new-v2-demo", initial.run_id, "workshop-full-2"
    )
    assert (
        start_today_run(
            engine, initial.factory_id, "new-v2-demo", initial.run_id, "workshop-full-2"
        )
        == result
    )
    with pytest.raises(SimulationError, match="IDEMPOTENCY_CONFLICT"):
        start_today_run(engine, initial.factory_id, "new-v2-demo", initial.run_id)
    with Session(engine) as db:
        original = db.get(SourceRun, initial.run_id)
        current = db.get(World, initial.factory_id)
        assert original.initial_snapshot["profile"]["version"] == initial.profile.version
        assert len(original.initial_snapshot["orders"]) == len(initial.orders)
        assert current.document["profile"]["version"] == "V1.6.workshop-full-2"
        assert len(current.document["orders"]) == 6
        assert current.mode == "PAUSED" and current.run_id != initial.run_id
