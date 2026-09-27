"""An optional business timestamp preserves old actions and never grants authority."""

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from packages.agent.decisions import ActionError, SolveAction, parse_action
from packages.domain.models import canonical_hash
from services.api.planning import SolveInput


def action(value=None, *, include=True):
    parameters = {"allow_overtime": False, "time_limit": 30}
    if include:
        parameters["new_actions_not_before"] = value
    return {
        "action": "solve_scenario",
        "parameters": parameters,
        "reason_summary": "Explicitly reserve fifteen minutes for this manual review.",
    }


def test_api_and_agent_preserve_explicit_absolute_business_timestamp():
    when = "2030-01-01T08:15:00+08:00"
    parsed = parse_action(json.dumps(action(when)))
    assert isinstance(parsed, SolveAction)
    expected = datetime(2030, 1, 1, 0, 15, tzinfo=UTC)
    assert parsed.parameters.new_actions_not_before == expected
    assert (
        SolveInput(request_id="new-time", new_actions_not_before=when).new_actions_not_before
        == expected
    )
    assert (
        parsed.parameters.model_dump(mode="json")["new_actions_not_before"]
        == "2030-01-01T00:15:00Z"
    )


@pytest.mark.parametrize("include", [False, True])
def test_absent_or_null_time_preserves_previous_durable_action_parameter_hash(include):
    previous = action(include=False)
    parsed = parse_action(json.dumps(action(include=include)))
    assert isinstance(parsed, SolveAction)
    assert parsed.parameters.new_actions_not_before is None
    assert parsed.parameters.model_dump(mode="json") == previous["parameters"]
    assert canonical_hash(
        {"action": parsed.action, "parameters": parsed.parameters.model_dump(mode="json")}
    ) == canonical_hash({"action": previous["action"], "parameters": previous["parameters"]})
    assert SolveInput(request_id="old-time").new_actions_not_before is None


@pytest.mark.parametrize("value", [True, 0, 1800, "2030-01-01T08:15:00", "15 minutes", {}, []])
def test_both_boundaries_reject_non_timestamp_or_missing_timezone(value):
    with pytest.raises(ActionError):
        parse_action(json.dumps(action(value)))
    with pytest.raises(ValidationError):
        SolveInput(request_id="bad-time", new_actions_not_before=value)


@pytest.mark.parametrize(
    "extra", [{"confirmed": True}, {"factory_id": "other"}, {"review_minutes": 15}]
)
def test_explicit_time_does_not_introduce_confirmation_identity_or_unregistered_minutes(extra):
    data = action("2030-01-01T00:15:00Z")
    data["parameters"].update(extra)
    with pytest.raises(ActionError):
        parse_action(json.dumps(data))
    with pytest.raises(ValidationError):
        SolveInput.model_validate({"request_id": "bad-fields", **data["parameters"]})
