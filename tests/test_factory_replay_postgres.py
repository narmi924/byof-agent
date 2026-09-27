"""Private historical replay preserves physical outcomes and original PostgreSQL records."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_business_acceptance import command as business_command
from test_business_acceptance import source_with_terms
from test_dynamic_factory_postgres import (
    control,
    plan_input,
    send_plan,
    snapshot,
)
from test_dynamic_factory_postgres import (
    dynamic_source as dynamic_source,
)

from packages.domain.business_acceptance import BusinessAcceptance
from packages.domain.models import Approval, Candidate, Snapshot, canonical_hash
from packages.persistence import connect
from packages.planning.solver import solve
from services.factory_sim.engine import SimulationError
from services.factory_sim.replay import replay_tick, start_replay
from services.factory_sim.storage import SourceAction, SourceChange, SourceRun, World


def stored(engine, factory_id):
    with Session(engine) as db:
        world = db.get(World, factory_id)
        return Snapshot.model_validate(world.document), dict(world.replay_state or {}), world.mode


def tick(engine, factory_id):
    with Session(engine) as db, db.begin():
        world = db.scalar(select(World).where(World.factory_id == factory_id).with_for_update())
        return replay_tick(db, world)


def original_rows(engine, run_id):
    with Session(engine) as db:
        initial = db.get(SourceRun, run_id).initial_snapshot
        changes = [
            r.document
            for r in db.scalars(
                select(SourceChange)
                .where(SourceChange.run_id == run_id)
                .order_by(SourceChange.revision)
            )
        ]
        actions = [
            (r.action_id, r.operation_id, r.kind, r.payload_hash, r.request, r.result)
            for r in db.scalars(
                select(SourceAction)
                .where(SourceAction.run_id == run_id)
                .order_by(SourceAction.action_id)
            )
        ]
        return initial, changes, actions


def finish(engine, factory_id, limit=30):
    for _ in range(limit):
        current, state, _ = stored(engine, factory_id)
        assert state["error_code"] is None, state["error_code"]
        if state["done"]:
            return current, state
        assert tick(engine, factory_id) is not None
    raise AssertionError("Replay failed to finish within its recorded event count")


@pytest.fixture
def business_source(dynamic_source):
    """Give this isolated source its commercial facts before recording any actions."""
    client, tokens, initial, app_engine, engine = dynamic_source
    raw = source_with_terms().model_dump(mode="python", exclude={"content_hash"})
    raw.update(
        factory_id=initial.factory_id,
        run_id=initial.run_id,
        snapshot_id=initial.snapshot_id,
        source=initial.source.model_dump(mode="python"),
    )
    raw["profile"]["factory_id"] = initial.factory_id
    configured = Snapshot.model_validate(raw)
    with Session(engine) as db, db.begin():
        world = db.get(World, initial.factory_id)
        assert world.revision == 1 and world.active_candidate is None
        assert db.get(SourceRun, initial.run_id) is None
        world.document = configured.model_dump(mode="json")
        db.add(
            SourceRun(
                run_id=initial.run_id,
                factory_id=initial.factory_id,
                initial_snapshot=world.document,
                created_at=datetime.now(UTC),
            )
        )
    return client, tokens, configured, app_engine, engine


@pytest.mark.parametrize("dynamic_source", [{"risk_fixture": True}], indirect=True)
@pytest.mark.parametrize("kind", ["order.change", "order.revise"])
@pytest.mark.parametrize("started", [False, True])
def test_demand_cancellation_replay_preserves_lots_but_uses_new_event_provenance(
    dynamic_source, kind, started
):
    source = dynamic_source
    engine, initial = source[4], source[2]
    assert send_plan(source, plan_input(source)).json()["source_state"] == "ACTIVE"
    if started:
        assert control(source, "start-first-lot", "clock.step").status_code == 200
    order = snapshot(source).orders[0]
    payload = {"order_id": order.order_id, "expected_version": order.version, "quantity": 0}
    if kind == "order.revise":
        payload.update(
            due_at=order.due_at.isoformat(),
            priority_weight=order.priority_weight,
            hard_deadline=order.hard_deadline,
        )
    response = control(source, "cancel-demand", kind, payload)
    assert response.status_code == 200, response.text
    assert control(source, "continue-after-cancellation", "clock.step").status_code == 200
    expected = snapshot(source)
    assert expected.orders[0].status == "CANCELLED" and expected.orders[0].quantity == 0
    assert [batch.purpose for batch in expected.production_batches] == (
        ["STOCK", "CANCELLED", "CANCELLED"] if started else ["CANCELLED"] * 3
    )
    rows = original_rows(engine, initial.run_id)

    start_replay(engine, initial.factory_id, "replay-cancel-demand", initial.run_id)
    finished, state = finish(engine, initial.factory_id)

    assert state["physical_match"] is True
    assert finished.orders == expected.orders and finished.inventory == expected.inventory
    assert [
        batch.model_dump(exclude={"source_event_id"}) for batch in finished.production_batches
    ] == [batch.model_dump(exclude={"source_event_id"}) for batch in expected.production_batches]
    assert all(
        replayed.source_event_id != original.source_event_id
        for replayed, original in zip(
            finished.production_batches, expected.production_batches, strict=True
        )
    )
    assert len(finished.actuals) == int(started)
    assert original_rows(engine, initial.run_id) == rows


def test_business_agreement_replay_rebinds_origin_hash_and_preserves_partial_promises(
    business_source,
):
    source = business_source
    engine, initial = source[4], source[2]
    payload = BusinessAcceptance.model_validate(
        business_command(initial, include_order=True, include_quote=True)
    ).model_dump(mode="json")
    response = control(source, "accept-business", "business.accept", payload)
    assert response.status_code == 200, response.text
    assert (
        control(source, "receive-expedited-material", "clock.step", {"minutes": 10}).status_code
        == 200
    )
    expected = snapshot(source)
    assert expected.receipts[0].status == "RECEIVED"
    assert expected.receipts[0].eta == initial.business_terms.expedite_quotes[0].expedited_eta
    rows = original_rows(engine, initial.run_id)

    with (
        patch("packages.planning.solver.solve", side_effect=AssertionError("Unexpected solve")),
        patch("httpx.Client.request", side_effect=AssertionError("Unexpected external HTTP")),
    ):
        start_replay(engine, initial.factory_id, "replay-business", initial.run_id)
        finished, state = finish(engine, initial.factory_id)

    assert state["physical_match"] is True
    assert finished.orders == expected.orders
    assert finished.receipts == expected.receipts and finished.inventory == expected.inventory
    lots = [batch for batch in finished.production_batches if batch.order_id == "BIZ-URGENT"]
    assert [batch.quantity for batch in lots] == [50, 50]
    assert [batch.delivery_due_at for batch in lots] == [
        datetime.fromisoformat(payload["first_delivery_due_at"]),
        datetime.fromisoformat(payload["final_delivery_due_at"]),
    ]
    assert all(batch.source_event_id.startswith("replay:") for batch in lots)
    assert finished.active_plan_version is None and finished.actuals == ()
    assert original_rows(engine, initial.run_id) == rows
    with Session(engine) as db:
        original = db.scalar(
            select(SourceAction).where(
                SourceAction.run_id == initial.run_id, SourceAction.kind == "business.accept"
            )
        )
        assert original.request["payload"]["expected_snapshot_hash"] == initial.content_hash
        assert original.request["payload"]["expected_snapshot_hash"] != finished.content_hash


def test_business_replay_does_not_rebind_a_forged_origin_snapshot_hash(business_source):
    source = business_source
    engine, initial = source[4], source[2]
    payload = business_command(initial, include_quote=True)
    response = control(source, "accept-business", "business.accept", payload)
    assert response.status_code == 200, response.text
    # The runtime role intentionally cannot edit historical actions. Only the
    # isolated test owner can construct a corrupt persisted record for this check.
    owner = connect(os.environ["TEST_MIGRATION_DATABASE_URL"])
    assert owner.url.database == "byof_test"
    try:
        with Session(owner) as db, db.begin():
            action = db.scalar(
                select(SourceAction).where(
                    SourceAction.run_id == initial.run_id, SourceAction.kind == "business.accept"
                )
            )
            # A self-consistent record hash must not authorize an unrelated source snapshot.
            action.request = {
                **action.request,
                "payload": {**action.request["payload"], "expected_snapshot_hash": "f" * 64},
            }
            action.payload_hash = canonical_hash(action.request)
    finally:
        owner.dispose()
    rows = original_rows(engine, initial.run_id)
    start_replay(engine, initial.factory_id, "replay-forged-business", initial.run_id)
    before, pending, _ = stored(engine, initial.factory_id)

    assert tick(engine, initial.factory_id) is None

    after, failed, mode = stored(engine, initial.factory_id)
    assert after == before and mode == "PAUSED"
    assert failed["next_revision"] == pending["next_revision"]
    assert failed["error_code"] == "REPLAY_COMMAND_ORIGIN_MISMATCH"
    assert failed["done"] is False and "physical_match" not in failed
    assert original_rows(engine, initial.run_id) == rows


def test_plan_and_production_replay_is_idempotent_private_and_preserves_original_rows(
    dynamic_source,
):
    source = dynamic_source
    engine, initial = source[4], source[2]
    payload = plan_input(source)
    assert send_plan(source, payload).json()["source_state"] == "ACTIVE"
    assert control(source, "produce-two-minutes", "clock.step", {"minutes": 2}).status_code == 200
    expected = snapshot(source)
    original = original_rows(engine, initial.run_id)

    # Replaying records cannot obtain new plans or make network/model calls.
    with (
        patch("packages.planning.solver.solve", side_effect=AssertionError("Unexpected solve")),
        patch("httpx.Client.request", side_effect=AssertionError("Unexpected external HTTP")),
    ):
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(
                pool.map(
                    lambda _: start_replay(
                        engine, initial.factory_id, "replay-request", initial.run_id
                    ),
                    range(2),
                )
            )
        assert receipts[0] == receipts[1]
        assert receipts[0]["run_id"] != initial.run_id
        replay, state, mode = stored(engine, initial.factory_id)
        assert mode == "PAUSED" and state["next_revision"] == 2
        assert replay.snapshot_clock == initial.snapshot_clock and replay.actuals == ()
        assert replay.source.source_system == "factory-simulator-replay"
        assert state["external_actions_enabled"] is False
        finished, state = finish(engine, initial.factory_id)
        assert tick(engine, initial.factory_id) is None

    assert state["physical_match"] is True
    assert finished.snapshot_clock == expected.snapshot_clock
    assert finished.inventory == expected.inventory
    assert len(finished.actuals) == 1
    actual, prior = finished.actuals[0], expected.actuals[0]
    assert (actual.actual_start, actual.actual_end, actual.remaining_minutes) == (
        prior.actual_start,
        prior.actual_end,
        8,
    )
    assert [(s.phase, s.start_at, s.end_at) for s in actual.segments] == [
        (s.phase, s.start_at, s.end_at) for s in prior.segments
    ]
    assert finished.active_plan_hash != expected.active_plan_hash
    assert original_rows(engine, initial.run_id) == original
    with Session(engine) as db:
        run = db.get(SourceRun, finished.run_id)
        assert run.replay_of == initial.run_id
        evidence = db.scalar(
            select(SourceAction).where(
                SourceAction.run_id == finished.run_id, SourceAction.kind == "replay.plan_reused"
            )
        )
        assert evidence.request["origin_candidate_hash"] == payload["candidate"]["content_hash"]
        assert evidence.result["new_solver_calls"] == evidence.result["new_human_approvals"] == 0
        assert evidence.result["checker_status"] == "PASS"
        assert evidence.result["solver_evidence"] == "REUSED_FROM_ORIGIN"
        assert "approvals" not in evidence.request
    # Returning an old operation receipt never rewinds the current world again.
    assert start_replay(engine, initial.factory_id, "replay-request", initial.run_id) == receipts[0]
    assert stored(engine, initial.factory_id)[0] == finished
    with pytest.raises(SimulationError, match="IDEMPOTENCY_CONFLICT"):
        start_replay(engine, initial.factory_id, "replay-request", finished.run_id)
    client = source[0]
    visible = client.get("/factory/v1/snapshot", params={"factory_id": initial.factory_id}).json()
    assert "replay_state" not in visible and "expected_final_snapshot" not in visible
    assert client.get(f"/simulator/v1/factories/{initial.factory_id}").status_code == 403


def test_unknown_downtime_confirmation_and_resumption_replay_exact_physical_history(dynamic_source):
    source = dynamic_source
    engine, initial = source[4], source[2]
    assert send_plan(source, plan_input(source)).json()["source_state"] == "ACTIVE"
    assert control(source, "work", "clock.step").status_code == 200
    running = snapshot(source).actuals[0]
    assert (
        control(source, "down", "resource.down", {"resource_id": running.resource_id}).status_code
        == 200
    )
    assert control(source, "wait", "clock.step", {"minutes": 2}).status_code == 200
    assert snapshot(source).actuals[0].remaining_minutes is None
    assert (
        control(
            source, "restore", "resource.restore", {"resource_id": running.resource_id}
        ).status_code
        == 200
    )
    assert (
        control(
            source,
            "confirm",
            "execution.confirm_remaining",
            {
                "operation_id": running.operation_id,
                "remaining_minutes": running.remaining_minutes,
                "remaining_setup_minutes": 0,
            },
        ).status_code
        == 200
    )
    assert control(source, "resume", "clock.step").status_code == 200
    expected = snapshot(source)
    original = original_rows(engine, initial.run_id)
    start_replay(engine, initial.factory_id, "replay-downtime", initial.run_id)
    assert tick(engine, initial.factory_id) is not None  # Recorded plan acceptance.
    assert tick(engine, initial.factory_id) is not None  # Actual first production minute.
    blocked = tick(engine, initial.factory_id)
    assert blocked.actuals[0].state == "BLOCKED"
    assert blocked.actuals[0].remaining_minutes is None
    finished, state = finish(engine, initial.factory_id)
    actual = finished.actuals[0]
    assert state["physical_match"] and actual.state == "IN_PROGRESS"
    assert actual.actual_start == running.actual_start
    assert actual.remaining_minutes == running.remaining_minutes - 1
    assert len(actual.segments) == 2
    assert sum((s.end_at - s.start_at for s in actual.segments), timedelta()) == timedelta(
        minutes=2
    )
    assert finished.inventory == expected.inventory
    assert [(r.batch_id, r.material_id, r.quantity) for r in finished.reservations] == [
        (r.batch_id, r.material_id, r.quantity) for r in expected.reservations
    ]
    assert original_rows(engine, initial.run_id) == original


def test_missing_record_pauses_without_advancing_or_skipping_cursor(dynamic_source):
    source = dynamic_source
    engine, initial = source[4], source[2]
    assert control(source, "single-tick", "clock.step").status_code == 200
    start_replay(engine, initial.factory_id, "replay-missing-record", initial.run_id)
    before, state, _ = stored(engine, initial.factory_id)
    get = Session.get

    def missing_change(self, entity, ident, *args, **kwargs):
        if entity is SourceChange:
            return None
        return get(self, entity, ident, *args, **kwargs)

    with patch.object(Session, "get", missing_change):
        assert tick(engine, initial.factory_id) is None
    after, failed, mode = stored(engine, initial.factory_id)
    assert after == before and failed["next_revision"] == state["next_revision"]
    assert failed["error_code"] == "REPLAY_CHANGE_MISSING" and mode == "PAUSED"
    assert failed["done"] is False and tick(engine, initial.factory_id) is None


def test_initial_execution_is_rejected_and_does_not_clear_live_state(dynamic_source):
    source = dynamic_source
    engine, initial = source[4], source[2]
    assert send_plan(source, plan_input(source)).json()["source_state"] == "ACTIVE"
    assert control(source, "single-tick", "clock.step").status_code == 200
    raw = snapshot(source).model_dump(mode="python", exclude={"content_hash"})
    imported_run = str(uuid4())
    raw.update(run_id=imported_run, snapshot_id=imported_run + "-imported")
    expected = Snapshot.model_validate(raw)
    # Import a separate already-running origin; existing immutable runs stay untouched.
    with Session(engine) as db, db.begin():
        world = db.get(World, initial.factory_id)
        world.run_id, world.document = imported_run, expected.model_dump(mode="json")
        db.add(
            SourceRun(
                run_id=imported_run,
                factory_id=initial.factory_id,
                initial_snapshot=world.document,
                created_at=datetime.now(UTC),
            )
        )
    with pytest.raises(SimulationError, match="REPLAY_INITIAL_STATE_UNSUPPORTED"):
        start_replay(engine, initial.factory_id, "unsupported-initial", imported_run)
    current, state, _ = stored(engine, initial.factory_id)
    assert current == expected and not state


def test_final_inventory_divergence_is_not_committed_as_successful_replay(dynamic_source):
    from services.factory_sim.engine import advance

    source = dynamic_source
    engine, initial = source[4], source[2]
    assert control(source, "single-tick", "clock.step").status_code == 200
    start_replay(engine, initial.factory_id, "replay-physical-divergence", initial.run_id)
    before, pending, _ = stored(engine, initial.factory_id)

    def changed_physics(state, plan):
        raw = advance(state, plan).model_dump(mode="python", exclude={"content_hash"})
        raw["inventory"][0]["on_hand"] += 1
        return Snapshot.model_validate(raw)

    with patch("services.factory_sim.replay.advance", changed_physics):
        assert tick(engine, initial.factory_id) is None
    after, failed, mode = stored(engine, initial.factory_id)
    assert after == before and mode == "PAUSED"
    assert failed["next_revision"] == pending["next_revision"]
    assert failed["error_code"] == "REPLAY_PHYSICAL_STATE_MISMATCH"
    assert failed["done"] is False and "physical_match" not in failed


def test_empty_run_replay_is_verified_and_cannot_be_replayed_recursively(dynamic_source):
    _, _, initial, _, engine = dynamic_source
    receipt = start_replay(engine, initial.factory_id, "empty-run", initial.run_id)
    _, state, mode = stored(engine, initial.factory_id)
    assert state["done"] and state["physical_match"] and mode == "PAUSED"
    assert tick(engine, initial.factory_id) is None
    with pytest.raises(SimulationError, match="REPLAY_INITIAL_STATE_UNSUPPORTED"):
        start_replay(engine, initial.factory_id, "nested-replay", receipt["run_id"])


def test_replanning_in_progress_rebinds_both_snapshot_and_accepted_baseline(dynamic_source):
    source = dynamic_source
    engine, initial = source[4], source[2]
    original = plan_input(source)
    assert send_plan(source, original).json()["source_state"] == "ACTIVE"
    assert control(source, "produce", "clock.step", {"minutes": 12}).status_code == 200
    current = snapshot(source)
    candidate = solve(
        current, baseline=Candidate.model_validate(original["candidate"]), time_limit=2
    )
    assert candidate.checker.status == "PASS"
    now = datetime.now(UTC)
    approval = Approval(
        approval_id="replan-approval",
        factory_id=initial.factory_id,
        candidate_hash=candidate.content_hash,
        binding=candidate.binding,
        approver_id="test-planner",
        approver_role="planner",
        action_scope="publish_plan",
        decision="APPROVED",
        decided_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    replanned = {
        "operation_id": "publish-replan",
        "factory_id": initial.factory_id,
        "run_id": current.run_id,
        "expected_source_revision": current.source.source_revision,
        "expected_snapshot_hash": current.content_hash,
        "expected_active_plan_version": current.active_plan_version,
        "candidate": candidate.model_dump(mode="json"),
        "approvals": [approval.model_dump(mode="json")],
    }
    assert send_plan(source, replanned).json()["source_state"] == "ACTIVE"
    assert control(source, "continued", "clock.step").status_code == 200
    expected = snapshot(source)
    rows = original_rows(engine, initial.run_id)
    start_replay(engine, initial.factory_id, "replay-replanned", initial.run_id)
    finished, state = finish(engine, initial.factory_id)
    assert state["physical_match"] and finished.inventory == expected.inventory
    assert [
        (a.operation_id, a.actual_start, a.actual_end, a.remaining_minutes)
        for a in finished.actuals
    ] == [
        (a.operation_id, a.actual_start, a.actual_end, a.remaining_minutes)
        for a in expected.actuals
    ]
    assert original_rows(engine, initial.run_id) == rows
    with Session(engine) as db:
        reused = list(
            db.scalars(
                select(SourceAction).where(
                    SourceAction.run_id == finished.run_id,
                    SourceAction.kind == "replay.plan_reused",
                )
            )
        )
        active = Candidate.model_validate(db.get(World, initial.factory_id).active_candidate)
        assert len(reused) == 2
        assert all(r.result["checker_status"] == "PASS" for r in reused)
        assert active.binding.baseline_plan_version.startswith("src:" + finished.run_id + ":")
        assert active.assignments == candidate.assignments
