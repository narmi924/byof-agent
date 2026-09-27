"""Case-bound studies and disabled legacy business writes against PostgreSQL/source HTTP."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from test_assistant_postgres import (
    case_context as case_context,
)
from test_assistant_postgres import (
    dynamic_source as dynamic_source,
)
from test_assistant_postgres import (
    publishing as publishing,
)
from test_assistant_postgres import ready, request, run
from test_assistant_postgres import (
    reviewer as reviewer,
)
from test_dynamic_factory_postgres import control, snapshot

from packages.agent.assistant import process_one
from packages.agent.assistant_store import AssistantAction
from packages.agent.cases import create_case
from packages.agent.cases_store import CaseInput, CaseRecord
from packages.auth import AccessError
from packages.domain.business_options import BusinessStudy, BusinessStudyRequest
from packages.domain.business_terms import BusinessTerms, DeliveryRule, ExpediteQuote
from packages.domain.demand import finished_goods
from packages.domain.models import Order, canonical_hash
from packages.persistence import Membership
from packages.planning.business_service import request_business_study, study_view
from packages.planning.publication import Publication, deliver_one
from packages.planning.service import request_solve, synchronize
from packages.planning.store import CandidateRecord, SolveJob
from services.api import assistant as assistant_api
from services.factory_sim.engine import evolve
from services.factory_sim.service import _write
from services.factory_sim.storage import SourceAction, World
from services.solver_worker.main import run_once


@pytest.fixture
def business_context(reviewer):
    ctx = reviewer
    original = snapshot(ctx[0])
    receipt = next(
        receipt
        for receipt in original.receipts
        if receipt.status in {"EXPECTED", "CONFIRMED"}
        and receipt.eta > original.snapshot_clock + timedelta(minutes=1)
    )
    terms = BusinessTerms(
        version="test-business/1",
        evidence_mode="synthetic",
        delivery_rules=(
            DeliveryRule(
                product_id=original.orders[0].product_id,
                partial_delivery_allowed=True,
                minimum_partial_quantity=50,
                max_deliveries=2,
            ),
        ),
        expedite_quotes=(
            ExpediteQuote(
                quote_id="TEST-QUOTE",
                receipt_id=receipt.receipt_id,
                receipt_version=receipt.version,
                original_eta=receipt.eta,
                expedited_eta=original.snapshot_clock + timedelta(minutes=1),
                quantity=receipt.quantity,
                valid_until=original.snapshot_clock + timedelta(minutes=60),
                source_reference="synthetic-business-test",
                evidence_mode="synthetic",
                cost_minor=1250,
                currency="CNY",
            ),
        ),
    )
    # Trusted test setup updates the isolated source through its normal delta writer.
    with Session(ctx[0][4]) as db, db.begin():
        world = db.get(World, original.factory_id, with_for_update=True)
        updated = evolve(original, schema_version="byof.snapshot/3", business_terms=terms)
        _write(db, world, original, updated, "test.business_terms")
    synchronize(ctx[0][3], ctx[1], original.factory_id)
    return ctx


def study_request(current, *, material=False):
    if material:
        quote = current.business_terms.expedite_quotes[0]
        return BusinessStudyRequest(
            kind="material_shortage",
            receipt_id=quote.receipt_id,
            expedite_quote_ids=(quote.quote_id,),
            total_time_limit=6,
        )
    return BusinessStudyRequest(
        kind="urgent_order", existing_order_id=current.orders[0].order_id, total_time_limit=6
    )


def queue_study(ctx, *, material=False, request_id="study-1"):
    current = synchronize(ctx[0][3], ctx[1], ctx[0][2].factory_id)
    body = study_request(current, material=material)
    job = request_business_study(
        ctx[0][3],
        ctx[3],
        current.factory_id,
        request_id=request_id,
        request=body,
        expected_snapshot_hash=current.content_hash,
        expected_run_id=current.run_id,
        case_id=ctx[7]["case_id"],
    )
    return current, body, job


def complete_study(ctx, *, material=False):
    current, body, job = queue_study(ctx, material=material)
    assert run_once(ctx[0][3])
    with Session(ctx[0][3]) as db:
        saved = db.get(SolveJob, job.job_id)
        assert saved.state == "SUCCEEDED", saved.error_code
        result = BusinessStudy.model_validate(saved.business_result)
        assert saved.candidate_id is None
        document_hash = canonical_hash(saved.business_result)
    option = next(
        option
        for option in result.options
        if option.kind == ("receipt_expedite" if material else "normal")
    )
    assert option.status == "FEASIBLE", option.summary
    return current, body, job, result, option, document_hash


def choice(completed, *, costs=False):
    return {
        "job_id": completed[2].job_id,
        "option_id": completed[4].option_id,
        "study_hash": completed[5],
        "confirm_extra_cost": costs,
        "allow_overtime": False,
    }


def count_source_actions(ctx, kind):
    with Session(ctx[0][4]) as db:
        return db.scalar(
            select(func.count())
            .select_from(SourceAction)
            .where(
                SourceAction.factory_id == ctx[0][2].factory_id,
                SourceAction.kind == kind,
            )
        )


def formal_release(ctx):
    current = synchronize(ctx[0][3], ctx[1], ctx[0][2].factory_id)
    job = request_solve(
        ctx[0][3],
        ctx[3],
        current.factory_id,
        request_id="formal-new-facts",
        allow_overtime=False,
        time_limit=5,
    )
    assert run_once(ctx[0][3])
    with Session(ctx[0][3]) as db:
        saved = db.get(SolveJob, job.job_id)
        assert saved.state == "SUCCEEDED", saved.error_code
        record = db.get(CandidateRecord, saved.candidate_id)
        assert record.document["checker"]["status"] == "PASS"
        assert record.document["binding"]["snapshot_hash"] == current.content_hash
        payload = {"candidate_id": record.candidate_id, "candidate_hash": record.content_hash}
    queued = request(ctx, "approve", payload, "approve-new-facts")
    assert run(ctx)
    assert deliver_one(ctx[0][3], ctx[1], ctx[2])
    ready(ctx)
    assert run(ctx)
    with Session(ctx[0][3]) as db:
        row = db.get(AssistantAction, queued["action_id"])
        assert row.state == "DONE", row.result
        publication = db.scalar(
            select(Publication).where(Publication.factory_id == current.factory_id)
        )
        assert publication.document["source_state"] == "ACTIVE"
    assert snapshot(ctx[0]).active_plan_hash == payload["candidate_hash"]


def test_queued_study_is_snapshot_bound_idempotent_read_only_and_not_approvable(business_context):
    ctx = business_context
    with Session(ctx[0][3]) as db:
        before_count = db.scalar(
            select(func.count())
            .select_from(CandidateRecord)
            .where(CandidateRecord.factory_id == ctx[0][2].factory_id)
        )
    before, body, job = queue_study(ctx)
    same = request_business_study(
        ctx[0][3],
        ctx[3],
        before.factory_id,
        request_id="study-1",
        request=body,
        expected_snapshot_hash=before.content_hash,
        expected_run_id=before.run_id,
        case_id=ctx[7]["case_id"],
    )
    assert same.job_id == job.job_id and job.snapshot_id == before.snapshot_id
    assert snapshot(ctx[0]).content_hash == before.content_hash
    assert run_once(ctx[0][3])
    assert snapshot(ctx[0]).content_hash == before.content_hash
    with Session(ctx[0][3]) as db:
        saved = db.get(SolveJob, job.job_id)
        assert saved.state == "SUCCEEDED" and saved.candidate_id is None
        assert (
            db.scalar(
                select(func.count())
                .select_from(CandidateRecord)
                .where(CandidateRecord.factory_id == before.factory_id)
            )
            == before_count
        )
        study = BusinessStudy.model_validate(saved.business_result)
        public = study_view(saved, before)
        assert public["advisory_only"] and public["current"]
        assert all(
            "candidate" not in option and "derived_snapshot" not in option
            for option in public["study"]["options"]
        )
        candidate = next(
            option.candidate for option in study.options if option.status == "FEASIBLE"
        )
    with pytest.raises(AccessError, match="plan"):
        request(
            ctx,
            "approve",
            {"candidate_id": candidate.candidate_id, "candidate_hash": candidate.content_hash},
        )
    assert count_source_actions(ctx, "business.accept") == 0


@pytest.mark.parametrize("roles", [{"manager", "planner"}, {"maintainer", "sim_admin"}])
def test_business_source_choices_are_not_registered_for_either_role(business_context, roles):
    ctx = restrict_roles(business_context, roles)
    before = snapshot(ctx[0])
    with pytest.raises(ValidationError):
        request(
            ctx,
            "business_accept",
            {"job_id": "old-study", "option_id": "old-choice", "study_hash": "a" * 64},
        )
    assert snapshot(ctx[0]).content_hash == before.content_hash
    assert count_source_actions(ctx, "business.accept") == 0


@pytest.mark.parametrize("roles", [{"manager", "planner"}, {"maintainer", "sim_admin"}])
def test_legacy_queued_business_choice_fails_without_writing_source_or_starting_case(
    business_context, roles, monkeypatch
):
    completed = complete_study(business_context)
    ctx = restrict_roles(business_context, roles)
    before = snapshot(ctx[0])
    action_id = str(uuid4())
    with Session(ctx[0][3]) as db, db.begin():
        before_cases = db.scalar(select(func.count()).select_from(CaseRecord))
        before_inputs = db.scalar(select(func.count()).select_from(CaseInput))
        db.add(
            AssistantAction(
                action_id=action_id,
                factory_id=before.factory_id,
                user_id=ctx[3].user_id,
                request_id="legacy-business-choice",
                run_id=before.run_id,
                kind="business_accept",
                payload=choice(completed),
                state="QUEUED",
                created_at=datetime.now(UTC),
                next_attempt_at=datetime.now(UTC),
            )
        )
    monkeypatch.setattr(
        "packages.agent.assistant.synchronize",
        lambda *args: pytest.fail("A disabled legacy command must not access the source"),
    )
    assert process_one(ctx[0][3], ctx[1], ctx[6])
    with Session(ctx[0][3]) as db:
        row = db.get(AssistantAction, action_id)
        assert row.state == "FAILED" and row.result["code"] == "BUSINESS_ACCEPT_UNSUPPORTED"
        assert db.scalar(select(func.count()).select_from(CaseRecord)) == before_cases
        assert db.scalar(select(func.count()).select_from(CaseInput)) == before_inputs
    assert snapshot(ctx[0]).content_hash == before.content_hash
    assert count_source_actions(ctx, "business.accept") == 0
    assert not process_one(ctx[0][3], ctx[1], ctx[6])


def test_order_change_preserves_wip_through_source_http_and_incremental_sync(business_context):
    ctx = business_context
    formal_release(ctx)
    assert control(ctx[0], "pause-for-change", "clock.pause").status_code == 200
    assert control(ctx[0], "start-wip", "clock.step", {"minutes": 1}).status_code == 200
    started = synchronize(ctx[0][3], ctx[1], ctx[0][2].factory_id)
    assert started.actuals
    order = started.orders[0]
    assert (
        control(
            ctx[0],
            "cancel-wip",
            "order.revise",
            {
                "order_id": order.order_id,
                "expected_version": order.version,
                "quantity": 0,
                "due_at": order.due_at.isoformat(),
                "priority_weight": order.priority_weight,
                "hard_deadline": order.hard_deadline,
            },
        ).status_code
        == 200
    )
    cancelled = synchronize(ctx[0][3], ctx[1], started.factory_id)
    assert cancelled.actuals == started.actuals and cancelled.reservations == started.reservations
    assert cancelled.orders[0].status == "CANCELLED"
    assert any(batch.purpose == "STOCK" for batch in cancelled.production_batches)
    assert finished_goods(cancelled) == ()
    assert control(ctx[0], "finish-wip", "clock.step", {"minutes": 60}).status_code == 200
    assert control(ctx[0], "finish-wip-2", "clock.step", {"minutes": 30}).status_code == 200
    completed = synchronize(ctx[0][3], ctx[1], started.factory_id)
    assert sum(lot.quantity for lot in finished_goods(completed)) == order.quantity
    assert count_source_actions(ctx, "order.revise") == 1


def test_cancelling_unstarted_demand_can_publish_an_explicit_empty_plan(business_context):
    ctx = business_context
    before = snapshot(ctx[0])
    order = before.orders[0]
    assert (
        control(
            ctx[0],
            "cancel-unstarted",
            "order.revise",
            {
                "order_id": order.order_id,
                "expected_version": order.version,
                "quantity": 0,
                "due_at": order.due_at.isoformat(),
                "priority_weight": order.priority_weight,
                "hard_deadline": order.hard_deadline,
            },
        ).status_code
        == 200
    )
    cancelled = synchronize(ctx[0][3], ctx[1], before.factory_id)
    assert cancelled.orders[0].status == "CANCELLED" and cancelled.actuals == ()
    formal_release(ctx)
    active = snapshot(ctx[0])
    with Session(ctx[0][3]) as db:
        record = db.scalar(
            select(CandidateRecord).where(CandidateRecord.content_hash == active.active_plan_hash)
        )
        assert record.document["empty_demand"] is True
        assert record.document["assignments"] == []
    assert control(ctx[0], "pause-empty-plan", "clock.pause").status_code == 200
    assert control(ctx[0], "empty-plan-step", "clock.step", {"minutes": 5}).status_code == 200
    after = synchronize(ctx[0][3], ctx[1], before.factory_id)
    assert after.actuals == () and after.inventory == before.inventory


def test_partial_study_only_offers_promises_without_writing_source(business_context):
    ctx = business_context
    current = synchronize(ctx[0][3], ctx[1], ctx[0][2].factory_id)
    order = Order.model_validate(
        {
            **current.orders[0].model_dump(),
            "order_id": "BUSINESS-PARTIAL",
            "quantity": 100,
            "due_at": current.snapshot_clock + timedelta(minutes=75),
            "version": 1,
            "split_revision": 1,
            "status": "CONFIRMED",
        }
    )
    assert (
        control(
            ctx[0], "source-partial-order", "order.add", order.model_dump(mode="json")
        ).status_code
        == 200
    )
    current = synchronize(ctx[0][3], ctx[1], current.factory_id)
    body = BusinessStudyRequest(
        kind="urgent_order",
        existing_order_id=order.order_id,
        partial_delivery_allowed=True,
        minimum_partial_quantity=50,
        total_time_limit=10,
    )
    job = request_business_study(
        ctx[0][3],
        ctx[3],
        current.factory_id,
        request_id="study-partial",
        request=body,
        expected_snapshot_hash=current.content_hash,
        expected_run_id=current.run_id,
        case_id=ctx[7]["case_id"],
    )
    assert run_once(ctx[0][3])
    with Session(ctx[0][3]) as db:
        saved = db.get(SolveJob, job.job_id)
        assert saved.state == "SUCCEEDED", saved.error_code
        study = BusinessStudy.model_validate(saved.business_result)
        option = next(o for o in study.options if o.kind == "partial_delivery")
        assert option.status == "FEASIBLE" and option.on_time_quantity == 50
        assert len(option.deliveries) == 2
        assert sum(d.quantity for d in option.deliveries) == 100
        assert option.deliveries[0].ready_at <= order.due_at
        assert option.deliveries[1].ready_at > order.due_at
        assert saved.case_id == ctx[7]["case_id"]
    assert snapshot(ctx[0]).content_hash == current.content_hash
    assert count_source_actions(ctx, "business.accept") == 0


def restrict_roles(ctx, roles):
    actor = ctx[3]
    with Session(ctx[0][3]) as db, db.begin():
        db.execute(
            delete(Membership).where(
                Membership.user_id == actor.user_id,
                Membership.factory_id == ctx[0][2].factory_id,
                Membership.role.not_in(roles),
            )
        )
    actor = actor.model_copy(update={"grants": tuple(g for g in actor.grants if g.role in roles)})
    return (*ctx[:3], actor, *ctx[4:])


def assistant_reader(ctx, monkeypatch):
    app = FastAPI()
    app.state.engine = ctx[0][3]
    app.include_router(assistant_api.router)
    monkeypatch.setattr(assistant_api, "principal", lambda request: ctx[3])

    @app.exception_handler(AccessError)
    async def access_error(request, error):
        return JSONResponse({"code": error.code}, status_code=error.status)

    return TestClient(app)


def test_assistant_case_filter_precedes_global_study_limit(business_context, monkeypatch):
    ctx = business_context
    current, body, original = queue_study(ctx)
    other = create_case(
        ctx[0][3],
        ctx[3],
        current.factory_id,
        "other-study-case",
        "Another conversation",
        start_new=True,
    )
    for index in range(11):
        job = request_business_study(
            ctx[0][3],
            ctx[3],
            current.factory_id,
            request_id=f"other-study-{index}",
            request=body,
            expected_snapshot_hash=current.content_hash,
            expected_run_id=current.run_id,
            case_id=other["case_id"],
        )
        # Completed/cancelled history must not consume the queue's concurrency allowance.
        with Session(ctx[0][3]) as db, db.begin():
            db.get(SolveJob, job.job_id).state = "CANCELLED"
    with assistant_reader(ctx, monkeypatch) as client:
        path = f"/api/factories/{current.factory_id}/assistant"
        default = client.get(path)
        assert default.status_code == 200
        assert len(default.json()["business_studies"]) == 10
        assert original.job_id not in {job["job_id"] for job in default.json()["business_studies"]}
        scoped = client.get(path, params={"case_id": ctx[7]["case_id"]})
        assert scoped.status_code == 200
        assert [job["job_id"] for job in scoped.json()["business_studies"]] == [original.job_id]
        assert scoped.json()["material_balance"] == default.json()["material_balance"]
        assert scoped.json()["learning"] == default.json()["learning"]


def test_assistant_case_filter_rejects_unknown_and_wrong_factory_but_reads_previous_run(
    business_context, monkeypatch
):
    ctx = business_context
    current, _, _ = queue_study(ctx)
    case_id = ctx[7]["case_id"]
    with assistant_reader(ctx, monkeypatch) as client:
        path = f"/api/factories/{current.factory_id}/assistant"
        missing = client.get(path, params={"case_id": "not-a-case"})
        assert missing.status_code == 404 and missing.json()["code"] == "CASE_NOT_FOUND"
        with Session(ctx[0][3]) as db, db.begin():
            row = db.get(CaseRecord, case_id)
            row.factory_id = "another-factory"
        cross_factory = client.get(path, params={"case_id": case_id})
        assert cross_factory.status_code == 404
        with Session(ctx[0][3]) as db, db.begin():
            row = db.get(CaseRecord, case_id)
            row.factory_id = current.factory_id
            row.run_id = "previous-run"
        previous_run = client.get(path, params={"case_id": case_id})
        # History stays readable after a reset; nothing from another run is current.
        assert previous_run.status_code == 200
        assert not any(study["current"] for study in previous_run.json()["business_studies"])
        # Restore fixture-owned case identity before its cleanup.
        with Session(ctx[0][3]) as db, db.begin():
            db.get(CaseRecord, case_id).run_id = current.run_id


def test_assistant_case_filter_keeps_factory_role_authorization(business_context, monkeypatch):
    ctx = business_context
    with assistant_reader(ctx, monkeypatch) as client:
        monkeypatch.setattr(
            assistant_api, "principal", lambda request: ctx[3].model_copy(update={"grants": ()})
        )
        response = client.get(
            f"/api/factories/{ctx[0][2].factory_id}/assistant",
            params={"case_id": ctx[7]["case_id"]},
        )
        assert response.status_code == 403 and response.json()["code"] == "FORBIDDEN"
