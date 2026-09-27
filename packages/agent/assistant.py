"""Durable single-reviewer orchestration over the existing authorized services."""

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import Field, StrictBool, ValidationError
from sqlalchemy import or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from packages.agent.assistant_store import AssistantAction
from packages.agent.cases import TERMINAL, create_case, live_actor, message_case, recover_case_input
from packages.agent.cases_store import CaseInput, CaseRecord
from packages.agent.planning_context import latest_solver_state
from packages.agent.recovery_paths import recovery_paths
from packages.auth import AccessError, Grant, Principal
from packages.domain.execution import ScenarioConfiguration, SimulatorCommand, TimedOutage
from packages.domain.models import Candidate, Contract, Digest, Identifier, Snapshot
from packages.integrations.factory_http import ConnectorError, FactoryControls, FactoryHTTP
from packages.persistence import Membership, User
from packages.planning.publication import Publication, commit_publication
from packages.planning.revalidation import issue_certificate
from packages.planning.review_store import ApprovalReviewRecord
from packages.planning.reviews import approve_progress
from packages.planning.service import active_baseline, approve, require_live, synchronize
from packages.planning.store import ApprovalRecord, CandidateRecord, FactoryState, SnapshotRecord

METRICS = {
    "delivery": "weighted_tardiness",
    "stability": "changed_operations",
    "overtime": "incremental_overtime_metric",
}
DEFAULT_WEIGHTS = {"delivery": 0.5, "stability": 0.3, "overtime": 0.2}


class ApprovalChoice(Contract):
    candidate_id: Identifier
    candidate_hash: Digest
    allow_overtime: StrictBool = False
    remember: StrictBool = True
    priority: Literal["auto", "delivery", "stability", "overtime"] = "auto"


class StartDay(Contract):
    interval_ms: int = Field(default=60000, strict=True, ge=1000, le=60000)


class PreferenceChoice(Contract):
    priority: Literal["delivery", "stability", "overtime"]


class RecoveryChoice(Contract):
    case_id: Identifier
    path_id: Identifier


class AssistantRequest(Contract):
    request_id: Identifier
    run_id: Identifier
    kind: Literal[
        "start",
        "approve",
        "recover",
        "outage",
        "scenario",
        "preference",
        "reset",
        "treatment_execute",
    ]
    payload: dict = Field(default_factory=dict)


def action_view(row: AssistantAction) -> dict:
    return {
        key: getattr(row, key)
        for key in (
            "action_id",
            "request_id",
            "run_id",
            "kind",
            "payload",
            "state",
            "result",
            "created_at",
        )
    }


def track_recovery_paths(db: Session, snapshot: Snapshot, paths: list) -> list[dict]:
    """Match the factory-wide duplicate guard, including work queued in other chats."""
    active = list(
        db.scalars(
            select(AssistantAction)
            .join(CaseRecord, CaseRecord.case_id == AssistantAction.payload["case_id"].astext)
            .where(
                AssistantAction.factory_id == snapshot.factory_id,
                AssistantAction.run_id == snapshot.run_id,
                AssistantAction.kind == "recover",
                AssistantAction.state.in_(("QUEUED", "DONE")),
                CaseRecord.state.not_in(TERMINAL),
            )
            .order_by(AssistantAction.created_at.desc())
        )
    )
    result = []
    for path in paths:
        tracking = next(
            (
                row
                for row in active
                if row.payload.get("path_id") == path["path_id"]
                or (row.result or {}).get("path", {}).get("kind") == path["kind"]
            ),
            None,
        )
        result.append(
            {**path, "tracking_case_id": tracking.payload["case_id"] if tracking else None}
        )
    return result


def learning(db: Session, factory_id: str, user_id: str) -> dict:
    rows = list(
        db.scalars(
            select(AssistantAction)
            .where(
                AssistantAction.factory_id == factory_id,
                AssistantAction.user_id == user_id,
                AssistantAction.state == "DONE",
                AssistantAction.kind.in_(("approve", "preference", "reset")),
            )
            .order_by(AssistantAction.created_at.desc(), AssistantAction.action_id)
            .limit(100)
        )
    )
    votes = {key: 0.0 for key in METRICS}
    samples, explicit = 0, None
    for row in rows:
        if row.kind == "reset":
            break
        if row.kind == "preference":
            explicit = row.payload["priority"]
            break
        evidence = (row.result or {}).get("learning", {})
        if evidence:
            samples += 1
            for key in votes:
                votes[key] += evidence.get(key, 0)
    weights = dict(DEFAULT_WEIGHTS)
    if explicit:
        weights = {key: 0.7 if key == explicit else 0.15 for key in votes}
    if samples >= 3:
        total = sum(votes.values())
        weights = {key: round((3 * weights[key] + votes[key]) / (3 + total), 4) for key in votes}
    return {
        "weights": weights,
        "samples": samples,
        "explicit": explicit,
        "active": samples >= 3 or explicit is not None,
        "summary": "Only adjusts the recommendation order of feasible plans; production constraints and approval permissions do not change.",
    }


def _snapshot(db: Session, factory_id: str) -> Snapshot:
    state = db.get(FactoryState, factory_id)
    saved = db.get(SnapshotRecord, state.snapshot_id) if state else None
    if saved is None:
        raise AccessError("SNAPSHOT_REQUIRED", "Connecting to the factory; try again shortly.", 409)
    snapshot = Snapshot.model_validate(saved.document)
    require_live(snapshot)
    return snapshot


def enqueue(engine: Engine, actor: Principal, factory_id: str, body: AssistantRequest) -> dict:
    if body.kind == "treatment_execute":
        from packages.agent.treatment_execution import (
            TreatmentApproval,
            TreatmentSelection,
            approve_treatment,
        )

        try:
            selected = TreatmentSelection.model_validate(body.payload)
        except ValidationError as exc:
            raise AccessError(
                "INVALID_INPUT", "Approve execution from a costed option card.", 422
            ) from exc
        return approve_treatment(
            engine,
            actor,
            factory_id,
            selected.job_id,
            selected.option_id,
            TreatmentApproval(
                request_id=body.request_id,
                study_hash=selected.study_hash,
                allow_overtime=selected.allow_overtime,
                accept_customer_change=selected.accept_customer_change,
            ),
            expected_run_id=body.run_id,
        )
    contracts: dict = {
        "start": StartDay,
        "approve": ApprovalChoice,
        "recover": RecoveryChoice,
        "outage": TimedOutage,
        "scenario": ScenarioConfiguration,
        "preference": PreferenceChoice,
        "reset": Contract,
    }
    try:
        payload = contracts[body.kind].model_validate(body.payload).model_dump(mode="json")
    except ValidationError:
        raise AccessError(
            "INVALID_INPUT",
            "The action parameters are incomplete or out of range; check the card.",
            422,
        ) from None
    with Session(engine) as db, db.begin():
        db.get(FactoryState, factory_id, with_for_update=True)
        live_actor(db, actor, factory_id, {"planner"}, lock=True)
        if body.kind in {"start", "outage", "scenario"}:
            live_actor(db, actor, factory_id, {"sim_admin"}, lock=True)
        if body.kind == "approve" and payload["allow_overtime"]:
            live_actor(db, actor, factory_id, {"manager"}, lock=True)
        if body.kind == "recover":
            live_actor(db, actor, factory_id, {"manager"}, lock=True)
        prior = db.scalar(
            select(AssistantAction).where(
                AssistantAction.factory_id == factory_id,
                AssistantAction.request_id == body.request_id,
            )
        )
        if prior:
            if (prior.user_id, prior.run_id, prior.kind, prior.payload) != (
                actor.user_id,
                body.run_id,
                body.kind,
                payload,
            ):
                raise AccessError(
                    "IDEMPOTENCY_CONFLICT",
                    "The original request was used for another action; check the records.",
                    409,
                )
            if prior.state == "ATTENTION":
                prior.state, prior.attempts, prior.next_attempt_at = "QUEUED", 0, datetime.now(UTC)
            return action_view(prior)
        snapshot = _snapshot(db, factory_id)
        if snapshot.run_id != body.run_id:
            raise AccessError("SOURCE_RUN_CHANGED", "The factory run has changed; refresh.", 409)
        if (
            body.kind in {"start", "outage", "scenario"}
            and snapshot.source.ownership != "simulator_fact"
        ):
            raise AccessError(
                "SIMULATION_ONLY", "This action only applies to the simulated factory.", 409
            )
        if body.kind == "approve":
            record = db.get(CandidateRecord, payload["candidate_id"])
            if (
                record is None
                or record.factory_id != factory_id
                or record.content_hash != payload["candidate_hash"]
            ):
                raise AccessError(
                    "CANDIDATE_CHANGED", "The plan does not exist or has changed.", 409
                )
            candidate = Candidate.model_validate(record.document)
            if "allow_overtime" in candidate.required_consents and not payload["allow_overtime"]:
                raise AccessError(
                    "OVERTIME_CONFIRMATION_REQUIRED",
                    "Tick the overtime approval of this plan explicitly.",
                    409,
                )
            duplicate = db.scalar(
                select(AssistantAction).where(
                    AssistantAction.factory_id == factory_id,
                    AssistantAction.kind == "approve",
                    AssistantAction.state.in_(("QUEUED", "DONE")),
                    AssistantAction.payload["candidate_hash"].astext == payload["candidate_hash"],
                )
            )
            if duplicate:
                raise AccessError(
                    "APPROVAL_ALREADY_SUBMITTED",
                    "This plan was already submitted for approval and execution; see the execution receipt.",
                    409,
                )
        if body.kind == "recover":
            case = db.get(CaseRecord, payload["case_id"])
            if (
                case is None
                or case.factory_id != factory_id
                or case.run_id != body.run_id
                or case.owner_id != actor.user_id
                or case.state in TERMINAL
            ):
                raise AccessError(
                    "CASE_CHANGED",
                    "The current conversation has changed; review the shop floor again.",
                    409,
                )
            baseline = active_baseline(db, snapshot) if snapshot.active_plan_hash else None
            selected_path = next(
                (
                    path
                    for path in recovery_paths(
                        snapshot,
                        baseline,
                        solver_state=latest_solver_state(db, snapshot, case.case_id),
                    )
                    if path["path_id"] == payload["path_id"]
                ),
                None,
            )
            if selected_path is None:
                raise AccessError(
                    "RECOVERY_CHANGED",
                    "The recovery conditions have changed; choose again based on the current shop floor.",
                    409,
                )
            recovery_duplicate = db.scalar(
                select(AssistantAction.action_id)
                .join(CaseRecord, CaseRecord.case_id == AssistantAction.payload["case_id"].astext)
                .where(
                    AssistantAction.factory_id == factory_id,
                    AssistantAction.run_id == body.run_id,
                    AssistantAction.kind == "recover",
                    AssistantAction.state.in_(("QUEUED", "DONE")),
                    or_(
                        AssistantAction.payload["path_id"].astext == payload["path_id"],
                        AssistantAction.result["path"]["kind"].astext == selected_path["kind"],
                    ),
                    CaseRecord.state.not_in(TERMINAL),
                )
                .limit(1)
            )
            if recovery_duplicate:
                raise AccessError(
                    "RECOVERY_ALREADY_SUBMITTED",
                    "This recovery direction is already being followed; see the current record.",
                    409,
                )
        now = datetime.now(UTC)
        row = AssistantAction(
            action_id=str(uuid4()),
            factory_id=factory_id,
            user_id=actor.user_id,
            request_id=body.request_id,
            run_id=body.run_id,
            kind=body.kind,
            payload=payload,
            state="QUEUED",
            created_at=now,
            next_attempt_at=now,
            attempts=0,
        )
        db.add(row)
        db.flush()
        return action_view(row)


def _control(
    controls: FactoryControls | None,
    row: AssistantAction,
    kind: str,
    payload: dict,
    suffix: str = "",
) -> dict:
    if controls is None:
        raise AccessError(
            "SIMULATOR_UNAVAILABLE",
            "Simulator control is not configured; contact the administrator.",
            409,
        )
    return controls.command(
        row.factory_id,
        SimulatorCommand.model_validate(
            {
                "request_id": f"assistant:{row.action_id}:{suffix or kind}",
                "run_id": row.run_id,
                "kind": kind,
                "payload": payload,
            }
        ),
    )


def _record(db: Session, candidate_id: str) -> CandidateRecord:
    record = db.get(CandidateRecord, candidate_id)
    if record is None:
        raise AccessError("NOT_FOUND", "The plan is no longer available; recalculate.", 404)
    return record


def _message(
    engine: Engine, actor: Principal, row: AssistantAction, message: str, prefix: str = "assistant:"
) -> dict:
    with Session(engine) as db:
        prior = db.scalar(
            select(CaseInput).where(
                CaseInput.factory_id == row.factory_id,
                CaseInput.input_key == "user:" + prefix + row.action_id,
            )
        )
        if prior is not None:
            recovered = recover_case_input(
                engine,
                actor,
                row.factory_id,
                prefix + row.action_id,
                message,
                case_id=prior.case_id,
                start_new=prior.payload.get("start_new", False),
            )
            assert recovered is not None
            return recovered
        case = db.scalar(
            select(CaseRecord)
            .where(
                CaseRecord.factory_id == row.factory_id,
                CaseRecord.run_id == row.run_id,
                CaseRecord.owner_id == actor.user_id,
                CaseRecord.state.not_in(TERMINAL),
            )
            .order_by(CaseRecord.updated_at.desc())
            .limit(1)
        )
        case_id = case.case_id if case else None
    if case_id:
        return message_case(engine, actor, row.factory_id, case_id, prefix + row.action_id, message)
    return create_case(
        engine, actor, row.factory_id, prefix + row.action_id, message, start_new=True
    )


def _selection_evidence(db: Session, row: AssistantAction, candidate: Candidate) -> dict:
    if not row.payload["remember"]:
        return {}
    if row.payload["priority"] != "auto":
        return {row.payload["priority"]: 1.0}
    snapshot_id = _record(db, candidate.candidate_id).snapshot_id
    peers = [
        Candidate.model_validate(r.document)
        for r in db.scalars(
            select(CandidateRecord).where(
                CandidateRecord.factory_id == row.factory_id,
                CandidateRecord.snapshot_id == snapshot_id,
            )
        )
    ]
    peers = [
        c
        for c in peers
        if c.has_solution
        and c.checker.status == "PASS"
        and c.binding == candidate.binding
        and c.content_hash != candidate.content_hash
    ]
    selected = {m.name: m.value for m in candidate.objective}
    evidence = {}
    for key, metric in METRICS.items():
        values = [
            m.value for c in peers for m in c.objective if m.name == metric and m.value is not None
        ]
        value = selected.get(metric)
        if value is not None and values and value <= min(values) and value < max(values):
            evidence[key] = 1.0
    total = sum(evidence.values())
    return {key: value / total for key, value in evidence.items()} if total else {}


def _approve(engine: Engine, row: AssistantAction, actor: Principal, current: Snapshot) -> dict:
    candidate_id, digest = row.payload["candidate_id"], row.payload["candidate_hash"]
    with Session(engine) as db:
        existing = db.scalar(
            select(Publication).where(
                Publication.factory_id == row.factory_id,
                Publication.request_id == "assistant:" + row.action_id,
            )
        )
        if existing:
            return {
                "summary": "Approval recorded; the plan is queued for automatic release.",
                "release_id": existing.release_id,
                "learning": _selection_evidence(
                    db,
                    row,
                    Candidate.model_validate(_record(db, candidate_id).document),
                ),
            }
        candidate = Candidate.model_validate(_record(db, candidate_id).document)
        if candidate.content_hash != digest:
            raise AccessError("CANDIDATE_CHANGED", "The plan has changed; review it again.", 409)
    for scope in sorted({"publish_plan", *candidate.required_consents}):
        request_id = f"assistant:{row.action_id}:{scope}"
        with Session(engine) as db:
            previous = db.scalar(
                select(ApprovalRecord).where(
                    ApprovalRecord.factory_id == row.factory_id,
                    ApprovalRecord.request_id == request_id,
                )
            )
            progress = (
                bool(
                    db.scalar(
                        select(ApprovalReviewRecord).where(
                            ApprovalReviewRecord.approval_id == previous.approval_id
                        )
                    )
                )
                if previous
                else current.content_hash != candidate.binding.snapshot_hash
            )
        (approve_progress if progress else approve)(
            engine,
            actor,
            row.factory_id,
            candidate_id,
            request_id=request_id,
            candidate_hash=digest,
            action_scope=scope,
            decision="APPROVED",
        )
    certificate_id = None
    if current.content_hash != candidate.binding.snapshot_hash:
        certificate_id = issue_certificate(
            engine,
            actor,
            row.factory_id,
            candidate_id,
            request_id=f"assistant:{row.action_id}:validation:{current.source.source_revision}",
            candidate_hash=digest,
        ).certificate_id
    release = commit_publication(
        engine,
        actor,
        row.factory_id,
        candidate_id,
        request_id="assistant:" + row.action_id,
        candidate_hash=digest,
        certificate_id=certificate_id,
    )
    with Session(engine) as db:
        evidence = _selection_evidence(db, row, candidate)
    return {
        "summary": "Approval recorded; the plan is queued for automatic release.",
        "release_id": release.release_id,
        "learning": evidence,
    }


def process_one(engine: Engine, reader: FactoryHTTP, controls: FactoryControls | None) -> bool:
    """An action lock fences workers; each external/local sub-action also has a stable key."""
    with Session(engine) as db, db.begin():
        row = db.scalar(
            select(AssistantAction)
            .where(
                AssistantAction.state == "QUEUED",
                AssistantAction.kind != "treatment_execute",
                AssistantAction.next_attempt_at <= datetime.now(UTC),
            )
            .order_by(AssistantAction.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return False
        if row.kind == "business_accept":
            # Old queued choices cannot write factory facts after role separation.
            row.state, row.result = (
                "FAILED",
                {
                    "code": "BUSINESS_ACCEPT_UNSUPPORTED",
                    "summary": "The entry that wrote business options directly into the shop floor is retired. The administrator updates factory facts as they actually are; the manager analyzes and approves production plans in the conversation.",
                },
            )
            return True
        user = db.get(User, row.user_id)
        grants = tuple(
            Grant.model_validate({"factory_id": g.factory_id, "role": g.role})
            for g in db.scalars(
                select(Membership).where(
                    Membership.user_id == row.user_id, Membership.factory_id == row.factory_id
                )
            )
        )
        try:
            if user is None or not user.active:
                raise AccessError(
                    "FORBIDDEN", "The account is disabled; the action did not continue."
                )
            actor = Principal(user_id=user.user_id, username=user.username, grants=grants)
            live_actor(db, actor, row.factory_id, {"planner"}, lock=True)
            if row.kind in {"start", "outage", "scenario"}:
                live_actor(db, actor, row.factory_id, {"sim_admin"}, lock=True)
            if row.kind == "approve" and row.payload["allow_overtime"]:
                live_actor(db, actor, row.factory_id, {"manager"}, lock=True)
            if row.kind == "recover":
                live_actor(db, actor, row.factory_id, {"manager"}, lock=True)
            current = synchronize(engine, reader, row.factory_id)
            if current.run_id != row.run_id:
                raise AccessError(
                    "SOURCE_RUN_CHANGED",
                    "The factory run has changed; the original action stopped.",
                    409,
                )
            if row.kind == "approve":
                result = _approve(engine, row, actor, current)
                # The initial day begins only after the first source acceptance. Existing
                # production is never paused by review or publication.
                with Session(engine) as check:
                    initial_plan = (
                        Candidate.model_validate(
                            _record(check, row.payload["candidate_id"]).document
                        ).binding.baseline_plan_version
                        is None
                    )
                if initial_plan and any(g.role in {"manager", "sim_admin"} for g in grants):
                    # Starting the accepted first plan is part of the manager's
                    # approval, not a general simulator-control permission.
                    live_actor(
                        db,
                        actor,
                        row.factory_id,
                        {"manager"} if any(g.role == "manager" for g in grants) else {"sim_admin"},
                        lock=True,
                    )
                    row.result = result
                    row.next_attempt_at = datetime.now(UTC) + timedelta(seconds=2)
                    with Session(engine) as check:
                        publication = check.get(Publication, result["release_id"])
                        assert publication is not None
                        source_state = publication.document["source_state"]
                    if source_state == "REJECTED":
                        raise AccessError(
                            "SOURCE_REJECTED",
                            "The shop floor did not accept the plan; ask the Agent to recalculate.",
                            409,
                        )
                    if source_state != "ACTIVE":
                        return True
                    _control(controls, row, "clock.run", {"interval_ms": 60000})
            elif row.kind == "recover":
                from packages.agent.recovery_followup import followup_key

                with Session(engine) as check:
                    baseline = active_baseline(check, current) if current.active_plan_hash else None
                    path = next(
                        (
                            option
                            for option in recovery_paths(
                                current,
                                baseline,
                                solver_state=latest_solver_state(
                                    check, current, row.payload["case_id"]
                                ),
                            )
                            if option["path_id"] == row.payload["path_id"]
                        ),
                        None,
                    )
                if path is None:
                    raise AccessError(
                        "RECOVERY_CHANGED",
                        "Shop floor conditions have changed; choose the recovery direction again.",
                        409,
                    )
                case = message_case(
                    engine,
                    actor,
                    row.factory_id,
                    row.payload["case_id"],
                    "recovery:" + row.action_id,
                    "I choose and authorize this recovery direction: "
                    + path["title"]
                    + ". "
                    + path["prompt"],
                )
                result = {
                    "summary": "Recovery direction recorded. The Agent keeps following up; purchases, rework or customer changes only enter the official schedule after the shop floor confirms them.",
                    "case_id": case["case_id"],
                    "path": path,
                    "followup_key": followup_key(current, path["kind"]),
                }
            elif row.kind == "start":
                if current.active_plan_version is not None:
                    _control(controls, row, "clock.run", row.payload)
                case = _message(
                    engine,
                    actor,
                    row,
                    "Start today's production. With an existing plan, follow its execution and handle disruptions, reserving 15 minutes of review time when rescheduling; without a plan the production clock has not started, so first generate today's plan without delaying the start and give it to me to approve. Prioritize delivery and compare the schedule without overtime first.",
                )
                result = {
                    "summary": "Following today's production."
                    if current.active_plan_version
                    else "Preparing today's plan; production starts automatically after the first approval once the shop floor accepts it.",
                    "case_id": case["case_id"],
                }
            elif row.kind in {"outage", "scenario"}:
                kind = "resource.outage" if row.kind == "outage" else "scenario.configure"
                receipt = _control(controls, row, kind, row.payload)
                synchronize(engine, reader, row.factory_id)
                message = (
                    f"The shop floor confirmed machine {row.payload['resource_id']} is down for {row.payload['minutes']} minutes; the unavailable window and remaining shop floor work were updated through the enterprise interface. Analyze the impact, reschedule and make a recommendation."
                    if row.kind == "outage"
                    else "Random event settings updated. Keep following production and analyze disruptions proactively with options."
                )
                case = _message(engine, actor, row, message)
                result = {"summary": message, "case_id": case["case_id"], "receipt": receipt}
            else:
                result = {
                    "summary": "Learning records reset; default recommendations restored."
                    if row.kind == "reset"
                    else "Recommendation preference updated; production constraints stay in force."
                }
            row.state, row.result = "DONE", result
        except ConnectorError as exc:
            if exc.status < 500:
                row.state, row.result = "FAILED", {"summary": str(exc), "code": exc.code}
                return True
            row.attempts += 1
            row.next_attempt_at = datetime.now(UTC) + timedelta(seconds=10)
            row.result = {
                "summary": "The enterprise interface is temporarily unavailable; checking the original request."
            }
            if row.attempts >= 6:
                row.state = "ATTENTION"
                row.result = {
                    "summary": "The interface stays unavailable and the original action result is pending. Check the original request after the connection recovers."
                }
        except AccessError as exc:
            row.state, row.result = "FAILED", {"summary": exc.message, "code": exc.code}
            if row.kind == "approve" and exc.status == 409 and exc.code != "SOURCE_RUN_CHANGED":
                try:
                    _message(
                        engine,
                        actor,
                        row,
                        "The review could not be executed: "
                        + exc.message
                        + " Check the new facts and generate a new plan for review if needed; the old approval is not reused.",
                        prefix="replan:",
                    )
                except AccessError:
                    # Retain the failed decision even if the original conversation has closed.
                    row.result = {
                        **row.result,
                        "summary": exc.message + " Raise the request again in the conversation.",
                    }
        return True
