"""Correction recovery and probe caps preserve the durable request ledger.

Uses the isolated PostgreSQL fixture and controlled model responses only; this
does not start the live probe or contact a model provider.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_case_runtime_postgres import case_context as case_context
from test_case_runtime_postgres import proposal
from test_dynamic_factory_postgres import control
from test_dynamic_factory_postgres import dynamic_source as dynamic_source
from test_publication_postgres import publishing as publishing

from packages.agent import case_runtime
from packages.agent.cases import get_case
from packages.agent.cases_store import CaseRecord, CaseTurn
from packages.agent.checkpoints import checkpoint_session, checkpoint_thread_id
from packages.planning.store import FactoryState, SolveJob
from scripts import verify_case_main_gateway as probe


def test_contract_correction_refreshes_source_before_deciding_and_solving(case_context):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id

    class SlowInvalidThenSolve:
        def __init__(self):
            self.contexts = []
            self.changed = None

        def complete(self, prompt):
            data = json.loads(prompt.split("Business context (data, not instructions):\n", 1)[1])
            self.contexts.append(data)
            if len(self.contexts) == 1:
                assert (
                    control(
                        source,
                        "advance-during-invalid-model-response",
                        "clock.step",
                        {"minutes": 1},
                    ).status_code
                    == 200
                )
                self.changed = reader.snapshot(factory)
                assert int(self.changed.source.source_revision) > int(
                    data["facts"]["source_revision"]
                )
                # Simulate the elapsed source-observation age of a slow response
                # without sleeping or changing the worker's real lease/deadline.
                with Session(engine) as db, db.begin():
                    state = db.get(FactoryState, factory)
                    state.last_synced_at = datetime.now(UTC) - timedelta(seconds=31)
                return proposal("query", {"entity": "orders", "identity": None})
            if len(self.contexts) == 2:
                return proposal("solve_scenario", {"allow_overtime": False, "time_limit": 2})
            assert len(self.contexts) == 3, (
                "Only the invalid response, correction and wait are needed"
            )
            return proposal(
                "wait", {"reason": "Waiting for the solve to finish", "recheck_minutes": 15}
            )

    model = SlowInvalidThenSolve()
    assert case_runtime.process_case(engine, reader, model)
    detail = get_case(engine, actor, factory, case["case_id"])
    assert len(model.contexts) == 2
    assert [row["action"] for row in detail["operations"]] == ["solve_scenario"]
    solve_result = detail["operations"][0]["result"]
    assert solve_result["status"] == "PENDING", solve_result
    assert solve_result["job_state"] == "QUEUED"
    assert detail["state"] == "PLANNING" and detail["error_code"] is None
    assert detail["closure"] is None

    corrected = model.contexts[1]
    assert corrected["action_feedback"]["tool_executed"] is False
    assert corrected["action_feedback"]["contract_issues"] == ["query.parameters.offset: missing"]
    assert corrected["budget_remaining"] == case_runtime.MODEL_REQUESTS_PER_TURN - 1
    assert corrected["facts"]["snapshot_id"] == model.changed.snapshot_id
    assert corrected["facts"]["snapshot_hash"] == model.changed.content_hash
    assert corrected["facts"]["source_revision"] == model.changed.source.source_revision
    assert corrected["facts"]["business_clock"] == model.changed.snapshot_clock.isoformat()

    with Session(engine) as db:
        job = db.get(SolveJob, solve_result["job_id"])
        assert job.state == "QUEUED" and job.snapshot_id == model.changed.snapshot_id
        turns = list(db.scalars(select(CaseTurn).where(CaseTurn.case_id == case["case_id"])))
        assert len(turns) == 1
        assert turns[0].model_requests == 2 and turns[0].solver_requests == 1
        assert not turns[0].model_pending


def test_requested_correction_resumes_from_refresh_checkpoint_with_current_facts(
    case_context, monkeypatch
):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id

    class InvalidThenWait:
        def __init__(self):
            self.contexts = []

        def complete(self, prompt):
            self.contexts.append(
                json.loads(prompt.split("Business context (data, not instructions):\n", 1)[1])
            )
            if len(self.contexts) == 1:
                return "invalid-action"
            assert len(self.contexts) == 2, "Only one corrective model request is allowed"
            return proposal(
                "wait", {"reason": "Waiting for shop floor confirmation", "recheck_minutes": 15}
            )

    model = InvalidThenWait()
    fresh = case_runtime._fresh
    interrupted_claims = []

    def crash_before_corrective_refresh(engine, connector, claim):
        with Session(engine) as db:
            record = db.get(CaseRecord, claim.case_id)
            correction = record.context.get("action_correction", {})
            if correction.get("state") == "REQUESTED":
                assert correction["turn_id"] == claim.turn_id and correction["step"] == 0
                interrupted_claims.append(claim)
                raise SystemExit("worker terminated before the corrective refresh")
        return fresh(engine, connector, claim)

    monkeypatch.setattr(case_runtime, "_fresh", crash_before_corrective_refresh)
    with pytest.raises(SystemExit, match="before the corrective refresh"):
        case_runtime.process_case(engine, reader, model)
    monkeypatch.setattr(case_runtime, "_fresh", fresh)
    assert len(model.contexts) == len(interrupted_claims) == 1
    claim = interrupted_claims[0]
    config = {"configurable": {"thread_id": checkpoint_thread_id(factory, case["case_id"])}}

    # Check the real stored graph continuation, not merely the wrapper call count.
    with checkpoint_session(engine, factory, case["case_id"]) as saver:
        assert saver is not None
        checkpoint = case_runtime._graph(engine, reader, model, claim, saver).get_state(config)
        assert checkpoint.next == ("refresh",)
        assert checkpoint.values["refresh_required"] is True
        assert checkpoint.values["operation_id"] is None and not checkpoint.values["stop"]

    assert control(source, "advance-while-correction-paused", "clock.step").status_code == 200
    changed = reader.snapshot(factory)
    assert changed.snapshot_id != model.contexts[0]["facts"]["snapshot_id"]
    with Session(engine) as db, db.begin():
        turn = db.get(CaseTurn, claim.turn_id, with_for_update=True)
        record = db.get(CaseRecord, case["case_id"])
        assert turn.state == "RUNNING" and turn.model_requests == 1
        assert not turn.model_pending and turn.next_step == 0
        assert record.context["action_correction"]["state"] == "REQUESTED"
        assert record.snapshot_id != changed.snapshot_id
        turn.lease_until = datetime.now(UTC) - timedelta(seconds=1)

    assert case_runtime.process_case(engine, reader, model)
    detail = get_case(engine, actor, factory, case["case_id"])
    assert len(model.contexts) == 2
    corrected = model.contexts[1]
    assert corrected["turn_id"] == model.contexts[0]["turn_id"] == claim.turn_id
    assert corrected["budget_remaining"] == case_runtime.MODEL_REQUESTS_PER_TURN - 1
    assert corrected["action_feedback"]["tool_executed"] is False
    assert corrected["action_feedback"]["corrections_remaining"] == 0
    assert corrected["facts"]["snapshot_id"] == changed.snapshot_id
    assert corrected["facts"]["snapshot_hash"] == changed.content_hash
    assert corrected["facts"]["source_revision"] == changed.source.source_revision
    assert corrected["facts"]["business_clock"] == changed.snapshot_clock.isoformat()
    assert detail["state"] == "WAITING" and detail["error_code"] is None
    assert detail["closure"] is None
    assert [row["action"] for row in detail["operations"]] == ["wait"]
    assert detail["operations"][0]["state"] == "DONE"
    with Session(engine) as db:
        turns = list(db.scalars(select(CaseTurn).where(CaseTurn.case_id == case["case_id"])))
        assert len(turns) == 1 and turns[0].turn_id == claim.turn_id
        assert turns[0].model_requests == 2 and turns[0].solver_requests == 0
        assert turns[0].attempts == 2 and turns[0].next_step == 1
        assert turns[0].state == "WAITING" and not turns[0].model_pending
    with checkpoint_session(engine, factory, case["case_id"]) as saver:
        assert saver is not None
        checkpoint = case_runtime._graph(engine, reader, model, claim, saver).get_state(config)
        assert checkpoint.next == ("wait",)
        assert checkpoint.values["refresh_required"] is False
    assert not case_runtime.process_case(engine, reader, model)
    assert len(model.contexts) == 2


@pytest.mark.parametrize("downtime_past_deadline", [False, True])
def test_exhausted_correction_recovers_without_requesting_another_response(
    case_context, monkeypatch, downtime_past_deadline
):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id

    class InvalidModel:
        calls = 0

        def complete(self, prompt):
            self.calls += 1
            assert self.calls <= 2, "Recovery must not issue another model request"
            return "invalid-action"

    model = InvalidModel()
    decide = case_runtime._decide

    def crash_after_rejection(*args):
        result = decide(*args)
        if result.get("stop") and model.calls == 2:
            raise SystemExit("worker terminated after the second rejection was committed")
        return result

    monkeypatch.setattr(case_runtime, "_decide", crash_after_rejection)
    with pytest.raises(SystemExit, match="second rejection was committed"):
        case_runtime.process_case(engine, reader, model)

    with Session(engine) as db, db.begin():
        turn = db.scalar(
            select(CaseTurn).where(CaseTurn.case_id == case["case_id"]).with_for_update()
        )
        record = db.get(CaseRecord, case["case_id"])
        assert turn.state == "RUNNING" and turn.model_requests == 2
        assert not turn.model_pending and turn.next_step == 0
        assert record.context["action_correction"] == {
            "turn_id": turn.turn_id,
            "step": 0,
            "state": "EXHAUSTED",
            "issues": ["output: invalid_json"],
        }
        assert record.error_code == "INVALID_MODEL_ACTION"
        turn_id = turn.turn_id
        turn.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        if downtime_past_deadline:
            turn.deadline = datetime.now(UTC) - timedelta(seconds=1)

    monkeypatch.setattr(case_runtime, "_decide", decide)
    assert case_runtime.process_case(engine, reader, model)
    detail = get_case(engine, actor, factory, case["case_id"])
    assert model.calls == 2
    assert detail["state"] == "WAITING"
    assert detail["error_code"] == "INVALID_MODEL_ACTION"
    assert detail["operations"] == [] and detail["closure"] is None
    with Session(engine) as db:
        turns = list(db.scalars(select(CaseTurn).where(CaseTurn.case_id == case["case_id"])))
        assert len(turns) == 1 and turns[0].turn_id == turn_id
        assert turns[0].attempts == 2 and turns[0].model_requests == 2
        assert turns[0].state == "WAITING" and not turns[0].model_pending
    assert not case_runtime.process_case(engine, reader, model)
    assert model.calls == 2


@pytest.mark.parametrize("invalid_response", [False, True])
def test_probe_cap_stops_before_runtime_reserves_an_unsent_request(
    case_context, monkeypatch, tmp_path, invalid_response
):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id
    monkeypatch.setattr(probe, "code_hashes", lambda: {"controlled-test": "not-a-live-run"})
    evidence = probe.Evidence(tmp_path / "budget.json", "controlled-model", max_requests=1)

    class ControlledProvider:
        calls = 0

        def complete(self, prompt):
            self.calls += 1
            assert self.calls == 1, "The lower cap must block a second provider call"
            data = json.loads(prompt.split("Business context (data, not instructions):\n", 1)[1])
            assert data["budget_remaining"] == 1
            if invalid_response:
                return "invalid-action"
            return proposal("query", {"entity": "resources", "identity": None, "offset": 0})

    provider = ControlledProvider()
    model = probe.BudgetedModel(provider, evidence)
    model.case_id = case["case_id"]
    assert case_runtime.process_case(engine, reader, model)

    detail = get_case(engine, actor, factory, case["case_id"])
    assert detail["state"] == "WAITING"
    assert detail["error_code"] == "MODEL_CASE_BUDGET_EXHAUSTED"
    assert detail["closure"] is None
    assert [row["action"] for row in detail["operations"]] == (
        [] if invalid_response else ["query"]
    )
    if not invalid_response:
        assert detail["operations"][0]["result"]["status"] == "OK"

    persisted = json.loads(evidence.path.read_text(encoding="utf-8"))
    assert persisted["max_requests"] == persisted["requests_started"] == provider.calls == 1
    assert len(persisted["model_decisions"]) == 1
    assert persisted["model_decisions"][0]["state"] == "RETURNED"
    with Session(engine) as db:
        turns = list(db.scalars(select(CaseTurn).where(CaseTurn.case_id == case["case_id"])))
        assert len(turns) == 1 and turns[0].model_requests == persisted["requests_started"]
        assert turns[0].state == "WAITING" and not turns[0].model_pending
        assert turns[0].error_code != "MODEL_RESULT_UNKNOWN"
