"""Atomic source actions. Credentials/roles are checked at the HTTP boundary."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from packages.domain.execution import (
    ActionReceipt,
    ClockStep,
    PlanSubmission,
    RunSpeed,
    ScenarioConfiguration,
    SimulatorCommand,
)
from packages.domain.models import Candidate, Event, FieldChange, Snapshot, canonical_hash
from packages.domain.snapshot_delta import make_delta
from packages.planning.checker import check_candidate
from services.factory_sim.engine import (
    SimulationError,
    advance,
    evolve,
    inject,
    missed_dispatch_events,
)
from services.factory_sim.storage import SourceAction, SourceChange, SourceRun, World


def _locked(db: Session, factory_id: str) -> World:
    world = db.scalar(select(World).where(World.factory_id == factory_id).with_for_update())
    if world is None:
        raise SimulationError("FACTORY_NOT_FOUND")
    if db.get(SourceRun, world.run_id) is None:
        db.add(
            SourceRun(
                run_id=world.run_id,
                factory_id=factory_id,
                initial_snapshot=world.document,
                created_at=datetime.now(UTC),
            )
        )
    return world


def _prior(
    db: Session, world: World, operation_id: str, payload_hash: str, kind: str
) -> dict | None:
    record = db.scalar(
        select(SourceAction).where(
            SourceAction.factory_id == world.factory_id,
            SourceAction.run_id == world.run_id,
            SourceAction.operation_id == operation_id,
        )
    )
    if record:
        if record.payload_hash != payload_hash or record.kind != kind:
            raise SimulationError("IDEMPOTENCY_CONFLICT")
        return record.result
    return None


def _record(
    db: Session, world: World, operation_id: str, kind: str, request: dict, result: dict
) -> None:
    db.add(
        SourceAction(
            action_id=str(uuid4()),
            factory_id=world.factory_id,
            run_id=world.run_id,
            operation_id=operation_id,
            payload_hash=canonical_hash(request),
            kind=kind,
            request=request,
            result=result,
        )
    )


def _write(
    db: Session,
    world: World,
    before: Snapshot,
    after: Snapshot,
    cause: str,
    *,
    plan: Candidate | None = None,
) -> None:
    events = [
        event.model_dump(mode="json") for event in missed_dispatch_events(before, after, plan)
    ]
    for collection, key in (
        ("orders", "order_id"),
        ("inventory", "material_id"),
        ("receipts", "receipt_id"),
        ("resources", "resource_id"),
        ("workers", "worker_id"),
        ("actuals", "operation_id"),
    ):
        old = {
            getattr(row, key): row.model_dump(mode="json") for row in getattr(before, collection)
        }
        for row in getattr(after, collection):
            identity = getattr(row, key)
            prior = old.get(identity, {})
            changes = []
            for field, value in row.model_dump(mode="json").items():
                if (
                    field == "calendar"
                    and collection in {"resources", "workers"}
                    and cause.removeprefix("replay.") == "overtime_window.set"
                    and value != prior.get(field)
                ):
                    # FieldChange is scalar-only. Preserve verifiable calendar evidence
                    # so attention thresholds can distinguish removal from added capacity.
                    changes.append(
                        FieldChange(
                            field=field,
                            before=json.dumps(prior.get(field), sort_keys=True),
                            after=json.dumps(value, sort_keys=True),
                        )
                    )
                if field == key or value == prior.get(field) or isinstance(value, (list, dict)):
                    continue
                if type(value) in (str, int, bool) or value is None:
                    changes.append(FieldChange(field=field, before=prior.get(field), after=value))
            if not changes:
                continue
            event_kind = cause
            if collection == "actuals":
                event_kind = "execution." + row.state.lower()
                if prior.get("state") == row.state:
                    event_kind = "execution.progress"
            elif collection == "receipts" and row.status == "RECEIVED":
                event_kind = "receipt.received"
            elif collection == "orders" and not prior:
                event_kind = "order.added"
            identity_event = (
                f"{after.run_id}:{after.source.source_revision}:{collection}:{identity}"
            )
            events.append(
                Event(
                    event_id=identity_event,
                    source_event_id=identity_event,
                    factory_id=after.factory_id,
                    run_id=after.run_id,
                    source_revision=after.source.source_revision,
                    entity_type=collection,
                    entity_id=identity,
                    entity_version=row.version,
                    event_type=event_kind,
                    occurred_at=after.snapshot_clock,
                    effective_at=after.snapshot_clock,
                    observed_at=after.source.observed_at,
                    changes=tuple(changes),
                ).model_dump(mode="json")
            )
    if before.business_terms != after.business_terms and after.business_terms is not None:
        prior_terms, terms = before.business_terms, after.business_terms
        identity = f"{after.run_id}:{after.source.source_revision}:business_terms"
        events.append(
            Event(
                event_id=identity,
                source_event_id=identity,
                factory_id=after.factory_id,
                run_id=after.run_id,
                source_revision=after.source.source_revision,
                entity_type="business_terms",
                entity_id="business-terms",
                entity_version=after.planning_revision,
                event_type=cause,
                occurred_at=after.snapshot_clock,
                effective_at=after.snapshot_clock,
                observed_at=after.source.observed_at,
                changes=(
                    FieldChange(
                        field="version",
                        before=prior_terms.version if prior_terms else None,
                        after=terms.version,
                    ),
                ),
            ).model_dump(mode="json")
        )
    db.add(
        SourceChange(
            factory_id=world.factory_id,
            run_id=after.run_id,
            revision=int(after.source.source_revision),
            document={
                "run_id": after.run_id,
                "factory_id": after.factory_id,
                "revision": after.source.source_revision,
                "previous_snapshot_hash": before.content_hash,
                "snapshot_hash": after.content_hash,
                "business_clock": after.snapshot_clock.isoformat(),
                "cause": cause,
                "events": events,
                "snapshot_delta": make_delta(before, after),
            },
        )
    )
    world.document = after.model_dump(mode="json")
    world.revision = int(after.source.source_revision)
    world.business_clock = after.snapshot_clock


def submit_plan(engine: Engine, submission: PlanSubmission) -> ActionReceipt:
    request = submission.model_dump(mode="json")
    with Session(engine) as db, db.begin():
        world = _locked(db, submission.factory_id)
        if world.run_id != submission.run_id:
            raise SimulationError("SOURCE_RUN_CHANGED")
        prior = _prior(db, world, submission.operation_id, canonical_hash(request), "plan.submit")
        if prior:
            return ActionReceipt.model_validate(prior)
        if world.replay_state is not None:
            raise SimulationError("REPLAY_READ_ONLY")
        snapshot = Snapshot.model_validate(world.document)
        candidate = submission.candidate
        now = datetime.now(UTC)
        baseline = (
            Candidate.model_validate(world.active_candidate) if world.active_candidate else None
        )
        required = {"publish_plan", *candidate.required_consents}
        scopes = {
            approval.action_scope
            for approval in submission.approvals
            if approval.factory_id == snapshot.factory_id
            and approval.candidate_hash == candidate.content_hash
            and approval.binding == candidate.binding
            and approval.decision == "APPROVED"
            and approval.decided_at <= now < approval.expires_at
        }
        error = None
        if candidate.factory_id != snapshot.factory_id:
            error = "FACTORY_MISMATCH"
        elif (
            submission.expected_source_revision != snapshot.source.source_revision
            or submission.expected_snapshot_hash != snapshot.content_hash
            or submission.expected_active_plan_version != snapshot.active_plan_version
            or (
                submission.certificate is None
                and candidate.binding.snapshot_hash != snapshot.content_hash
            )
        ):
            error = "SOURCE_CONDITIONS_CHANGED"
        elif not required <= scopes:
            error = "APPROVAL_REQUIRED"
        elif not (
            candidate.effective_not_before <= snapshot.snapshot_clock < candidate.accept_before
        ):
            error = "EXECUTION_TIME_CHANGED"
        elif submission.certificate is not None:
            from services.factory_sim.revalidation import check_source_certificate

            try:
                check_source_certificate(db, submission, snapshot, baseline)
            except (ValueError, KeyError) as exc:
                error = getattr(exc, "code", "VALIDATION_EVIDENCE_INVALID")
        elif (
            check_candidate(
                snapshot,
                candidate,
                baseline=baseline,
                allow_overtime="allow_overtime" in required,
                objective=submission.objective,
            ).status
            != "PASS"
        ):
            error = "CHECK_FAILED"
        now = datetime.now(UTC)
        if not error:
            if any(not a.decided_at <= now < a.expires_at for a in submission.approvals):
                error = "APPROVAL_REQUIRED"
            elif submission.certificate and not (
                submission.certificate.issued_at <= now < submission.certificate.expires_at
            ):
                error = "VALIDATION_EXPIRED"
        receipt_id = str(uuid4())
        version = None if error else f"src:{world.run_id}:{world.revision + 1}"
        receipt = ActionReceipt(
            operation_id=submission.operation_id,
            factory_id=world.factory_id,
            run_id=world.run_id,
            receipt_id=receipt_id,
            candidate_hash=str(candidate.content_hash),
            source_state="REJECTED" if error else "ACTIVE",
            plan_version=version,
            effective_at=None if error else snapshot.snapshot_clock,
            recorded_at=now,
            error_code=error,
        )
        if not error:
            after = evolve(
                snapshot, active_plan_version=version, active_plan_hash=candidate.content_hash
            )
            world.active_candidate = candidate.model_dump(mode="json")
            _write(db, world, snapshot, after, "plan.activated")
        _record(
            db,
            world,
            submission.operation_id,
            "plan.submit",
            request,
            receipt.model_dump(mode="json"),
        )
        return receipt


def query_action(
    engine: Engine, factory_id: str, run_id: str, operation_id: str
) -> ActionReceipt | None:
    with Session(engine) as db:
        row = db.scalar(
            select(SourceAction).where(
                SourceAction.factory_id == factory_id,
                SourceAction.run_id == run_id,
                SourceAction.operation_id == operation_id,
                SourceAction.kind == "plan.submit",
            )
        )
        return ActionReceipt.model_validate(row.result) if row else None


def command(engine: Engine, factory_id: str, body: SimulatorCommand) -> dict:
    request = body.model_dump(mode="json")
    with Session(engine) as db, db.begin():
        world = _locked(db, factory_id)
        if world.run_id != body.run_id:
            raise SimulationError("SOURCE_RUN_CHANGED")
        prior = _prior(db, world, body.request_id, canonical_hash(request), body.kind)
        if prior:
            return prior
        snapshot = Snapshot.model_validate(world.document)
        if body.kind == "clock.step":
            if world.mode != "PAUSED":
                raise SimulationError("PAUSE_BEFORE_SINGLE_STEP")
            step = ClockStep.model_validate(body.payload)
            if world.replay_state is not None:
                from services.factory_sim.replay import replay_tick

                for _ in range(step.minutes):
                    if replay_tick(db, world) is None:
                        break
                snapshot = Snapshot.model_validate(world.document)
            else:
                plan = (
                    Candidate.model_validate(world.active_candidate)
                    if world.active_candidate
                    else None
                )
                for _ in range(step.minutes):
                    after = advance(snapshot, plan)
                    _write(db, world, snapshot, after, "clock.tick", plan=plan)
                    snapshot = after
        elif body.kind == "clock.run":
            speed = RunSpeed.model_validate(body.payload)
            if world.replay_state is not None and (
                world.replay_state.get("done") or world.replay_state.get("error_code")
            ):
                raise SimulationError("REPLAY_FINISHED")
            world.mode, world.interval_ms = "RUNNING", speed.interval_ms
            world.next_tick_at = datetime.now(UTC) + timedelta(milliseconds=speed.interval_ms)
        elif body.kind == "scenario.configure":
            if world.replay_state is not None:
                raise SimulationError("REPLAY_READ_ONLY")
            config = ScenarioConfiguration.model_validate(body.payload)
            world.scenario_state = {
                **config.model_dump(),
                "counter": 0,
                "next_at": (
                    snapshot.snapshot_clock + timedelta(minutes=config.every_minutes)
                ).isoformat(),
            }
        elif body.kind == "clock.pause":
            if body.payload:
                raise SimulationError("INVALID_CONTROL_PAYLOAD")
            world.mode, world.next_tick_at = "PAUSED", None
        else:
            if world.replay_state is not None:
                raise SimulationError("REPLAY_READ_ONLY")
            after = inject(snapshot, event_id=body.request_id, kind=body.kind, payload=body.payload)
            _write(db, world, snapshot, after, body.kind)
            snapshot = after
        result = {
            "factory_id": factory_id,
            "run_id": world.run_id,
            "request_id": body.request_id,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_hash": snapshot.content_hash,
            "source_revision": snapshot.source.source_revision,
            "business_clock": snapshot.snapshot_clock.isoformat(),
            "mode": world.mode,
        }
        _record(db, world, body.request_id, body.kind, request, result)
        return result


def cancel_treatment_command(engine: Engine, factory_id: str, body: SimulatorCommand) -> dict:
    """Serialize cancellation with execution, retaining a tombstone for late requests."""
    if body.kind != "treatment.apply":
        raise SimulationError("UNSUPPORTED_CANCELLATION")
    request = body.model_dump(mode="json")
    with Session(engine) as db, db.begin():
        world = _locked(db, factory_id)
        if world.run_id != body.run_id:
            raise SimulationError("SOURCE_RUN_CHANGED")
        prior = _prior(db, world, body.request_id, canonical_hash(request), body.kind)
        if prior:
            return prior
        result = {
            "factory_id": factory_id,
            "run_id": body.run_id,
            "request_id": body.request_id,
            "cancelled": True,
        }
        _record(db, world, body.request_id, body.kind, request, result)
        return result


def run_due_tick(engine: Engine) -> bool:
    """A row lock serializes controls and execution, including concurrent simulator workers."""
    now = datetime.now(UTC)
    with Session(engine) as db, db.begin():
        world = db.scalar(
            select(World)
            .where(World.mode == "RUNNING", World.next_tick_at <= now)
            .order_by(World.next_tick_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if world is None:
            return False
        snapshot = Snapshot.model_validate(world.document)
        if world.replay_state is not None:
            from services.factory_sim.replay import replay_tick

            after = replay_tick(db, world)
            if after is None:
                world.mode, world.next_tick_at = "PAUSED", None
            elif world.mode == "RUNNING":
                world.next_tick_at = now + timedelta(milliseconds=world.interval_ms)
            return True
        if snapshot.snapshot_clock >= snapshot.horizon.end_at:
            world.mode, world.next_tick_at = "PAUSED", None
            return True
        plan = Candidate.model_validate(world.active_candidate) if world.active_candidate else None
        after = advance(snapshot, plan)
        _write(db, world, snapshot, after, "clock.tick", plan=plan)
        scenario = world.scenario_state
        if (
            scenario
            and scenario.get("enabled")
            and after.snapshot_clock >= datetime.fromisoformat(scenario["next_at"])
        ):
            from random import Random

            rng = Random(f"{scenario['seed']}:{scenario['counter']}")
            available = sorted(
                (
                    r.resource_id
                    for r in after.resources
                    if r.status == "AVAILABLE"
                    and not any(
                        w.start_at < after.snapshot_clock + timedelta(minutes=30)
                        and after.snapshot_clock < w.end_at
                        for w in r.unavailable
                    )
                ),
            )
            if available:
                resource_id = rng.choice(available)
                payload = {"resource_id": resource_id, "minutes": rng.choice((15, 20, 30))}
                event_id = "scenario:" + canonical_hash(
                    {
                        "run_id": world.run_id,
                        "seed": scenario["seed"],
                        "counter": scenario["counter"],
                        "clock": after.snapshot_clock.isoformat(),
                    }
                )
                interrupted = inject(
                    after, event_id=event_id, kind="resource.outage", payload=payload
                )
                _write(db, world, after, interrupted, "resource.outage")
                _record(
                    db,
                    world,
                    event_id,
                    "resource.outage",
                    SimulatorCommand(
                        request_id=event_id,
                        run_id=world.run_id,
                        kind="resource.outage",
                        payload=payload,
                    ).model_dump(mode="json"),
                    {
                        "resource_id": resource_id,
                        "minutes": payload["minutes"],
                        "random": True,
                        "source_revision": interrupted.source.source_revision,
                    },
                )
            world.scenario_state = {
                **scenario,
                "counter": scenario["counter"] + 1,
                "next_at": (
                    after.snapshot_clock + timedelta(minutes=scenario["every_minutes"])
                ).isoformat(),
            }
        world.next_tick_at = now + timedelta(milliseconds=world.interval_ms)
        return True


def changes(engine: Engine, factory_id: str, run_id: str, after: int, limit: int = 100) -> dict:
    with Session(engine) as db:
        world = db.get(World, factory_id)
        if world is None or world.run_id != run_id:
            raise SimulationError("SOURCE_RUN_CHANGED")
        rows = db.scalars(
            select(SourceChange)
            .where(
                SourceChange.factory_id == factory_id,
                SourceChange.run_id == run_id,
                SourceChange.revision > after,
                SourceChange.revision <= world.revision,
            )
            .order_by(SourceChange.revision)
            .limit(limit)
        ).all()
        return {
            "factory_id": factory_id,
            "run_id": run_id,
            "watermark": str(world.revision),
            "next_cursor": str(rows[-1].revision) if rows else str(after),
            "has_more": bool(rows and rows[-1].revision < world.revision),
            "changes": [row.document for row in rows],
        }
