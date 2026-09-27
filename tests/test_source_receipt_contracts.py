"""A response body cannot claim activation without a consistent source receipt."""

from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from packages.domain.execution import ActionReceipt, PlanSubmission
from packages.domain.models import Candidate
from packages.integrations.factory_http import ConnectorError, FactoryExecution, FactoryHTTP


def submission():
    candidate = Candidate.model_validate_json(
        (Path(__file__).parent / "fixtures/contracts/p1-candidate.json").read_bytes()
    )
    return PlanSubmission(
        operation_id="receipt-contract-action",
        factory_id=candidate.factory_id,
        run_id="receipt-contract-run",
        expected_source_revision="1",
        expected_snapshot_hash=candidate.binding.snapshot_hash,
        expected_active_plan_version=None,
        candidate=candidate,
        approvals=(),
    )


def receipt_data(*, rejected=False):
    request = submission()
    return {
        "operation_id": request.operation_id,
        "factory_id": request.factory_id,
        "run_id": request.run_id,
        "receipt_id": "source-receipt",
        "candidate_hash": request.candidate.content_hash,
        "source_state": "REJECTED" if rejected else "ACTIVE",
        "plan_version": None if rejected else "source-plan-2",
        "effective_at": None if rejected else "2030-01-01T08:00:00Z",
        "recorded_at": "2026-09-16T12:00:00Z",
        "error_code": "SOURCE_CONDITIONS_CHANGED" if rejected else None,
    }


INVALID_RECEIPTS = [
    pytest.param(False, {"plan_version": None}, id="active-without-execution-version"),
    pytest.param(False, {"effective_at": None}, id="active-without-effective-time"),
    pytest.param(False, {"error_code": "APPROVAL_REQUIRED"}, id="active-with-rejection-reason"),
    pytest.param(True, {"error_code": None}, id="rejected-without-reason"),
    pytest.param(True, {"plan_version": "never-activated"}, id="rejected-with-execution-version"),
    pytest.param(True, {"effective_at": "2030-01-01T08:00:00Z"}, id="rejected-with-effective-time"),
]


@pytest.mark.parametrize("rejected,changes", INVALID_RECEIPTS)
def test_inconsistent_receipt_is_rejected_by_contract_and_both_http_paths(rejected, changes):
    body = dict(receipt_data(rejected=rejected), **changes)
    with pytest.raises(ValidationError):
        ActionReceipt.model_validate(body)
    requests = []

    def respond(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json=body)

    request = submission()
    reader = FactoryHTTP(
        "http://127.0.0.1", "controlled-test-reader", transport=httpx.MockTransport(respond)
    )
    writer = FactoryExecution(
        "http://127.0.0.1", "controlled-test-writer", transport=httpx.MockTransport(respond)
    )
    try:
        with pytest.raises(ConnectorError, match="receipt violates contract"):
            reader.action(request.factory_id, request.run_id, request.operation_id)
        with pytest.raises(ConnectorError, match="receipt violates contract"):
            writer.submit(request)
    finally:
        reader.close()
        writer.close()
    assert requests == [
        ("GET", "/factory/v1/actions/receipt-contract-action"),
        ("POST", "/factory/v1/plans"),
    ]


@pytest.mark.parametrize("rejected", [False, True])
def test_valid_receipts_preserve_execution_evidence_and_business_real_clock_separation(rejected):
    body = receipt_data(rejected=rejected)
    expected = ActionReceipt.model_validate(body)
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    reader = FactoryHTTP("http://127.0.0.1", "controlled-test-reader", transport=transport)
    writer = FactoryExecution("http://127.0.0.1", "controlled-test-writer", transport=transport)
    request = submission()
    try:
        assert reader.action(request.factory_id, request.run_id, request.operation_id) == expected
        assert writer.submit(request) == expected
    finally:
        reader.close()
        writer.close()
    if rejected:
        assert (
            expected.source_state == "REJECTED"
            and expected.error_code == "SOURCE_CONDITIONS_CHANGED"
        )
        assert expected.plan_version is None and expected.effective_at is None
    else:
        assert expected.plan_version == "source-plan-2"
        assert expected.effective_at > expected.recorded_at
        assert expected.error_code is None


def test_well_formed_receipt_for_another_action_cannot_be_used_as_success():
    body = dict(receipt_data(), operation_id="unrelated-operation")
    request = submission()
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    reader = FactoryHTTP("http://127.0.0.1", "controlled-test-reader", transport=transport)
    writer = FactoryExecution("http://127.0.0.1", "controlled-test-writer", transport=transport)
    try:
        with pytest.raises(ConnectorError, match="outside the requested scope"):
            reader.action(request.factory_id, request.run_id, request.operation_id)
        with pytest.raises(ConnectorError, match="does not match submitted content"):
            writer.submit(request)
    finally:
        reader.close()
        writer.close()
