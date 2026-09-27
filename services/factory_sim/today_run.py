"""Start an explicitly dated demonstration run without altering source history."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from packages.domain.models import Snapshot, canonical_hash
from services.factory_sim.engine import SimulationError
from services.factory_sim.storage import SourceAction, SourceRun, World


def _shift_dates(value: object, days: int, zone: ZoneInfo) -> object:
    if isinstance(value, datetime):
        local = value.astimezone(zone)
        shifted = local + timedelta(days=days)
        if shifted.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != shifted.replace(
            tzinfo=None
        ):
            raise SimulationError("TODAY_RUN_CALENDAR_UNSUPPORTED")
        return shifted.astimezone(UTC)
    if isinstance(value, dict):
        return {key: _shift_dates(item, days, zone) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_shift_dates(item, days, zone) for item in value]
    return value


def dated_initial(initial: Snapshot, today: datetime, new_run_id: str) -> Snapshot:
    """Move the entire initial business calendar by whole local days."""
    if initial.actuals or initial.reservations or initial.active_plan_version is not None:
        raise SimulationError("TODAY_RUN_INITIAL_STATE_UNSUPPORTED")
    zone = ZoneInfo(initial.profile.timezone)
    days = (today.astimezone(zone).date() - initial.snapshot_clock.astimezone(zone).date()).days
    raw = _shift_dates(initial.model_dump(mode="python", exclude={"content_hash"}), days, zone)
    assert isinstance(raw, dict)
    raw.update(run_id=new_run_id, snapshot_id=f"{new_run_id}-1", planning_revision=1)
    raw["source"].update(
        source_system="factory-simulator-http-v1",
        source_revision="1",
        cursor="1",
        observed_at=today,
        freshness="CURRENT",
    )
    return Snapshot.model_validate(raw)


def start_today_run(
    engine: Engine,
    factory_id: str,
    request_id: str,
    expected_run_id: str,
    scenario_version: str | None = None,
) -> dict:
    request: dict[str, object] = {
        "factory_id": factory_id,
        "request_id": request_id,
        "expected_run_id": expected_run_id,
    }
    if scenario_version is not None:
        if scenario_version != "workshop-full-2":
            raise SimulationError("UNKNOWN_DEMO_SCENARIO")
        request["scenario_version"] = scenario_version
    with Session(engine) as db, db.begin():
        world = db.scalar(select(World).where(World.factory_id == factory_id).with_for_update())
        if world is None:
            raise SimulationError("FACTORY_NOT_FOUND")
        prior = db.scalar(
            select(SourceAction).where(
                SourceAction.factory_id == factory_id,
                SourceAction.operation_id == request_id,
            )
        )
        if prior:
            if prior.kind != "run.start_today" or prior.payload_hash != canonical_hash(request):
                raise SimulationError("IDEMPOTENCY_CONFLICT")
            return prior.result
        if world.run_id != expected_run_id:
            raise SimulationError("SOURCE_RUN_CHANGED")
        original = db.get(SourceRun, expected_run_id)
        if original is None:
            # The initial import precedes the first simulator action.
            original = SourceRun(
                run_id=expected_run_id,
                factory_id=factory_id,
                initial_snapshot=world.document,
                created_at=datetime.now(UTC),
            )
            db.add(original)
        if original.factory_id != factory_id or original.replay_of is not None:
            raise SimulationError("TODAY_RUN_ORIGIN_UNAVAILABLE")
        initial = Snapshot.model_validate(original.initial_snapshot)
        if initial.run_id != expected_run_id or initial.factory_id != factory_id:
            raise SimulationError("TODAY_RUN_ORIGIN_MISMATCH")
        now = datetime.now(UTC)
        new_run_id = str(uuid4())
        if scenario_version == "workshop-full-2":
            from scripts.setup_team import workshop_snapshot

            data = workshop_snapshot().model_dump(mode="python", exclude={"content_hash"})
            data["factory_id"] = data["profile"]["factory_id"] = factory_id
            initial = Snapshot.model_validate(data)
        fresh = dated_initial(initial, now, new_run_id)
        world.run_id = new_run_id
        world.revision = 1
        world.business_clock = fresh.snapshot_clock
        world.document = fresh.model_dump(mode="json")
        world.active_candidate = None
        world.mode, world.next_tick_at = "PAUSED", None
        world.replay_state = world.scenario_state = None
        db.add(
            SourceRun(
                run_id=new_run_id,
                factory_id=factory_id,
                initial_snapshot=world.document,
                created_at=now,
            )
        )
        result = {
            "factory_id": factory_id,
            "request_id": request_id,
            "origin_run_id": expected_run_id,
            "run_id": new_run_id,
            "business_clock": fresh.snapshot_clock.isoformat(),
            "horizon_end": fresh.horizon.end_at.isoformat(),
        }
        db.add(
            SourceAction(
                action_id=str(uuid4()),
                factory_id=factory_id,
                run_id=new_run_id,
                operation_id=request_id,
                payload_hash=canonical_hash(request),
                kind="run.start_today",
                request=request,
                result=result,
            )
        )
        return result
