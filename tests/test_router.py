import json
import unittest
from unittest.mock import Mock, patch

from packages.agent.decisions import ActionError, parse_action
from packages.agent.router import RouteError, RouterAgent
from packages.providers.gateway import MAX_OUTPUT_TOKENS, Gateway, GatewayError


class RouterTests(unittest.TestCase):
    def test_dispatch_and_preserve_tool_result(self):
        for decision, method, arg in [
            ({"route": "query", "entity": "inventory"}, "query", "inventory"),
            ({"route": "planning", "task": "replan"}, "plan", "replan"),
        ]:
            with self.subTest(decision=decision):
                model, factory = Mock(), Mock()
                model.complete.return_value = json.dumps(decision)
                getattr(factory, method).return_value = {"status": "sentinel"}
                result = RouterAgent(model, factory).run("test request")
                getattr(factory, method).assert_called_once_with(arg)
                self.assertEqual(result["result"], {"status": "sentinel"})
                self.assertFalse(result["published"])
                model.complete.assert_called_once()

    def test_invalid_decisions_never_execute(self):
        for text in [
            "[]",
            "{}",
            "not json",
            '{"route":"publish"}',
            '{"route":"query","entity":"orders","approved":true}',
            '{"route":"query","entity":[]}',
            '{"route":"planning","task":"delete"}',
            '{"route":"clarify","question":" "}',
            '{"route":"query","route":"planning","task":"initial"}',
            '```json\n{"route":"query","entity":"orders"}\n```',
        ]:
            with self.subTest(text=text):
                model, factory = Mock(), Mock()
                model.complete.return_value = text
                with self.assertRaises(RouteError):
                    RouterAgent(model, factory).run("test")
                self.assertEqual(factory.mock_calls, [])

    def test_clarification_has_no_tool_side_effect(self):
        model, factory = Mock(), Mock()
        model.complete.return_value = '{"route":"clarify","question":"Which order?"}'
        result = RouterAgent(model, factory).run("help")
        self.assertEqual(result["result"]["status"], "needs_input")
        self.assertEqual(factory.mock_calls, [])

    def test_empty_input_never_calls_model(self):
        model = Mock()
        for text in ("", " ", "x" * 8001, None):
            with self.assertRaises(RouteError):
                RouterAgent(model).run(text)
        model.complete.assert_not_called()

    def test_planning_does_not_fabricate_schedule(self):
        model = Mock()
        model.complete.return_value = '{"route":"planning","task":"initial"}'
        result = RouterAgent(model).run("schedule")
        self.assertEqual(result["result"]["status"], "not_implemented")
        self.assertNotIn("schedule", result["result"])


class GatewayTests(unittest.TestCase):
    @patch.dict(
        "os.environ",
        {
            "LLM_GATEWAY_URL": "https://example.invalid",
            "LLM_GATEWAY_API_KEY": "test-only",
            "LLM_MODEL": "fixture",
        },
        clear=True,
    )
    @patch("packages.providers.gateway.request.build_opener")
    def test_protocol_and_incomplete_response(self, opener):
        response = opener.return_value.open.return_value.__enter__.return_value
        response.read.return_value = json.dumps(
            {"done": True, "message": {"content": "{}"}}
        ).encode()
        self.assertEqual(Gateway().complete("test"), "{}")
        req = opener.return_value.open.call_args.args[0]
        self.assertEqual(req.full_url, "https://example.invalid/api/chat")
        self.assertFalse(json.loads(req.data)["stream"])
        self.assertEqual(json.loads(req.data)["options"], {"temperature": 0, "num_predict": 1024})
        self.assertEqual(MAX_OUTPUT_TOKENS, 1024)
        opener.return_value.open.assert_called_once_with(req, timeout=30)
        response.read.assert_called_once_with(65537)
        for body in (
            {"done": False, "message": {"content": "{}"}},
            {"done": True, "message": {}},
            {"done": True, "done_reason": "length", "message": {"content": "{}"}},
        ):
            response.read.return_value = json.dumps(body).encode()
            with self.assertRaises(GatewayError):
                Gateway().complete("test")

    @patch("packages.providers.gateway.request.build_opener")
    def test_full_business_reply_is_preserved_and_done_true_does_not_bypass_json_validation(
        self, opener
    ):
        message = "Checked order demand, due dates and material; awaiting confirmation." * 15
        content = json.dumps(
            {
                "action": "reply",
                "parameters": {
                    "message": message,
                    "choices": ["Confirm the chosen option", "Decline the order for now"],
                },
                "reason_summary": "Checked plan; full delivery needs a choice." * 10,
            },
            ensure_ascii=False,
        )
        gateway = Gateway(url="https://example.invalid", key="test-only", model="fixture")
        response = opener.return_value.open.return_value.__enter__.return_value
        response.read.return_value = json.dumps(
            {"done": True, "message": {"content": content}}
        ).encode()
        complete = gateway.complete("Output the full business option reply as JSON.")
        self.assertEqual(complete, content)
        self.assertEqual(parse_action(complete).parameters.message, message)
        opener.return_value.open.assert_called_once()

        # The gateway can report done=true for text cut in the final JSON field.
        # Keep strict parsing: a larger allowance never repairs or accepts a partial action.
        response.read.return_value = json.dumps(
            {"done": True, "message": {"content": content[:-8]}}
        ).encode()
        opener.return_value.open.reset_mock()
        with self.assertRaises(ActionError):
            parse_action(gateway.complete("Output the full business option reply as JSON."))
        opener.return_value.open.assert_called_once()

    @patch.dict("os.environ", {}, clear=True)
    def test_missing_credentials(self):
        with self.assertRaises(GatewayError):
            Gateway()


if __name__ == "__main__":
    unittest.main()
