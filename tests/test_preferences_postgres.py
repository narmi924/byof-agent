"""PostgreSQL preference confirmation, shared-resource coordination and candidate invalidation."""

import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from test_human_tasks_postgres import human_context as human_context

from packages.agent.cases import create_case
from packages.agent.cases_store import CaseRecord
from packages.auth import AccessError, Grant, Principal
from packages.domain.models import Candidate
from packages.domain.objectives import ObjectiveDefinition
from packages.persistence import Membership, connect
from packages.planning import preferences as service
from packages.planning.preference_store import (
    ObjectiveRecord,
    PreferenceAction,
    PreferenceCoordination,
    PreferenceHead,
    PreferenceProposal,
    PreferenceRevision,
    PreferenceState,
)
from packages.planning.service import approve, claim_job, complete_job, request_solve, workspace
from packages.planning.solver import solve
from packages.planning.store import ApprovalRecord, CandidateRecord, SolveJob


@pytest.fixture
def context(human_context):
    ctx = human_context
    try:
        yield ctx
    finally:
        owner = connect(os.environ["TEST_MIGRATION_DATABASE_URL"])
        try:
            with owner.begin() as db:
                for model in (
                    ApprovalRecord,
                    SolveJob,
                    CandidateRecord,
                    ObjectiveRecord,
                    PreferenceAction,
                    PreferenceCoordination,
                    PreferenceHead,
                    PreferenceRevision,
                    PreferenceProposal,
                    PreferenceState,
                ):
                    db.execute(delete(model).where(model.factory_id == ctx.factory))
        finally:
            owner.dispose()


def definition(selection="delivery_first", **bounds):
    if selection == "stability_first":
        bounds = {"max_weighted_tardiness": 1000, "max_incremental_overtime_minutes": 0, **bounds}
    elif selection == "overtime_first":
        bounds = {"max_weighted_tardiness": 1000, **bounds}
    return ObjectiveDefinition(selection=selection, **bounds)


def propose(
    ctx, *, scope="FACTORY", scope_id=None, actor=None, expected=0, selected=None, request_id=None
):
    return service.propose(
        ctx.engine,
        ctx.actors[actor or ("planner" if scope == "CASE" else "admin")],
        ctx.factory,
        service.ProposalInput(
            request_id=request_id or str(uuid4()),
            scope_type=scope,
            scope_id=scope_id or (ctx.case_id if scope == "CASE" else ctx.factory),
            definition=selected or definition(),
            expected_version=expected,
            reason="Check the delivery and resource bounds of this turn",
        ),
    )


def confirm(ctx, proposed, epoch=0, actor=None, request_id=None):
    return service.confirm(
        ctx.engine,
        ctx.actors[actor or ("planner" if proposed["scope_type"] == "CASE" else "admin")],
        ctx.factory,
        proposed["proposal_id"],
        service.ConfirmationInput(
            request_id=request_id or str(uuid4()), expected_state_version=epoch
        ),
    )


def current(ctx):
    with Session(ctx.engine) as db:
        return service.resolve_objective(db, ctx.snapshot)


def test_proposal_and_repeated_get_never_activate_or_modify_source(context):
    ctx = context
    before = ctx.snapshot.content_hash
    assert current(ctx) is None
    proposal = propose(ctx)
    assert proposal["state"] == "PENDING" and current(ctx) is None
    first = service.get_preferences(ctx.engine, ctx.actors["admin"], ctx.factory)
    assert first == service.get_preferences(ctx.engine, ctx.actors["admin"], ctx.factory)
    assert first["effective"]["objective_version"] == "delivery-v1" and first["state_version"] == 0
    result = confirm(ctx, proposal)
    resolved = current(ctx)
    assert resolved is not None and resolved.objective_version != "delivery-v1"
    assert resolved.sources[0].confirmed_by == ctx.actors["admin"].user_id
    assert resolved.sources[0].preference_id == result["preference_id"]
    assert workspace(ctx.engine, ctx.factory)["snapshot"].content_hash == before


@pytest.mark.parametrize("actor", ["planner", "manager", "maintainer", "outsider"])
def test_factory_preferences_require_current_admin(context, actor):
    with pytest.raises(AccessError):
        propose(context, actor=actor)
    assert current(context) is None


def test_case_confirmation_requires_owner_and_cannot_be_granted_by_model(context):
    ctx = context
    p = propose(ctx, scope="CASE", selected=definition("stability_first"))
    for actor in ("manager", "admin", "maintainer", "outsider"):
        with pytest.raises(AccessError):
            confirm(ctx, p, actor=actor)
    with pytest.raises(ValueError):
        service.ConfirmationInput(request_id="model-true", expected_state_version=0, confirmed=True)
    confirm(ctx, p)
    assert current(ctx).definition.selection == "stability_first"


def test_idempotency_and_stale_confirmation_keep_one_immutable_revision(context):
    ctx = context
    p = propose(ctx, request_id="proposal")
    assert propose(ctx, request_id="proposal") == p
    result = confirm(ctx, p, request_id="confirm")
    assert confirm(ctx, p, request_id="confirm") == result
    p2 = propose(ctx, expected=1)
    with pytest.raises(AccessError) as failure:
        confirm(ctx, p2, epoch=0)
    assert failure.value.code == "PREFERENCE_STATE_CHANGED"
    with Session(ctx.engine) as db:
        assert (
            len(
                list(
                    db.scalars(
                        select(PreferenceRevision).where(
                            PreferenceRevision.factory_id == ctx.factory
                        )
                    )
                )
            )
            == 1
        )
    assert current(ctx).resolution_version == 1


def test_concurrent_confirmations_do_not_overwrite_factory_version(context):
    ctx = context
    proposals = [propose(ctx), propose(ctx, selected=definition("overtime_first"))]

    def attempt(p):
        try:
            return confirm(ctx, p)["state_version"]
        except AccessError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, proposals))
    assert sorted(str(r) for r in results) == ["1", "PREFERENCE_STATE_CHANGED"]


def test_process_selector_is_explicit_and_case_override_resolves_hierarchy(context):
    ctx = context
    product = next(
        p
        for p in ctx.snapshot.profile.products
        if p.product_id == ctx.snapshot.orders[0].product_id
    )
    scope_id = service.process_scope(product.product_id, product.route_version)
    confirm(ctx, propose(ctx))
    confirm(
        ctx,
        propose(ctx, scope="PROCESS", scope_id=scope_id, selected=definition("overtime_first")),
        epoch=1,
    )
    # If multiple process classes differ, full-factory solving requires explicit coordination or a Case override.
    confirm(ctx, propose(ctx, scope="CASE", selected=definition("stability_first")), epoch=2)
    resolved = current(ctx)
    assert resolved.definition.selection == "stability_first"
    assert {s.scope_type for s in resolved.sources} == {"FACTORY", "PROCESS", "CASE"}
    assert (
        next(s for s in resolved.sources if s.scope_type == "PROCESS").route_version
        == product.route_version
    )
    with pytest.raises(AccessError):
        propose(ctx, scope="PROCESS", scope_id="unknown-process")


def test_two_cases_conflict_until_planner_confirms_one_shared_objective(context):
    ctx = context
    second = create_case(
        ctx.engine,
        ctx.actors["planner"],
        ctx.factory,
        "second-case",
        "Another delivery case",
        start_new=True,
    )
    assert second["case_id"] != ctx.case_id
    assert (
        create_case(
            ctx.engine,
            ctx.actors["planner"],
            ctx.factory,
            "second-case",
            "Another delivery case",
            start_new=True,
        )["case_id"]
        == second["case_id"]
    )
    confirm(ctx, propose(ctx, scope="CASE", selected=definition("stability_first")))
    confirm(
        ctx,
        propose(
            ctx, scope="CASE", scope_id=second["case_id"], selected=definition("overtime_first")
        ),
        epoch=1,
    )
    data = service.get_preferences(ctx.engine, ctx.actors["planner"], ctx.factory)
    assert data["effective"]["status"] == "CONFLICT"
    with pytest.raises(AccessError) as failure:
        request_solve(
            ctx.engine,
            ctx.actors["planner"],
            ctx.factory,
            request_id="blocked",
            allow_overtime=False,
            time_limit=1,
        )
    assert failure.value.code == "OBJECTIVE_CONFLICT"
    body = service.CoordinationInput(
        request_id="merge",
        expected_state_version=2,
        context_hash=data["effective"]["context_hash"],
        definition=definition("delivery_first"),
        reason="Both cases share staff; confirm delivery first",
    )
    for actor in ("admin", "maintainer", "outsider"):
        with pytest.raises(AccessError):
            service.coordinate(ctx.engine, ctx.actors[actor], ctx.factory, body)
    result = service.coordinate(ctx.engine, ctx.actors["planner"], ctx.factory, body)
    assert result["status"] == "READY"
    assert service.coordinate(ctx.engine, ctx.actors["planner"], ctx.factory, body) == result
    jobs = [
        request_solve(
            ctx.engine,
            ctx.actors["planner"],
            ctx.factory,
            request_id=f"solve-{i}",
            allow_overtime=False,
            time_limit=1,
            case_id=c,
        )
        for i, c in enumerate((ctx.case_id, second["case_id"]))
    ]
    assert jobs[0].objective_version == jobs[1].objective_version == result["objective_version"]
    assert current(ctx).coordination_id is not None


def test_matching_case_preferences_share_without_unnecessary_coordination(context):
    ctx = context
    second = create_case(
        ctx.engine, ctx.actors["planner"], ctx.factory, "second", "Second case", start_new=True
    )
    confirm(ctx, propose(ctx, scope="CASE"))
    confirm(ctx, propose(ctx, scope="CASE", scope_id=second["case_id"]), epoch=1)
    assert len(current(ctx).sources) == 2 and current(ctx).coordination_id is None


def test_deactivation_and_case_close_do_not_revive_original_objective_version(context):
    ctx = context
    p = propose(ctx)
    confirm(ctx, p)
    first = current(ctx)
    service.deactivate(
        ctx.engine,
        ctx.actors["admin"],
        ctx.factory,
        "FACTORY",
        ctx.factory,
        service.DeactivationInput(
            request_id="disable",
            expected_state_version=1,
            expected_version=1,
            reason="Restore the original default",
        ),
    )
    second = current(ctx)
    assert second.sources == () and second.definition == definition()
    assert second.objective_version not in {"delivery-v1", first.objective_version}
    confirm(ctx, propose(ctx, scope="CASE", selected=definition("overtime_first")), epoch=2)
    third = current(ctx)
    with Session(ctx.engine) as db, db.begin():
        db.get(CaseRecord, ctx.case_id).state = "CANCELLED"
    fourth = current(ctx)
    assert fourth.sources == () and fourth.resolution_version == 3
    assert fourth.objective_version not in {
        "delivery-v1",
        second.objective_version,
        third.objective_version,
    }


def test_objective_change_invalidates_approved_candidate_without_changing_snapshot(context):
    ctx = context
    job = request_solve(
        ctx.engine,
        ctx.actors["planner"],
        ctx.factory,
        request_id="original",
        allow_overtime=False,
        time_limit=2,
    )
    claimed = claim_job(ctx.engine)
    assert claimed.job_id == job.job_id
    candidate = solve(ctx.snapshot, time_limit=2)
    assert candidate.has_solution and candidate.checker.status == "PASS"
    assert complete_job(ctx.engine, claimed, candidate)
    approval = approve(
        ctx.engine,
        ctx.actors["planner"],
        ctx.factory,
        candidate.candidate_id,
        request_id="approve",
        candidate_hash=candidate.content_hash,
        action_scope="publish_plan",
        decision="APPROVED",
    )
    assert approval.decision == "APPROVED"
    confirm(ctx, propose(ctx, selected=definition("overtime_first")))
    data = workspace(ctx.engine, ctx.factory)
    assert data["snapshot"].content_hash == ctx.snapshot.content_hash
    assert data["candidates"][0]["state"] == "STALE"
    with pytest.raises(AccessError) as failure:
        approve(
            ctx.engine,
            ctx.actors["planner"],
            ctx.factory,
            candidate.candidate_id,
            request_id="approve-again",
            candidate_hash=candidate.content_hash,
            action_scope="publish_plan",
            decision="APPROVED",
        )
    assert failure.value.code == "OBJECTIVE_CHANGED"


def test_solver_worker_result_must_match_claimed_objective(context):
    ctx = context
    confirm(ctx, propose(ctx))
    job = request_solve(
        ctx.engine,
        ctx.actors["planner"],
        ctx.factory,
        request_id="new-contract",
        allow_overtime=False,
        time_limit=2,
    )
    claimed = claim_job(ctx.engine)
    assert claimed.job_id == job.job_id
    old = solve(ctx.snapshot, time_limit=2)
    with pytest.raises(ValueError):
        complete_job(ctx.engine, claimed, old)
    with Session(ctx.engine) as db:
        objective = service.load_objective(db, ctx.factory, job.objective_version)
    candidate = solve(ctx.snapshot, time_limit=2, objective=objective)
    assert candidate.checker.status == "PASS" and complete_job(ctx.engine, claimed, candidate)
    with Session(ctx.engine) as db:
        record = db.get(CandidateRecord, candidate.candidate_id)
        assert (
            Candidate.model_validate(record.document).binding.objective_version
            == job.objective_version
        )


def grant(ctx, actor_name, role):
    actor = ctx.actors[actor_name]
    with Session(ctx.engine) as db, db.begin():
        db.add(Membership(user_id=actor.user_id, factory_id=ctx.factory, role=role))
    ctx.actors[actor_name] = Principal(
        user_id=actor.user_id,
        username=actor.username,
        grants=(*actor.grants, Grant(factory_id=ctx.factory, role=role)),
    )


def custom_definition():
    return ObjectiveDefinition(
        selection="custom",
        objective_order=definition("stability_first").objective_order,
        max_weighted_tardiness=1000,
        max_incremental_overtime_minutes=0,
    )


def make_conflict(ctx):
    second = create_case(
        ctx.engine, ctx.actors["planner"], ctx.factory, "second", "Second case", start_new=True
    )
    confirm(ctx, propose(ctx, scope="CASE", selected=definition("stability_first")))
    confirm(
        ctx,
        propose(
            ctx, scope="CASE", scope_id=second["case_id"], selected=definition("overtime_first")
        ),
        epoch=1,
    )
    return service.get_preferences(ctx.engine, ctx.actors["planner"], ctx.factory)["effective"]


def test_custom_case_confirmation_requires_admin_in_addition_to_owner(context):
    ctx = context
    proposal = propose(ctx, scope="CASE", selected=custom_definition())
    with pytest.raises(AccessError) as failure:
        confirm(ctx, proposal)
    assert failure.value.code == "FORBIDDEN"
    data = service.get_preferences(ctx.engine, ctx.actors["planner"], ctx.factory)
    assert data["state_version"] == 0 and data["proposals"][0]["state"] == "PENDING"
    assert data["heads"] == [] and current(ctx) is None
    grant(ctx, "planner", "admin")
    confirm(ctx, proposal)
    assert current(ctx).definition == custom_definition()
    assert current(ctx).sources[0].confirmed_by == ctx.actors["planner"].user_id


def test_custom_coordination_requires_admin_and_preserves_conflict_on_denial(context):
    ctx = context
    conflict = make_conflict(ctx)
    body = service.CoordinationInput(
        request_id="custom-coordinate",
        expected_state_version=2,
        context_hash=conflict["context_hash"],
        definition=custom_definition(),
        reason="Check the custom order",
    )
    with pytest.raises(AccessError) as failure:
        service.coordinate(ctx.engine, ctx.actors["planner"], ctx.factory, body)
    assert failure.value.code == "FORBIDDEN"
    data = service.get_preferences(ctx.engine, ctx.actors["planner"], ctx.factory)
    assert data["state_version"] == 2 and data["effective"] == conflict
    with Session(ctx.engine) as db:
        assert (
            db.scalar(
                select(PreferenceCoordination).where(
                    PreferenceCoordination.factory_id == ctx.factory
                )
            )
            is None
        )
        assert db.scalar(select(SolveJob).where(SolveJob.factory_id == ctx.factory)) is None
    grant(ctx, "planner", "admin")
    result = service.coordinate(ctx.engine, ctx.actors["planner"], ctx.factory, body)
    assert result["definition"] == custom_definition().model_dump(mode="json")
    assert result["resolution_version"] == 3


def test_coordinated_current_and_historical_view_name_the_actual_coordinator(context):
    ctx = context
    conflict = make_conflict(ctx)
    grant(ctx, "admin", "planner")
    result = service.coordinate(
        ctx.engine,
        ctx.actors["admin"],
        ctx.factory,
        service.CoordinationInput(
            request_id="coordinate",
            expected_state_version=2,
            context_hash=conflict["context_hash"],
            definition=definition(),
            reason="Shared delivery bounds checked",
        ),
    )
    audit = result["coordination"]
    assert result["definition"]["selection"] == "delivery_first"
    assert audit["confirmed_by"] == ctx.actors["admin"].user_id
    assert {s["confirmed_by"] for s in result["sources"]} == {ctx.actors["planner"].user_id}
    assert audit["reason"] == "Shared delivery bounds checked" and audit["confirmed_at"]
    with Session(ctx.engine) as db, db.begin():
        objective = service.resolve_objective(db, ctx.snapshot, save=True)
    job = request_solve(
        ctx.engine,
        ctx.actors["planner"],
        ctx.factory,
        request_id="coordinated-solve",
        allow_overtime=False,
        time_limit=2,
    )
    claimed = claim_job(ctx.engine)
    assert claimed.job_id == job.job_id
    candidate = solve(ctx.snapshot, time_limit=2, objective=objective)
    assert candidate.checker.status == "PASS" and complete_job(ctx.engine, claimed, candidate)
    view = workspace(ctx.engine, ctx.factory)
    assert view["objective_state"]["coordination"] == audit
    assert view["objective_contracts"][objective.objective_version]["coordination"] == audit
