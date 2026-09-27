"""Hosted gateway /api/chat text protocol; not an OpenAI or Bedrock client."""

import json
import os
from urllib import error, parse, request

MAX_OUTPUT_TOKENS = 1024


class GatewayError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GatewayError("Gateway redirect refused; check configured URL")


class Gateway:
    def __init__(self, *, url=None, key=None, model=None):
        self.url = (url if url is not None else os.environ.get("LLM_GATEWAY_URL", "")).rstrip("/")
        self.key = key if key is not None else os.environ.get("LLM_GATEWAY_API_KEY", "")
        self.model = model if model is not None else os.environ.get("LLM_MODEL", "")
        if not all((self.url, self.key, self.model)):
            raise GatewayError("Set LLM_GATEWAY_URL, LLM_GATEWAY_API_KEY and LLM_MODEL")
        url = parse.urlsplit(self.url)
        if (
            url.scheme != "https"
            or not url.netloc
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise GatewayError("Gateway URL must be HTTPS without credentials, query or fragment")

    def complete(self, prompt):
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
            "options": {"temperature": 0, "num_predict": MAX_OUTPUT_TOKENS},
        }
        req = request.Request(
            self.url + "/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "X-API-Key": self.key},
        )
        try:
            with request.build_opener(NoRedirect).open(req, timeout=30) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise GatewayError("Gateway response exceeds size limit")
            body = json.loads(raw)
            if (
                not isinstance(body, dict)
                or body.get("done") is not True
                or body.get("done_reason") == "length"
            ):
                raise GatewayError("Gateway response incomplete")
            content = body.get("message", {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise GatewayError("Gateway returned no text")
            return content
        except error.HTTPError as exc:
            raise GatewayError(
                f"Gateway HTTP {exc.code}; response body suppressed", status_code=exc.code
            ) from None
        except (error.URLError, TimeoutError, OSError):
            raise GatewayError("Gateway connection failed or timed out") from None
        except (ValueError, TypeError, AttributeError):
            raise GatewayError("Invalid gateway response") from None
