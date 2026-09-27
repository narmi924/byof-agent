"""Workspace/API projection of real domain surplus, with read-only storage doubles.

The source transitions and quality rules are real; no PostgreSQL transaction or model
call is simulated as passing here. The test never connects to a database.
"""

from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_demand_changes import change, initial

from packages.auth import Grant, Principal
from packages.domain.demand import finished_goods
from packages.domain.models import Snapshot
from packages.planning import preferences, revalidation, reviews, service
from packages.planning.solver import solve
from packages.planning.store import FactoryState, SnapshotRecord
from services.api import planning
from services.factory_sim.engine import advance, evolve


@pytest.fixture(scope="module")
def demand_states():
    source, baseline = initial()
    started = advance(source, baseline)
    cancelled = change(started, 0, command="order.revise")
    candidate = solve(cancelled, baseline=baseline, time_limit=3)
    assert candidate.checker.status == "PASS"
    accepted = evolve(
        cancelled, active_plan_version="stock-plan", active_plan_hash=candidate.content_hash
    )
    completed = advance(accepted, candidate, minutes=150)
    restored = change(completed, 50, event="restore", command="order.revise")
    assert len(finished_goods(completed)) == 1
    return source, cancelled, completed, restored


class EmptyRows(list):
    def all(self):
        return self


def source_storage(monkeypatch, snapshot):
    original = snapshot.model_dump(mode="json") if snapshot else None
    state = (
        SimpleNamespace(
            snapshot_id=snapshot.snapshot_id,
            last_synced_at=datetime.now(UTC),
            connector_capabilities=None,
            capabilities_observed_at=None,
        )
        if snapshot
        else None
    )

    class ReadSession:
        def get(self, kind, identifier):
            if kind is FactoryState:
                return state
            assert kind is SnapshotRecord and snapshot is not None
            assert identifier == snapshot.snapshot_id
            return SimpleNamespace(document=original)

        def scalars(self, _statement):
            return EmptyRows()

    db = ReadSession()
    monkeypatch.setattr(service, "Session", lambda _engine: nullcontext(db))
    # This projection fixture supplies no candidate repository; scheduling/plan
    # comparisons have separate PostgreSQL coverage.
    monkeypatch.setattr(service, "active_baseline", lambda *_: None)
    monkeypatch.setattr(preferences, "effective_view", lambda *_: {})
    monkeypatch.setattr(preferences, "objective_view", lambda *_: {})
    monkeypatch.setattr(revalidation, "certificates", lambda *_: [])
    monkeypatch.setattr(reviews, "reviews", lambda *_: [])
    return original


@pytest.mark.parametrize("index", range(4))
def test_workspace_uses_same_snapshot_for_stock_and_distinguishes_restored_demand(
    monkeypatch, demand_states, index
):
    snapshot = demand_states[index]
    original = source_storage(monkeypatch, snapshot)
    result = service.workspace(None, snapshot.factory_id)
    assert result["snapshot"].model_dump(mode="json") == original
    assert result["snapshot"].content_hash == snapshot.content_hash
    assert "finished_goods" not in result["snapshot"].model_dump()
    assert result["finished_goods"] == [
        lot.model_dump(mode="json") for lot in finished_goods(snapshot)
    ]
    assert bool(result["finished_goods"]) is (index == 2)
    if index == 2:
        assert result["finished_goods"][0]["quantity"] == 50
    assert snapshot.model_dump(mode="json") == original


@pytest.mark.parametrize("quality", ["PENDING", "UNKNOWN", "FAILED"])
def test_workspace_does_not_treat_unqualified_completed_stock_as_available(
    monkeypatch, demand_states, quality
):
    data = demand_states[2].model_dump(exclude={"content_hash"})
    data["actuals"][0]["quality_state"] = quality
    snapshot = Snapshot.model_validate(data)
    source_storage(monkeypatch, snapshot)
    assert service.workspace(None, snapshot.factory_id)["finished_goods"] == []


def test_workspace_without_synced_snapshot_reports_unknown_stock(monkeypatch):
    source_storage(monkeypatch, None)
    result = service.workspace(None, "not-synced")
    assert result["snapshot"] is None
    assert result["finished_goods"] is None


@pytest.mark.parametrize("role", ["manager", "sim_admin"])
def test_workspace_http_serializes_stock_for_both_existing_read_roles(
    monkeypatch, demand_states, role
):
    snapshot = demand_states[2]
    original = source_storage(monkeypatch, snapshot)
    actor = Principal(
        user_id=role,
        username=role,
        grants=(Grant(factory_id=snapshot.factory_id, role=role),),
    )
    monkeypatch.setattr(planning, "principal", lambda _request: actor)
    monkeypatch.setattr(planning, "publications", lambda *_, candidate_ids=None: [])
    app = FastAPI()
    app.state.engine = None
    app.include_router(planning.router)
    with TestClient(app) as client:
        response = client.get(f"/api/factories/{snapshot.factory_id}/workspace")
    assert response.status_code == 200
    payload = response.json()
    assert payload["snapshot"] == original
    assert payload["finished_goods"] == [
        lot.model_dump(mode="json") for lot in finished_goods(snapshot)
    ]
