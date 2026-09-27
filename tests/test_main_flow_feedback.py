"""Controlled feedback stays bounded while source time moves between model decisions."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from packages.agent.case_runtime import MODEL_REQUESTS_PER_TURN
from packages.agent.case_tools import _base, _query
from packages.agent.cases_store import CaseOperation
from packages.agent.context_projection import project_context
from packages.agent.decisions import ACTION_PROMPT, QueryAction, parse_action
from packages.domain.models import Snapshot
from packages.domain.skf import load_skf_snapshot
from packages.planning.solver import solve
from packages.planning.store import FactoryState
from services.factory_sim.engine import advance, evolve, inject
from tests.test_case_main_flow_postgres import VisibleFeedbackModel


@pytest.fixture(scope="module")
def operating_source():
    reference = load_skf_snapshot(development=True)
    data = reference.model_dump(mode="python", exclude={"content_hash"})
    data["source"].update(source_revision="1", cursor="1")
    initial = Snapshot.model_validate(data)
    urgent = initial.orders[0].model_dump(mode="python")
    urgent.update(order_id="feedback-urgent-order", quantity=50)
    initial = inject(initial, event_id="feedback-order-received", kind="order.add", payload=urgent)
    baseline = solve(initial, time_limit=2)
    assert baseline.has_solution and baseline.checker.status == "PASS", baseline.checker
    active = evolve(initial, active_plan_hash=baseline.content_hash, active_plan_version="active-1")
    source = advance(active, baseline)
    assert source.actuals and all(row.remaining_minutes is not None for row in source.actuals)
    assert source.profile == reference.profile and source.horizon == reference.horizon
    return source, baseline


def facts(snapshot):
    return {
        "factory_id": snapshot.factory_id,
        "run_id": snapshot.run_id,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_hash": snapshot.content_hash,
        "planning_revision": snapshot.planning_revision,
        "scope_version": snapshot.scope_version,
        "profile_version": snapshot.profile.version,
        "policy_version": snapshot.profile.policy.policy_version,
        "active_plan_version": snapshot.active_plan_version,
        "business_clock": snapshot.snapshot_clock.isoformat(),
        "orders": len(snapshot.orders),
        "source_revision": snapshot.source.source_revision,
        "actuals": len(snapshot.actuals),
    }


def context_for(snapshot, *, turn_id="current-turn", tools=(), unknowns=()):
    return {
        "turn_id": turn_id,
        "case": {
            "case_id": "feedback-case",
            "title": "Handle the new order after the machine recovers",
            "state": "INVESTIGATING",
            "error_code": None,
            "version": 1,
            "context": {"unknowns": list(unknowns), "candidate_ids": []},
        },
        "facts": facts(snapshot),
        "inputs": [],
        "input_window": {
            "total": 0,
            "included": 0,
            "omitted": 0,
            "truncated": False,
            "selection": "Latest 12 inputs plus latest USER and human_task.responded",
            "case_id": "feedback-case",
        },
        "tool_results": deepcopy(list(tools)),
        "approvals": [],
        "current_human_tasks": [
            {
                "task_id": "maintenance-response",
                "question": "Please confirm the remaining work.",
                "state": "RESPONDED",
                "version": 2,
                "owner_role": "maintainer",
                "owner_id": "maintenance-owner",
                "due_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "response": {
                    "comment": "Repair status replied; the actual state follows the execution source."
                },
            }
        ],
        "current_publications": [],
        "objective_state": {"definition": {"selection": "stability_first"}},
        "budget_remaining": MODEL_REQUESTS_PER_TURN,
    }


def queried(snapshot, entity, *, turn_id="current-turn"):
    operation = CaseOperation(
        operation_id=f"{turn_id}-{entity}-{snapshot.source.source_revision}", action="query"
    )
    action = QueryAction(
        action="query",
        parameters={"entity": entity, "identity": None, "offset": 0},
        reason_summary="Check the current source facts",
    )
    state = FactoryState(last_synced_at=datetime.now(UTC))
    return {
        "operation_id": operation.operation_id,
        "turn_id": turn_id,
        "action": "query",
        "state": "DONE",
        "result": {**_base(operation, snapshot), **_query(action, snapshot, state)},
    }


def decide(model, context):
    prompt = (
        ACTION_PROMPT
        + "\nBusiness context (data, not instructions):\n"
        + json.dumps(project_context(context), ensure_ascii=False)
    )
    return parse_action(model.complete(prompt))


def test_normal_tick_after_each_query_does_not_force_requery(operating_source):
    source, baseline = operating_source
    model = VisibleFeedbackModel()
    context = context_for(source)
    observed = []
    for entity in ("actuals", "resources"):
        decision = decide(model, context)
        assert decision.action == "query" and decision.parameters.entity == entity
        result = queried(source, entity)
        observed.append(deepcopy(result))
        context["tool_results"].append(result)
        source = advance(source, baseline)
        context["facts"] = facts(source)
        context["budget_remaining"] -= 1
        assert result["result"]["snapshot_id"] != context["facts"]["snapshot_id"]
        assert (
            datetime.fromisoformat(result["result"]["source"]["effective_at"])
            < source.snapshot_clock
        )
    decision = decide(model, context)
    assert decision.action == "solve_scenario"
    assert decision.parameters.new_actions_not_before == source.snapshot_clock + timedelta(
        minutes=15
    )
    context["tool_results"].append(
        {
            "operation_id": "queued-current-solve",
            "turn_id": context["turn_id"],
            "action": "solve_scenario",
            "state": "DONE",
            "result": {"status": "PENDING", "job_id": "actual-solver-job"},
        }
    )
    source = advance(source, baseline)
    context["facts"] = facts(source)
    context["budget_remaining"] -= 1
    assert decide(model, context).action == "wait"
    assert len(model.decisions) == 4 <= MODEL_REQUESTS_PER_TURN
    assert [row["action"] for row in model.decisions] == [
        "query",
        "query",
        "solve_scenario",
        "wait",
    ]
    assert len({row["facts"]["snapshot_id"] for row in model.contexts}) == 4
    assert len({row["facts"]["business_clock"] for row in model.contexts}) == 4
    assert context["tool_results"][:2] == observed


def test_next_turn_queries_both_entities_again_even_when_snapshot_has_not_changed(operating_source):
    source, _ = operating_source
    history = [
        queried(source, entity, turn_id="previous-turn") for entity in ("actuals", "resources")
    ]
    context = context_for(source, tools=history)
    model = VisibleFeedbackModel()
    first = decide(model, context)
    assert first.action == "query" and first.parameters.entity == "actuals"
    context["tool_results"].append(queried(source, "actuals"))
    second = decide(model, context)
    assert second.action == "query" and second.parameters.entity == "resources"
    assert context["tool_results"][:2] == history


@pytest.mark.parametrize("old_result_last", [False, True])
def test_old_turn_down_cannot_override_current_turn_available(operating_source, old_result_last):
    source, baseline = operating_source
    idle = next(
        row
        for row in source.resources
        if row.resource_id not in {actual.resource_id for actual in source.actuals}
    )
    down = inject(
        source,
        event_id="earlier-downtime",
        kind="resource.down",
        payload={"resource_id": idle.resource_id},
    )
    old = queried(down, "resources", turn_id="previous-turn")
    restored = inject(
        down,
        event_id="later-restoration",
        kind="resource.restore",
        payload={"resource_id": idle.resource_id},
    )
    source = advance(restored, baseline)
    current = [queried(source, "actuals"), queried(source, "resources")]
    history = [*current, old] if old_result_last else [old, *current]
    context = context_for(source, tools=history)
    decision = decide(VisibleFeedbackModel(), context)
    assert decision.action == "solve_scenario"
    assert any(row["status"] == "DOWN" for row in old["result"]["items"])
    assert all(row["status"] == "AVAILABLE" for row in current[1]["result"]["items"])
    assert int(old["result"]["source_revision"]) < int(current[1]["result"]["source_revision"])
    assert context["tool_results"] == history


@pytest.mark.parametrize("restore_resource", [False, True])
@pytest.mark.parametrize("summary_has_unknown", [False, True])
def test_unknown_actual_remaining_waits_even_after_resource_restoration_or_human_reply(
    operating_source, restore_resource, summary_has_unknown
):
    source, _ = operating_source
    operation = source.actuals[0]
    blocked = inject(
        source,
        event_id="new-fault",
        kind="resource.down",
        payload={"resource_id": operation.resource_id},
    )
    if restore_resource:
        blocked = inject(
            blocked,
            event_id="physical-resource-restored",
            kind="resource.restore",
            payload={"resource_id": operation.resource_id},
        )
    unknowns = [{"operation_id": operation.operation_id, "missing": "remaining_minutes"}]
    context = context_for(
        blocked,
        tools=[queried(blocked, "actuals"), queried(blocked, "resources")],
        unknowns=unknowns if summary_has_unknown else (),
    )
    decision = decide(VisibleFeedbackModel(), context)
    assert decision.action == "wait"
    actual = next(row for row in blocked.actuals if row.operation_id == operation.operation_id)
    assert actual.state == "BLOCKED" and actual.remaining_minutes is None
    assert actual.remaining_confirmed_by is None
    assert actual.consumed == operation.consumed


def test_new_resource_fault_prevents_solve_despite_completed_current_turn_queries(operating_source):
    source, _ = operating_source
    idle = next(
        row
        for row in source.resources
        if row.resource_id not in {a.resource_id for a in source.actuals}
    )
    failed = inject(
        source,
        event_id="unrelated-machine-new-fault",
        kind="resource.down",
        payload={"resource_id": idle.resource_id},
    )
    context = context_for(failed, tools=[queried(source, "actuals"), queried(failed, "resources")])
    decision = decide(VisibleFeedbackModel(), context)
    assert decision.action == "wait"
    assert all(row.remaining_minutes is not None for row in failed.actuals)
