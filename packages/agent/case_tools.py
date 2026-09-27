"""Registered business tools over scoped, persistent operations and current source facts."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from packages.agent.cases import TERMINAL, live_actor
from packages.agent.cases_store import CaseOperation, CaseRecord, CaseTurn
from packages.agent.decisions import (
    Action,
    ActionError,
    ApprovalAction,
    BusinessStudyAction,
    CompareAction,
    FinishAction,
    HandoffAction,
    InformationAction,
    PreferenceAction,
    ProductionReportAction,
    QueryAction,
    ReplyAction,
    SimulationAction,
    SolveAction,
    WaitAction,
    parse_action,
)
from packages.agent.human_tasks import (
    LIVE_STATES,
    HumanTaskRecord,
    TaskAction,
    create_task,
)
from packages.auth import AccessError, Principal, lock_membership, lock_user
from packages.domain.execution import ActionReceipt, PlanSubmission
from packages.domain.models import Candidate, Release, Snapshot, batch_operations, canonical_hash
from packages.planning.checker import check_candidate
from packages.planning.publication import Publication
from packages.planning.service import active_baseline, request_solve, require_live
from packages.planning.store import CandidateRecord, FactoryState, SnapshotRecord, SolveJob

FRESH_SECONDS = 30
# A choice asks the Agent for new work. Approval stays on the card, and pointing back at the
# card or putting the decision off would only repeat the last reply.
IDLE_CHOICE = re.compile(r"approv|submit|\bcards?\b|decide later|then decide", re.IGNORECASE)
PAGE_SIZE = 50
OBJECTIVE_ORDERS = {
    "delivery_first": (
        "weighted_tardiness",
        "incremental_overtime_metric",
        "changed_operations",
        "total_start_shift",
        "makespan",
    ),
    "stability_first": (
        "changed_operations",
        "total_start_shift",
        "weighted_tardiness",
        "incremental_overtime_metric",
        "makespan",
    ),
    "overtime_first": (
        "incremental_overtime_metric",
        "weighted_tardiness",
        "changed_operations",
        "total_start_shift",
        "makespan",
    ),
}


def _reject(code: str, summary: str) -> AccessError:
    return AccessError(code, summary, 409)


def _operation(
    db: Session, actor: Principal, supplied: CaseOperation
) -> tuple[CaseOperation, CaseRecord, Action]:
    live_actor(db, actor, supplied.factory_id, {"planner"})
    operation = db.get(CaseOperation, supplied.operation_id)
    if operation is None or any(
        getattr(operation, field) != getattr(supplied, field)
        for field in (
            "case_id",
            "factory_id",
            "turn_id",
            "action",
            "parameters",
            "parameter_hash",
            "snapshot_id",
            "expected_case_version",
        )
    ):
        raise _reject(
            "INVALID_OPERATION", "The operation does not match the registered task action."
        )
    digest = canonical_hash({"action": operation.action, "parameters": operation.parameters})
    if digest != operation.parameter_hash:
        raise _reject("INVALID_OPERATION", "The operation checksum does not match.")
    case = db.get(CaseRecord, operation.case_id)
    turn = db.get(CaseTurn, operation.turn_id)
    if (
        case is None
        or case.factory_id != operation.factory_id
        or turn is None
        or turn.case_id != case.case_id
        or turn.factory_id != case.factory_id
    ):
        raise _reject(
            "INVALID_OPERATION", "The operation does not belong to a task of this factory."
        )
    action = parse_action(
        json.dumps(
            {
                "action": operation.action,
                "parameters": operation.parameters,
                "reason_summary": "Run the registered business action.",
            },
            ensure_ascii=False,
        )
    )
    return operation, case, action


def _snapshot(db: Session, factory_id: str, snapshot_id: str) -> Snapshot:
    saved = db.get(SnapshotRecord, snapshot_id)
    if saved is None or saved.factory_id != factory_id:
        raise _reject(
            "SNAPSHOT_REQUIRED", "This factory has no business snapshot; sync and try again."
        )
    snapshot = Snapshot.model_validate(saved.document)
    if (
        snapshot.factory_id != factory_id
        or snapshot.snapshot_id != snapshot_id
        or snapshot.content_hash != saved.content_hash
    ):
        raise _reject(
            "SNAPSHOT_INTEGRITY_ERROR", "The business snapshot does not match its saved checksum."
        )
    return snapshot


def _current(db: Session, case: CaseRecord) -> tuple[Snapshot, FactoryState]:
    state = db.get(FactoryState, case.factory_id)
    if state is None:
        raise _reject("SNAPSHOT_REQUIRED", "Sync the factory data first.")
    snapshot = _snapshot(db, case.factory_id, state.snapshot_id)
    if snapshot.run_id != case.run_id or state.run_id != case.run_id:
        raise _reject(
            "SOURCE_RUN_CHANGED",
            "The source run has changed; tasks of the old run cannot continue.",
        )
    if state.source_revision != snapshot.source.source_revision:
        raise _reject(
            "SNAPSHOT_INTEGRITY_ERROR",
            "The current source version does not match the business snapshot.",
        )
    return snapshot, state


def _fresh(snapshot: Snapshot, state: FactoryState) -> bool:
    age = (datetime.now(UTC) - state.last_synced_at).total_seconds()
    return (
        0 <= age <= FRESH_SECONDS
        and snapshot.source.complete
        and snapshot.source.consistency != "UNVERIFIED"
        and snapshot.source.freshness == "CURRENT"
    )


def _base(operation: CaseOperation, snapshot: Snapshot) -> dict:
    return {
        "tool": operation.action,
        "operation_id": operation.operation_id,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_hash": snapshot.content_hash,
        "source_revision": snapshot.source.source_revision,
        "source": snapshot.source.model_dump(mode="json"),
    }


def _query(action: QueryAction, snapshot: Snapshot, state: FactoryState) -> dict:
    entity = action.parameters.entity
    if entity == "policy":
        records = [snapshot.profile.policy.model_dump(mode="json")]
        identity_key = "policy_version"
    elif entity == "products":
        records = [product.model_dump(mode="json") for product in snapshot.profile.products]
        identity_key = "product_id"
    elif entity == "business_terms":
        records = (
            [snapshot.business_terms.model_dump(mode="json")] if snapshot.business_terms else []
        )
        identity_key = "version"
    elif entity == "finished_goods":
        from packages.domain.demand import finished_goods

        records = [row.model_dump(mode="json") for row in finished_goods(snapshot)]
        identity_key = "batch_id"
    else:
        identity_key = {
            "orders": "order_id",
            "inventory": "material_id",
            "receipts": "receipt_id",
            "resources": "resource_id",
            "workers": "worker_id",
            "actuals": "operation_id",
        }[entity]
        records = [row.model_dump(mode="json") for row in getattr(snapshot, entity)]
    if action.parameters.identity is not None:
        records = [row for row in records if row[identity_key] == action.parameters.identity]
    records.sort(key=lambda row: row[identity_key])
    offset = action.parameters.offset
    page = records[offset : offset + PAGE_SIZE]
    truncated = offset + len(page) < len(records)
    return {
        "status": "OK",
        "summary": "Read the saved factory facts.",
        "entity": entity,
        "items": page,
        "total": len(records),
        "offset": offset,
        "limit": PAGE_SIZE,
        "truncated": truncated,
        "next_offset": offset + len(page) if truncated else None,
        "data_freshness": "CURRENT" if _fresh(snapshot, state) else "STALE",
        "last_synced_at": state.last_synced_at.isoformat(),
    }


def _candidates(db: Session, snapshot: Snapshot, ids: tuple[str, ...]) -> tuple[list[dict], bool]:
    from packages.planning.preferences import require_current_objective

    candidates = []
    stale = False
    for candidate_id in ids:
        row = db.get(CandidateRecord, candidate_id)
        if row is None or row.factory_id != snapshot.factory_id:
            raise _reject(
                "CANDIDATE_NOT_FOUND", "The plan does not exist or does not belong to this factory."
            )
        candidate = Candidate.model_validate(row.document)
        if (
            candidate.factory_id != snapshot.factory_id
            or candidate.content_hash != row.content_hash
        ):
            raise _reject(
                "CANDIDATE_INTEGRITY_ERROR", "The plan does not match its saved checksum."
            )
        current = (
            row.snapshot_id == snapshot.snapshot_id
            and candidate.binding.snapshot_hash == snapshot.content_hash
            and candidate.binding.planning_revision == snapshot.planning_revision
            and candidate.binding.scope_version == snapshot.scope_version
            and candidate.binding.profile_version == snapshot.profile.version
            and candidate.binding.policy_version == snapshot.profile.policy.policy_version
            and candidate.binding.baseline_plan_version == snapshot.active_plan_version
            and snapshot.snapshot_clock < candidate.accept_before
            and snapshot.snapshot_clock <= candidate.effective_not_before
        )
        rechecked = None
        progress = None
        objective = None
        try:
            objective = require_current_objective(db, snapshot, candidate.binding.objective_version)
        except AccessError:
            current = False
        if (
            not current
            and candidate.has_solution
            and snapshot.profile.policy.progress_revalidation_enabled
        ):
            from packages.planning.revalidation import check_progress

            try:
                progress = check_progress(db, snapshot, candidate)
            except AccessError:
                pass
            else:
                current = True
                rechecked = progress.checked.report.model_dump(mode="json")
        if current and candidate.has_solution and progress is None:
            rechecked = check_candidate(
                snapshot,
                candidate,
                baseline=active_baseline(db, snapshot),
                allow_overtime="allow_overtime" in candidate.required_consents,
                objective=objective,
            ).model_dump(mode="json")
        stale = stale or not current
        candidates.append(
            {
                "candidate_id": candidate_id,
                "candidate_hash": candidate.content_hash,
                "new_actions_not_before": candidate.new_actions_not_before.isoformat()
                if candidate.new_actions_not_before is not None
                else None,
                "snapshot_hash": candidate.binding.snapshot_hash,
                "binding": candidate.binding.model_dump(mode="json"),
                "current": current,
                "native_status": candidate.native_status,
                "has_solution": candidate.has_solution,
                "termination_reason": candidate.termination_reason,
                "last_search_status": candidate.last_search_status,
                "objective": [
                    metric.model_dump(mode="json")
                    for metric in (progress.checked.metrics if progress else candidate.objective)
                ],
                "objective_snapshot_hash": snapshot.content_hash
                if progress
                else candidate.binding.snapshot_hash,
                "metrics_revalidated": progress is not None,
                "original_objective": [
                    metric.model_dump(mode="json") for metric in candidate.objective
                ],
                "approval_mode": "PROGRESS_CHECKED" if progress else "STRICT",
                "proven_objective_levels": candidate.proven_objective_levels,
                "checker": candidate.checker.model_dump(mode="json"),
                "current_checker": rechecked,
                "scenario": [fact.model_dump(mode="json") for fact in candidate.scenario],
                "required_consents": list(candidate.required_consents),
            }
        )
    return candidates, stale


def _task_parameters(
    action: InformationAction | ApprovalAction | HandoffAction,
    case: CaseRecord,
    actor: Principal,
) -> dict:
    if isinstance(action, InformationAction):
        return action.parameters.model_dump(mode="json")
    if isinstance(action, ApprovalAction):
        manager = any(
            grant.factory_id == case.factory_id and grant.role == "manager"
            for grant in actor.grants
        )
        return {
            "question": "Review the plan and approve or reject it explicitly. Replying to this request does not approve the plan."
            if manager
            else "Review the plan on the plan page and approve or reject it explicitly. Replying to this request does not approve the plan.",
            "role": "manager" if manager else "planner",
            "subject_id": action.parameters.candidate_id,
            "fields": ["comment"],
            "deadline_minutes": 60,
        }
    return {
        "question": action.parameters.reason,
        "role": action.parameters.role,
        "subject_id": case.case_id,
        "fields": ["comment"],
        "deadline_minutes": 60,
    }


def _task_result(action: InformationAction | ApprovalAction | HandoffAction, task: dict) -> dict:
    result = {
        "status": "PENDING",
        "summary": "Manual task registered; waiting for the owner.",
        "task_id": task["task_id"],
        "task": task,
        "evidence": {"task_id": task["task_id"], "task_state": task["state"]},
    }
    if isinstance(action, ApprovalAction):
        result.update(
            {
                "summary": "Asked the manager to review the plan; not approved yet."
                if task["owner_role"] == "manager"
                else "Asked the planner to review the plan; not approved yet.",
                "approval_state": "NOT_GRANTED",
                "required_action": "EXPLICIT_APPROVAL_POST",
                "candidate_id": action.parameters.candidate_id,
            }
        )
    if isinstance(action, HandoffAction):
        result.update(
            {
                "summary": "Asked an owner to take over; waiting for explicit confirmation.",
                "handoff_state": "AWAITING_ACCEPTANCE",
            }
        )
    return result


def _subject(snapshot: Snapshot, case: CaseRecord, subject_id: str) -> bool:
    batches, operations = batch_operations(snapshot)
    known = {case.case_id, snapshot.profile.policy.policy_version}
    for rows, field in (
        (snapshot.orders, "order_id"),
        (snapshot.inventory, "material_id"),
        (snapshot.receipts, "receipt_id"),
        (snapshot.resources, "resource_id"),
        (snapshot.workers, "worker_id"),
        (batches, "batch_id"),
        (operations, "operation_id"),
    ):
        known.update(getattr(row, field) for row in rows)
    return subject_id in known


def _job_result(db: Session, operation: CaseOperation, job: SolveJob) -> dict:
    snapshot = _snapshot(db, operation.factory_id, job.snapshot_id)
    return {
        **_base(operation, snapshot),
        "status": "PENDING",
        "summary": "Reusing a calculation under the same conditions; the result will show in this conversation."
        if job.reused_from_id
        else "Solve job registered; wait for the actual result.",
        "job_id": job.job_id,
        "new_actions_not_before": job.new_actions_not_before.astimezone(UTC).isoformat()
        if job.new_actions_not_before is not None
        else None,
        "job_state": job.state,
        "evidence": {"job_id": job.job_id, "request_id": job.request_id},
    }


def _preference(
    engine: Engine, actor: Principal, supplied: CaseOperation, action: PreferenceAction
) -> dict:
    with Session(engine) as db, db.begin():
        operation, case, _ = _operation(db, actor, supplied)
        locked_case = db.get(CaseRecord, case.case_id, with_for_update=True, populate_existing=True)
        assert locked_case is not None
        case = locked_case
        user = lock_user(db, actor.user_id)
        membership = lock_membership(db, actor.user_id, case.factory_id, "planner")
        if user is None or not user.active or membership is None:
            raise _reject(
                "AUTHORIZATION_REVOKED",
                "This account may no longer propose preference changes for this factory.",
            )
        if case.state in TERMINAL:
            raise _reject(
                "CASE_CLOSED", "The task is closed; no preference change can be proposed."
            )
        proposals = dict(case.context.get("pending_preference", {}))
        previous = proposals.get(operation.operation_id)
        if previous is not None:
            return previous
        snapshot, _ = _current(db, case)
        proposal = {
            "proposal_id": operation.operation_id,
            "scope_type": "CASE",
            "scope_id": case.case_id,
            "selection": action.parameters.selection,
            "state": "PENDING_CONFIRMATION",
            "objective_order": list(OBJECTIVE_ORDERS[action.parameters.selection]),
            "based_on_policy_version": snapshot.profile.policy.policy_version,
            "bounds": None,
            "requires_authorized_confirmation": True,
            "summary": "Preference proposal recorded. It is enabled only after its objective bounds and permissions are confirmed.",
        }
        result = {
            **_base(operation, snapshot),
            "status": "PENDING",
            "summary": proposal["summary"],
            "proposal": proposal,
        }
        proposals[operation.operation_id] = result
        case.context = {**case.context, "pending_preference": proposals}
        case.version += 1
        case.updated_at = datetime.now(UTC)
        return result


def _finish(
    db: Session, case: CaseRecord, snapshot: Snapshot, state: FactoryState, action: FinishAction
) -> dict:
    if not _fresh(snapshot, state):
        raise _reject(
            "FRESH_FACTS_REQUIRED",
            "The execution facts are incomplete or out of date; sync and check.",
        )
    row = db.get(Publication, action.parameters.evidence_release_id)
    if (
        row is None
        or row.factory_id != case.factory_id
        or row.candidate_id not in case.context.get("candidate_ids", [])
    ):
        raise _reject(
            "RELEASE_NOT_IN_CASE",
            "The release record does not belong to a plan handled by this task.",
        )
    release = Release.model_validate(row.document)
    payload = PlanSubmission.model_validate(row.payload)
    if (
        release.release_id != row.release_id
        or release.factory_id != case.factory_id
        or payload.factory_id != case.factory_id
        or payload.run_id != case.run_id
        or payload.candidate.candidate_id != row.candidate_id
        or release.candidate_hash != payload.candidate.content_hash
        or release.payload_hash != canonical_hash(row.payload)
    ):
        raise _reject(
            "RELEASE_EVIDENCE_MISMATCH",
            "The release evidence does not match this task run or plan.",
        )
    if release.source_state != "ACTIVE" or release.execution_state not in {
        "IN_PROGRESS",
        "COMPLETED",
    }:
        raise _reject(
            "EXECUTION_NOT_COMPLETED",
            "The plan is not in execution yet, so this case cannot be closed.",
        )
    if row.source_receipt is None:
        raise _reject(
            "SOURCE_RECEIPT_REQUIRED", "The effective receipt from the execution source is missing."
        )
    receipt = ActionReceipt.model_validate(row.source_receipt)
    if (
        receipt.factory_id != case.factory_id
        or receipt.run_id != case.run_id
        or receipt.operation_id != release.operation_id
        or receipt.receipt_id != release.source_receipt_id
        or receipt.candidate_hash != release.candidate_hash
        or receipt.source_state != "ACTIVE"
    ):
        raise _reject(
            "RELEASE_EVIDENCE_MISMATCH", "The effective receipt does not match this release record."
        )
    superseded = (
        receipt.plan_version != snapshot.active_plan_version
        or payload.candidate.binding.scope_version != snapshot.scope_version
    )
    # This conversation's plan went live; a later change was decided in another conversation.
    active = db.scalar(
        select(CandidateRecord.candidate_id)
        .where(
            CandidateRecord.factory_id == case.factory_id,
            CandidateRecord.content_hash == snapshot.active_plan_hash,
        )
        .limit(1)
    )
    # A change after this plan went live that another, newer conversation took up is a new matter.
    newer_conversation = db.scalar(
        select(CaseRecord.case_id)
        .where(
            CaseRecord.factory_id == case.factory_id,
            CaseRecord.run_id == case.run_id,
            CaseRecord.case_id != case.case_id,
            CaseRecord.created_at > release.committed_at,
        )
        .limit(1)
    )
    taken_over = superseded and (
        (active is not None and active not in case.context.get("candidate_ids", []))
        or newer_conversation is not None
    )
    # The source accepts a plan only against its current facts, so with this plan still in
    # effect the scope moved on after it went live: that change waits among the new changes.
    later = superseded and receipt.plan_version == snapshot.active_plan_version
    if (
        (
            not payload.candidate.assignments
            and not (
                payload.candidate.empty_demand
                and release.execution_state == "COMPLETED"
                and not batch_operations(snapshot)[1]
            )
        )
        or (superseded and not taken_over and not later)
        or release.execution_state == "BLOCKED"
    ):
        raise _reject(
            "UNRESOLVED_SCOPE",
            "The shop floor scope or active plan has changed; check the current issue again.",
        )
    if case.context.get("unknowns"):
        raise _reject(
            "UNRESOLVED_FACTS", "The task still has facts to confirm and cannot be closed."
        )
    open_task = db.scalar(
        select(HumanTaskRecord.task_id)
        .where(
            HumanTaskRecord.case_id == case.case_id,
            HumanTaskRecord.factory_id == case.factory_id,
            HumanTaskRecord.state.in_(LIVE_STATES),
        )
        .limit(1)
    )
    if open_task is not None:
        raise _reject(
            "OPEN_HUMAN_TASKS", "The task still has unfinished manual items; handle them first."
        )
    return {
        "status": "RESOLVED",
        "summary": "The plan of this conversation is active and production has started; later shop floor changes are handled in other conversations."
        if taken_over
        else "The plan of this conversation is active and production has started; handle later shop floor changes from Field changes in the top bar."
        if later
        else "The plan is active on the shop floor and production keeps running.",
        "closing_evidence": {
            "release_id": release.release_id,
            "candidate_id": row.candidate_id,
            "source_receipt_id": receipt.receipt_id,
            "run_id": case.run_id,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_hash": snapshot.content_hash,
            "source_revision": snapshot.source.source_revision,
            "active_plan_version": snapshot.active_plan_version,
            "execution_state": release.execution_state,
            "scope_version": snapshot.scope_version,
            "open_human_tasks": 0,
            "risk_summary": action.parameters.risk_summary,
        },
    }


def execute_operation(engine: Engine, actor: Principal, operation: CaseOperation) -> dict:
    """Runtime fences the Case; tools independently validate identity, scope and evidence."""
    try:
        with Session(engine) as db:
            saved, case, action = _operation(db, actor, operation)
            if saved.state == "DONE" and saved.result is not None:
                return saved.result
            if saved.state not in {"PREPARED", "STARTED"}:
                raise _reject(
                    "INVALID_OPERATION_STATE",
                    "The current operation state does not allow running it.",
                )
            if case.state in TERMINAL:
                raise _reject("CASE_CLOSED", "The task is closed; no new action can run.")
            snapshot, state = _current(db, case)
            require_live(snapshot)
            base = _base(saved, snapshot)
            if isinstance(action, ProductionReportAction):
                from packages.domain.production_facts import production_brief

                if not _fresh(snapshot, state):
                    raise _reject(
                        "FRESH_FACTS_REQUIRED",
                        "Sync the latest shop floor before checking delivery facts.",
                    )
                if action.parameters.order_id is not None and not any(
                    o.order_id == action.parameters.order_id for o in snapshot.orders
                ):
                    raise _reject("ORDER_NOT_FOUND", "This factory has no such order.")
                # Facts for the Agent's own answer; the manager reads the reply, not this table.
                return {
                    **base,
                    "status": "OK",
                    "summary": "Checked delivery and materials for "
                    f"{action.parameters.order_id or 'all orders'}.",
                    "report": production_brief(
                        snapshot, active_baseline(db, snapshot), action.parameters.order_id
                    ),
                }
            if isinstance(action, ReplyAction):
                return {
                    **base,
                    "status": "WAITING",
                    "summary": action.parameters.message,
                    "choices": [c for c in action.parameters.choices if not IDLE_CHOICE.search(c)],
                    "await_user": True,
                }
            if isinstance(action, SimulationAction):
                resource_id = action.parameters.resource_id
                if snapshot.source.ownership != "simulator_fact":
                    raise _reject(
                        "SIMULATION_ONLY", "This entry only applies to the simulated factory."
                    )
                if resource_id is not None and not any(
                    item.resource_id == resource_id for item in snapshot.resources
                ):
                    raise _reject(
                        "SUBJECT_NOT_FOUND", "The machine does not belong to this factory."
                    )
                return {
                    **base,
                    "status": "WAITING",
                    "summary": action.reason_summary,
                    "simulation": action.parameters.model_dump(mode="json"),
                    "await_user": True,
                }
            if isinstance(action, QueryAction):
                return {**base, **_query(action, snapshot, state)}
            if isinstance(action, WaitAction):
                return {
                    **base,
                    "status": "WAITING",
                    "summary": action.parameters.reason,
                    "recheck_minutes": action.parameters.recheck_minutes,
                    "clock": "real",
                }
            if isinstance(action, FinishAction):
                return {**base, **_finish(db, case, snapshot, state, action)}
            if isinstance(action, (CompareAction, ApprovalAction)):
                ids = (
                    action.parameters.candidate_ids
                    if isinstance(action, CompareAction)
                    else (action.parameters.candidate_id,)
                )
                candidates, stale = _candidates(db, snapshot, ids)
                if stale:
                    return {
                        **base,
                        "status": "REJECTED",
                        "code": "STALE_CANDIDATE",
                        "summary": "The plan basis has changed; recalculate with the current facts.",
                        "candidates": candidates,
                    }
                if isinstance(action, CompareAction):
                    return {
                        **base,
                        "status": "OK",
                        "summary": "Read the actual plan metrics and check results.",
                        "candidates": candidates,
                    }
                if not _fresh(snapshot, state):
                    raise _reject(
                        "FRESH_FACTS_REQUIRED", "Sync the current facts before requesting approval."
                    )
                if (
                    not candidates[0]["has_solution"]
                    or candidates[0]["current_checker"]["status"] != "PASS"
                ):
                    raise _reject(
                        "CANDIDATE_NOT_APPROVABLE",
                        "The plan failed the independent check and approval cannot be requested.",
                    )
            if isinstance(action, InformationAction) and not _subject(
                snapshot, case, action.parameters.subject_id
            ):
                raise _reject(
                    "SUBJECT_NOT_FOUND",
                    "The object of the requested information is not in the current facts of this factory.",
                )
            if isinstance(action, (InformationAction, ApprovalAction, HandoffAction)):
                task_parameters = _task_parameters(action, case, actor)
            if isinstance(action, SolveAction) and not _fresh(snapshot, state):
                raise _reject(
                    "FRESH_FACTS_REQUIRED",
                    "Sync the current factory facts before requesting a solve.",
                )
        # These services own their transaction. In particular create_task locks Case itself.
        if isinstance(action, BusinessStudyAction):
            from packages.planning.business_service import request_business_study

            job = request_business_study(
                engine,
                actor,
                operation.factory_id,
                request_id=operation.operation_id,
                request=action.parameters,
                expected_snapshot_hash=snapshot.content_hash,
                expected_run_id=case.run_id,
                case_id=operation.case_id,
            )
            with Session(engine) as db:
                return _job_result(db, operation, job)
        if isinstance(action, (InformationAction, ApprovalAction, HandoffAction)):
            task = create_task(
                engine,
                actor=actor,
                case_id=operation.case_id,
                factory_id=operation.factory_id,
                operation_id=operation.operation_id,
                **task_parameters,
            )
            return {**base, **_task_result(action, task)}
        if isinstance(action, PreferenceAction):
            return _preference(engine, actor, operation, action)
        if isinstance(action, SolveAction):
            job = request_solve(
                engine,
                actor,
                operation.factory_id,
                request_id=operation.operation_id,
                allow_overtime=action.parameters.allow_overtime,
                time_limit=action.parameters.time_limit,
                case_id=operation.case_id,
                new_actions_not_before=action.parameters.new_actions_not_before,
            )
            with Session(engine) as db:
                return _job_result(db, operation, job)
        raise _reject("UNKNOWN_TOOL", "This action is not registered.")
    except (ActionError, ValidationError):
        return {
            "status": "REJECTED",
            "code": "INVALID_TOOL_INPUT",
            "summary": "The action parameters or business evidence do not meet the contract.",
        }
    except AccessError as exc:
        return {"status": "REJECTED", "code": exc.code, "summary": str(exc)}


def recover_operation(engine: Engine, actor: Principal, operation: CaseOperation) -> dict | None:
    """Read an effect committed before a worker crash without replaying a business write."""
    try:
        with Session(engine) as db:
            saved, case, action = _operation(db, actor, operation)
            if saved.state == "DONE":
                return saved.result
            if isinstance(action, BusinessStudyAction):
                job = db.scalar(
                    select(SolveJob).where(
                        SolveJob.factory_id == saved.factory_id,
                        SolveJob.request_id == saved.operation_id,
                    )
                )
                if job is None:
                    return None
                if (
                    job.business_request != action.parameters.model_dump(mode="json")
                    or job.requester_id != actor.user_id
                    or job.case_id != case.case_id
                ):
                    raise _reject(
                        "IDEMPOTENCY_CONFLICT",
                        "The existing business trial does not match the registered action.",
                    )
                if _snapshot(db, case.factory_id, job.snapshot_id).run_id != case.run_id:
                    raise _reject(
                        "SOURCE_RUN_CHANGED",
                        "The existing business trial does not belong to this task run.",
                    )
                return _job_result(db, saved, job)
            if isinstance(action, SolveAction):
                job = db.scalar(
                    select(SolveJob).where(
                        SolveJob.factory_id == saved.factory_id,
                        SolveJob.request_id == saved.operation_id,
                    )
                )
                if job is None:
                    return None
                if (
                    job.requester_id != actor.user_id
                    or job.case_id != case.case_id
                    or job.allow_overtime != action.parameters.allow_overtime
                    or job.time_limit != action.parameters.time_limit
                    or job.new_actions_not_before != action.parameters.new_actions_not_before
                ):
                    raise _reject(
                        "IDEMPOTENCY_CONFLICT",
                        "The existing solve request does not match the registered action.",
                    )
                if _snapshot(db, case.factory_id, job.snapshot_id).run_id != case.run_id:
                    raise _reject(
                        "SOURCE_RUN_CHANGED",
                        "The existing solve request does not belong to this task run.",
                    )
                return _job_result(db, saved, job)
            if isinstance(action, (InformationAction, ApprovalAction, HandoffAction)):
                creation = db.scalar(
                    select(TaskAction).where(
                        TaskAction.factory_id == saved.factory_id,
                        TaskAction.request_id == saved.operation_id,
                    )
                )
                if creation is None:
                    return None
                expected = {
                    "case_id": case.case_id,
                    "factory_id": case.factory_id,
                    "operation_id": saved.operation_id,
                    **_task_parameters(action, case, actor),
                }
                task = db.get(HumanTaskRecord, creation.task_id)
                if (
                    creation.kind != "CREATE"
                    or creation.case_id != case.case_id
                    or creation.payload_hash != canonical_hash(expected)
                    or creation.actor_id != actor.user_id
                    or task is None
                    or task.case_id != case.case_id
                    or task.factory_id != case.factory_id
                ):
                    raise _reject(
                        "IDEMPOTENCY_CONFLICT",
                        "The registered manual task does not match the action content.",
                    )
                snapshot = _snapshot(db, case.factory_id, saved.snapshot_id)
                return {**_base(saved, snapshot), **_task_result(action, creation.result)}
            if isinstance(action, PreferenceAction):
                proposal = case.context.get("pending_preference", {}).get(saved.operation_id)
                if proposal is not None and (
                    proposal.get("proposal", {}).get("selection") != action.parameters.selection
                    or proposal.get("proposal", {}).get("scope_id") != case.case_id
                    or proposal.get("operation_id") != saved.operation_id
                ):
                    raise _reject(
                        "IDEMPOTENCY_CONFLICT",
                        "The registered preference proposal does not match the action content.",
                    )
                return proposal
            return None
    except (ActionError, ValidationError):
        return {
            "status": "REJECTED",
            "code": "INVALID_TOOL_INPUT",
            "summary": "The registered action or recovery evidence does not meet the contract.",
        }
    except AccessError as exc:
        return {"status": "REJECTED", "code": exc.code, "summary": str(exc)}
