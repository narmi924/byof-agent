"""Case studies reference synchronized facts instead of entering another source order."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from packages.auth import AccessError, Principal
from packages.domain.business_options import BusinessStudyRequest
from packages.domain.business_scenarios import business_scenarios
from packages.domain.skf import load_skf_snapshot
from packages.planning.business_service import request_business_study, study_view, validate_request
from packages.planning.store import SolveJob


def test_urgent_request_selects_exactly_one_order_source():
    source, proposal = business_scenarios()[1]
    assert proposal is not None
    with pytest.raises(ValidationError, match="exactly one"):
        BusinessStudyRequest(kind="urgent_order")
    with pytest.raises(ValidationError, match="exactly one"):
        BusinessStudyRequest(
            kind="urgent_order", order=proposal.order, existing_order_id=source.orders[0].order_id
        )
    with pytest.raises(ValidationError, match="Order promises"):
        BusinessStudyRequest(kind="material_shortage", existing_order_id=source.orders[0].order_id)
    assert "existing_order_id" not in proposal.model_dump(mode="json")


def test_source_order_service_checks_source_date_and_delivery_rules():
    source, _ = business_scenarios()[1]
    order = source.orders[0]
    request = BusinessStudyRequest(
        kind="urgent_order", existing_order_id=order.order_id, partial_delivery_allowed=True
    )
    validate_request(source, request)
    with pytest.raises(AccessError) as missing:
        validate_request(source, request.model_copy(update={"existing_order_id": "not-synced"}))
    assert missing.value.code == "ORDER_NOT_FOUND"
    for final in (
        order.due_at - timedelta(minutes=1),
        source.horizon.end_at + timedelta(minutes=1),
    ):
        with pytest.raises(AccessError) as invalid:
            validate_request(source, request.model_copy(update={"final_due_at": final}))
        assert invalid.value.code == "INVALID_INPUT"
    with pytest.raises(AccessError) as invalid:
        validate_request(source, request.model_copy(update={"minimum_partial_quantity": 1}))
    assert invalid.value.code == "INVALID_INPUT"


def test_case_service_rejects_order_entry_before_queuing_any_study():
    source, proposal = business_scenarios()[1]
    assert proposal is not None
    actor = Principal(user_id="manager", username="manager", grants=())
    with pytest.raises(AccessError) as forbidden:
        request_business_study(
            None,  # Rejection occurs before using a database or emitting a queue job.
            actor,
            source.factory_id,
            request_id="case-study",
            request=proposal,
            case_id="case-1",
        )
    assert forbidden.value.code == "SOURCE_ORDER_REQUIRED"


def test_study_view_identifies_the_case_and_keeps_legacy_unscoped_jobs_readable():
    job = SolveJob(job_id="study-job", state="QUEUED", created_at=datetime.now(UTC))
    assert study_view(job)["case_id"] is None
    job.case_id = "case-1"
    assert study_view(job)["case_id"] == "case-1"


def test_default_skf_can_compare_source_orders_and_waiting_without_added_business_terms():
    source = load_skf_snapshot()
    before = source.model_dump_json()
    assert source.business_terms is None
    assert len(source.orders) == 6 and sum(order.quantity for order in source.orders) == 5400
    validate_request(
        source,
        BusinessStudyRequest(kind="urgent_order", existing_order_id=source.orders[0].order_id),
    )
    validate_request(source, BusinessStudyRequest(kind="material_shortage"))
    assert source.model_dump_json() == before


def test_missing_source_terms_never_grant_partial_delivery_or_invent_expedite_quotes():
    source = load_skf_snapshot()
    with pytest.raises(AccessError) as no_permission:
        validate_request(
            source,
            BusinessStudyRequest(
                kind="urgent_order",
                existing_order_id=source.orders[0].order_id,
                partial_delivery_allowed=True,
            ),
        )
    assert no_permission.value.code == "PARTIAL_DELIVERY_NOT_ALLOWED"
    with pytest.raises(AccessError) as no_quote:
        validate_request(
            source,
            BusinessStudyRequest(kind="material_shortage", expedite_quote_ids=("invented-quote",)),
        )
    assert no_quote.value.code == "QUOTE_NOT_FOUND"
