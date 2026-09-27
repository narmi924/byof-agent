"""Public business reasons remain bound to the durable action through restart recovery."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_case_runtime_postgres import case_context as case_context
from test_dynamic_factory_postgres import dynamic_source as dynamic_source
from test_publication_postgres import publishing as publishing

from packages.agent import case_runtime
from packages.agent.cases import get_case
from packages.agent.cases_store import CaseOperation, CaseTurn
from packages.domain.models import canonical_hash
from packages.planning.store import SolveJob


def decision(action, parameters, reason):
    return json.dumps(
        {"action": action, "parameters": parameters, "reason_summary": reason}, ensure_ascii=False
    )


class WaitModel:
    def __init__(self, reason):
        self.reason = reason
        self.calls = 0

    def complete(self, prompt):
        self.calls += 1
        return decision(
            "wait",
            {
                "reason": "Waiting for the planner to review the actual result",
                "recheck_minutes": 15,
            },
            self.reason,
        )


def test_reason_is_exact_and_public_without_replacing_candidate_or_snapshot_evidence(case_context):
    context, case = case_context
    source, reader, _, actor, candidate_id, _ = context
    engine, factory = source[3], source[2].factory_id
    compare_reason = "Compare the actual metrics of existing plans and check the delivery target and required permissions."
    wait_reason = "Waiting for the planner to review the checked plan."

    class CompareModel:
        calls = 0

        def complete(self, prompt):
            self.calls += 1
            evidence = json.loads(
                prompt.split("Business context (data, not instructions):\n", 1)[1]
            )
            if not any(item["action"] == "compare_candidates" for item in evidence["tool_results"]):
                return decision(
                    "compare_candidates", {"candidate_ids": [candidate_id]}, compare_reason
                )
            return decision(
                "wait",
                {
                    "reason": "Waiting for the planner to review the actual result",
                    "recheck_minutes": 15,
                },
                wait_reason,
            )

    model = CompareModel()
    assert case_runtime.process_case(engine, reader, model)
    detail = get_case(engine, actor, factory, case["case_id"])
    assert model.calls == 2 and detail["state"] == "WAITING"
    assert [row["reason_summary"] for row in detail["operations"]] == [compare_reason, wait_reason]
    compared = detail["operations"][0]
    assert compared["parameters"] == {"candidate_ids": [candidate_id]}
    assert compared["result"]["status"] == "OK"
    assert compared["result"]["candidates"][0]["candidate_id"] == candidate_id
    assert compared["snapshot_id"] == compared["result"]["snapshot_id"]
    with Session(engine) as db:
        row = db.get(CaseOperation, compared["operation_id"])
        assert row.reason_summary == compare_reason
        assert row.parameter_hash == canonical_hash(
            {"action": row.action, "parameters": row.parameters}
        )
        assert row.reason_summary not in json.dumps(row.parameters, ensure_ascii=False)
    assert not {"model_response", "prompt", "reasoning", "thinking"} & set(compared)


def test_existing_operation_without_reason_remains_null_in_repeated_reads(case_context):
    context, case = case_context
    source, _, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id
    identity = str(uuid4())
    params = {"entity": "resources", "identity": None, "offset": 0}
    with Session(engine) as db, db.begin():
        db.add(
            CaseOperation(
                operation_id=identity,
                case_id=case["case_id"],
                factory_id=factory,
                turn_id="historical-turn",
                step=0,
                action="query",
                parameters=params,
                parameter_hash=canonical_hash({"action": "query", "parameters": params}),
                expected_case_version=case["version"],
                snapshot_id=case["snapshot_id"],
                state="DONE",
                result={"status": "OK", "summary": "Read the resource records"},
                created_at=datetime.now(UTC),
            )
        )
    for _ in range(2):
        detail = get_case(engine, actor, factory, case["case_id"])
        assert len(detail["operations"]) == 1
        assert detail["operations"][0]["reason_summary"] is None
    with Session(engine) as db:
        assert db.get(CaseOperation, identity).reason_summary is None


def test_prepared_reason_survives_restart_without_second_model_request(case_context, monkeypatch):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id
    reason = "Wait for the owner's explicit reply based on the facts obtained; do not repeat the same request."
    model = WaitModel(reason)
    decide = case_runtime._decide
    monkeypatch.setattr(case_runtime, "MODEL_REQUESTS_PER_TURN", 1)

    def crash_after_preparation(*args):
        result = decide(*args)
        assert result["operation_id"]
        raise SystemExit("Controlled interruption after the operation transaction")

    monkeypatch.setattr(case_runtime, "_decide", crash_after_preparation)
    with pytest.raises(SystemExit):
        case_runtime.process_case(engine, reader, model)
    with Session(engine) as db, db.begin():
        prepared = db.scalar(select(CaseOperation).where(CaseOperation.case_id == case["case_id"]))
        operation_id = prepared.operation_id
        assert prepared.state == "PREPARED" and prepared.reason_summary == reason
        turn = db.get(CaseTurn, prepared.turn_id, with_for_update=True)
        assert turn.model_requests == 1 and not turn.model_pending
        turn.lease_until = turn.deadline = datetime.now(UTC) - timedelta(seconds=1)
    monkeypatch.setattr(case_runtime, "_decide", decide)
    assert case_runtime.process_case(engine, reader, model)
    result = get_case(engine, actor, factory, case["case_id"])
    assert model.calls == 1 and len(result["operations"]) == 1
    operation = result["operations"][0]
    assert operation["operation_id"] == operation_id and operation["state"] == "DONE"
    assert operation["reason_summary"] == reason and operation["result"]["status"] == "WAITING"


@pytest.mark.parametrize("invalid", ["too_long", "non_text", "private_reasoning"])
def test_invalid_reason_or_extra_private_reasoning_has_no_operation_effect(case_context, invalid):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id
    payload = json.loads(
        decision("solve_scenario", {"allow_overtime": False, "time_limit": 2}, "Check due dates")
    )
    if invalid == "too_long":
        payload["reason_summary"] = "r" * 501
    elif invalid == "non_text":
        payload["reason_summary"] = True
    else:
        payload["reasoning"] = "Private reasoning must not be accepted or persisted"

    class InvalidModel:
        calls = 0

        def complete(self, prompt):
            self.calls += 1
            assert "Private reasoning must not be accepted or persisted" not in prompt
            return json.dumps(payload, ensure_ascii=False)

    with Session(engine) as db:
        before = db.scalar(
            select(func.count()).select_from(SolveJob).where(SolveJob.factory_id == factory)
        )
    model = InvalidModel()
    assert case_runtime.process_case(engine, reader, model)
    detail = get_case(engine, actor, factory, case["case_id"])
    assert model.calls == 2 and detail["operations"] == []
    assert detail["error_code"] == "INVALID_MODEL_ACTION" and detail["closure"] is None
    with Session(engine) as db:
        assert (
            db.scalar(
                select(func.count()).select_from(SolveJob).where(SolveJob.factory_id == factory)
            )
            == before
        )
