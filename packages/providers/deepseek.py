"""Explicit, bounded DeepSeek Chat Completions adapter for JSON decision text."""

import json
import re
from http.client import HTTPException
from urllib import error, parse, request

from packages.providers.gateway import GatewayError

RESPONSE_LIMIT = 65536


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GatewayError("DeepSeek redirect refused; check configured URL")


class DeepSeek:
    def __init__(self, *, url: str, key: str, model: str):
        if (
            not isinstance(key, str)
            or not key
            or not key.isascii()
            or any(ord(character) <= 32 or ord(character) >= 127 for character in key)
            or not isinstance(model, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}", model) is None
        ):
            raise GatewayError("Set an explicit DeepSeek API key and model")
        if (
            not isinstance(url, str)
            or not url
            or any(ord(character) <= 32 or ord(character) >= 127 for character in url)
        ):
            raise GatewayError("DeepSeek URL must be the official HTTPS base URL")
        try:
            parsed = parse.urlsplit(url)
            valid = (
                parsed.scheme == "https"
                and parsed.netloc.lower() in {"api.deepseek.com", "api.deepseek.com:443"}
                and parsed.path in {"", "/", "/v1", "/v1/"}
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise GatewayError("DeepSeek URL must be the official HTTPS base URL")
        self.url = "https://api.deepseek.com" + parsed.path.rstrip("/")
        self._key = key
        self.model = model

    def complete(self, prompt: str) -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise GatewayError("DeepSeek requires a nonempty text prompt")
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [
                {
                    "role": "system",
                    "content": "Return only the JSON object required by the user prompt, "
                    "without Markdown fences or surrounding text.",
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "temperature": 0,
            "max_tokens": 2048,
        }
        try:
            req = request.Request(
                self.url + "/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + self._key,
                },
                method="POST",
            )
            # One attempt only. Never redirect credentials or include remote errors in logs.
            with request.build_opener(_NoRedirect).open(req, timeout=30) as response:
                raw = response.read(RESPONSE_LIMIT + 1)
            if len(raw) > RESPONSE_LIMIT:
                raise GatewayError("DeepSeek response exceeds size limit")
            body = json.loads(raw)
            choices = body.get("choices") if isinstance(body, dict) else None
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise GatewayError("Invalid DeepSeek response")
            choice = choices[0]
            if choice.get("finish_reason") != "stop":
                raise GatewayError("DeepSeek response incomplete")
            message = choice.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                raise GatewayError("Invalid DeepSeek response")
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                raise GatewayError("DeepSeek returned no text")
            return content
        except error.HTTPError as exc:
            raise GatewayError(
                f"DeepSeek HTTP {exc.code}; response body suppressed", status_code=exc.code
            ) from None
        except (error.URLError, TimeoutError, OSError, HTTPException):
            raise GatewayError("DeepSeek connection failed or timed out") from None
        except (ValueError, TypeError, AttributeError, RecursionError):
            raise GatewayError("Invalid DeepSeek response") from None
