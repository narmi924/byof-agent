"""A missing or stale remote guarantee cannot authorize an external production action."""

from datetime import UTC, datetime, timedelta

import pytest

from packages.auth import AccessError
from packages.domain.models import ConnectorCapabilities
from packages.integrations.capabilities import execution_support, require_execution_support


def declared(**changes):
    return ConnectorCapabilities.model_validate(
        {
            "read_snapshot": True,
            "read_changes": True,
            "query_detail": True,
            "accept_plan": True,
            "query_action": True,
            "idempotency": True,
            "conditional_acceptance": True,
            "snapshot_consistency": "ATOMIC_SNAPSHOT",
            **changes,
        }
    ).model_dump(mode="json")


@pytest.mark.parametrize(
    "changes",
    [
        {"accept_plan": False, "conditional_acceptance": False},
        {"query_action": False},
        {"idempotency": False},
        {"conditional_acceptance": False},
        {"snapshot_consistency": "UNVERIFIED"},
    ],
)
def test_missing_guarantees_allow_only_export(changes):
    now = datetime.now(UTC)
    support = execution_support(declared(**changes), now)
    assert support["mode"] == "EXPORT_ONLY" and support["status"] == "UNSUPPORTED"
    assert support["missing_capabilities"]
    with pytest.raises(AccessError) as failure:
        require_execution_support(declared(**changes), now)
    assert failure.value.code == "EXECUTION_CAPABILITY_REQUIRED"


@pytest.mark.parametrize("offset", [-31, 1])
def test_stale_or_future_observation_never_authorizes_execution(offset):
    now = datetime.now(UTC)
    result = execution_support(declared(), now + timedelta(seconds=offset), now=now)
    assert result["mode"] == "EXPORT_ONLY" and result["status"] == "STALE"


def test_unknown_or_malformed_contract_is_unavailable_and_valid_declaration_is_not_receipt():
    now = datetime.now(UTC)
    assert execution_support(None, None)["status"] == "UNKNOWN"
    assert execution_support({"confirmed": True}, now)["status"] == "INVALID"
    supported = execution_support(declared(), now)
    assert supported["status"] == "DECLARED" and supported["mode"] == "CONDITIONAL"
    assert "source_state" not in supported and "effective_at" not in supported
