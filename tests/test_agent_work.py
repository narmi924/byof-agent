"""Bounded Agent work uses real PostgreSQL and a real socket factory, with controlled models."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session
from test_factory_http_postgres import source as source

from packages.agent.work import AgentRun, enqueue, process_one, view
from packages.auth import AccessError, Grant, Principal
from packages.integrations.factory_http import FactoryHTTP
from packages.persistence import Membership, User
from packages.planning.store import SnapshotRecord, SolveJob
from packages.providers.registry import GatewayModel, ProviderUnavailable, configured_model
from packages.settings import Settings


@pytest.fixture
def agent_context(source):
    engine, _, connector, initial, _ = source
    user_id = "agent-test-" + uuid4().hex
    with Session(engine) as db, db.begin():
        db.add(
            User(user_id=user_id, username=user_id, password_hash="not-used-for-login", active=True)
        )
        db.flush()
        db.add(Membership(user_id=user_id, factory_id=initial.factory_id, role="planner"))
    actor = Principal(
        user_id=user_id,
        username=user_id,
        grants=(Grant(factory_id=initial.factory_id, role="planner"),),
    )
    try:
        yield engine, connector, initial, actor
    finally:
        with engine.begin() as connection:
            connection.execute(delete(AgentRun).where(AgentRun.factory_id == initial.factory_id))
            connection.execute(delete(Membership).where(Membership.user_id == user_id))
            connection.execute(delete(User).where(User.user_id == user_id))


def read_run(engine, run_id):
    with Session(engine) as db:
        return view(db.get(AgentRun, run_id))


def counts(engine, factory_id):
    with Session(engine) as db:
        return {
            "jobs": len(
                db.scalars(select(SolveJob).where(SolveJob.factory_id == factory_id)).all()
            ),
            "facts": len(
                db.scalars(
                    select(SnapshotRecord).where(SnapshotRecord.factory_id == factory_id)
                ).all()
            ),
            "runs": len(
                db.scalars(select(AgentRun).where(AgentRun.factory_id == factory_id)).all()
            ),
        }


def test_provider_selection_does_not_fall_back_or_mutate_process_environment():
    custom = Settings(_env_file=None, llm_provider="custom")
    with patch("packages.providers.registry.Gateway") as gateway:
        with pytest.raises(ProviderUnavailable, match="CUSTOM_PROVIDER_UNVERIFIED"):
            configured_model(custom)
        gateway.assert_not_called()
        settings = Settings(
            _env_file=None,
            llm_provider="gateway",
            llm_gateway_url="https://configured.invalid",
            llm_gateway_api_key="test-only-key",
            llm_model="gateway-model",
        )
        adapter = configured_model(settings)
        assert isinstance(adapter, GatewayModel) and adapter.gateway is gateway.return_value
        gateway.assert_called_once_with(
            url="https://configured.invalid", key="test-only-key", model="gateway-model"
        )


def test_real_query_records_source_evidence_without_private_model_output(agent_context):
    engine, connector, initial, actor = agent_context
    run = enqueue(engine, actor, initial.factory_id, uuid4().hex, "Which orders are there now?")
    model = Mock()
    model.complete.return_value = '{"route":"query","entity":"orders"}'
    assert process_one(engine, connector, model)
    result = read_run(engine, run.run_id)
    assert result["state"] == "SUCCEEDED" and result["model_requests"] == 1
    assert result["result"]["record_count"] == 6
    assert result["result"]["snapshot_hash"] == initial.content_hash
    assert result["result"]["source_revision"] == "1"
    assert result["result"]["tool"] == "query"
    assert (
        result["result"]["status"] == "OK"
        and result["result"]["snapshot_id"] == initial.snapshot_id
    )
    assert "message" not in result and "response" not in result and "prompt" not in result
    assert counts(engine, initial.factory_id) == {"runs": 1, "facts": 1, "jobs": 0}
    model.complete.assert_called_once()
    assert "one short question in English" in model.complete.call_args.args[0]


@pytest.mark.parametrize(
    "opening, newline", [("```json", "\n"), ("```", "\n"), ("```json", "\r\n")]
)
def test_gateway_single_fence_executes_the_real_query(agent_context, opening, newline):
    engine, connector, initial, actor = agent_context
    run = enqueue(engine, actor, initial.factory_id, uuid4().hex, "Query orders")
    gateway = Mock()
    gateway.complete.return_value = newline.join(
        (opening, '{"route":"query","entity":"orders"}', "```")
    )
    assert process_one(engine, connector, GatewayModel(gateway))
    stored = read_run(engine, run.run_id)
    assert stored["state"] == "SUCCEEDED" and stored["model_requests"] == 1
    assert stored["result"]["record_count"] == 6
    assert stored["result"]["snapshot_hash"] == initial.content_hash
    assert counts(engine, initial.factory_id) == {"runs": 1, "facts": 1, "jobs": 0}
    gateway.complete.assert_called_once()


def test_gateway_fence_does_not_relax_strict_decisions_or_extract_embedded_blocks(agent_context):
    engine, _, initial, actor = agent_context
    connector = Mock(spec=FactoryHTTP)
    valid = '```json\n{"route":"query","entity":"orders"}\n```'
    responses = (
        "Here is the decision:\n" + valid,
        valid + "\nI have queried the factory.",
        valid + "\n" + valid,
        '```json\n{"route":"query","entity":"orders",}\n```',
        '```json\n{"route":"publish"}\n```',
        '```json\n{"route":"query","entity":"orders","confirmed":true}\n```',
        '```json\n{"route":"query","route":"planning","task":"initial"}\n```',
        '```python\n{"route":"query","entity":"orders"}\n```',
    )
    for response in responses:
        run = enqueue(engine, actor, initial.factory_id, uuid4().hex, "Query orders")
        gateway = Mock()
        gateway.complete.return_value = response
        assert process_one(engine, connector, GatewayModel(gateway))
        stored = read_run(engine, run.run_id)
        assert stored["state"] == "FAILED" and stored["error_code"] == "INVALID_MODEL_DECISION"
        assert stored["result"] is None and stored["model_requests"] == 1
        gateway.complete.assert_called_once()
    assert connector.mock_calls == []
    assert counts(engine, initial.factory_id) == {"runs": len(responses), "facts": 0, "jobs": 0}


def test_planning_creates_one_actual_job_on_the_synchronized_snapshot(agent_context):
    engine, connector, initial, actor = agent_context
    request_id = uuid4().hex
    run = enqueue(
        engine, actor, initial.factory_id, request_id, "Generate the initial production plan"
    )
    model = Mock()
    model.complete.return_value = '{"route":"planning","task":"initial"}'
    assert process_one(engine, connector, model)
    result = read_run(engine, run.run_id)
    assert result["state"] == "SUCCEEDED" and result["result"]["tool"] == "solve_scenario"
    assert (
        result["result"]["status"] == "OK"
        and result["result"]["snapshot_id"] == initial.snapshot_id
    )
    with Session(engine) as db:
        job = db.get(SolveJob, result["result"]["job_id"])
        assert job.requester_id == actor.user_id and job.factory_id == initial.factory_id
        assert job.request_id == "agent:" + run.run_id and job.state == "QUEUED"
        assert job.snapshot_id == initial.snapshot_id and not job.allow_overtime
        assert job.time_limit == 30
    assert (
        enqueue(
            engine, actor, initial.factory_id, request_id, "Generate the initial production plan"
        ).run_id
        == run.run_id
    )
    assert not process_one(engine, connector, model)
    model.complete.assert_called_once()
    assert counts(engine, initial.factory_id) == {"runs": 1, "facts": 1, "jobs": 1}


def test_bad_json_unknown_actions_and_model_confirmation_have_zero_tool_effect(agent_context):
    engine, _, initial, actor = agent_context
    connector = Mock(spec=FactoryHTTP)
    for response in (
        "not-json",
        "[]",
        '{"route":"publish"}',
        '{"route":"query","entity":"orders","confirmed":true}',
        '{"route":"query","route":"planning","task":"initial"}',
        '{"route":"query","entity":"factory-secret"}',
    ):
        run = enqueue(engine, actor, initial.factory_id, uuid4().hex, "Please query")
        model = Mock()
        model.complete.return_value = response
        assert process_one(engine, connector, model)
        stored = read_run(engine, run.run_id)
        assert stored["state"] == "FAILED" and stored["error_code"] == "INVALID_MODEL_DECISION"
        assert stored["result"] is None and stored["model_requests"] == 1
    assert connector.mock_calls == []
    assert counts(engine, initial.factory_id) == {"runs": 6, "facts": 0, "jobs": 0}


def test_clarification_keeps_a_durable_question_without_query_or_plan(agent_context):
    engine, _, initial, actor = agent_context
    connector = Mock(spec=FactoryHTTP)
    model = Mock()
    model.complete.return_value = '{"route":"clarify","question":"Which order should be queried?"}'
    run = enqueue(engine, actor, initial.factory_id, uuid4().hex, "Handle this for me")
    assert process_one(engine, connector, model)
    stored = read_run(engine, run.run_id)
    assert stored["state"] == "NEEDS_INPUT"
    prompt = model.complete.call_args.args[0]
    assert '"question":"one short question in English"' in prompt
    assert "one short question in Chinese" not in prompt
    assert stored["result"] == {
        "tool": "clarify",
        "status": "NEEDS_INPUT",
        "summary": "Which order should be queried?",
    }
    assert connector.mock_calls == [] and counts(engine, initial.factory_id)["jobs"] == 0


def test_lost_model_outcome_is_not_retried_after_worker_restart(agent_context):
    engine, connector, initial, actor = agent_context
    run = enqueue(engine, actor, initial.factory_id, uuid4().hex, "Query inventory")
    model = Mock()
    model.complete.side_effect = SystemExit("simulated worker crash")
    with pytest.raises(SystemExit):
        process_one(engine, connector, model)
    with engine.begin() as db:
        db.execute(
            update(AgentRun)
            .where(AgentRun.run_id == run.run_id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    restarted = Mock()
    assert process_one(engine, connector, restarted)
    assert read_run(engine, run.run_id)["error_code"] == "MODEL_RESULT_UNKNOWN"
    assert read_run(engine, run.run_id)["model_requests"] == 1
    restarted.complete.assert_not_called()
    assert counts(engine, initial.factory_id)["facts"] == 0


def test_model_timeout_fails_once_and_empty_poll_has_no_model_call(agent_context):
    engine, connector, initial, actor = agent_context
    model = Mock()
    assert not process_one(engine, connector, model)
    model.complete.assert_not_called()
    run = enqueue(engine, actor, initial.factory_id, uuid4().hex, "Query machines")
    model.complete.side_effect = TimeoutError("private response must not be logged")
    assert process_one(engine, connector, model)
    assert not process_one(engine, connector, model)
    stored = read_run(engine, run.run_id)
    assert stored["state"] == "FAILED" and stored["error_code"] == "MODEL_RESULT_UNKNOWN"
    assert stored["result"] is None and stored["model_requests"] == 1
    model.complete.assert_called_once()


def test_concurrent_enqueue_and_workers_do_not_repeat_the_model_request(agent_context):
    engine, connector, initial, actor = agent_context
    request_id = uuid4().hex

    def add():
        return enqueue(engine, actor, initial.factory_id, request_id, "Query orders")

    with ThreadPoolExecutor(max_workers=2) as pool:
        runs = list(pool.map(lambda _: add(), range(2)))
    assert runs[0].run_id == runs[1].run_id
    with pytest.raises(AccessError, match="The same request ID"):
        enqueue(engine, actor, initial.factory_id, request_id, "Different content")
    model = Mock()
    model.complete.return_value = '{"route":"query","entity":"orders"}'
    with ThreadPoolExecutor(max_workers=2) as pool:
        processed = list(pool.map(lambda _: process_one(engine, connector, model), range(2)))
    assert sorted(processed) == [False, True]
    model.complete.assert_called_once()
    assert counts(engine, initial.factory_id)["facts"] == 1


@pytest.mark.parametrize("during_model", [False, True])
def test_requester_role_is_rechecked_before_tools_and_before_model(agent_context, during_model):
    engine, _, initial, actor = agent_context
    connector = Mock(spec=FactoryHTTP)
    run = enqueue(engine, actor, initial.factory_id, uuid4().hex, "Generate a schedule")

    def revoke():
        with engine.begin() as db:
            db.execute(delete(Membership).where(Membership.user_id == actor.user_id))
        return '{"route":"planning","task":"initial"}'

    model = Mock()
    if during_model:
        model.complete.side_effect = lambda _: revoke()
    else:
        revoke()
    assert process_one(engine, connector, model)
    result = read_run(engine, run.run_id)
    assert result["state"] == "FAILED" and result["error_code"] == "AUTHORIZATION_REVOKED"
    assert result["model_requests"] == int(during_model)
    assert connector.mock_calls == [] and counts(engine, initial.factory_id)["jobs"] == 0


def test_expired_worker_cannot_apply_a_model_decision(agent_context):
    engine, _, initial, actor = agent_context
    run = enqueue(engine, actor, initial.factory_id, uuid4().hex, "Generate a schedule")
    connector = Mock(spec=FactoryHTTP)

    def expired(_):
        with engine.begin() as db:
            db.execute(
                update(AgentRun)
                .where(AgentRun.run_id == run.run_id)
                .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
            )
        return '{"route":"planning","task":"initial"}'

    model = Mock()
    model.complete.side_effect = expired
    assert process_one(engine, connector, model)
    assert process_one(engine, connector, model)
    assert read_run(engine, run.run_id)["error_code"] == "MODEL_RESULT_UNKNOWN"
    model.complete.assert_called_once()
    assert connector.mock_calls == [] and counts(engine, initial.factory_id)["jobs"] == 0


def test_inactive_requester_and_cross_factory_inputs_cannot_enqueue(agent_context):
    engine, _, initial, actor = agent_context
    with pytest.raises(AccessError):
        enqueue(engine, actor, "other-factory", uuid4().hex, "Query")
    with engine.begin() as db:
        db.execute(update(User).where(User.user_id == actor.user_id).values(active=False))
    with pytest.raises(AccessError, match="may no longer"):
        enqueue(engine, actor, initial.factory_id, uuid4().hex, "Query")
    assert counts(engine, initial.factory_id)["runs"] == 0
