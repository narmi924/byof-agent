"""Controlled HTTP responses verify the official adapter without network or real keys."""

import json
import traceback
from http.client import IncompleteRead
from io import BytesIO
from unittest.mock import MagicMock, Mock
from urllib import error, request

import pytest

from packages.providers import deepseek
from packages.providers.deepseek import DeepSeek
from packages.providers.gateway import GatewayError

KEY = "test-only-not-a-real-key"
PROMPT = 'Output only JSON: {"action":"reply","text":"checked"}.'


def reply(content='{"action":"reply","text":"checked"}', finish_reason="stop"):
    return {
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content},
            }
        ]
    }


@pytest.fixture(autouse=True)
def transport(monkeypatch):
    # Every test replaces the opener, including configuration failures: no live fallback.
    opener = MagicMock()
    response = opener.open.return_value.__enter__.return_value
    response.read.return_value = json.dumps(reply()).encode()
    build = Mock(return_value=opener)
    monkeypatch.setattr(deepseek.request, "build_opener", build)
    return build, opener, response


def model(url="https://api.deepseek.com"):
    return DeepSeek(url=url, key=KEY, model="deepseek-flash")


@pytest.mark.parametrize(
    "url,path",
    [
        ("https://api.deepseek.com", "/chat/completions"),
        ("https://api.deepseek.com/", "/chat/completions"),
        ("https://api.deepseek.com/v1", "/v1/chat/completions"),
        ("https://api.deepseek.com/v1/", "/v1/chat/completions"),
        ("https://api.deepseek.com:443", "/chat/completions"),
    ],
)
def test_official_request_contract_and_bounded_read(transport, url, path):
    build, opener, response = transport
    assert model(url).complete(PROMPT) == reply()["choices"][0]["message"]["content"]
    req = opener.open.call_args.args[0]
    assert req.full_url == "https://api.deepseek.com" + path
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer " + KEY
    assert req.get_header("Content-type") == "application/json"
    assert req.get_header("X-api-key") is None
    payload = json.loads(req.data)
    assert payload == {
        "model": "deepseek-flash",
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": "Return only the JSON object required by the user prompt, "
                "without Markdown fences or surrounding text.",
            },
            {"role": "user", "content": PROMPT},
        ],
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
        "temperature": 0,
        "max_tokens": 2048,
    }
    assert KEY.encode() not in req.data
    assert KEY not in req.full_url
    assert opener.open.call_args.kwargs == {"timeout": 30}
    opener.open.assert_called_once()
    response.read.assert_called_once_with(65537)
    assert issubclass(build.call_args.args[0], request.HTTPRedirectHandler)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://api.deepseek.com",
        "https://elsewhere.invalid",
        "https://api.deepseek.com.evil.invalid",
        "https://evil.invalid/api.deepseek.com",
        "https://api.deepseek.com@evil.invalid",
        "https://user:password@api.deepseek.com",
        "https://api.deepseek.com:8443",
        "https://api.deepseek.com:bad",
        "https://[invalid",
        "https://api.deepseek.com/chat/completions",
        "https://api.deepseek.com/v1/chat/completions",
        "https://api.deepseek.com/beta",
        "https://api.deepseek.com/other",
        "https://api.deepseek.com?key=" + KEY,
        "https://api.deepseek.com#" + KEY,
        " https://api.deepseek.com",
        "https://api.deepseek.com\n",
        "https://api.deepseek.com/\t",
        "https://api.deepseek.com./",
    ],
)
def test_untrusted_or_ambiguous_base_url_is_rejected_without_network(transport, url):
    with pytest.raises(GatewayError) as caught:
        model(url)
    assert KEY not in str(caught.value)
    transport[0].assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("key", ""),
        ("key", " "),
        ("key", KEY + "\r\nInjected: header"),
        ("model", ""),
        ("model", " "),
        ("model", "chosen model"),
        ("model", "x" * 161),
    ],
)
def test_explicit_configuration_is_required_without_environment_fallback(
    monkeypatch, transport, field, value
):
    monkeypatch.setenv("LLM_GATEWAY_API_KEY", "unrelated-environment-key")
    monkeypatch.setenv("LLM_MODEL", "unrelated-model")
    config = {"url": "https://api.deepseek.com", "key": KEY, "model": "chosen-model"}
    config[field] = value
    with pytest.raises(GatewayError) as caught:
        DeepSeek(**config)
    assert KEY not in str(caught.value)
    transport[0].assert_not_called()


def test_configured_model_is_passed_without_model_switching(transport):
    DeepSeek(url="https://api.deepseek.com", key=KEY, model="explicit-model").complete(PROMPT)
    assert json.loads(transport[1].open.call_args.args[0].data)["model"] == "explicit-model"


@pytest.mark.parametrize("prompt", ["", " \n", None, {}])
def test_invalid_prompt_cannot_start_a_request(transport, prompt):
    with pytest.raises(GatewayError):
        model().complete(prompt)
    transport[0].assert_not_called()


@pytest.mark.parametrize(
    "reason",
    [None, "length", "content_filter", "tool_calls", "insufficient_system_resource", "aborted"],
)
def test_only_naturally_completed_text_is_accepted(transport, reason):
    transport[2].read.return_value = json.dumps(reply(finish_reason=reason)).encode()
    with pytest.raises(GatewayError, match="response incomplete"):
        model().complete(PROMPT)
    transport[1].open.assert_called_once()


@pytest.mark.parametrize("content", [None, "", " \n", [], {}, 5])
def test_empty_or_nontext_content_is_rejected_without_using_reasoning(transport, content):
    document = reply(content)
    document["choices"][0]["message"]["reasoning_content"] = "not the final answer"
    transport[2].read.return_value = json.dumps(document).encode()
    with pytest.raises(GatewayError, match="returned no text"):
        model().complete(PROMPT)


@pytest.mark.parametrize(
    "document",
    [
        [],
        None,
        {},
        {"choices": {}},
        {"choices": []},
        {"choices": [None]},
        {"choices": [{"finish_reason": "stop"}]},
        {"choices": [{"finish_reason": "stop", "message": []}]},
        {"choices": [{"finish_reason": "stop", "message": {"role": "user", "content": "{}"}}]},
    ],
)
def test_malformed_response_envelopes_fail_closed(transport, document):
    transport[2].read.return_value = json.dumps(document).encode()
    with pytest.raises(GatewayError, match="Invalid DeepSeek response"):
        model().complete(PROMPT)


@pytest.mark.parametrize(
    "raw", [b"not JSON", b"\xff", b"[" * 5000], ids=["not-json", "invalid-utf8", "too-deep"]
)
def test_invalid_encoded_json_is_a_sanitized_provider_error(transport, raw):
    transport[2].read.return_value = raw
    with pytest.raises(GatewayError, match="Invalid DeepSeek response"):
        model().complete(PROMPT)


def test_oversized_response_is_rejected_before_json_parsing(transport):
    transport[2].read.return_value = b" " * 65537
    with pytest.raises(GatewayError, match="exceeds size limit"):
        model().complete(PROMPT)
    transport[2].read.assert_called_once_with(65537)


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
def test_http_failures_hide_body_url_reason_and_never_retry(transport, status):
    remote = error.HTTPError(
        "https://api.deepseek.com?secret=" + KEY,
        status,
        KEY + PROMPT,
        {},
        BytesIO((KEY + PROMPT).encode()),
    )
    transport[1].open.side_effect = remote
    with pytest.raises(GatewayError) as caught:
        model().complete(PROMPT)
    assert str(caught.value) == f"DeepSeek HTTP {status}; response body suppressed"
    rendered = "".join(traceback.format_exception(caught.value))
    assert KEY not in rendered and PROMPT not in rendered
    assert remote.fp.tell() == 0  # Never read an error body.
    transport[1].open.assert_called_once()


@pytest.mark.parametrize(
    "failure",
    [error.URLError(KEY), TimeoutError(KEY), OSError(KEY), IncompleteRead(KEY.encode(), 500)],
)
def test_transport_failure_is_sanitized_and_never_retried(transport, failure):
    transport[1].open.side_effect = failure
    with pytest.raises(GatewayError, match="connection failed or timed out") as caught:
        model().complete(PROMPT)
    assert KEY not in "".join(traceback.format_exception(caught.value))
    transport[1].open.assert_called_once()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_handler_rejects_even_same_host_redirect_without_exposing_target(
    transport, status
):
    model().complete(PROMPT)
    handler = transport[0].call_args.args[0]()
    with pytest.raises(GatewayError, match="redirect refused") as caught:
        handler.redirect_request(
            request.Request("https://api.deepseek.com/chat/completions"),
            None,
            status,
            KEY,
            {},
            "https://api.deepseek.com/redirect?key=" + KEY,
        )
    assert KEY not in str(caught.value)


def test_adapter_preserves_content_for_existing_strict_decision_parser(transport):
    content = ' {"action":"reply","text":"ok","text":"duplicate"} '
    document = reply(content)
    document["choices"][0]["message"]["reasoning_content"] = "private reasoning"
    transport[2].read.return_value = json.dumps(document).encode()
    assert model().complete(PROMPT) == content
