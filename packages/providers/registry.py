"""Explicit provider selection; an unverified adapter never receives private requests."""

import re
from typing import Protocol

from packages.providers.deepseek import DeepSeek
from packages.providers.gateway import Gateway, GatewayError
from packages.settings import Settings


class TextModel(Protocol):
    def complete(self, prompt: str) -> str: ...


class GatewayModel:
    def __init__(self, gateway: TextModel):
        self.gateway = gateway

    def complete(self, prompt: str) -> str:
        response = self.gateway.complete(prompt)
        # Normalize only the gateway's single code-block envelope. The original
        # strict decision parser still validates every field and duplicate key.
        match = re.fullmatch(r"```(?:json)?\r?\n(?P<body>.*?)\r?\n```", response, re.DOTALL)
        if match is not None and "```" not in match["body"]:
            return match["body"]
        return response


class ProviderUnavailable(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def configured_model(settings: Settings) -> TextModel:
    if settings.llm_provider == "deepseek":
        try:
            return DeepSeek(
                url=settings.llm_gateway_url,
                key=settings.llm_gateway_api_key.get_secret_value(),
                model=settings.llm_model,
            )
        except GatewayError:
            raise ProviderUnavailable("DEEPSEEK_PROVIDER_NOT_CONFIGURED") from None
    if settings.llm_provider != "gateway":
        raise ProviderUnavailable("CUSTOM_PROVIDER_UNVERIFIED")
    try:
        return GatewayModel(
            Gateway(
                url=settings.llm_gateway_url,
                key=settings.llm_gateway_api_key.get_secret_value(),
                model=settings.llm_model,
            )
        )
    except GatewayError:
        raise ProviderUnavailable("GATEWAY_PROVIDER_NOT_CONFIGURED") from None
