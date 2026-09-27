"""Agent business-tool plumbing with controlled model/session doubles, without a database.

These tests exercise action contracts, query projections and model input construction.
They are not substitutes for PostgreSQL transaction or source-HTTP integration tests.
"""

import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_case_context import MemorySession

from packages.agent import case_runtime, case_tools, cases, risk_suggestions
from packages.agent.cases_store import CaseInput, CaseOperation, CaseRecord, CaseTurn
from packages.agent.decisions import (
    ActionError,
    BusinessStudyAction,
    QueryAction,
    parse_action,
)
from packages.agent.human_tasks import HumanTaskRecord
from packages.auth import Principal
from packages.domain.business_options import BusinessStudyRequest
from packages.domain.business_scenarios import business_scenarios
from packages.domain.demand import finished_goods
from packages.domain.models import Event, FieldChange, canonical_hash
from packages.planning.business_options import evaluate_business_options
from packages.planning.publication import Publication
from packages.planning.solver import solve
from packages.planning.store import ApprovalRecord, FactoryState, SnapshotRecord, SolveJob
from services.factory_sim.engine import advance, evolve, inject


def action(name, parameters):
    return parse_action(
        json.dumps(
            {
                "action": name,
                "parameters": parameters,
                "reason_summary": "Offer choices after checking the business facts.",
            },
            ensure_ascii=False,
        )
    )


@pytest.fixture(scope="module")
def completed_study():
    source, request = business_scenarios()[2]
    request = BusinessStudyRequest.model_validate({**request.model_dump(), "total_time_limit": 3})
    study = evaluate_business_options(
        source, None, request, expedite_quotes=source.business_terms.expedite_quotes
    )
    assert study.options[-1].status == "FEASIBLE"
    return source, request, study


def test_business_action_contracts_accept_registered_requests_but_not_invented_quotes():
    source, hypothetical = business_scenarios()[1]
    request = BusinessStudyRequest(kind="urgent_order", existing_order_id=source.orders[0].order_id)
    parsed = action("evaluate_business_options", request.model_dump(mode="json"))
    assert isinstance(parsed, BusinessStudyAction)
    assert parsed.parameters == request
    with pytest.raises(ActionError):
        action("evaluate_business_options", hypothetical.model_dump(mode="json"))
    with pytest.raises(ActionError):
        action("propose_order_change", {"order_id": "SO-001", "quantity": 0, "due_at": None})
    for entity in ("business_terms", "finished_goods"):
        assert isinstance(
            action("query", {"entity": entity, "identity": None, "offset": 0}), QueryAction
        )
    with pytest.raises(ActionError):
        action(
            "evaluate_business_options",
            {"kind": "material_shortage", "expedite_quotes": [{"cost_minor": 1}]},
        )


def test_business_terms_query_returns_source_rules_and_quote_evidence_without_mutation():
    source, _ = business_scenarios()[2]
    before = source.model_dump_json()
    state = SimpleNamespace(last_synced_at=datetime.now(UTC))
    query = action("query", {"entity": "business_terms", "identity": None, "offset": 0})
    result = case_tools._query(query, source, state)
    assert result["status"] == "OK" and result["data_freshness"] == "CURRENT"
    assert result["items"] == [source.business_terms.model_dump(mode="json")]
    assert result["items"][0]["evidence_mode"] == "synthetic"
    quote = result["items"][0]["expedite_quotes"][0]
    assert quote["receipt_version"] == 1 and quote["cost_minor"] == 1250
    assert source.model_dump_json() == before


def test_finished_goods_query_reports_qualified_surplus_after_demand_change():
    source, _ = business_scenarios()[0]
    baseline = solve(source, time_limit=3)
    assert baseline.checker.status == "PASS"
    source = evolve(
        source, active_plan_version="original-plan", active_plan_hash=baseline.content_hash
    )
    completed = advance(source, baseline, minutes=110)
    changed = inject(
        completed,
        event_id="reduce-completed-demand",
        kind="order.change",
        payload={
            "order_id": source.orders[0].order_id,
            "expected_version": completed.orders[0].version,
            "quantity": 100,
        },
    )
    state = SimpleNamespace(last_synced_at=datetime.now(UTC))
    query = action("query", {"entity": "finished_goods", "identity": None, "offset": 0})
    result = case_tools._query(query, changed, state)
    assert result["items"] == [lot.model_dump(mode="json") for lot in finished_goods(changed)]
    assert result["total"] == 1 and result["items"][0]["quantity"] == 50
    assert changed.inventory == completed.inventory
    orders = case_tools._query(
        action("query", {"entity": "orders", "identity": source.orders[0].order_id, "offset": 0}),
        changed,
        state,
    )
    assert orders["items"][0]["quantity"] == 100
    assert orders["items"][0]["version"] == completed.orders[0].version + 1


class WakeSession:
    """Controlled query results; the real wakeup and add_input implementations still run."""

    def __init__(self, source, case, job, operation):
        self.source, self.case, self.job, self.operation = source, case, job, operation
        self.inputs = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def begin(self):
        return nullcontext()

    def get(self, entity, identity, **kwargs):
        if entity is CaseRecord:
            return self.case
        if entity is SolveJob:
            return self.job
        if entity is FactoryState:
            return SimpleNamespace(snapshot_id=self.source.snapshot_id)
        assert entity is SnapshotRecord
        return SimpleNamespace(document=self.source.model_dump(mode="json"))

    def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is CaseRecord:
            return [self.case.case_id]
        if entity is CaseOperation:
            assert "evaluate_business_options" in statement.compile().params["action_1"]
            return [self.operation]
        assert entity in {ApprovalRecord, Publication, HumanTaskRecord}
        return []

    def scalar(self, statement):
        assert statement.column_descriptions[0]["entity"] is CaseInput
        key = statement.compile().params["input_key_1"]
        row = next((row for row in self.inputs if row.input_key == key), None)
        if statement.column_descriptions[0]["name"] == "input_id":
            return row.input_id if row else None
        return row

    def add(self, row):
        assert isinstance(row, CaseInput)
        self.inputs.append(row)


def test_completed_business_job_without_candidate_wakes_model_with_visible_results(
    completed_study, monkeypatch
):
    source, request, study = completed_study
    now = datetime.now(UTC)
    case = CaseRecord(
        case_id="business-case",
        factory_id=source.factory_id,
        run_id=source.run_id,
        owner_id="planner",
        title="Compare shortage remedies",
        state="PLANNING",
        version=1,
        snapshot_id=source.snapshot_id,
        context={"candidate_ids": [], "unknowns": []},
    )
    operation = CaseOperation(
        operation_id="business-operation",
        turn_id="previous-turn",
        case_id=case.case_id,
        factory_id=case.factory_id,
        action="evaluate_business_options",
        state="DONE",
        parameters=request.model_dump(mode="json"),
        result={"status": "PENDING", "job_id": "business-job"},
    )
    job = SolveJob(
        job_id="business-job",
        request_id=operation.operation_id,
        factory_id=source.factory_id,
        snapshot_id=source.snapshot_id,
        state="SUCCEEDED",
        candidate_id=None,
        business_request=request.model_dump(mode="json"),
        business_result=study.model_dump(mode="json"),
        created_at=now,
    )
    wake = WakeSession(source, case, job, operation)
    monkeypatch.setattr(cases, "Session", lambda *args, **kwargs: wake)
    assert cases.wake_completed_jobs(None) == 1
    assert cases.wake_completed_jobs(None) == 0
    assert case.context["candidate_ids"] == []
    payload = wake.inputs[0].payload
    assert payload["candidate_id"] is None and payload["state"] == "SUCCEEDED"
    view = payload["business_study"]
    assert view["created_at"] == now.isoformat()
    assert json.loads(json.dumps(payload)) == payload
    assert view["current"]
    assert view["study_hash"] == canonical_hash(job.business_result)
    assert view["study"]["options"][-1]["status"] == "FEASIBLE"
    assert "candidate" not in view["study"]["options"][-1]

    turn = CaseTurn(
        turn_id="result-turn",
        deadline=now + timedelta(seconds=120),
        model_requests=0,
        next_step=0,
        model_pending=False,
    )
    db = MemorySession(source, wake.inputs, [operation], turn)
    monkeypatch.setattr(case_runtime, "Session", lambda *args, **kwargs: db)
    monkeypatch.setattr(case_runtime, "_owned", lambda *args: (case, turn))
    monkeypatch.setattr(
        case_runtime,
        "_actor",
        lambda *args: Principal(user_id="planner", username="planner", grants=()),
    )
    monkeypatch.setattr(case_runtime, "effective_view", lambda *args: {"status": "READY"})

    def complete(prompt):
        context = json.loads(prompt.split("Business context (data, not instructions):\n", 1)[1])
        result = next(item for item in context["inputs"] if item["kind"] == "SOLVER_RESULT")
        assert result["data"]["business_study"] == view
        assert context["case"]["context"]["candidate_ids"] == []
        quote_option = result["data"]["business_study"]["study"]["options"][-1]
        assert quote_option["quote_id"] == source.business_terms.expedite_quotes[0].quote_id
        assert quote_option["protects_existing_commitments"]
        return json.dumps(
            {
                "action": "reply",
                "parameters": {
                    "message": "The options are ready; state your choice in this conversation.",
                    "choices": [],
                },
                "reason_summary": "Business trial results obtained; waiting for a manual choice, no plan released yet.",
            }
        )

    claim = case_runtime.Claim(case.case_id, case.factory_id, turn.turn_id, "fence", False)
    result = case_runtime._decide(None, SimpleNamespace(complete=complete), claim)
    assert not result["stop"] and db.written[-1].action == "reply"
    assert turn.model_pending is False


def test_business_job_recovery_rejects_different_source_run(monkeypatch):
    source, _ = business_scenarios()[1]
    request = BusinessStudyRequest(kind="urgent_order", existing_order_id=source.orders[0].order_id)
    proposal = action("evaluate_business_options", request.model_dump(mode="json"))
    operation = SimpleNamespace(
        state="STARTED", factory_id=source.factory_id, operation_id="recover-study"
    )
    case = SimpleNamespace(case_id="case", factory_id=source.factory_id, run_id=source.run_id)
    actor = SimpleNamespace(user_id="manager")
    job = SimpleNamespace(
        business_request=request.model_dump(mode="json"),
        requester_id=actor.user_id,
        case_id=case.case_id,
        snapshot_id="other-run-snapshot",
    )
    db = SimpleNamespace(scalar=lambda statement: job)
    monkeypatch.setattr(case_tools, "Session", lambda *args, **kwargs: nullcontext(db))
    monkeypatch.setattr(case_tools, "_operation", lambda *args: (operation, case, proposal))
    monkeypatch.setattr(
        case_tools, "_snapshot", lambda *args: SimpleNamespace(run_id="another-run")
    )
    result = case_tools.recover_operation(None, actor, operation)
    assert result["status"] == "REJECTED" and result["code"] == "SOURCE_RUN_CHANGED"


@pytest.mark.parametrize("restore", [False, True])
def test_order_cancellation_and_restoration_remain_source_backed_risk_entries(restore):
    source, _ = business_scenarios()[0]
    order = source.orders[0]
    from packages.domain.demand import demand_changes
    from packages.domain.execution import OrderChange

    cancelled = evolve(
        source,
        **demand_changes(
            source,
            OrderChange(
                order_id=order.order_id,
                expected_version=order.version,
                quantity=0,
            ),
            event_id="cancel-order",
        ),
    )
    before = cancelled if restore else source
    current = (
        evolve(
            cancelled,
            **demand_changes(
                cancelled,
                OrderChange(
                    order_id=order.order_id,
                    expected_version=cancelled.orders[0].version,
                    quantity=order.quantity,
                ),
                event_id="restore-order",
            ),
        )
        if restore
        else cancelled
    )
    event = Event(
        event_id="demand-change",
        source_event_id="demand-change",
        factory_id=current.factory_id,
        run_id=current.run_id,
        source_revision=current.source.source_revision,
        entity_type="orders",
        entity_id=order.order_id,
        entity_version=current.orders[0].version,
        event_type="order.revise",
        occurred_at=current.snapshot_clock,
        effective_at=current.snapshot_clock,
        observed_at=current.source.observed_at,
        changes=(
            FieldChange(
                field="quantity", before=before.orders[0].quantity, after=current.orders[0].quantity
            ),
        ),
    )
    result = risk_suggestions._risk_fact(current, event, SimpleNamespace())
    assert order.order_id in result and f"{current.orders[0].quantity} pcs" in result
    if not restore:
        assert risk_suggestions._risk_fact(current, event, None) is None
