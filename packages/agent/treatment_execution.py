"""Human-approved simulation bundles with durable effects and bounded schedule publication."""

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import StrictBool
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from packages.agent.assistant_store import AssistantAction
from packages.agent.cases import live_actor
from packages.agent.cases_store import CaseRecord, add_input
from packages.agent.planning_context import decision_key
from packages.auth import AccessError, Grant, Principal
from packages.domain.business_options import BusinessOption, BusinessStudy
from packages.domain.execution import SimulatorCommand
from packages.domain.models import Candidate, Contract, Digest, Identifier, Snapshot, canonical_hash
from packages.domain.production_facts import order_facts, plan_review
from packages.domain.treatment import treatment_changes
from packages.integrations.factory_http import ConnectorError, FactoryControls, FactoryHTTP
from packages.persistence import Membership, User
from packages.planning.publication import Publication
from packages.planning.service import request_solve, synchronize
from packages.planning.store import CandidateRecord, FactoryState, SnapshotRecord, SolveJob

KIND = "treatment_execute"


class TreatmentApproval(Contract):
    request_id: Identifier
    study_hash: Digest
    allow_overtime: StrictBool = False
    accept_customer_change: StrictBool = False


class TreatmentSelection(Contract):
    job_id: Identifier
    option_id: Identifier
    study_hash: Digest
    allow_overtime: StrictBool = False
    accept_customer_change: StrictBool = False


class ExecutionDecision(Contract):
    request_id: Identifier
    decision: Literal["cancel", "resume"]


def decide_execution(
    engine: Engine,
    controls: FactoryControls,
    actor: Principal,
    factory_id: str,
    action_id: str,
    body: ExecutionDecision,
) -> dict:
    from packages.agent.assistant import action_view

    with Session(engine) as db, db.begin():
        live_actor(db, actor, factory_id, {"manager"}, lock=True)
        row = db.get(AssistantAction, action_id, with_for_update=True)
        if (
            row is None
            or row.factory_id != factory_id
            or row.kind != KIND
            or row.user_id != actor.user_id
        ):
            raise AccessError("NOT_FOUND", "The execution record does not exist.", 404)
        result = dict(row.result or {})
        decisions = list(result.get("decisions", []))
        previous = next((d for d in decisions if d["request_id"] == body.request_id), None)
        if previous:
            if previous["decision"] != body.decision:
                raise AccessError(
                    "IDEMPOTENCY_CONFLICT",
                    "The request was already used for another decision.",
                    409,
                )
            return action_view(row)
        case = db.get(CaseRecord, row.payload["case_id"], with_for_update=True)
        if body.decision == "resume":
            if row.state != "ATTENTION" or not result.get("can_resume"):
                raise AccessError(
                    "NEW_PROPOSAL_REQUIRED",
                    "The current conditions need an adjusted plan; the old decision cannot run again.",
                    409,
                )
            if case and case.context.get("execution_pending") not in {None, row.action_id}:
                raise AccessError("EXECUTION_PENDING", "Another plan is being executed.", 409)
            row.state, row.attempts, row.next_attempt_at = "QUEUED", 0, datetime.now(UTC)
            if case:
                case.context = {**case.context, "execution_pending": row.action_id}
            result["summary"] = (
                "Checking the original request again; the original approval and completed measures are kept."
            )
        else:
            if row.state == "DONE" or result.get("stage") == "PUBLISHING":
                raise AccessError(
                    "PLAN_ALREADY_DISPATCHED",
                    "The schedule was already released; cancelling cannot withdraw an active schedule. Assess an adjusted plan separately.",
                    409,
                )
            if result.get("stage") == "APPLY":
                receipt = controls.cancel_treatment(
                    row.factory_id, SimulatorCommand.model_validate(result["source_command"])
                )
                if not receipt.get("cancelled"):
                    result["source_receipt"] = receipt
            row.state = "CANCELLED"
            result["summary"] = (
                "Remaining steps cancelled. Completed measures and costs already incurred are kept; nothing is returned or refunded automatically."
                if result.get("source_receipt")
                else "Plan cancelled; no business measures were executed."
            )
            if case and case.context.get("execution_pending") == row.action_id:
                case.context = {k: v for k, v in case.context.items() if k != "execution_pending"}
        result["decisions"] = [
            *decisions,
            {**body.model_dump(), "at": datetime.now(UTC).isoformat(), "actor_id": actor.user_id},
        ]
        row.result = result
        if case:
            case.context = {
                **case.context,
                "last_execution": {
                    "action_id": row.action_id,
                    "state": row.state,
                    "stage": result.get("stage"),
                    "summary": result.get("summary"),
                    "source_effects_confirmed": bool(result.get("source_receipt")),
                },
            }
        return action_view(row)


def _snapshot(db: Session, factory_id: str) -> Snapshot:
    state = db.get(FactoryState, factory_id)
    record = db.get(SnapshotRecord, state.snapshot_id) if state else None
    if record is None:
        raise AccessError("SNAPSHOT_REQUIRED", "Sync the factory facts first.", 409)
    return Snapshot.model_validate(record.document)


def approve_treatment(
    engine: Engine,
    actor: Principal,
    factory_id: str,
    job_id: str,
    option_id: str,
    body: TreatmentApproval,
    *,
    expected_run_id: str,
) -> dict:
    from packages.agent.assistant import action_view

    with Session(engine) as db, db.begin():
        db.get(FactoryState, factory_id, with_for_update=True)
        live_actor(db, actor, factory_id, {"manager"}, lock=True)
        live_actor(db, actor, factory_id, {"planner"}, lock=True)
        prior = db.scalar(
            select(AssistantAction).where(
                AssistantAction.factory_id == factory_id,
                AssistantAction.request_id == body.request_id,
            )
        )
        selection = {"job_id": job_id, "option_id": option_id, **body.model_dump(mode="json")}
        if prior:
            if (
                prior.user_id != actor.user_id
                or prior.kind != KIND
                or prior.payload["approval"] != selection
            ):
                raise AccessError(
                    "IDEMPOTENCY_CONFLICT",
                    "This request was already used for another decision.",
                    409,
                )
            return action_view(prior)
        job = db.get(SolveJob, job_id)
        if (
            not job
            or job.factory_id != factory_id
            or not job.business_result
            or canonical_hash(job.business_result) != body.study_hash
        ):
            raise AccessError("STUDY_CHANGED", "The plan has changed; review it again.", 409)
        study = BusinessStudy.model_validate(job.business_result)
        option = next((o for o in study.options if o.option_id == option_id), None)
        if (
            study.request.kind != "production_exception"
            or option is None
            or option.status != "FEASIBLE"
            or option.diagnostic_only
            or option.economics is None
            or option.economics.status != "ESTIMATED"
        ):
            raise AccessError(
                "OPTION_NOT_EXECUTABLE",
                "This option has no feasible schedule or complete cost and cannot be executed.",
                409,
            )
        case = db.get(CaseRecord, job.case_id, with_for_update=True) if job.case_id else None
        if (
            case is None
            or case.owner_id != actor.user_id
            or case.state in {"RESOLVED", "HANDED_OFF", "CANCELLED"}
        ):
            raise AccessError(
                "CASE_CHANGED",
                "You can only approve response options in your current conversation.",
                409,
            )
        if case.context.get("execution_pending"):
            raise AccessError(
                "EXECUTION_PENDING",
                "An approved plan is already being executed; check its result first.",
                409,
            )
        duplicate = db.scalar(
            select(AssistantAction.action_id)
            .where(
                AssistantAction.factory_id == factory_id,
                AssistantAction.kind == KIND,
                AssistantAction.payload["approval"]["job_id"].astext == job_id,
                AssistantAction.payload["approval"]["option_id"].astext == option_id,
                AssistantAction.state.not_in(("CANCELLED", "FAILED")),
            )
            .limit(1)
        )
        if duplicate:
            raise AccessError(
                "ALREADY_APPROVED",
                "This option is already approved; see the original execution record.",
                409,
            )
        snapshot = _snapshot(db, factory_id)
        if (
            snapshot.run_id != study.run_id
            or snapshot.run_id != expected_run_id
            or snapshot.source.ownership != "simulator_fact"
        ):
            raise AccessError(
                "SIMULATION_ONLY", "This option only applies to the original factory run.", 409
            )
        if snapshot.snapshot_clock > study.origin_snapshot_clock + timedelta(minutes=15):
            raise AccessError(
                "QUOTE_EXPIRED", "The quote is older than 15 minutes; compare again.", 409
            )
        original = db.get(SnapshotRecord, job.snapshot_id)
        # Normal production progress does not invalidate the decision; execution re-plans on the
        # latest facts and keeps the approved delivery, overtime and cost scope.
        if original is None or decision_key(
            Snapshot.model_validate(original.document)
        ) != decision_key(snapshot):
            raise AccessError(
                "STUDY_FACTS_CHANGED",
                "The business facts behind the option have changed; check them and compare again.",
                409,
            )
        if option.allow_overtime and not body.allow_overtime:
            raise AccessError(
                "OVERTIME_CONFIRMATION_REQUIRED", "Confirm the overtime scope of the option.", 409
            )
        if (
            any(a.kind in {"order_due", "order_quantity"} for a in option.actions)
            and not body.accept_customer_change
        ):
            raise AccessError(
                "CUSTOMER_CONFIRMATION_REQUIRED",
                "Confirm that the customer agreed to the listed due date or quantity change.",
                409,
            )
        try:
            treatment_changes(snapshot, option.actions)
        except ValueError as exc:
            raise AccessError(
                "TREATMENT_CHANGED",
                "The quantity, resource state or time the measures need has changed; compare again.",
                409,
            ) from exc
        row = AssistantAction(
            action_id=str(uuid4()),
            factory_id=factory_id,
            user_id=actor.user_id,
            request_id=body.request_id,
            run_id=study.run_id,
            kind=KIND,
            state="QUEUED",
            attempts=0,
            payload={
                "approval": selection,
                "case_id": case.case_id,
                "option": option.model_dump(mode="json"),
                "approved_at": datetime.now(UTC).isoformat(),
            },
            result={
                "stage": "PREPARE",
                "summary": "The full option is approved; the Agent will execute the measures and check the schedule.",
                "case_id": case.case_id,
            },
            created_at=datetime.now(UTC),
            next_attempt_at=datetime.now(UTC),
        )
        db.add(row)
        case.context = {**case.context, "execution_pending": row.action_id}
        return action_view(row)


def _within_scope(snapshot: Snapshot, candidate: Candidate, option: BusinessOption) -> None:
    allowed = {i.order_id: i for i in option.impacts}
    # A date the customer agreed to is part of the approved delivery scope.
    agreed = {a.target_id: a.ready_at for a in option.actions if a.kind == "order_due"}
    facts = order_facts(snapshot, candidate)
    for row in facts:
        if row["status"] in {"COMPLETED", "CANCELLED"}:
            continue
        impact = allowed.get(row["order_id"])
        completion = row["planned_completion_at"]
        if (
            impact is None
            or row["quantity"] != impact.quantity
            or not completion
            or impact.completion_at is None
        ):
            raise AccessError(
                "EXECUTION_SCOPE_CHANGED",
                "The order scope or full delivery schedule has changed; review the new option.",
                409,
            )
        latest = max(
            impact.requested_due_at,
            impact.completion_at,
            agreed.get(row["order_id"], impact.requested_due_at),
        )
        if datetime.fromisoformat(completion) > latest:
            raise AccessError(
                "DELIVERY_SCOPE_CHANGED",
                "Rescheduling would exceed the approved delivery impact and needs another decision.",
                409,
            )
    if not option.allow_overtime and "allow_overtime" in candidate.required_consents:
        raise AccessError(
            "OVERTIME_SCOPE_CHANGED", "The new schedule needs overtime that was not approved.", 409
        )
    # Fixed action prices cannot grow. Enforce the approved remaining overtime premium too.
    minutes = sum(w["minutes"] for w in plan_review(snapshot, candidate, None)["overtime"])
    original = option.derived_snapshot
    assert original is not None and option.candidate is not None
    permitted = sum(w["minutes"] for w in plan_review(original, option.candidate, None)["overtime"])
    if minutes > permitted:
        raise AccessError(
            "COST_SCOPE_CHANGED",
            "The overtime needed exceeds the approved cost scope and needs another decision.",
            409,
        )


def process_treatment(engine: Engine, reader: FactoryHTTP, controls: FactoryControls) -> bool:
    from packages.agent.assistant import _approve

    with Session(engine) as db, db.begin():
        row = db.scalar(
            select(AssistantAction)
            .where(
                AssistantAction.kind == KIND,
                AssistantAction.state == "QUEUED",
                AssistantAction.next_attempt_at <= datetime.now(UTC),
            )
            .order_by(AssistantAction.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return False
        result = dict(row.result or {})
        stage = result.get("stage", "PREPARE")
        try:
            user = db.get(User, row.user_id)
            if user is None:
                raise AccessError(
                    "AUTHORIZATION_REVOKED", "The approver's account is no longer available.", 403
                )
            actor = Principal(
                user_id=user.user_id,
                username=user.username,
                grants=tuple(
                    Grant.model_validate({"factory_id": g.factory_id, "role": g.role})
                    for g in db.scalars(
                        select(Membership).where(
                            Membership.user_id == row.user_id,
                            Membership.factory_id == row.factory_id,
                        )
                    )
                ),
            )
            live_actor(db, actor, row.factory_id, {"manager"}, lock=True)
            live_actor(db, actor, row.factory_id, {"planner"}, lock=True)
            option = BusinessOption.model_validate(row.payload["option"])
            if stage == "PREPARE":
                current = synchronize(engine, reader, row.factory_id)
                if current.run_id != row.run_id:
                    raise AccessError(
                        "SOURCE_RUN_CHANGED",
                        "The factory run has switched; execution of the old plan stopped.",
                        409,
                    )
                job = db.get(SolveJob, row.payload["approval"]["job_id"])
                original = db.get(SnapshotRecord, job.snapshot_id) if job else None
                if original is None or decision_key(
                    Snapshot.model_validate(original.document)
                ) != decision_key(current):
                    raise AccessError(
                        "STUDY_FACTS_CHANGED",
                        "The business facts changed before execution and no measures were applied; compare again.",
                        409,
                    )
                treatment_changes(current, option.actions)
                result.update(
                    stage="APPLY" if option.actions else "PLAN",
                    source_command={
                        "request_id": "treatment:" + row.action_id,
                        "run_id": row.run_id,
                        "kind": "treatment.apply",
                        "payload": {
                            "expected_snapshot_hash": current.content_hash,
                            "catalog_version": "byof-demo-economics/1",
                            "actions": [a.model_dump(mode="json") for a in option.actions],
                        },
                    },
                    summary="Measures checked; executing the approved business operations.",
                )
            elif stage == "APPLY":
                try:
                    receipt = controls.command(
                        row.factory_id, SimulatorCommand.model_validate(result["source_command"])
                    )
                except ConnectorError as exc:
                    if exc.code != "TREATMENT_FACTS_CHANGED" or result.get("refreshed", 0) >= 3:
                        raise
                    # The clock moved between checking and applying; nothing was applied. Resend
                    # the same request against the latest facts if only normal progress happened.
                    current = synchronize(engine, reader, row.factory_id)
                    job = db.get(SolveJob, row.payload["approval"]["job_id"])
                    original = db.get(SnapshotRecord, job.snapshot_id) if job else None
                    if original is None or decision_key(
                        Snapshot.model_validate(original.document)
                    ) != decision_key(current):
                        raise AccessError(
                            "STUDY_FACTS_CHANGED",
                            "The business facts changed before execution and no measures were applied; compare again.",
                            409,
                        ) from exc
                    treatment_changes(current, option.actions)
                    result["source_command"]["payload"]["expected_snapshot_hash"] = (
                        current.content_hash
                    )
                    result["refreshed"] = result.get("refreshed", 0) + 1
                    receipt = None
                if receipt is not None and receipt.get("cancelled"):
                    row.state = "CANCELLED"
                    result.update(
                        stage="CANCELLED",
                        summary="This plan was cancelled; no business measures were executed.",
                    )
                    row.result = result
                    case = db.get(CaseRecord, row.payload["case_id"], with_for_update=True)
                    if case and case.context.get("execution_pending") == row.action_id:
                        case.context = {
                            k: v for k, v in case.context.items() if k != "execution_pending"
                        }
                    return True
                if receipt is not None:
                    result.update(
                        stage="PLAN",
                        source_receipt=receipt,
                        summary="Business measures completed; checking the schedule after them.",
                    )
            elif stage == "PLAN":
                current = synchronize(engine, reader, row.factory_id)
                if current.run_id != row.run_id:
                    raise AccessError(
                        "SOURCE_RUN_CHANGED",
                        "The factory run has switched; completed measures are kept and the old plan does not continue.",
                        409,
                    )
                job = request_solve(
                    engine,
                    actor,
                    row.factory_id,
                    request_id="treatment-plan:" + row.action_id,
                    allow_overtime=option.allow_overtime,
                    time_limit=60,
                    case_id=row.payload["case_id"],
                )
                result.update(
                    stage="SOLVING",
                    job_id=job.job_id,
                    summary="Measures applied; verifying the production schedule within the approved scope.",
                )
            elif stage == "SOLVING":
                solved_job = db.get(SolveJob, result["job_id"])
                if solved_job is None or solved_job.state in {"QUEUED", "RUNNING"}:
                    row.next_attempt_at = datetime.now(UTC) + timedelta(seconds=2)
                    return True
                record = (
                    db.get(CandidateRecord, solved_job.candidate_id)
                    if solved_job.candidate_id
                    else None
                )
                if (
                    record is None
                    or not record.document["has_solution"]
                    or record.document["checker"]["status"] != "PASS"
                ):
                    raise AccessError(
                        "PLAN_NOT_VERIFIED",
                        "Completed measures are kept; the schedule has not passed the check and cannot be released. Adjust the plan.",
                        409,
                    )
                candidate = Candidate.model_validate(record.document)
                current = synchronize(engine, reader, row.factory_id)
                _within_scope(current, candidate, option)
                # This is an execution of the recorded human approval, never model consent.
                approved = AssistantAction(
                    action_id=row.action_id,
                    factory_id=row.factory_id,
                    user_id=row.user_id,
                    run_id=row.run_id,
                    payload={
                        "candidate_id": candidate.candidate_id,
                        "candidate_hash": candidate.content_hash,
                        "remember": False,
                        "priority": "auto",
                    },
                )
                published = _approve(engine, approved, actor, current)
                result.update(
                    stage="PUBLISHING",
                    candidate_id=candidate.candidate_id,
                    release_id=published["release_id"],
                    summary="The production schedule is within the approved scope; waiting for the factory to accept it.",
                )
            elif stage == "PUBLISHING":
                publication = db.get(Publication, result["release_id"])
                if publication and publication.document["source_state"] == "ACTIVE":
                    controls.command(
                        row.factory_id,
                        SimulatorCommand(
                            request_id="treatment-start:" + row.action_id,
                            run_id=row.run_id,
                            kind="clock.run",
                            payload={"interval_ms": 60000},
                        ),
                    )
                    row.state = "DONE"
                    result.update(
                        stage="DONE",
                        summary="Plan executed: measures applied and the factory accepted the production schedule; receipts and production follow the plan.",
                    )
                elif publication and publication.document["source_state"] == "REJECTED":
                    raise AccessError(
                        "SOURCE_REJECTED",
                        "Measures completed, but the factory did not accept the schedule; check the changes and adjust the plan.",
                        409,
                    )
            row.attempts = 0
        except ConnectorError:
            row.attempts += 1
            result["summary"] = (
                "The interface result is being checked; the original request is kept and nothing is purchased twice."
            )
            result["can_resume"] = True
            if row.attempts >= 3:
                row.state = "ATTENTION"
        except (AccessError, ValueError) as exc:
            row.state = "ATTENTION"
            result["can_resume"] = False
            result.update(
                code=exc.code if isinstance(exc, AccessError) else "TREATMENT_CONDITIONS_CHANGED",
                summary=str(exc)
                if isinstance(exc, AccessError)
                else "The facts or availability the measures need have changed; the execution ledger is kept. Adjust the plan.",
            )
        row.result = result
        row.next_attempt_at = datetime.now(UTC) + timedelta(seconds=2)
        case = db.get(CaseRecord, row.payload["case_id"], with_for_update=True)
        if case:
            context = dict(case.context)
            context["last_execution"] = {
                "action_id": row.action_id,
                "state": row.state,
                "stage": result.get("stage"),
                "summary": result.get("summary"),
                "source_effects_confirmed": bool(result.get("source_receipt")),
                "candidate_id": result.get("candidate_id"),
                "release_id": result.get("release_id"),
            }
            if result.get("candidate_id"):
                context["candidate_ids"] = list(
                    dict.fromkeys([*context.get("candidate_ids", []), result["candidate_id"]])
                )
            if (
                row.state != "QUEUED"
                and stage != "APPLY"
                and context.get("execution_pending") == row.action_id
            ):
                context.pop("execution_pending", None)
            case.context = context
            if row.state == "DONE":
                case.state = "WAITING"
                add_input(
                    db,
                    case,
                    f"treatment:{row.action_id}:done",
                    "EXECUTION_RESULT",
                    context["last_execution"],
                )
        return True
