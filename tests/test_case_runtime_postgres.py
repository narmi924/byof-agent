"""Real graph checkpoints, tool feedback, durable waits and fenced restart recovery."""

import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session
from test_dynamic_factory_postgres import control
from test_dynamic_factory_postgres import dynamic_source as dynamic_source
from test_publication_postgres import approve_and_commit
from test_publication_postgres import publishing as publishing

from packages.agent import case_runtime
from packages.agent.cases import (
    create_case,
    get_case,
    message_case,
    stop_case_turn,
    wake_for_confirmed_work,
)
from packages.agent.cases_store import CaseCursor, CaseInput, CaseOperation, CaseRecord, CaseTurn
from packages.agent.checkpoints import checkpoint_thread_id
from packages.agent.human_tasks import HumanTaskRecord, TaskAction, TaskReminder
from packages.domain.models import ActualExecution
from packages.integrations.sync import SourceBatch
from packages.persistence import Membership, connect
from packages.planning.publication import deliver_one
from packages.planning.service import request_solve, synchronize
from packages.planning.store import SolveJob
from packages.providers.gateway import GatewayError


@pytest.fixture
def case_context(publishing):
    source, reader, _, actor, *_ = publishing
    engine, factory = source[3], source[2].factory_id
    case = create_case(
        engine, actor, factory, "case-request", "Query the current machines and follow up"
    )
    try:
        yield publishing, case
    finally:
        owner = connect(os.environ["TEST_MIGRATION_DATABASE_URL"])
        assert owner.url.database == "byof_test"
        with owner.begin() as db:
            for table in (
                TaskReminder,
                TaskAction,
                HumanTaskRecord,
                CaseOperation,
                CaseInput,
                CaseTurn,
                CaseCursor,
                CaseRecord,
            ):
                db.execute(delete(table).where(table.factory_id == factory))
            thread_id = checkpoint_thread_id(factory, case["case_id"])
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                db.execute(
                    text(f"DELETE FROM byof.{table} WHERE thread_id=:thread_id"),
                    {"thread_id": thread_id},
                )
        owner.dispose()


def proposal(action, parameters):
    return json.dumps(
        {
            "action": action,
            "parameters": parameters,
            "reason_summary": "Continue from the current tool results",
        },
        ensure_ascii=False,
    )


def wait_action():
    return proposal(
        "wait", {"reason": "Waiting for the owner to add information", "recheck_minutes": 15}
    )


def test_stop_queued_message_cancels_only_that_turn(case_context):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id
    before = get_case(engine, actor, factory, case["case_id"])
    assert before["activity"]["phase"] == "QUEUED"
    target = before["activity"]["stop_target"]
    stopped = stop_case_turn(engine, actor, factory, case["case_id"], "stop-queued", target)
    assert stopped["state"] == "WAITING"
    assert stop_case_turn(engine, actor, factory, case["case_id"], "stop-queued", target) == stopped
    after = get_case(engine, actor, factory, case["case_id"])
    assert after["activity"]["phase"] == "STOPPED"
    with Session(engine) as db:
        stopped_turn = db.get(CaseTurn, after["inputs"][0]["turn_id"])
        assert stopped_turn is not None and stopped_turn.state == "CANCELLED"
    assert not case_runtime.process_case(
        engine, reader, FeedbackModel(source[2].resources[0].resource_id)
    )
    message_case(engine, actor, factory, case["case_id"], "another-message", "Please analyze again")
    assert (
        stop_case_turn(engine, actor, factory, case["case_id"], "stop-queued", target)["state"]
        == "WAITING"
    )
    assert get_case(engine, actor, factory, case["case_id"])["activity"]["phase"] == "QUEUED"


def test_stop_during_model_call_fences_late_result(case_context):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id

    class LateModel:
        def complete(self, prompt):
            visible = get_case(engine, actor, factory, case["case_id"])
            assert visible["activity"]["phase"] == "THINKING"
            stop_case_turn(
                engine,
                actor,
                factory,
                case["case_id"],
                "stop-running",
                visible["activity"]["stop_target"],
            )
            return proposal("query", {"entity": "resources", "identity": None, "offset": 0})

    assert case_runtime.process_case(engine, reader, LateModel())
    after = get_case(engine, actor, factory, case["case_id"])
    assert after["activity"]["phase"] == "STOPPED"
    assert after["operations"] == []
    assert after["error_code"] is None
    assert not case_runtime.process_case(engine, reader, LateModel())


@pytest.mark.parametrize("increase_budget", [False, True])
@pytest.mark.parametrize("study", [False, True])
def test_solver_completion_cannot_reset_unchanged_problem_budget(
    case_context, increase_budget, study
):
    from packages.agent.cases import wake_completed_jobs

    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id

    class RepeatsSolve:
        calls = 0

        def complete(self, prompt):
            self.calls += 1
            if study:
                return proposal(
                    "evaluate_business_options",
                    {
                        "kind": "material_shortage",
                        "total_time_limit": 60 if increase_budget and self.calls > 1 else 30,
                    },
                )
            return proposal(
                "solve_scenario",
                {
                    "allow_overtime": False,
                    "time_limit": 60 if increase_budget and self.calls > 1 else 30,
                },
            )

    model = RepeatsSolve()
    expected_jobs = 2 if increase_budget else 1
    for _ in range(expected_jobs):
        assert case_runtime.process_case(engine, reader, model)
        with Session(engine) as db, db.begin():
            job = db.scalar(
                select(SolveJob).where(
                    SolveJob.case_id == case["case_id"], SolveJob.state == "QUEUED"
                )
            )
            assert job is not None
            job.state, job.error_code = "FAILED", "TIME_LIMIT"
        assert wake_completed_jobs(engine) == 1
    assert case_runtime.process_case(engine, reader, model)
    detail = get_case(engine, actor, factory, case["case_id"])
    assert detail["state"] == "WAITING"
    assert detail["operations"][-1]["result"]["code"] == "UNCHANGED_SEARCH"
    assert not case_runtime.process_case(engine, reader, model)
    # A plain re-solve without any comparison is first pointed at the treatment options once.
    assert model.calls == expected_jobs + (1 if study else 2)
    if not study:
        assert detail["operations"][-2]["result"]["code"] == "SEARCH_NEEDS_OPTIONS"


@pytest.mark.parametrize("proven_infeasible", [False, True])
def test_repeated_search_explains_whether_infeasibility_was_proven(
    case_context, monkeypatch, proven_infeasible
):
    from packages.agent import planning_context

    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id
    monkeypatch.setattr(
        planning_context,
        "solve_limit_reason",
        lambda *args: None if proven_infeasible else "UNCHANGED_SEARCH",
    )
    monkeypatch.setattr(planning_context, "repeated_infeasible", lambda *args: proven_infeasible)

    class RepeatsSolve:
        def complete(self, prompt):
            return proposal("solve_scenario", {"allow_overtime": False, "time_limit": 30})

    assert case_runtime.process_case(engine, reader, RepeatsSolve())
    detail = get_case(engine, actor, factory, case["case_id"])
    result = detail["operations"][-1]["result"]
    assert detail["state"] == "WAITING"
    assert result["code"] == ("UNCHANGED_INFEASIBLE" if proven_infeasible else "UNCHANGED_SEARCH")
    # A proof and a search that ran out of time read differently, without disclaimers.
    if proven_infeasible:
        assert "No schedule is possible" in result["summary"]
        assert "time limit" not in result["summary"]
    else:
        assert "within the time limit" in result["summary"]
        assert "No schedule is possible" not in result["summary"]
    with Session(engine) as db:
        assert db.scalar(select(SolveJob).where(SolveJob.case_id == case["case_id"])) is None


def test_stop_solver_prevents_completion_and_source_events_restarting_analysis(case_context):
    from packages.agent.cases import wake_completed_jobs
    from packages.agent.cases_store import add_input

    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id

    class SolveOnce:
        def complete(self, prompt):
            return proposal("solve_scenario", {"allow_overtime": False, "time_limit": 30})

    assert case_runtime.process_case(engine, reader, SolveOnce())
    detail = get_case(engine, actor, factory, case["case_id"])
    assert detail["activity"]["phase"] == "SOLVING"
    stop_case_turn(
        engine, actor, factory, case["case_id"], "stop-solver", detail["activity"]["stop_target"]
    )
    with Session(engine) as db, db.begin():
        job = db.scalar(select(SolveJob).where(SolveJob.case_id == case["case_id"]))
        job.state, job.error_code = "FAILED", "TIME_LIMIT"
        row = db.get(CaseRecord, case["case_id"], with_for_update=True)
        add_input(db, row, "source-after-stop", "SOURCE", {"event_ids": []})
    assert wake_completed_jobs(engine) == 0
    assert not case_runtime.process_case(engine, reader, SolveOnce())
    assert get_case(engine, actor, factory, case["case_id"])["activity"]["phase"] == "STOPPED"
    message_case(
        engine, actor, factory, case["case_id"], "resume-explicit", "Please check the machines"
    )
    assert case_runtime.process_case(
        engine, reader, FeedbackModel(source[2].resources[0].resource_id)
    )


def test_review_minutes_resolve_from_factory_clock_and_persist_absolute_intent(case_context):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id

    class ReviewModel:
        def complete(self, prompt):
            return proposal(
                "solve_scenario",
                {
                    "allow_overtime": False,
                    "time_limit": 30,
                    "review_minutes": 15,
                },
            )

    assert case_runtime.process_case(engine, reader, ReviewModel())
    with Session(engine) as db:
        job = db.scalar(select(SolveJob).where(SolveJob.case_id == case["case_id"]))
        assert job.new_actions_not_before == source[2].snapshot_clock + timedelta(minutes=15)
    detail = get_case(engine, actor, factory, case["case_id"])
    assert "review_minutes" not in detail["operations"][0]["parameters"]


def test_a_found_plan_can_be_solved_again_after_it_expires(case_context):
    from packages.agent.planning_context import solve_limit_reason
    from services.solver_worker.main import run_once

    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id
    job = request_solve(
        engine,
        actor,
        factory,
        request_id="found-once",
        allow_overtime=False,
        time_limit=5,
        case_id=case["case_id"],
    )
    assert run_once(engine)
    with Session(engine) as db:
        assert db.get(SolveJob, job.job_id).state == "SUCCEEDED"
    current = synchronize(engine, reader, factory)
    with Session(engine) as db:
        # Only a repeated failed search is refused; the per-message search cap still applies.
        assert (
            solve_limit_reason(
                db, case["case_id"], current, {"allow_overtime": False, "time_limit": 5}
            )
            is None
        )


def test_distinct_conversations_share_cpu_but_retain_their_requests(case_context):
    from packages.planning.service import claim_job, complete_job

    context, case = case_context
    source, _, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id
    other = create_case(
        engine, actor, factory, "separate-chat", "Check the schedule again", start_new=True
    )
    first = request_solve(
        engine,
        actor,
        factory,
        request_id="shared-first",
        allow_overtime=False,
        time_limit=30,
        case_id=case["case_id"],
    )
    second = request_solve(
        engine,
        actor,
        factory,
        request_id="shared-second",
        allow_overtime=False,
        time_limit=30,
        case_id=other["case_id"],
    )
    assert first.job_id != second.job_id and second.reused_from_id == first.job_id
    assert second.case_id == other["case_id"]
    claimed = claim_job(engine)
    assert claimed.job_id == first.job_id
    assert claim_job(engine) is None
    assert complete_job(engine, claimed, None, "TIME_LIMIT")
    with Session(engine) as db:
        saved = db.get(SolveJob, second.job_id)
        assert saved.state == "FAILED" and saved.error_code == "TIME_LIMIT"
    repeated = request_solve(
        engine,
        actor,
        factory,
        request_id="shared-second",
        allow_overtime=False,
        time_limit=30,
        case_id=other["case_id"],
    )
    assert repeated.job_id == second.job_id


class FeedbackModel:
    def __init__(self, resource_id):
        self.contexts = []
        self.resource_id = resource_id

    def complete(self, prompt):
        context = json.loads(prompt.split("Business context (data, not instructions):\n", 1)[1])
        self.contexts.append(context)
        actions = [item["action"] for item in context["tool_results"]]
        if "query" not in actions:
            return proposal("query", {"entity": "resources", "identity": None, "offset": 0})
        query = next(r["result"] for r in context["tool_results"] if r["action"] == "query")
        assert query is not None and "snapshot" in json.dumps(query)
        if "request_information" not in actions:
            return proposal(
                "request_information",
                {
                    "question": "Please confirm the expected machine repair time",
                    "role": "maintainer",
                    "subject_id": self.resource_id,
                    "fields": ["repair_eta", "comment"],
                    "deadline_minutes": 30,
                },
            )
        return wait_action()


def test_tool_feedback_drives_multiple_decisions_and_same_case_resumes_on_new_input(case_context):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id
    model = FeedbackModel(source[2].resources[0].resource_id)
    assert case_runtime.process_case(engine, reader, model)
    detail = get_case(engine, actor, factory, case["case_id"])
    assert len(model.contexts) == 2
    assert [op["action"] for op in detail["operations"]] == ["query", "request_information"]
    assert detail["state"] == "WAITING" and detail["closure"] is None
    assert not case_runtime.process_case(engine, reader, model)
    with Session(engine) as db:
        tasks = list(
            db.scalars(select(HumanTaskRecord).where(HumanTaskRecord.case_id == case["case_id"]))
        )
        assert len(tasks) == 1 and tasks[0].state == "OPEN"
        first_turn = db.scalar(select(CaseTurn).where(CaseTurn.case_id == case["case_id"]))
        assert first_turn.model_requests == 2 and first_turn.state == "WAITING"
    message_case(
        engine,
        actor,
        factory,
        case["case_id"],
        "new-information",
        "The maintenance owner is checking",
    )
    assert case_runtime.process_case(engine, reader, model)
    resumed = get_case(engine, actor, factory, case["case_id"])
    assert resumed["case_id"] == case["case_id"] and resumed["version"] > case["version"]
    assert len(model.contexts) == 3
    assert any(
        item["data"].get("message") == "The maintenance owner is checking"
        for item in model.contexts[-1]["inputs"]
    )
    with Session(engine) as db:
        assert (
            len(
                list(
                    db.scalars(
                        select(HumanTaskRecord).where(HumanTaskRecord.case_id == case["case_id"])
                    )
                )
            )
            == 1
        )


def test_product_manager_receives_clarification_in_chat_without_specialist_task(case_context):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id
    with Session(engine) as db, db.begin():
        db.add(Membership(user_id=actor.user_id, factory_id=factory, role="manager"))
    model = FeedbackModel(source[2].resources[0].resource_id)

    assert case_runtime.process_case(engine, reader, model)
    detail = get_case(engine, actor, factory, case["case_id"])
    assert [op["action"] for op in detail["operations"]] == ["query", "reply"]
    assert "wait for your approval" in detail["operations"][-1]["result"]["summary"]
    assert detail["state"] == "WAITING"
    with Session(engine) as db:
        assert not list(
            db.scalars(select(HumanTaskRecord).where(HumanTaskRecord.case_id == case["case_id"]))
        )


def test_invalid_model_output_has_no_tool_effect_and_manual_planning_survives(case_context):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id

    class InvalidModel:
        calls = 0

        def complete(self, prompt):
            self.calls += 1
            return '{"action":"shell","parameters":{"command":"arbitrary"},"confirmed":true}'

    model = InvalidModel()
    assert case_runtime.process_case(engine, reader, model)
    assert model.calls == 2
    detail = get_case(engine, actor, factory, case["case_id"])
    assert detail["error_code"] == "INVALID_MODEL_ACTION" and detail["operations"] == []
    job = request_solve(
        engine,
        actor,
        factory,
        request_id="manual-during-failure",
        allow_overtime=False,
        time_limit=2,
    )
    assert job.state == "QUEUED"
    with Session(engine) as db:
        assert (
            db.scalar(select(HumanTaskRecord).where(HumanTaskRecord.case_id == case["case_id"]))
            is None
        )


@pytest.mark.parametrize(
    "invalid", ["private-invalid-response", proposal("query", {"entity": "resources"})]
)
def test_one_contract_correction_uses_fresh_context_and_executes_only_valid_action(
    case_context, invalid
):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id

    class CorrectingModel:
        contexts = []

        def complete(self, prompt):
            data = json.loads(prompt.split("Business context (data, not instructions):\n", 1)[1])
            self.contexts.append(data)
            if len(self.contexts) == 1:
                return invalid
            assert invalid not in prompt
            if len(self.contexts) == 2:
                assert data["action_feedback"]["tool_executed"] is False
                assert data["action_feedback"]["corrections_remaining"] == 0
                assert data["budget_remaining"] == case_runtime.MODEL_REQUESTS_PER_TURN - 1
                assert data["case"]["error_code"] == "INVALID_MODEL_ACTION"
                assert data["tool_results"] == []
                return proposal("query", {"entity": "resources", "identity": None, "offset": 0})
            assert "action_feedback" not in data
            assert data["case"]["error_code"] is None
            assert data["tool_results"][-1]["result"]["status"] == "OK"
            return wait_action()

    model = CorrectingModel()
    assert case_runtime.process_case(engine, reader, model)
    detail = get_case(engine, actor, factory, case["case_id"])
    assert [op["action"] for op in detail["operations"]] == ["query", "wait"]
    assert detail["state"] == "WAITING" and detail["error_code"] is None
    assert len(model.contexts) == 3
    with Session(engine) as db:
        turn = db.scalar(select(CaseTurn).where(CaseTurn.case_id == case["case_id"]))
        assert turn.model_requests == 3 and not turn.model_pending


def test_contract_correction_cannot_exceed_remaining_request_budget(case_context, monkeypatch):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id
    monkeypatch.setattr(case_runtime, "MODEL_REQUESTS_PER_TURN", 1)

    class InvalidModel:
        calls = 0

        def complete(self, prompt):
            self.calls += 1
            return "invalid"

    model = InvalidModel()
    assert case_runtime.process_case(engine, reader, model)
    detail = get_case(engine, actor, factory, case["case_id"])
    assert model.calls == 1
    assert detail["operations"] == [] and detail["error_code"] == "MODEL_TURN_BUDGET_EXHAUSTED"


def test_unknown_model_network_result_is_not_automatically_retried(case_context):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id

    class UnavailableModel:
        calls = 0

        def complete(self, prompt):
            self.calls += 1
            raise TimeoutError("private-provider-detail")

    model = UnavailableModel()
    assert case_runtime.process_case(engine, reader, model)
    detail = get_case(engine, actor, factory, case["case_id"])
    assert model.calls == 1 and detail["operations"] == []
    assert detail["error_code"] == "MODEL_RESULT_UNKNOWN"
    assert "private-provider-detail" not in json.dumps(detail, default=str)


def test_gateway_429_is_reported_without_claiming_a_model_result(case_context):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id

    class RateLimitedModel:
        calls = 0

        def complete(self, prompt):
            self.calls += 1
            raise GatewayError("private-provider-detail", status_code=429)

    model = RateLimitedModel()
    assert case_runtime.process_case(engine, reader, model)
    detail = get_case(engine, actor, factory, case["case_id"])
    assert model.calls == 1 and detail["operations"] == []
    assert detail["error_code"] == "MODEL_GATEWAY_429"
    assert "private-provider-detail" not in json.dumps(detail, default=str)


@pytest.mark.parametrize("downtime_past_deadline", [False, True])
def test_tool_effect_before_checkpoint_is_recovered_with_same_operation_and_one_task(
    case_context, monkeypatch, downtime_past_deadline
):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine = source[3]

    class AskThenWait:
        calls = 0

        def complete(self, prompt):
            self.calls += 1
            data = json.loads(prompt.split("Business context (data, not instructions):\n", 1)[1])
            if any(r["action"] == "request_information" for r in data["tool_results"]):
                return wait_action()
            return proposal(
                "request_information",
                {
                    "question": "Please confirm the current repair situation",
                    "role": "maintainer",
                    "subject_id": source[2].resources[0].resource_id,
                    "fields": ["comment"],
                    "deadline_minutes": 30,
                },
            )

    model = AskThenWait()
    actual_execute = case_runtime.execute_operation

    def crash_after_effect(*args):
        result = actual_execute(*args)
        assert result["status"] == "PENDING" and result["task_id"]
        raise SystemExit("simulated worker termination after durable effect")

    monkeypatch.setattr(case_runtime, "execute_operation", crash_after_effect)
    with pytest.raises(SystemExit):
        case_runtime.process_case(engine, reader, model)
    with Session(engine) as db, db.begin():
        turn = db.scalar(
            select(CaseTurn).where(CaseTurn.case_id == case["case_id"]).with_for_update()
        )
        assert turn.model_requests == 1
        assert turn.lease_until < turn.deadline
        turn.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        if downtime_past_deadline:
            turn.deadline = datetime.now(UTC) - timedelta(seconds=1)
        original_task = db.scalar(
            select(HumanTaskRecord).where(HumanTaskRecord.case_id == case["case_id"])
        )
        identity = original_task.task_id
    monkeypatch.setattr(case_runtime, "execute_operation", actual_execute)
    assert case_runtime.process_case(engine, reader, model)
    with Session(engine) as db:
        tasks = list(
            db.scalars(select(HumanTaskRecord).where(HumanTaskRecord.case_id == case["case_id"]))
        )
        assert len(tasks) == 1 and tasks[0].task_id == identity
        turn = db.scalar(select(CaseTurn).where(CaseTurn.case_id == case["case_id"]))
        assert turn.model_requests == 1 and turn.attempts == 2 and turn.state == "WAITING"
    assert model.calls == 1


@pytest.mark.parametrize("change", ["new_input", "source_revision"])
def test_finish_cannot_close_over_concurrent_input_or_new_source_facts(
    case_context, monkeypatch, change
):
    context, case = case_context
    source, reader, writer, actor, candidate_id, _ = context
    engine, factory = source[3], source[2].factory_id
    _, release = approve_and_commit(context)
    assert deliver_one(engine, reader, writer)
    for index in range(2):
        assert (
            control(source, f"complete-{index}", "clock.step", {"minutes": 60}).status_code == 200
        )
    snapshot = synchronize(engine, reader, factory)
    assert len(snapshot.actuals) == 8 and all(a.state == "COMPLETED" for a in snapshot.actuals)
    with Session(engine) as db, db.begin():
        row = db.get(CaseRecord, case["case_id"], with_for_update=True)
        row.context = {**row.context, "candidate_ids": [candidate_id]}

    class FinishThenWait:
        calls = 0

        def complete(self, prompt):
            self.calls += 1
            if self.calls > 1:
                return wait_action()
            return proposal(
                "finish",
                {
                    "evidence_release_id": release.release_id,
                    "risk_summary": "Execution completion checked",
                },
            )

    execute = case_runtime.execute_operation

    def arrive_between_evidence_and_commit(*args):
        result = execute(*args)
        if args[-1].action == "finish":
            assert result["status"] == "RESOLVED", result
            if change == "new_input":
                message_case(
                    engine,
                    actor,
                    factory,
                    case["case_id"],
                    "new-risk",
                    "There is a new risk to handle",
                )
            else:
                assert (
                    control(source, "next-clock", "clock.step", {"minutes": 1}).status_code == 200
                )
                synchronize(engine, reader, factory)
        return result

    monkeypatch.setattr(case_runtime, "execute_operation", arrive_between_evidence_and_commit)
    model = FinishThenWait()
    assert case_runtime.process_case(engine, reader, model)
    result = get_case(engine, actor, factory, case["case_id"])
    assert model.calls == 2 and result["state"] == "WAITING" and result["closure"] is None
    assert result["operations"][0]["result"]["code"] == "CASE_FACTS_CHANGED"
    if change == "new_input":
        assert any(
            i["payload"].get("message") == "There is a new risk to handle" and i["turn_id"] is None
            for i in result["inputs"]
        )


def test_rejected_finish_does_not_loop_when_only_summary_changes(case_context, monkeypatch):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id
    execute = case_runtime.execute_operation

    def reject_finish(*args):
        if args[-1].action == "finish":
            return {
                "status": "REJECTED",
                "code": "UNRESOLVED_SCOPE",
                "summary": "The order scope has changed; check the current issue again.",
            }
        return execute(*args)

    monkeypatch.setattr(case_runtime, "execute_operation", reject_finish)

    class RepeatsFinish:
        calls = 0

        def complete(self, prompt):
            self.calls += 1
            return proposal(
                "finish",
                {
                    "evidence_release_id": "same-release",
                    "risk_summary": f"In place, wording {self.calls}",
                },
            )

    model = RepeatsFinish()
    assert case_runtime.process_case(engine, reader, model)
    detail = get_case(engine, actor, factory, case["case_id"])
    assert model.calls == 2
    assert detail["error_code"] is None and detail["closure"] is None
    assert detail["operations"][-1]["action"] == "reply"
    assert "The order scope has changed" in detail["operations"][-1]["result"]["summary"]


def test_repeated_queries_are_bounded_and_cannot_claim_case_resolved(case_context):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id

    class RepeatingModel:
        calls = 0

        def complete(self, prompt):
            self.calls += 1
            return proposal("query", {"entity": "inventory", "identity": None, "offset": 0})

    model = RepeatingModel()
    assert case_runtime.process_case(engine, reader, model)
    detail = get_case(engine, actor, factory, case["case_id"])
    assert model.calls == case_runtime.MODEL_REQUESTS_PER_TURN and detail["state"] == "WAITING"
    assert detail["error_code"] == "MODEL_TURN_BUDGET_EXHAUSTED" and detail["closure"] is None
    assert [r["result"]["code"] for r in detail["operations"] if r["result"].get("code")] == [
        "NO_NEW_INFORMATION"
    ] * (case_runtime.MODEL_REQUESTS_PER_TURN - 2)
    with Session(engine) as db:
        assert len(list(db.scalars(select(SolveJob).where(SolveJob.factory_id == factory)))) == 1


def test_four_distinct_fact_queries_can_still_produce_a_manager_reply(case_context):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id

    class AnalyzeThenReply:
        calls = 0

        def complete(self, prompt):
            self.calls += 1
            checked = json.loads(prompt.split("Business context (data, not instructions):\n", 1)[1])
            queried = [
                item["result"]["entity"]
                for item in checked["tool_results"]
                if item["action"] == "query" and item["state"] == "DONE"
            ]
            if len(queried) < 4:
                entity = ("orders", "actuals", "inventory", "resources")[len(queried)]
                return proposal("query", {"entity": entity, "identity": None, "offset": 0})
            return proposal(
                "reply",
                {
                    "message": "Checked orders, progress, stock and machines; confirm whether rescheduling is needed.",
                    "choices": [
                        "Calculate a recovery plan",
                        "Check the cost and due date details on the card before deciding",
                        "Approve this option",
                    ],
                },
            )

    model = AnalyzeThenReply()
    assert case_runtime.process_case(engine, reader, model)
    detail = get_case(engine, actor, factory, case["case_id"])
    assert model.calls == 5
    assert [item["action"] for item in detail["operations"]] == ["query"] * 4 + ["reply"]
    assert detail["error_code"] is None and detail["state"] == "WAITING"
    # Only choices that ask for new work remain; approval and card pointers repeat the reply.
    assert detail["operations"][-1]["result"]["choices"] == ["Calculate a recovery plan"]


def test_turn_budget_can_continue_in_the_same_case(case_context, monkeypatch):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id
    monkeypatch.setattr(case_runtime, "MODEL_REQUESTS_PER_TURN", 1)

    class QueryOnce:
        def complete(self, prompt):
            return proposal("query", {"entity": "resources", "identity": None, "offset": 0})

    assert case_runtime.process_case(engine, reader, QueryOnce())
    paused = get_case(engine, actor, factory, case["case_id"])
    assert paused["error_code"] == "MODEL_TURN_BUDGET_EXHAUSTED"

    message_case(
        engine,
        actor,
        factory,
        case["case_id"],
        "continue-request",
        "Continue analyzing the checked shop floor",
    )

    class ReplyOnce:
        def complete(self, prompt):
            context = json.loads(prompt.split("Business context (data, not instructions):\n", 1)[1])
            assert any(result["action"] == "query" for result in context["tool_results"])
            return proposal(
                "reply",
                {"message": "Machine information checked; continuing with the current case."},
            )

    assert case_runtime.process_case(engine, reader, ReplyOnce())
    resumed = get_case(engine, actor, factory, case["case_id"])
    assert resumed["case_id"] == paused["case_id"]
    assert [item["action"] for item in resumed["operations"]] == ["query", "reply"]
    assert resumed["error_code"] is None and resumed["state"] == "WAITING"


def test_prepared_last_budgeted_decision_recovers_without_new_model_call(case_context, monkeypatch):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id
    monkeypatch.setattr(case_runtime, "MODEL_REQUESTS_PER_TURN", 1)

    class AskOnce:
        calls = 0

        def complete(self, prompt):
            self.calls += 1
            return proposal(
                "request_information",
                {
                    "question": "Please confirm the current machine state",
                    "role": "maintainer",
                    "subject_id": source[2].resources[0].resource_id,
                    "fields": ["comment"],
                    "deadline_minutes": 30,
                },
            )

    model = AskOnce()
    decide = case_runtime._decide

    def crash_after_preparation(*args):
        result = decide(*args)
        assert result["operation_id"]
        raise SystemExit("termination between durable decision and graph checkpoint")

    monkeypatch.setattr(case_runtime, "_decide", crash_after_preparation)
    with pytest.raises(SystemExit):
        case_runtime.process_case(engine, reader, model)
    with Session(engine) as db, db.begin():
        turn = db.scalar(
            select(CaseTurn).where(CaseTurn.case_id == case["case_id"]).with_for_update()
        )
        assert turn.model_requests == 1 and not turn.model_pending
        turn.lease_until = turn.deadline = datetime.now(UTC) - timedelta(seconds=1)
    monkeypatch.setattr(case_runtime, "_decide", decide)
    assert case_runtime.process_case(engine, reader, model)
    result = get_case(engine, actor, factory, case["case_id"])
    assert model.calls == 1 and len(result["operations"]) == 1
    assert result["operations"][0]["state"] == "DONE"
    assert result["operations"][0]["result"]["task_id"]
    assert (
        result["error_code"] is None
    )  # A persisted human request waits without another model call.
    with Session(engine) as db:
        tasks = list(
            db.scalars(select(HumanTaskRecord).where(HumanTaskRecord.case_id == case["case_id"]))
        )
        assert len(tasks) == 1


def test_history_cursor_preserves_timestamp_ties_without_losing_old_messages(case_context):
    from packages.agent.cases import case_history

    context, case = case_context
    source, _, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id
    now = datetime.now(UTC)
    with Session(engine) as db, db.begin():
        for index in range(205):
            db.add(
                CaseInput(
                    input_id=f"{case['case_id']}-{index:04d}",
                    factory_id=factory,
                    case_id=case["case_id"],
                    input_key=f"history:{index}",
                    kind="USER",
                    payload={"message": str(index)},
                    payload_hash="a" * 64,
                    created_at=now,
                    available_at=now,
                )
            )
    page = get_case(engine, actor, factory, case["case_id"])
    seen = {row["input_id"] for row in page["inputs"]}
    while page["history_cursor"]:
        cursor = page["history_cursor"]
        page = case_history(engine, actor, factory, case["case_id"], cursor["at"], cursor["id"])
        identities = {row["input_id"] for row in page["inputs"]}
        assert not identities & seen
        seen |= identities
    assert len(seen) == 206  # Includes the original case opening message.


def test_new_manager_request_can_adjust_search_after_prior_budget_but_not_repeat_it(case_context):
    from packages.agent.planning_context import solve_limit_reason

    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id
    for index in range(1, 5):
        request_solve(
            engine,
            actor,
            factory,
            request_id=f"budget-{index}",
            allow_overtime=False,
            time_limit=index,
            case_id=case["case_id"],
        )
    current = synchronize(engine, reader, factory)
    with Session(engine) as db:
        assert (
            solve_limit_reason(
                db, case["case_id"], current, {"allow_overtime": False, "time_limit": 5}
            )
            == "PROBLEM_SEARCH_LIMIT"
        )
    message_case(
        engine,
        actor,
        factory,
        case["case_id"],
        "human-adjustment",
        "Increase the calculation time and compare again",
    )
    with Session(engine) as db:
        assert (
            solve_limit_reason(
                db, case["case_id"], current, {"allow_overtime": False, "time_limit": 5}
            )
            is None
        )
        assert (
            solve_limit_reason(
                db, case["case_id"], current, {"allow_overtime": False, "time_limit": 3}
            )
            == "UNCHANGED_SEARCH"
        )


def test_confirmed_remaining_work_continues_a_conversation_waiting_for_it(case_context):
    context, case = case_context
    source, reader, _, actor, *_ = context
    engine, factory = source[3], source[2].factory_id
    current = synchronize(engine, reader, factory)
    revision = int(current.source.source_revision)
    with Session(engine) as db, db.begin():
        db.add(
            SolveJob(
                job_id="waiting-for-remaining-work",
                factory_id=factory,
                request_id="waiting-for-remaining-work",
                requester_id=actor.user_id,
                snapshot_id=current.snapshot_id,
                allow_overtime=False,
                time_limit=5,
                state="FAILED",
                created_at=datetime.now(UTC),
                attempts=1,
                error_code="WIP_CONFIRMATION_REQUIRED",
                case_id=case["case_id"],
            )
        )
        db.add(
            SourceBatch(
                run_id=current.run_id,
                revision=revision + 1,
                factory_id=factory,
                content_hash="c" * 64,
                document={"cause": "execution.confirm_remaining", "events": []},
                received_at=datetime.now(UTC),
            )
        )
    confirmed = current.model_copy(
        update={"source": current.source.model_copy(update={"source_revision": str(revision + 1)})}
    )
    # Another stopped operation still without its remaining work would stop any calculation.
    other = ActualExecution.model_construct(
        operation_id="other-stop", state="BLOCKED", remaining_minutes=None
    )
    with Session(engine) as db, db.begin():
        still_stopped = confirmed.model_copy(update={"actuals": (*confirmed.actuals, other)})
        assert wake_for_confirmed_work(db, still_stopped, revision) == 0
    for _ in range(2):
        with Session(engine) as db, db.begin():
            assert wake_for_confirmed_work(db, confirmed, revision) == 1
    with Session(engine) as db:
        woken = list(
            db.scalars(
                select(CaseInput).where(
                    CaseInput.case_id == case["case_id"], CaseInput.kind == "SOURCE"
                )
            )
        )
        assert len(woken) == 1 and "remaining work" in woken[0].payload["message"]
        assert woken[0].turn_id is None
