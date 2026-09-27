"""PR11 keeps factory mutations in the administrator surface, never model chat."""

import json

import pytest
from pydantic import ValidationError

from packages.agent.assistant import AssistantRequest
from packages.agent.decisions import ActionError, parse_action


@pytest.mark.parametrize(
    "kind", ["order", "order_change", "receipt_delay", "worker_absence", "business_accept"]
)
def test_legacy_chat_factory_commands_are_not_registered(kind):
    with pytest.raises(ValidationError):
        AssistantRequest(request_id="legacy-event", run_id="run", kind=kind, payload={})


@pytest.mark.parametrize(
    "action",
    [
        "propose_order",
        "propose_order_change",
        "propose_receipt_delay",
        "propose_worker_absence",
    ],
)
def test_model_cannot_propose_factory_write_cards(action):
    with pytest.raises(ActionError):
        parse_action(
            json.dumps(
                {"action": action, "parameters": {}, "reason_summary": "Unauthorized source write"}
            )
        )
