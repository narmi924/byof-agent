"""Server-owned model menu. Browsers select IDs, never endpoints or credentials."""

from dataclasses import dataclass
from typing import Literal

from packages.providers.registry import ProviderUnavailable, TextModel, configured_model
from packages.settings import Settings


@dataclass(frozen=True)
class ModelProfile:
    model_id: str
    label: str
    provider: Literal["gateway", "deepseek"]
    model: str
    url: str
    credential_field: str


# Add models here; a new protocol needs one adapter in registry.py, not UI branches.
PROFILES = (
    # Claude through a hosted gateway that speaks the /api/chat text protocol; its address is
    # deployment configuration (GATEWAY_URL), not part of the code.
    ModelProfile(
        "claude",
        "Claude Sonnet 4.5 · Gateway",
        "gateway",
        "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "",
        "gateway_api_key",
    ),
    ModelProfile(
        "deepseek",
        "DeepSeek Flash",
        "deepseek",
        "deepseek-flash",
        "https://api.deepseek.com",
        "deepseek_api_key",
    ),
)


class ModelCatalog:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.default_id = next(
            (p.model_id for p in PROFILES if p.provider == settings.llm_provider), "deepseek"
        )

    def profile(self, model_id: str) -> ModelProfile:
        profile = next((p for p in PROFILES if p.model_id == model_id), None)
        if profile is None:
            raise ProviderUnavailable("MODEL_NOT_REGISTERED")
        return profile

    def model(self, model_id: str) -> TextModel:
        profile = self.profile(model_id)
        key = getattr(self.settings, profile.credential_field)
        # Compatible with existing single-provider local environments.
        if not key.get_secret_value() and profile.provider == self.settings.llm_provider:
            key = self.settings.llm_gateway_api_key
        return configured_model(
            self.settings.model_copy(
                update={
                    "llm_provider": profile.provider,
                    "llm_model": profile.model,
                    "llm_gateway_url": profile.url or self.settings.gateway_url,
                    "llm_gateway_api_key": key,
                }
            )
        )

    def public(self) -> list[dict]:
        rows = []
        for profile in PROFILES:
            try:
                self.model(profile.model_id)  # Configuration validation only; no paid call.
                available = True
            except ProviderUnavailable:
                available = False
            rows.append(
                {"model_id": profile.model_id, "label": profile.label, "available": available}
            )
        return rows
