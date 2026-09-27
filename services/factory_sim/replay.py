"""Private, deterministic replay of one recorded simulator run; no external actions."""

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from packages.domain.execution import ActionReceipt, PlanSubmission, SimulatorCommand
from packages.domain.models import Candidate, CheckReport, Snapshot, canonical_hash
from packages.planning.checker import check_candidate
from services.factory_sim.engine import SimulationError, advance, evolve, inject
from services.factory_sim.storage import SourceAction, SourceChange, SourceRun, World


def _physical_facts(snapshot: Snapshot) -> dict:
    """Exclude only run/provenance identities; preserve physical quantities and times."""
    data = snapshot.model_dump(mode="json")
    for field in (
        "schema_version",
        "snapshot_id",
        "run_id",
        "source",
        "content_hash",
        "active_plan_hash",
        "active_plan_version",
    ):
        data.pop(field)
    data["has_active_plan"] = snapshot.active_plan_version is not None
    for row in data["actuals"]:
        row["remaining_confirmed_by"] = row["remaining_confirmed_by"] is not None
        for consumed in row["consumed"]:
            consumed.pop("event_id")
        for segment in row["segments"]:
            segment.pop("source_event_id")
    for reservation in data["reservations"]:
        for field in ("reservation_id", "plan_version", "source_event_id"):
            reservation.pop(field)
    for batch in data.get("production_batches") or ():
        # Each replay records its own event provenance, while preserving batch identity/purpose.
        batch.pop("source_event_id")
    return data


def start_replay(engine: Engine, factory_id: str, request_id: str, expected_run_id: str) -> dict:
    """Atomically switch the simulator pointer; previously recorded rows stay untouched."""
    from services.factory_sim.service import _locked, _record

    request: dict = {
        "factory_id": factory_id,
        "request_id": request_id,
        "expected_run_id": expected_run_id,
    }
    with Session(engine) as db, db.begin():
        world = _locked(db, factory_id)
        prior = db.scalar(
            select(SourceAction).where(
                SourceAction.factory_id == factory_id,
                SourceAction.operation_id == request_id,
                SourceAction.kind == "replay.start",
            )
        )
        if prior:
            if prior.payload_hash != canonical_hash(request):
                raise SimulationError("IDEMPOTENCY_CONFLICT")
            return prior.result
        if world.run_id != expected_run_id:
            raise SimulationError("SOURCE_RUN_CHANGED")
        original = db.get(SourceRun, expected_run_id)
        if original is None or original.factory_id != factory_id:
            raise SimulationError("REPLAY_ORIGIN_MISSING")
        initial = Snapshot.model_validate(original.initial_snapshot)
        if (
            original.replay_of is not None
            or initial.actuals
            or initial.reservations
            or initial.active_plan_version is not None
        ):
            raise SimulationError("REPLAY_INITIAL_STATE_UNSUPPORTED")
        if initial.run_id != expected_run_id or initial.factory_id != factory_id:
            raise SimulationError("REPLAY_ORIGIN_MISMATCH")
        target = Snapshot.model_validate(world.document)
        if (
            int(initial.source.source_revision) > world.revision
            or target.run_id != expected_run_id
            or target.factory_id != factory_id
            or int(target.source.source_revision) != world.revision
        ):
            raise SimulationError("REPLAY_TIMELINE_INVALID")
        new_run = str(uuid4())
        raw = initial.model_dump(mode="python", exclude={"content_hash"})
        raw.update(
            schema_version=initial.schema_version,
            run_id=new_run,
            snapshot_id=f"{new_run}-{initial.source.source_revision}",
        )
        raw["source"].update(
            source_system="factory-simulator-replay", observed_at=datetime.now(UTC)
        )
        replay = Snapshot.model_validate(raw)
        first = int(initial.source.source_revision) + 1
        if first > world.revision and _physical_facts(replay) != _physical_facts(target):
            raise SimulationError("REPLAY_PHYSICAL_STATE_MISMATCH")
        world.replay_state = {
            "origin_run_id": expected_run_id,
            "next_revision": first,
            "target_revision": world.revision,
            "origin_snapshot_hash": initial.content_hash,
            "expected_final_snapshot": target.model_dump(mode="json"),
            "done": first > world.revision,
            "error_code": None,
            "external_actions_enabled": False,
        }
        if first > world.revision:
            world.replay_state["physical_match"] = True
        world.run_id = new_run
        world.document = replay.model_dump(mode="json")
        world.revision = int(replay.source.source_revision)
        world.business_clock = replay.snapshot_clock
        world.active_candidate = None
        world.mode, world.next_tick_at = "PAUSED", None
        world.scenario_state = None
        db.add(
            SourceRun(
                run_id=new_run,
                factory_id=factory_id,
                initial_snapshot=world.document,
                replay_of=expected_run_id,
                created_at=datetime.now(UTC),
            )
        )
        result = {
            "factory_id": factory_id,
            "run_id": new_run,
            "request_id": request_id,
            "origin_run_id": expected_run_id,
            "source_revision": replay.source.source_revision,
            "snapshot_hash": replay.content_hash,
            "mode": "PAUSED",
            "external_actions_enabled": False,
        }
        _record(db, world, request_id, "replay.start", request, result)
        return result


def _original_action(db: Session, world: World, change: SourceChange) -> SourceAction:
    kind = change.document["cause"]
    query = select(SourceAction).where(
        SourceAction.factory_id == world.factory_id,
        SourceAction.run_id == change.run_id,
        SourceAction.kind == ("plan.submit" if kind == "plan.activated" else kind),
    )
    if kind == "plan.activated":
        query = query.where(
            SourceAction.request["expected_source_revision"].as_string()
            == str(change.revision - 1),
            SourceAction.result["source_state"].as_string() == "ACTIVE",
        )
    else:
        query = query.where(
            SourceAction.result["source_revision"].as_string() == str(change.revision)
        )
    actions = list(db.scalars(query.limit(2)))
    if len(actions) != 1:
        raise SimulationError("REPLAY_ACTION_MISSING_OR_AMBIGUOUS")
    action = actions[0]
    if action.payload_hash != canonical_hash(action.request):
        raise SimulationError("REPLAY_ACTION_HASH_MISMATCH")
    return action


def _reuse_plan(
    snapshot: Snapshot, baseline: Candidate | None, action: SourceAction, change: SourceChange
) -> Candidate:
    submission = PlanSubmission.model_validate(action.request)
    receipt = ActionReceipt.model_validate(action.result)
    original = submission.candidate
    if (
        submission.factory_id != snapshot.factory_id
        or submission.run_id != change.run_id
        or submission.operation_id != action.operation_id
        or submission.expected_snapshot_hash != change.document["previous_snapshot_hash"]
        or original.binding.snapshot_hash != submission.expected_snapshot_hash
        or receipt.operation_id != action.operation_id
        or receipt.run_id != change.run_id
        or receipt.factory_id != snapshot.factory_id
        or receipt.candidate_hash != original.content_hash
        or receipt.source_state != "ACTIVE"
        or receipt.effective_at != snapshot.snapshot_clock
    ):
        raise SimulationError("REPLAY_PLAN_ORIGIN_MISMATCH")
    raw = original.model_dump(mode="python", exclude={"content_hash"})
    raw["binding"].update(
        snapshot_hash=str(snapshot.content_hash),
        planning_revision=snapshot.planning_revision,
        scope_version=snapshot.scope_version,
        profile_version=snapshot.profile.version,
        policy_version=snapshot.profile.policy.policy_version,
        baseline_plan_version=snapshot.active_plan_version,
    )
    raw["checker"] = CheckReport(
        checker_version="replay-pending", snapshot_hash=str(snapshot.content_hash), status="NOT_RUN"
    )
    rebound = Candidate.model_validate(raw)
    report = check_candidate(
        snapshot,
        rebound,
        baseline=baseline,
        allow_overtime="allow_overtime" in rebound.required_consents,
    )
    if report.status != "PASS":
        raise SimulationError("REPLAY_PLAN_CHECK_FAILED")
    if not rebound.effective_not_before <= snapshot.snapshot_clock < rebound.accept_before:
        raise SimulationError("REPLAY_PLAN_TIME_MISMATCH")
    raw["checker"] = report
    return Candidate.model_validate(raw)


def replay_tick(db: Session, world: World) -> Snapshot | None:
    """Apply one historical change under the caller's World row lock and transaction."""
    from services.factory_sim.service import _record, _write

    if world.replay_state is None:
        raise SimulationError("REPLAY_NOT_ACTIVE")
    state = dict(world.replay_state)
    if state["done"] or state["error_code"] is not None:
        world.mode, world.next_tick_at = "PAUSED", None
        return None
    try:
        revision = state["next_revision"]
        change = db.get(SourceChange, (state["origin_run_id"], revision))
        if change is None:
            raise SimulationError("REPLAY_CHANGE_MISSING")
        if (
            change.factory_id != world.factory_id
            or change.document["run_id"] != change.run_id
            or change.document["factory_id"] != world.factory_id
            or int(change.document["revision"]) != revision
            or change.document["previous_snapshot_hash"] != state["origin_snapshot_hash"]
        ):
            raise SimulationError("REPLAY_TIMELINE_MISMATCH")
        before = Snapshot.model_validate(world.document)
        baseline = (
            Candidate.model_validate(world.active_candidate) if world.active_candidate else None
        )
        cause = change.document["cause"]
        action = None
        candidate = None
        if cause == "clock.tick":
            after = advance(before, baseline)
        else:
            action = _original_action(db, world, change)
            if cause == "plan.activated":
                candidate = _reuse_plan(before, baseline, action, change)
                after = evolve(
                    before,
                    active_plan_version=f"src:{world.run_id}:{world.revision + 1}",
                    active_plan_hash=candidate.content_hash,
                )
            else:
                command = SimulatorCommand.model_validate(action.request)
                if (
                    command.run_id != change.run_id
                    or command.request_id != action.operation_id
                    or command.kind != cause
                    or cause.startswith("clock.")
                ):
                    raise SimulationError("REPLAY_COMMAND_ORIGIN_MISMATCH")
                payload = command.payload
                if cause == "business.accept":
                    if payload.get("expected_snapshot_hash") != state["origin_snapshot_hash"]:
                        raise SimulationError("REPLAY_COMMAND_ORIGIN_MISMATCH")
                    payload = {**payload, "expected_snapshot_hash": before.content_hash}
                after = inject(
                    before,
                    event_id=f"replay:{world.run_id}:{revision}",
                    kind=command.kind,
                    payload=payload,
                )
        if (
            after.snapshot_clock != datetime.fromisoformat(change.document["business_clock"])
            or int(after.source.source_revision) != revision
        ):
            raise SimulationError("REPLAY_CLOCK_MISMATCH")
        done = revision == state["target_revision"]
        if done:
            expected = Snapshot.model_validate(state["expected_final_snapshot"])
            if expected.content_hash != change.document["snapshot_hash"] or _physical_facts(
                after
            ) != _physical_facts(expected):
                raise SimulationError("REPLAY_PHYSICAL_STATE_MISMATCH")
    except (SimulationError, ValidationError, KeyError, TypeError, ValueError) as exc:
        state["error_code"] = (
            exc.code if isinstance(exc, SimulationError) else "REPLAY_RECORD_INVALID"
        )
        world.replay_state = state
        world.mode, world.next_tick_at = "PAUSED", None
        return None
    _write(db, world, before, after, "replay." + cause)
    if candidate is not None:
        assert action is not None
        world.active_candidate = candidate.model_dump(mode="json")
        _record(
            db,
            world,
            f"replay-plan:{revision}",
            "replay.plan_reused",
            {
                "origin_run_id": change.run_id,
                "origin_revision": revision,
                "origin_operation_id": action.operation_id,
                "origin_candidate_hash": action.result["candidate_hash"],
            },
            {
                "candidate_hash": candidate.content_hash,
                "source_revision": after.source.source_revision,
                "checker_status": candidate.checker.status,
                "solver_evidence": "REUSED_FROM_ORIGIN",
                "new_solver_calls": 0,
                "new_human_approvals": 0,
                "external_actions_enabled": False,
            },
        )
    state.update(
        next_revision=revision + 1, origin_snapshot_hash=change.document["snapshot_hash"], done=done
    )
    if done:
        state["physical_match"] = True
        world.mode, world.next_tick_at = "PAUSED", None
    world.replay_state = state
    return after
