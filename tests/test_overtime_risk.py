"""Withdrawn overtime is relevant only when the current active plan needs that window."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_source_business_controls import apply, overtime_only_source

from packages.agent.impact import classify_events
from packages.agent.risk_suggestions import _risk_fact
from packages.domain.models import Event, Snapshot
from packages.planning.solver import solve
from services.factory_sim.engine import advance, evolve
from services.factory_sim.service import _write


def source_events(before, after):
    records = []
    _write(
        SimpleNamespace(add=records.append),
        SimpleNamespace(factory_id=before.factory_id),
        before,
        after,
        "overtime_window.set",
    )
    return tuple(Event.model_validate(item) for item in records[0].document["events"])


@pytest.fixture
def planned():
    source = overtime_only_source()
    baseline = solve(source, time_limit=2, allow_overtime=True)
    assert baseline.has_solution and baseline.checker.status == "PASS"
    assert "allow_overtime" in baseline.required_consents
    source = evolve(
        source, active_plan_version="approved-test-plan", active_plan_hash=baseline.content_hash
    )
    return source, baseline


def withdrawal(source, baseline, target_type):
    key = f"{target_type}_id"
    target_id = getattr(baseline.assignments[0], key)
    row = next(
        item for item in getattr(source, f"{target_type}s") if getattr(item, key) == target_id
    )
    window = next(item for item in row.calendar if item.kind == "OVERTIME")
    body = {
        "target_type": target_type,
        "target_id": target_id,
        "expected_version": row.version,
        "action": "remove",
        "start_at": window.start_at.isoformat(),
        "end_at": window.end_at.isoformat(),
    }
    after = apply(source, "overtime_window.set", body)
    return after, source_events(source, after)[0], body


@pytest.mark.parametrize("target_type", ["worker", "resource"])
def test_withdrawal_of_planned_window_crosses_threshold_without_claiming_a_solved_delay(
    planned, target_type
):
    source, baseline = planned
    changed, event, _ = withdrawal(source, baseline, target_type)
    fact = _risk_fact(changed, event, baseline)
    assert "overtime window revoked" in fact and "unfinished operation" in fact
    assert "delay" not in fact
    assert "EVENT_SNAPSHOT_MISMATCH" not in classify_events((event,), changed)["reasons"]
    assert _risk_fact(changed, event, None) is None
    inactive = evolve(changed, active_plan_hash=None, active_plan_version=None)
    assert _risk_fact(inactive, event, baseline) is None


def test_added_or_unused_window_has_no_risk_and_restored_capacity_clears_stale_risk(planned):
    source, baseline = planned
    removed, event, body = withdrawal(source, baseline, "worker")
    restored = apply(
        source=removed,
        kind="overtime_window.set",
        payload={
            **body,
            "action": "add",
            "expected_version": body["expected_version"] + 1,
        },
    )
    assert _risk_fact(restored, event, baseline) is None
    assert _risk_fact(restored, source_events(removed, restored)[0], baseline) is None
    later_start = source.snapshot_clock + timedelta(minutes=130)
    extra = apply(
        source,
        "overtime_window.set",
        {
            **body,
            "action": "add",
            "start_at": later_start.isoformat(),
            "end_at": (later_start + timedelta(minutes=30)).isoformat(),
        },
    )
    assert _risk_fact(extra, source_events(source, extra)[0], baseline) is None
    removed_extra = apply(
        extra,
        "overtime_window.set",
        {
            **body,
            "action": "remove",
            "expected_version": body["expected_version"] + 1,
            "start_at": later_start.isoformat(),
            "end_at": (later_start + timedelta(minutes=30)).isoformat(),
        },
    )
    assert _risk_fact(removed_extra, source_events(extra, removed_extra)[0], baseline) is None


def test_old_malformed_or_completed_window_evidence_is_not_a_current_risk(planned):
    source, baseline = planned
    changed, event, _ = withdrawal(source, baseline, "worker")
    for bad in (None, "not-json", "[]"):
        malformed = event.model_copy(
            update={
                "changes": tuple(
                    item.model_copy(update={"after": bad}) if item.field == "calendar" else item
                    for item in event.changes
                )
            }
        )
        assert _risk_fact(changed, malformed, baseline) is None
    completed = advance(source, baseline, minutes=120)
    assert all(item.state == "COMPLETED" for item in completed.actuals)
    data = changed.model_dump(exclude={"content_hash"})
    data.update(
        actuals=completed.actuals,
        snapshot_clock=completed.snapshot_clock,
        inventory=completed.inventory,
        reservations=completed.reservations,
        orders=completed.orders,
        production_batches=completed.production_batches,
    )
    finished = Snapshot.model_validate(data)
    assert _risk_fact(finished, event, baseline) is None


@pytest.mark.parametrize("target_type", ["worker", "resource"])
def test_unrelated_later_capacity_does_not_clear_an_unresolved_withdrawal(planned, target_type):
    source, baseline = planned
    removed, event, body = withdrawal(source, baseline, target_type)
    later_start = source.snapshot_clock + timedelta(minutes=130)
    unrelated = apply(
        removed,
        "overtime_window.set",
        {
            **body,
            "action": "add",
            "expected_version": body["expected_version"] + 1,
            "start_at": later_start.isoformat(),
            "end_at": (later_start + timedelta(minutes=30)).isoformat(),
        },
    )
    assert _risk_fact(unrelated, event, baseline) == _risk_fact(removed, event, baseline)
    assert _risk_fact(unrelated, source_events(removed, unrelated)[0], baseline) is None
    restored = apply(
        unrelated,
        "overtime_window.set",
        {
            **body,
            "action": "add",
            "expected_version": body["expected_version"] + 2,
        },
    )
    assert _risk_fact(restored, event, baseline) is None
