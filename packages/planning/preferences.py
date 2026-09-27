"""Human-confirmed preference scopes resolve to one objective for shared factory resources."""

from datetime import UTC, datetime
from typing import Literal, cast
from uuid import uuid4

from pydantic import Field, StrictStr
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from packages.agent.cases import READ_ROLES, TERMINAL, live_actor
from packages.agent.cases_store import CaseRecord, add_input
from packages.agent.human_tasks import _current_run, lock_review_cases, reconcile_reviews
from packages.auth import AccessError, Principal
from packages.domain.models import (
    Contract,
    Digest,
    Identifier,
    NonNegative,
    Snapshot,
    canonical_hash,
)
from packages.domain.objectives import EffectiveObjective, ObjectiveDefinition, ObjectiveSource
from packages.planning.preference_store import (
    ObjectiveRecord,
    PreferenceAction,
    PreferenceCoordination,
    PreferenceHead,
    PreferenceProposal,
    PreferenceRevision,
    PreferenceState,
)
from packages.planning.service import require_live
from packages.planning.store import FactoryState, SnapshotRecord

Scope = Literal["FACTORY", "PROCESS", "CASE"]


class ObjectiveConflict(AccessError):
    def __init__(self, signature: str, sources: list[dict]):
        super().__init__(
            "OBJECTIVE_CONFLICT",
            "Shared factory resources have different objectives; a planner must merge and confirm them.",
            409,
        )
        self.context_hash = signature
        self.sources = sources


class ProposalInput(Contract):
    request_id: Identifier
    scope_type: Scope
    scope_id: Identifier
    definition: ObjectiveDefinition
    expected_version: NonNegative
    reason: StrictStr = Field(min_length=1, max_length=500, pattern=r"\S")
    source_proposal_id: Identifier | None = None


class ConfirmationInput(Contract):
    request_id: Identifier
    expected_state_version: NonNegative


class RejectionInput(Contract):
    request_id: Identifier
    reason: StrictStr = Field(min_length=1, max_length=500, pattern=r"\S")


class CoordinationInput(ConfirmationInput):
    context_hash: Digest
    definition: ObjectiveDefinition
    reason: StrictStr = Field(min_length=1, max_length=500, pattern=r"\S")


class DeactivationInput(ConfirmationInput):
    expected_version: NonNegative
    reason: StrictStr = Field(min_length=1, max_length=500, pattern=r"\S")


def process_scope(product_id: str, route_version: str) -> str:
    return "process:" + canonical_hash({"product_id": product_id, "route_version": route_version})


def _snapshot(db: Session, factory_id: str, *, lock=False) -> tuple[FactoryState, Snapshot]:
    state = db.get(FactoryState, factory_id, with_for_update=lock)
    record = db.get(SnapshotRecord, state.snapshot_id) if state else None
    if state is None or record is None or record.factory_id != factory_id:
        raise AccessError("SNAPSHOT_REQUIRED", "Sync the factory facts first.", 409)
    snapshot = Snapshot.model_validate(record.document)
    if snapshot.factory_id != factory_id or snapshot.run_id != state.run_id:
        raise AccessError(
            "SOURCE_RUN_CHANGED", "The factory run has changed; refresh the data.", 409
        )
    if lock:
        lock_review_cases(db, factory_id)
    return state, snapshot


def _state(db: Session, factory_id: str) -> PreferenceState:
    row = db.get(PreferenceState, factory_id)
    if row is None:
        row = PreferenceState(factory_id=factory_id, version=0)
        db.add(row)
        db.flush()
    return row


def _scope(
    db: Session,
    actor: Principal,
    state: FactoryState,
    snapshot: Snapshot,
    scope_type: str,
    scope_id: str,
) -> dict:
    require_live(snapshot)
    selector = {}
    if scope_type == "CASE":
        case = db.get(CaseRecord, scope_id, with_for_update=True)
        if case is None or case.factory_id != snapshot.factory_id:
            raise AccessError("NOT_FOUND", "The case does not belong to the current factory.", 404)
        _current_run(db, state, case)
        if case.state in TERMINAL:
            raise AccessError(
                "CASE_CLOSED", "The case is closed; its preference cannot change.", 409
            )
        live_actor(db, actor, snapshot.factory_id, {"planner"}, lock=True)
        if case.owner_id != actor.user_id:
            raise AccessError(
                "CASE_OWNER_REQUIRED", "Only the case owner can confirm this preference.", 403
            )
    else:
        live_actor(db, actor, snapshot.factory_id, {"admin"}, lock=True)
        if scope_type == "FACTORY" and scope_id != snapshot.factory_id:
            raise AccessError(
                "INVALID_PREFERENCE_SCOPE", "The factory preference scope does not match.", 422
            )
        if scope_type == "PROCESS":
            product = next(
                (
                    p
                    for p in snapshot.profile.products
                    if process_scope(p.product_id, p.route_version) == scope_id
                ),
                None,
            )
            if product is None:
                raise AccessError(
                    "INVALID_PROCESS_SCOPE",
                    "Choose a product route version in the current configuration.",
                    422,
                )
            selector = {"product_id": product.product_id, "route_version": product.route_version}
        elif scope_type != "FACTORY":
            raise AccessError(
                "INVALID_PREFERENCE_SCOPE", "The preference scope is not recognized.", 422
            )
    return selector


def _prior(
    db: Session, actor: Principal, factory_id: str, request_id: str, kind: str, payload: dict
):
    row = db.get(PreferenceAction, (factory_id, request_id))
    if row and (
        row.actor_id != actor.user_id
        or row.kind != kind
        or row.payload_hash != canonical_hash(payload)
    ):
        raise AccessError(
            "IDEMPOTENCY_CONFLICT",
            "The original action ID has different preference content or identity.",
            409,
        )
    return row.result if row else None


def _record(
    db: Session,
    actor: Principal,
    factory_id: str,
    request_id: str,
    kind: str,
    payload: dict,
    result: dict,
) -> dict:
    db.add(
        PreferenceAction(
            factory_id=factory_id,
            request_id=request_id,
            actor_id=actor.user_id,
            kind=kind,
            payload_hash=canonical_hash(payload),
            result=result,
            created_at=datetime.now(UTC),
        )
    )
    return result


def proposal_view(row: PreferenceProposal) -> dict:
    return {
        "proposal_id": row.proposal_id,
        "state": row.state,
        "scope_type": row.scope_type,
        "scope_id": row.scope_id,
        "definition": row.document["definition"],
        "expected_version": row.document["expected_version"],
        "created_at": row.created_at.isoformat(),
        "proposer_id": row.proposer_id,
        "reason": row.document["reason"],
    }


def propose(engine: Engine, actor: Principal, factory_id: str, body: ProposalInput) -> dict:
    with Session(engine) as db, db.begin():
        state, snapshot = _snapshot(db, factory_id, lock=True)
        selector = _scope(db, actor, state, snapshot, body.scope_type, body.scope_id)
        payload = body.model_dump(mode="json")
        old = _prior(db, actor, factory_id, body.request_id, "PROPOSE", payload)
        if old is not None:
            return old
        head = db.get(PreferenceHead, (factory_id, body.scope_type, body.scope_id))
        if (head.version if head else 0) != body.expected_version:
            raise AccessError(
                "PREFERENCE_VERSION_CHANGED",
                "The preference of this scope has changed; refresh before proposing.",
                409,
            )
        if body.source_proposal_id:
            case = db.get(CaseRecord, body.scope_id) if body.scope_type == "CASE" else None
            proposal = (
                case.context.get("pending_preference", {}).get(body.source_proposal_id)
                if case
                else None
            )
            if proposal is None:
                raise AccessError(
                    "PROPOSAL_NOT_IN_CASE", "The Agent proposal does not belong to this case.", 409
                )
        now = datetime.now(UTC)
        row = PreferenceProposal(
            proposal_id=str(uuid4()),
            factory_id=factory_id,
            scope_type=body.scope_type,
            scope_id=body.scope_id,
            proposer_id=actor.user_id,
            state="PENDING",
            document={
                **payload,
                "selector": selector,
                "profile_version": snapshot.profile.version,
                "policy_version": snapshot.profile.policy.policy_version,
                "run_id": snapshot.run_id,
            },
            created_at=now,
        )
        db.add(row)
        return _record(
            db, actor, factory_id, body.request_id, "PROPOSE", payload, proposal_view(row)
        )


def _wake_cases(db: Session, snapshot: Snapshot, request_id: str, version: int) -> None:
    for case in db.scalars(
        select(CaseRecord)
        .where(
            CaseRecord.factory_id == snapshot.factory_id,
            CaseRecord.run_id == snapshot.run_id,
            CaseRecord.state.not_in(TERMINAL),
        )
        .order_by(CaseRecord.case_id)
        .with_for_update()
    ):
        add_input(
            db,
            case,
            f"preference:{request_id}:{case.case_id}",
            "preference.changed",
            {"preference_state_version": version, "scope": "factory_shared_resources"},
        )
    db.flush()
    reconcile_reviews(db, snapshot)


def confirm(
    engine: Engine, actor: Principal, factory_id: str, proposal_id: str, body: ConfirmationInput
) -> dict:
    with Session(engine) as db, db.begin():
        state, snapshot = _snapshot(db, factory_id, lock=True)
        proposal = db.get(PreferenceProposal, proposal_id, with_for_update=True)
        if proposal is None or proposal.factory_id != factory_id:
            raise AccessError("NOT_FOUND", "The preference proposal does not exist.", 404)
        selector = _scope(db, actor, state, snapshot, proposal.scope_type, proposal.scope_id)
        definition = ObjectiveDefinition.model_validate(proposal.document["definition"])
        if definition.selection == "custom":
            live_actor(db, actor, factory_id, {"admin"}, lock=True)
        payload = {"proposal_id": proposal_id, **body.model_dump(mode="json")}
        prior = _prior(db, actor, factory_id, body.request_id, "CONFIRM", payload)
        if prior is not None:
            return prior
        prefs = _state(db, factory_id)
        if prefs.version != body.expected_state_version:
            raise AccessError(
                "PREFERENCE_STATE_CHANGED",
                "Other preferences have changed; check the overall objectives again.",
                409,
            )
        if proposal.state != "PENDING":
            raise AccessError(
                "PROPOSAL_CLOSED", "This preference proposal was already handled.", 409
            )
        document = proposal.document
        if (
            document["profile_version"] != snapshot.profile.version
            or document["policy_version"] != snapshot.profile.policy.policy_version
            or (proposal.scope_type == "CASE" and document["run_id"] != snapshot.run_id)
            or selector != document["selector"]
        ):
            raise AccessError(
                "PREFERENCE_BASE_CHANGED",
                "The route, policy or run has changed; propose the preference again.",
                409,
            )
        head = db.get(PreferenceHead, (factory_id, proposal.scope_type, proposal.scope_id))
        if (head.version if head else 0) != document["expected_version"]:
            raise AccessError(
                "PREFERENCE_VERSION_CHANGED",
                "This scope already has a new preference; propose again.",
                409,
            )
        now, identifier = datetime.now(UTC), str(uuid4())
        version = (head.version if head else 0) + 1
        source = ObjectiveSource(
            preference_id=identifier,
            version=version,
            scope_type=cast(Scope, proposal.scope_type),
            scope_id=proposal.scope_id,
            confirmed_by=actor.user_id,
            confirmed_at=now,
            **selector,
        )
        db.add(
            PreferenceRevision(
                preference_id=identifier,
                factory_id=factory_id,
                scope_type=proposal.scope_type,
                scope_id=proposal.scope_id,
                version=version,
                document={
                    "source": source.model_dump(mode="json"),
                    "definition": definition.model_dump(mode="json"),
                    "profile_version": snapshot.profile.version,
                    "policy_version": snapshot.profile.policy.policy_version,
                    "proposal_id": proposal_id,
                    "run_id": snapshot.run_id,
                },
                created_at=now,
            )
        )
        if head is None:
            head = PreferenceHead(
                factory_id=factory_id, scope_type=proposal.scope_type, scope_id=proposal.scope_id
            )
            db.add(head)
        head.version, head.preference_id, head.active = version, identifier, True
        proposal.state, prefs.version = "CONFIRMED", prefs.version + 1
        prefs.coordination_id = None
        _wake_cases(db, snapshot, body.request_id, prefs.version)
        result = {
            "preference_id": identifier,
            "version": version,
            "state_version": prefs.version,
            "scope_type": proposal.scope_type,
            "scope_id": proposal.scope_id,
            "definition": definition.model_dump(mode="json"),
        }
        return _record(db, actor, factory_id, body.request_id, "CONFIRM", payload, result)


def reject_proposal(
    engine: Engine, actor: Principal, factory_id: str, proposal_id: str, body: RejectionInput
) -> dict:
    with Session(engine) as db, db.begin():
        state, snapshot = _snapshot(db, factory_id, lock=True)
        proposal = db.get(PreferenceProposal, proposal_id, with_for_update=True)
        if proposal is None or proposal.factory_id != factory_id:
            raise AccessError("NOT_FOUND", "The preference proposal does not exist.", 404)
        _scope(db, actor, state, snapshot, proposal.scope_type, proposal.scope_id)
        payload = {"proposal_id": proposal_id, **body.model_dump(mode="json")}
        prior = _prior(db, actor, factory_id, body.request_id, "REJECT", payload)
        if prior is not None:
            return prior
        if proposal.state != "PENDING":
            raise AccessError(
                "PROPOSAL_CLOSED", "This preference proposal was already handled.", 409
            )
        proposal.state = "REJECTED"
        return _record(
            db,
            actor,
            factory_id,
            body.request_id,
            "REJECT",
            payload,
            {"proposal_id": proposal_id, "state": "REJECTED"},
        )


def _resolution(db: Session, snapshot: Snapshot) -> tuple[EffectiveObjective | None, str]:
    prefs = db.get(PreferenceState, snapshot.factory_id)
    epoch = prefs.version if prefs else 0
    cases = list(
        db.scalars(
            select(CaseRecord)
            .where(
                CaseRecord.factory_id == snapshot.factory_id,
                CaseRecord.run_id == snapshot.run_id,
                CaseRecord.state.not_in(TERMINAL),
            )
            .order_by(CaseRecord.case_id)
        )
    )
    current_cases = {case.case_id for case in cases}
    products = {p.product_id: p for p in snapshot.profile.products}
    current_products = {order.product_id for order in snapshot.orders}
    applicable = []
    for head in db.scalars(
        select(PreferenceHead)
        .where(PreferenceHead.factory_id == snapshot.factory_id, PreferenceHead.active.is_(True))
        .order_by(PreferenceHead.scope_type, PreferenceHead.scope_id)
    ):
        row = db.get(PreferenceRevision, head.preference_id)
        if row is None or row.factory_id != snapshot.factory_id or row.version != head.version:
            raise AccessError(
                "INVALID_PREFERENCE_RECORD", "The preference source records are inconsistent.", 409
            )
        doc = row.document
        source = ObjectiveSource.model_validate(doc["source"])
        if source.scope_type == "CASE" and source.scope_id not in current_cases:
            continue
        if source.scope_type == "PROCESS" and source.product_id not in current_products:
            continue
        if (
            doc["profile_version"] != snapshot.profile.version
            or doc["policy_version"] != snapshot.profile.policy.policy_version
            or (
                source.scope_type == "PROCESS"
                and (
                    source.product_id is None
                    or source.product_id not in products
                    or products[source.product_id].route_version != source.route_version
                )
            )
        ):
            raise AccessError(
                "PREFERENCE_BASE_CHANGED",
                "An existing preference references an old route or policy; the administrator must confirm it again.",
                409,
            )
        applicable.append((source, ObjectiveDefinition.model_validate(doc["definition"])))
    signature = canonical_hash(
        {
            "profile_version": snapshot.profile.version,
            "policy_version": snapshot.profile.policy.policy_version,
            "scope_version": snapshot.scope_version,
            "orders": sorted(order.order_id for order in snapshot.orders),
            "sources": [source.model_dump(mode="json") for source, _ in applicable],
        }
    )
    if not applicable and epoch == 0:
        return None, signature
    default = next(
        (definition for source, definition in applicable if source.scope_type == "FACTORY"),
        ObjectiveDefinition(),
    )
    case_definitions = [
        definition for source, definition in applicable if source.scope_type == "CASE"
    ]
    if case_definitions:
        definitions = case_definitions
    else:
        definitions = [
            next(
                (
                    definition
                    for source, definition in applicable
                    if source.scope_type == "PROCESS" and source.product_id == product
                ),
                default,
            )
            for product in sorted(current_products)
        ] or [default]
    different = len({canonical_hash(definition) for definition in definitions}) > 1
    coordinated = (
        db.get(PreferenceCoordination, prefs.coordination_id)
        if prefs and prefs.coordination_id
        else None
    )
    if different and (coordinated is None or coordinated.context_hash != signature):
        raise ObjectiveConflict(
            signature, [source.model_dump(mode="json") for source, _ in applicable]
        )
    coordination_id = None
    if coordinated and coordinated.context_hash == signature:
        definition = ObjectiveDefinition.model_validate(coordinated.document["definition"])
        coordination_id = coordinated.coordination_id
    else:
        definition = definitions[0]
    result = EffectiveObjective(
        factory_id=snapshot.factory_id,
        profile_version=snapshot.profile.version,
        policy_version=snapshot.profile.policy.policy_version,
        definition=definition,
        sources=tuple(source for source, _ in applicable),
        coordination_id=coordination_id,
        resolution_version=epoch,
    )
    result.validate_for(snapshot)
    return result, signature


def resolve_objective(
    db: Session, snapshot: Snapshot, *, save: bool = False
) -> EffectiveObjective | None:
    objective, _ = _resolution(db, snapshot)
    if save and objective is not None:
        existing = db.get(ObjectiveRecord, objective.objective_version)
        if existing is None:
            db.add(
                ObjectiveRecord(
                    objective_version=objective.objective_version,
                    factory_id=snapshot.factory_id,
                    document=objective.model_dump(mode="json"),
                    created_at=datetime.now(UTC),
                )
            )
    return objective


def load_objective(db: Session, factory_id: str, version: str) -> EffectiveObjective | None:
    if version == "delivery-v1":
        return None
    row = db.get(ObjectiveRecord, version)
    if row is None or row.factory_id != factory_id:
        raise AccessError(
            "OBJECTIVE_NOT_FOUND", "The confirmed objective contract of this plan is missing.", 409
        )
    objective = EffectiveObjective.model_validate(row.document)
    if objective.objective_version != version:
        raise AccessError(
            "OBJECTIVE_HASH_MISMATCH",
            "The objective contract content does not match its version.",
            409,
        )
    return objective


def require_current_objective(
    db: Session, snapshot: Snapshot, version: str
) -> EffectiveObjective | None:
    current = resolve_objective(db, snapshot)
    if (current.objective_version if current else "delivery-v1") != version:
        raise AccessError(
            "OBJECTIVE_CHANGED",
            "The scheduling objective has changed, so old plans and approvals are invalid; run the trial again.",
            409,
        )
    return load_objective(db, snapshot.factory_id, version)


def objective_view(db: Session, objective: EffectiveObjective | None) -> dict:
    coordination = None
    if objective and objective.coordination_id:
        row = db.get(PreferenceCoordination, objective.coordination_id)
        if row is None or row.factory_id != objective.factory_id:
            raise AccessError(
                "COORDINATION_NOT_FOUND",
                "The coordination confirmation record of this objective is missing.",
                409,
            )
        coordination = {
            "coordination_id": row.coordination_id,
            "context_hash": row.context_hash,
            **{key: row.document[key] for key in ("confirmed_by", "confirmed_at", "reason")},
        }
    return {
        "definition": (objective.definition if objective else ObjectiveDefinition()).model_dump(
            mode="json"
        ),
        "sources": [source.model_dump(mode="json") for source in objective.sources]
        if objective
        else [],
        "coordination": coordination,
        "resolution_version": objective.resolution_version if objective else 0,
    }


def effective_view(db: Session, snapshot: Snapshot) -> dict:
    try:
        objective, signature = _resolution(db, snapshot)
        return {
            "status": "READY",
            "objective_version": objective.objective_version if objective else "delivery-v1",
            **objective_view(db, objective),
            "context_hash": signature,
            "reason": None,
        }
    except ObjectiveConflict as exc:
        return {
            "status": "CONFLICT",
            "objective_version": None,
            "definition": None,
            "sources": exc.sources,
            "context_hash": exc.context_hash,
            "reason": exc.message,
        }
    except (AccessError, ValueError) as exc:
        return {
            "status": "INVALID",
            "objective_version": None,
            "definition": None,
            "sources": [],
            "context_hash": None,
            "reason": exc.message
            if isinstance(exc, AccessError)
            else "An existing preference contract failed validation; check the configuration again.",
        }


def coordinate(engine: Engine, actor: Principal, factory_id: str, body: CoordinationInput) -> dict:
    with Session(engine) as db, db.begin():
        _, snapshot = _snapshot(db, factory_id, lock=True)
        require_live(snapshot)
        live_actor(db, actor, factory_id, {"planner"}, lock=True)
        if body.definition.selection == "custom":
            live_actor(db, actor, factory_id, {"admin"}, lock=True)
        payload = body.model_dump(mode="json")
        prior = _prior(db, actor, factory_id, body.request_id, "COORDINATE", payload)
        if prior is not None:
            return prior
        prefs = _state(db, factory_id)
        current = effective_view(db, snapshot)
        if (
            prefs.version != body.expected_state_version
            or current["context_hash"] != body.context_hash
        ):
            raise AccessError(
                "PREFERENCE_STATE_CHANGED",
                "The scopes or preferences to coordinate have changed; check again.",
                409,
            )
        if current["status"] != "CONFLICT":
            raise AccessError(
                "NO_OBJECTIVE_CONFLICT", "There is no objective conflict to merge and confirm.", 409
            )
        now, identifier = datetime.now(UTC), str(uuid4())
        db.add(
            PreferenceCoordination(
                coordination_id=identifier,
                factory_id=factory_id,
                context_hash=body.context_hash,
                document={
                    "definition": body.definition.model_dump(mode="json"),
                    "confirmed_by": actor.user_id,
                    "confirmed_at": now.isoformat(),
                    "clock": "real",
                    "reason": body.reason,
                    "sources": current["sources"],
                },
                created_at=now,
            )
        )
        prefs.coordination_id, prefs.version = identifier, prefs.version + 1
        _wake_cases(db, snapshot, body.request_id, prefs.version)
        result = effective_view(db, snapshot)
        if result["status"] != "READY":
            raise AccessError(
                "COORDINATION_FAILED", "The merged objectives failed the full check.", 409
            )
        return _record(db, actor, factory_id, body.request_id, "COORDINATE", payload, result)


def deactivate(
    engine: Engine,
    actor: Principal,
    factory_id: str,
    scope_type: str,
    scope_id: str,
    body: DeactivationInput,
) -> dict:
    with Session(engine) as db, db.begin():
        state, snapshot = _snapshot(db, factory_id, lock=True)
        _scope(db, actor, state, snapshot, scope_type, scope_id)
        payload = {"scope_type": scope_type, "scope_id": scope_id, **body.model_dump(mode="json")}
        prior = _prior(db, actor, factory_id, body.request_id, "DEACTIVATE", payload)
        if prior is not None:
            return prior
        prefs = _state(db, factory_id)
        head = db.get(PreferenceHead, (factory_id, scope_type, scope_id))
        if prefs.version != body.expected_state_version:
            raise AccessError(
                "PREFERENCE_STATE_CHANGED",
                "The factory preferences have changed; refresh and check.",
                409,
            )
        if head is None or head.version != body.expected_version or not head.active:
            raise AccessError(
                "PREFERENCE_VERSION_CHANGED",
                "This preference has changed or is no longer used.",
                409,
            )
        head.active, head.version = False, head.version + 1
        prefs.version, prefs.coordination_id = prefs.version + 1, None
        _wake_cases(db, snapshot, body.request_id, prefs.version)
        return _record(
            db,
            actor,
            factory_id,
            body.request_id,
            "DEACTIVATE",
            payload,
            {
                "state_version": prefs.version,
                "scope_type": scope_type,
                "scope_id": scope_id,
                "version": head.version,
                "active": False,
            },
        )


def get_preferences(engine: Engine, actor: Principal, factory_id: str) -> dict:
    with Session(engine) as db:
        live_actor(db, actor, factory_id, READ_ROLES)
        _, snapshot = _snapshot(db, factory_id)
        prefs = db.get(PreferenceState, factory_id)
        heads = []
        for head in db.scalars(
            select(PreferenceHead)
            .where(PreferenceHead.factory_id == factory_id)
            .order_by(PreferenceHead.scope_type, PreferenceHead.scope_id)
        ):
            revision = db.get(PreferenceRevision, head.preference_id)
            heads.append(
                {
                    "scope_type": head.scope_type,
                    "scope_id": head.scope_id,
                    "version": head.version,
                    "active": head.active,
                    "preference_id": head.preference_id,
                    "definition": revision.document["definition"] if revision else None,
                }
            )
        cases = list(
            db.scalars(
                select(CaseRecord)
                .where(
                    CaseRecord.factory_id == factory_id,
                    CaseRecord.run_id == snapshot.run_id,
                    CaseRecord.state.not_in(TERMINAL),
                )
                .order_by(CaseRecord.created_at)
            )
        )
        agent_proposals = []
        for case in cases:
            for identifier, result in case.context.get("pending_preference", {}).items():
                proposal = result.get("proposal", {})
                selection = proposal.get("selection")
                if selection not in {"delivery_first", "stability_first", "overtime_first"}:
                    continue
                missing = [] if selection == "delivery_first" else ["max_weighted_tardiness"]
                if selection == "stability_first":
                    missing.append("max_incremental_overtime_minutes")
                agent_proposals.append(
                    {
                        "proposal_id": identifier,
                        "case_id": case.case_id,
                        "selection": selection,
                        "reason": proposal.get("summary", "Check the preference and its bounds."),
                        "missing_fields": missing,
                    }
                )
        return {
            "state_version": prefs.version if prefs else 0,
            "effective": effective_view(db, snapshot),
            "heads": heads,
            "proposals": [
                proposal_view(row)
                for row in db.scalars(
                    select(PreferenceProposal)
                    .where(PreferenceProposal.factory_id == factory_id)
                    .order_by(PreferenceProposal.created_at.desc())
                    .limit(100)
                )
            ],
            "processes": [
                {
                    "scope_id": process_scope(p.product_id, p.route_version),
                    "product_id": p.product_id,
                    "name": p.name,
                    "route_version": p.route_version,
                }
                for p in snapshot.profile.products
            ],
            "cases": [
                {"case_id": c.case_id, "title": c.title, "owner_id": c.owner_id, "state": c.state}
                for c in cases
            ],
            "agent_proposals": agent_proposals,
        }
