"""Source facts reach a manager suggestion before any case-scoped business study."""

from datetime import timedelta

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from test_business_flow_postgres import (
    business_context as business_context,
)
from test_business_flow_postgres import (
    case_context as case_context,
)
from test_business_flow_postgres import (
    dynamic_source as dynamic_source,
)
from test_business_flow_postgres import (
    publishing as publishing,
)
from test_business_flow_postgres import (
    reviewer as reviewer,
)
from test_case_tools_postgres import operation
from test_dynamic_factory_postgres import control, snapshot

from packages.agent.case_tools import execute_operation
from packages.agent.cases import create_case, ingest_sources, list_risk_suggestions
from packages.agent.cases_store import CaseRecord
from packages.agent.planning_context import recent_business_studies
from packages.auth import AccessError
from packages.domain.business_options import BusinessStudy, BusinessStudyRequest
from packages.domain.models import Order
from packages.persistence import Membership
from packages.planning.business_service import request_business_study, study_view
from packages.planning.service import synchronize
from packages.planning.store import SolveJob
from services.solver_worker.main import run_once


def test_admin_order_fact_becomes_suggestion_then_manager_case_study_without_source_write(
    business_context,
):
    ctx = business_context
    source, reader, _, original_actor, *_ = ctx
    engine, factory = source[3], source[2].factory_id
    # The manager has no source-write role; source facts arrive via the existing controller.
    with Session(engine) as db, db.begin():
        db.execute(
            delete(Membership).where(
                Membership.user_id == original_actor.user_id,
                Membership.factory_id == factory,
                Membership.role.not_in(("planner", "manager")),
            )
        )
        before_cases = db.scalar(
            select(func.count()).select_from(CaseRecord).where(CaseRecord.factory_id == factory)
        )
    manager = original_actor.model_copy(
        update={
            "grants": tuple(g for g in original_actor.grants if g.role in {"planner", "manager"})
        }
    )
    initial = synchronize(engine, reader, factory)
    proposed = Order.model_validate(
        {
            **initial.orders[0].model_dump(),
            "order_id": "ADMIN-URGENT-FACT",
            "quantity": 50,
            "due_at": initial.snapshot_clock + timedelta(minutes=1000),
            "version": 1,
            "split_revision": 1,
            "status": "CONFIRMED",
        }
    )
    assert (
        control(
            source, "admin-confirmed-urgent-order", "order.add", proposed.model_dump(mode="json")
        ).status_code
        == 200
    )
    current = synchronize(engine, reader, factory)
    assert ingest_sources(engine) == 0
    with Session(engine) as db:
        assert (
            db.scalar(
                select(func.count()).select_from(CaseRecord).where(CaseRecord.factory_id == factory)
            )
            == before_cases
        )
    suggestion = next(
        item
        for item in list_risk_suggestions(engine, manager, factory)["suggestions"]
        if proposed.order_id in item["detail"]
    )
    case = create_case(
        engine,
        manager,
        factory,
        "manager-handles-source-order",
        suggestion["prompt"],
        start_new=True,
        suggestion_id=suggestion["suggestion_id"],
    )
    request = BusinessStudyRequest(
        kind="urgent_order", existing_order_id=proposed.order_id, total_time_limit=6
    )
    context = ((*ctx[:3], manager, *ctx[4:6]), case["case_id"])
    op = operation(context, "evaluate_business_options", request.model_dump(mode="json"))
    result = execute_operation(engine, manager, op)
    assert result["status"] == "PENDING", result
    assert run_once(engine)
    with Session(engine) as db:
        job = db.get(SolveJob, result["job_id"])
        assert job is not None and job.state == "SUCCEEDED" and job.candidate_id is None
        assert job.case_id == case["case_id"]
        study = BusinessStudy.model_validate(job.business_result)
        feasible = next(option for option in study.options if option.status == "FEASIBLE")
        assert len(feasible.derived_snapshot.orders) == len(current.orders)
        assert (
            sum(order.order_id == proposed.order_id for order in feasible.derived_snapshot.orders)
            == 1
        )
        assert study_view(job, current)["case_id"] == case["case_id"]
        assert [
            item["job_id"] for item in recent_business_studies(db, current, case["case_id"])
        ] == [job.job_id]
        assert recent_business_studies(db, current, ctx[7]["case_id"]) == []
    assert snapshot(source).content_hash == current.content_hash
    assert list_risk_suggestions(engine, manager, factory)["suggestions"] == []
    # A manager/model-supplied order payload cannot bypass source facts.
    with pytest.raises(AccessError) as rejected:
        request_business_study(
            engine,
            manager,
            factory,
            request_id="manager-cannot-reenter-order",
            request=BusinessStudyRequest(kind="urgent_order", order=proposed),
            case_id=case["case_id"],
        )
    assert rejected.value.code == "SOURCE_ORDER_REQUIRED"
    assert snapshot(source).content_hash == current.content_hash
