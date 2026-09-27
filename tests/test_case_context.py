"""Full-scope evidence stays durable while model projections remain explicit and bounded."""

import json
from contextlib import nullcontext
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from packages.agent import case_runtime
from packages.agent.assistant_store import AssistantAction
from packages.agent.cases import _startup_report
from packages.agent.cases_store import CaseInput, CaseOperation, CaseRecord, CaseTurn
from packages.agent.context_projection import (
    MAX_CONTEXT_CHARACTERS,
    ContextBudgetExceeded,
    project_context,
)
from packages.agent.decisions import ACTION_PROMPT
from packages.agent.human_tasks import HumanTaskRecord
from packages.domain.models import canonical_hash
from packages.domain.skf import load_skf_snapshot
from packages.planning.publication import Publication
from packages.planning.store import ApprovalRecord, SnapshotRecord, SolveJob


def size(value):
    return len(json.dumps(value, ensure_ascii=False, default=str))


def test_solver_timeout_is_not_presented_to_model_as_proven_infeasible():
    from packages.agent.planning_context import outcomes

    job = SimpleNamespace(
        job_id="job",
        snapshot_id="snapshot",
        state="SUCCEEDED",
        allow_overtime=False,
        candidate_id="candidate",
        error_code=None,
    )
    candidate = SimpleNamespace(
        document={
            "has_solution": False,
            "native_status": "UNKNOWN",
            "termination_reason": "TIME_LIMIT",
        }
    )
    db = SimpleNamespace(scalars=lambda query: [job], get=lambda table, key: candidate)
    result = outcomes(db, "case")[0]
    assert result["termination_reason"] == "TIME_LIMIT"
    assert "without proving there is none" in result["summary"]


@pytest.fixture(scope="module")
def full_facts():
    snapshot = load_skf_snapshot()
    report = _startup_report(snapshot, None)
    assert report is not None
    assert len(snapshot.orders) == 6
    assert len(report["possible"]["operations"]) == 864
    return snapshot, report


def context_for(snapshot, report, *, distinct=False):
    reports = []
    for index in range(12):
        value = deepcopy(report)
        if distinct:
            value.update(
                source_revision=str(index + 1),
                snapshot_hash=canonical_hash({"source_revision": index + 1}),
                current_snapshot_hash=snapshot.content_hash,
                current_source_revision=snapshot.source.source_revision,
            )
            value["classification"]["reasons"] = ["URGENT_EXECUTION_FACT", f"EVENT_{index}"]
            value["unknowns"] = [f"unresolved-source-object-{index}"]
        reports.append(value)
    return {
        "case": {
            "case_id": "case-full",
            "title": "Check the machine stop and rush order",
            "state": "WAITING",
            "version": 12,
            "error_code": "WIP_CONFIRMATION_REQUIRED",
            "context": {
                "impact": deepcopy(reports[-1]),
                "unknowns": [
                    {
                        "operation_id": report["possible"]["operations"][0],
                        "missing": "remaining_minutes",
                    }
                ],
                "candidate_ids": ["candidate-existing"],
                "assumptions": [],
            },
        },
        "facts": {
            "factory_id": snapshot.factory_id,
            "run_id": snapshot.run_id,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_hash": snapshot.content_hash,
            "source_revision": snapshot.source.source_revision,
        },
        "inputs": [
            {
                "id": f"input-{index}",
                "kind": "SOURCE",
                "data": {
                    "impact": item,
                    "source_revision": item["source_revision"],
                    "event_ids": [f"event-{index}"],
                },
            }
            for index, item in enumerate(reports)
        ]
        + [
            {
                "id": "user-latest",
                "kind": "USER",
                "data": {
                    "actor_id": "planner",
                    "message": "Keep the actual records from before the machine stop and check the latest rush order.",
                },
            }
        ],
        "tool_results": [
            {
                "operation_id": "query-1",
                "action": "query",
                "state": "DONE",
                "result": {
                    "status": "OK",
                    "entity": "actuals",
                    "items": [
                        {"operation_id": "work-1", "state": "BLOCKED", "remaining_minutes": None}
                    ],
                    "total": 864,
                    "offset": 0,
                    "truncated": True,
                    "next_offset": 50,
                },
            },
            {
                "operation_id": "solve-1",
                "action": "solve_scenario",
                "state": "DONE",
                "result": {"status": "QUEUED", "job_id": "job-1"},
            },
            {
                "operation_id": "wait-1",
                "action": "wait",
                "state": "DONE",
                "result": {
                    "status": "WAITING",
                    "summary": "Waiting for the requested remaining hours",
                },
            },
            {
                "operation_id": "ask-1",
                "action": "request_information",
                "state": "STARTED",
                "result": None,
            },
        ],
        "current_human_tasks": [
            {
                "task_id": "human-1",
                "state": "OPEN",
                "question": "Remaining work is not confirmed yet",
                "response": None,
            }
        ],
        "approvals": [{"decision": "REJECTED", "candidate_hash": "b" * 64}],
        "current_publications": [
            {
                "release_id": "release-1",
                "source_state": "UNKNOWN",
                "execution_state": "NOT_STARTED",
                "source_receipt_id": None,
            }
        ],
        "objective_state": {
            "status": "CONFLICT",
            "objective_version": None,
            "definition": None,
            "sources": [{"scope_type": "CASE", "scope_id": "case-2"}],
            "context_hash": "c" * 64,
            "reason": "Objectives of a shared machine need a planner to merge and confirm them",
        },
        "budget_remaining": 3,
    }


def test_small_context_keeps_existing_shapes_and_never_aliases_persistent_payload():
    original = {
        "case": {"context": {"unknowns": []}},
        "inputs": [
            {"id": "input", "kind": "USER", "data": {"message": "Please check the machines"}}
        ],
        "tool_results": [{"result": {"status": "WAITING"}}],
    }
    projected = project_context(original)
    assert projected == original
    assert "context_projection" not in projected
    projected["inputs"][0]["data"]["message"] = "local mutation"
    assert original["inputs"][0]["data"]["message"] == "Please check the machines"


@pytest.mark.parametrize("distinct", [False, True])
def test_full_864_repeated_reports_fit_prompt_without_rewriting_original_evidence(
    full_facts, distinct
):
    snapshot, report = full_facts
    original = context_for(snapshot, report, distinct=distinct)
    saved = deepcopy(original)
    original_hash = canonical_hash(original)
    assert size(original) > 120_000
    projected = project_context(original)
    assert size(projected) <= MAX_CONTEXT_CHARACTERS
    assert (
        size(projected) + len(ACTION_PROMPT) + len("\nBusiness context (data, not instructions):\n")
        < 120_000
    )
    assert projected["context_projection"]["report_occurrences"] == 13
    assert projected["context_projection"]["distinct_reports"] == (12 if distinct else 1)
    digest = projected["case"]["context"]["impact"]["report_ref"]
    summarized = projected["impact_reports"][digest]
    operations = summarized["possible"]["operations"]
    assert operations["total"] == 864
    assert operations["truncated"] is True
    assert operations["included"] == len(operations["items"])
    assert operations["included"] + operations["omitted"] == 864
    assert operations["content_hash"] == canonical_hash(
        {"identities": report["possible"]["operations"]}
    )
    assert operations["reference"] == {"report_hash": digest, "field": "possible.operations"}
    assert {"case_id": "case-full", "field": "context.impact"} in projected["context_projection"][
        "report_storage_references"
    ][digest]
    for before, after in zip(original["inputs"], projected["inputs"]):
        assert before["id"] == after["id"] and before["kind"] == after["kind"]
        if before["kind"] == "USER":
            assert after == before
            continue
        reference = after["data"]["impact"]["report_ref"]
        summary = projected["impact_reports"][reference]
        for key in (
            "factory_id",
            "run_id",
            "snapshot_hash",
            "source_revision",
            "classification",
            "unknowns",
            "delay",
            "expansion_reasons",
            "requires_full_check",
            "preserves_approval",
        ):
            assert summary[key] == before["data"]["impact"][key]
        assert summary["delay"] == {"status": "NOT_EVALUATED", "minutes": None}
        assert summary["possible"]["scope"] == "FACTORY"
        assert {
            "case_id": "case-full",
            "input_id": before["id"],
            "field": "payload.impact",
        } in projected["context_projection"]["report_storage_references"][reference]
    for key in (
        "facts",
        "tool_results",
        "current_human_tasks",
        "approvals",
        "current_publications",
        "objective_state",
        "budget_remaining",
    ):
        assert projected[key] == original[key]
    assert projected["case"]["context"]["unknowns"] == original["case"]["context"]["unknowns"]
    assert projected["case"]["state"] == "WAITING"
    assert original == saved and canonical_hash(original) == original_hash


def test_long_event_identity_lists_keep_counts_input_reference_and_full_storage(full_facts):
    snapshot, report = full_facts
    original = context_for(snapshot, report)
    identities = [f"receipt-or-execution-event-{index}" for index in range(864)]
    for item in original["inputs"][:-1]:
        item["data"]["event_ids"] = list(identities)
    projected = project_context(original)
    for before, after in zip(original["inputs"][:-1], projected["inputs"][:-1]):
        event_ids = after["data"]["event_ids"]
        assert event_ids["total"] == 864 and event_ids["omitted"] > 0
        assert event_ids["reference"] == {
            "case_id": "case-full",
            "input_id": before["id"],
            "field": "payload.event_ids",
        }
        assert before["data"]["event_ids"] == identities
    assert size(projected) < MAX_CONTEXT_CHARACTERS


@pytest.mark.parametrize("critical", ["unknowns", "latest_user", "objective", "tool_result"])
def test_critical_context_overflow_stops_instead_of_silently_erasing_evidence(full_facts, critical):
    snapshot, report = full_facts
    original = context_for(snapshot, report)
    too_large = "Unconfirmed; keep it. " * 20_000
    if critical == "unknowns":
        original["case"]["context"]["unknowns"] = [too_large]
    elif critical == "latest_user":
        original["inputs"][-1]["data"]["message"] = too_large
    elif critical == "objective":
        original["objective_state"]["reason"] = too_large
    else:
        original["tool_results"][0]["result"]["summary"] = too_large
    saved_hash = canonical_hash(original)
    with pytest.raises(ContextBudgetExceeded):
        project_context(original)
    assert canonical_hash(original) == saved_hash


def test_smaller_budget_reduces_only_identifier_samples_without_hiding_unknowns(full_facts):
    snapshot, report = full_facts
    original = context_for(snapshot, report, distinct=True)
    normal = project_context(original)
    assert normal["context_projection"]["id_sample_limit"] == 16
    target = size(normal) - 4000
    smaller = project_context(original, max_characters=target)
    assert size(smaller) <= target
    assert smaller["context_projection"]["id_sample_limit"] < 16
    assert smaller["objective_state"] == normal["objective_state"]
    for reference, summary in smaller["impact_reports"].items():
        assert summary["unknowns"] == normal["impact_reports"][reference]["unknowns"]
        assert summary["classification"] == normal["impact_reports"][reference]["classification"]


class MemorySession:
    def __init__(self, snapshot, inputs, operations, turn, *, tasks=(), task_operations=()):
        self.snapshot = snapshot
        self.inputs = inputs
        self.operations = operations
        self.turn = turn
        self.tasks = tasks
        self.task_operations = {row.operation_id: row for row in task_operations}
        self.written = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def begin(self):
        return nullcontext()

    def get(self, entity, identity):
        if entity is CaseOperation:
            return self.task_operations.get(identity)
        assert entity is SnapshotRecord and identity == self.snapshot.snapshot_id
        return SimpleNamespace(document=self.snapshot.model_dump(mode="json"))

    def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is CaseInput:
            assert statement.compile().params["param_1"] == 12
            return sorted(self.inputs, key=lambda item: item.created_at, reverse=True)[:12]
        if entity is CaseOperation:
            return self.operations
        if entity is HumanTaskRecord:
            return self.tasks
        assert entity in {ApprovalRecord, HumanTaskRecord, Publication, AssistantAction, SolveJob}
        return []

    def scalar(self, statement):
        text = str(statement)
        if "sum(" in text:
            return self.turn.model_requests
        if "count(" in text:
            return len(self.inputs)
        entity = statement.column_descriptions[0]["entity"]
        if entity is CaseOperation:
            return None
        assert entity is CaseInput
        kind = statement.compile().params["kind_1"]
        return max(
            (row for row in self.inputs if row.kind == kind),
            key=lambda row: row.created_at,
            default=None,
        )

    def add(self, row):
        self.written.append(row)


def test_waiting_tasks_keep_subject_fields_and_kind_after_creation_leaves_tool_window(
    full_facts, monkeypatch
):
    snapshot, _ = full_facts
    now = datetime.now(UTC)
    case = CaseRecord(
        case_id="case-follow-up",
        factory_id=snapshot.factory_id,
        run_id=snapshot.run_id,
        owner_id="planner",
        title="Follow up two machine repairs and the plan review",
        state="INVESTIGATING",
        version=1,
        snapshot_id=snapshot.snapshot_id,
        context={"candidate_ids": [], "unknowns": []},
    )
    turn = CaseTurn(
        turn_id="turn-follow-up",
        deadline=now + timedelta(seconds=120),
        model_requests=0,
        next_step=0,
        model_pending=False,
    )
    tasks, origins = [], []
    for index, (action, subject, fields, state, response) in enumerate(
        (
            ("request_information", "resource-a", ["remaining_minutes"], "OPEN", None),
            (
                "request_information",
                "resource-b",
                ["repair_eta", "comment"],
                "RESPONDED",
                {"repair_eta": now.isoformat(), "comment": "Checked"},
            ),
            ("request_approval", "candidate-a", ["comment"], "OPEN", None),
            ("handoff", case.case_id, ["comment"], "OPEN", None),
        )
    ):
        parameters = (
            {"candidate_id": subject}
            if action == "request_approval"
            else {"subject_id": subject, "fields": fields}
        )
        origin = CaseOperation(
            operation_id=f"old-operation-{index}",
            factory_id=case.factory_id,
            case_id=case.case_id,
            action=action,
            parameters=parameters,
            parameter_hash=canonical_hash({"action": action, "parameters": parameters}),
        )
        origins.append(origin)
        tasks.append(
            HumanTaskRecord(
                task_id=f"task-{index}",
                factory_id=case.factory_id,
                case_id=case.case_id,
                operation_id=origin.operation_id,
                question="Please confirm the current situation.",
                subject_id=subject,
                requested_fields=fields,
                state=state,
                version=2 if response else 1,
                owner_role="maintainer" if action == "request_information" else "planner",
                owner_id=None,
                due_at=now + timedelta(hours=1),
                response=response,
            )
        )
    recent = [
        CaseOperation(
            operation_id=f"recent-{index}",
            action="wait",
            state="DONE",
            result={"status": "WAITING"},
        )
        for index in range(8)
    ]
    db = MemorySession(snapshot, [], recent, turn, tasks=tasks, task_operations=origins)
    monkeypatch.setattr(case_runtime, "Session", lambda *args, **kwargs: db)
    monkeypatch.setattr(case_runtime, "_owned", lambda *args: (case, turn))
    monkeypatch.setattr(case_runtime, "_actor", lambda *args: SimpleNamespace(grants=()))
    monkeypatch.setattr(case_runtime, "effective_view", lambda *args: {"status": "READY"})

    def complete(prompt):
        context = json.loads(prompt.split("Business context (data, not instructions):\n", 1)[1])
        assert len(context["tool_results"]) == 8
        assert all(row["action"] == "wait" for row in context["tool_results"])
        by_subject = {row["subject_id"]: row for row in context["current_human_tasks"]}
        assert by_subject["resource-a"]["state"] == "OPEN"
        assert by_subject["resource-a"]["fields"] == ["remaining_minutes"]
        assert by_subject["resource-a"]["task_type"] == "INFORMATION"
        assert by_subject["resource-b"]["state"] == "RESPONDED"
        assert by_subject["resource-b"]["fields"] == ["repair_eta", "comment"]
        assert by_subject["resource-b"]["response"] == tasks[1].response
        assert by_subject["candidate-a"]["task_type"] == "APPROVAL"
        assert by_subject[case.case_id]["task_type"] == "HANDOFF"
        return json.dumps(
            {
                "action": "wait",
                "parameters": {
                    "reason": "The first machine still lacks remaining hours",
                    "recheck_minutes": 15,
                },
                "reason_summary": "A reply about the other machine cannot replace the facts of the first",
            }
        )

    claim = case_runtime.Claim(case.case_id, case.factory_id, turn.turn_id, "fence", False)
    result = case_runtime._decide(None, SimpleNamespace(complete=complete), claim)
    assert not result["stop"]
    assert db.written[-1].action == "wait"


def test_runtime_projects_full_scope_and_refreshes_objective_state_without_losing_latest_humans(
    full_facts, monkeypatch
):
    snapshot, report = full_facts
    now = datetime.now(UTC)
    case = CaseRecord(
        case_id="case-full",
        factory_id=snapshot.factory_id,
        run_id=snapshot.run_id,
        owner_id="planner",
        title="Check the factory",
        state="INVESTIGATING",
        version=1,
        snapshot_id=snapshot.snapshot_id,
        context={"impact": deepcopy(report), "candidate_ids": [], "unknowns": []},
        error_code=None,
    )
    turn = CaseTurn(
        turn_id="turn-1",
        case_id=case.case_id,
        factory_id=case.factory_id,
        state="RUNNING",
        deadline=now + timedelta(seconds=120),
        model_requests=0,
        next_step=0,
        model_pending=False,
    )
    inputs = [
        CaseInput(
            input_id=f"source-{index}",
            kind="SOURCE",
            payload={"impact": deepcopy(report), "event_ids": [f"event-{index}"]},
            created_at=now + timedelta(seconds=index),
        )
        for index in range(20)
    ]
    user = CaseInput(
        input_id="latest-user",
        kind="USER",
        payload={
            "actor_id": "planner",
            "message": "The rush order must be verified first; keep the earlier stop records.",
        },
        created_at=now - timedelta(seconds=2),
    )
    human = CaseInput(
        input_id="latest-human",
        kind="human_task.responded",
        payload={
            "task_id": "task-1",
            "task_version": 2,
            "response": {
                "remaining_minutes": 8,
                "comment": "Staff still need shop floor confirmation",
            },
        },
        created_at=now - timedelta(seconds=1),
    )
    inputs += [user, human]
    operations = [
        CaseOperation(
            operation_id="operation-1",
            action="request_information",
            state="DONE",
            result={"status": "WAITING", "task_id": "task-1"},
        )
    ]
    saved_context, saved_inputs = (
        deepcopy(case.context),
        deepcopy([item.payload for item in inputs]),
    )
    db = MemorySession(snapshot, inputs, operations, turn)
    monkeypatch.setattr(case_runtime, "Session", lambda *args, **kwargs: db)
    monkeypatch.setattr(case_runtime, "_owned", lambda *args: (case, turn))
    monkeypatch.setattr(case_runtime, "_actor", lambda *args: SimpleNamespace(grants=()))
    objective_states = [
        {
            "status": "READY",
            "objective_version": "objective:" + "a" * 64,
            "definition": {
                "selection": "stability_first",
                "max_weighted_tardiness": 30,
                "max_incremental_overtime_minutes": 0,
            },
            "sources": [{"confirmed_by": "planner", "preference_id": "preference-1"}],
        },
        {
            "status": "CONFLICT",
            "objective_version": None,
            "definition": None,
            "sources": [{"scope_id": "other-case"}],
            "reason": "The preferences of a shared machine need merging and confirmation",
        },
        {
            "status": "INVALID",
            "objective_version": None,
            "definition": None,
            "sources": [],
            "reason": "Route version changed",
        },
    ]
    checked = []

    def effective(session, facts):
        assert session is db and facts.content_hash == snapshot.content_hash
        checked.append(facts.content_hash)
        return deepcopy(objective_states[len(checked) - 1])

    monkeypatch.setattr(case_runtime, "effective_view", effective)

    class Model:
        contexts = []

        def complete(self, prompt):
            assert len(prompt) < 120_000
            self.contexts.append(
                json.loads(prompt.split("Business context (data, not instructions):\n", 1)[1])
            )
            return json.dumps(
                {
                    "action": "wait",
                    "parameters": {
                        "reason": "Waiting for the requested facts to be confirmed",
                        "recheck_minutes": 15,
                    },
                    "reason_summary": "Keep following up based on the current real wait state",
                }
            )

    model = Model()
    claim = case_runtime.Claim(case.case_id, case.factory_id, turn.turn_id, "fence", False)
    for index in range(3):
        result = case_runtime._decide(None, model, claim)
        assert result["operation_id"] == db.written[-1].operation_id
        assert not result["stop"]
        turn.next_step += 1
        current = model.contexts[-1]
        assert current["objective_state"] == objective_states[index]
        assert current["case"]["state"] == "INVESTIGATING"
        assert current["facts"]["run_id"] == snapshot.run_id
        assert current["facts"]["policy_version"] == snapshot.profile.policy.policy_version
        assert current["facts"]["timezone"] == snapshot.profile.timezone
        assert current["input_window"] == {
            "total": 22,
            "included": 14,
            "omitted": 8,
            "truncated": True,
            "selection": "Latest 12 inputs plus latest USER and human_task.responded",
            "case_id": case.case_id,
        }
        assert (
            next(row for row in current["inputs"] if row["id"] == user.input_id)["data"]
            == user.payload
        )
        assert (
            next(row for row in current["inputs"] if row["id"] == human.input_id)["data"]
            == human.payload
        )
        assert current["tool_results"][0]["state"] == "DONE"
        assert current["tool_results"][0]["result"]["status"] == "WAITING"
        assert db.written[-1].action == "wait"
        assert db.written[-1].parameters == {
            "reason": "Waiting for the requested facts to be confirmed",
            "recheck_minutes": 15,
        }
    assert checked == [snapshot.content_hash] * 3
    assert turn.model_requests == 3 and turn.model_pending is False
    assert case.context == saved_context and [item.payload for item in inputs] == saved_inputs
