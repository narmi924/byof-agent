"""Server-owned menu: credentials never enter public selection data."""

import json

import pytest
from pydantic import ValidationError

from packages.providers.catalog import ModelCatalog
from packages.providers.registry import ProviderUnavailable
from packages.settings import Settings
from services.api.models import SelectionInput


def test_model_menu_is_explicit_redacted_and_missing_keys_disable_choices():
    catalog = ModelCatalog(
        Settings(_env_file=None, llm_gateway_api_key="", gateway_api_key="", deepseek_api_key="")
    )
    assert not any(row["available"] for row in catalog.public())
    configured = ModelCatalog(
        Settings(
            _env_file=None,
            llm_provider="deepseek",
            gateway_url="https://gateway.example",
            gateway_api_key="gateway-secret",
            deepseek_api_key="team-secret",
        )
    )
    menu = configured.public()
    assert all(row["available"] for row in menu)
    assert "secret" not in json.dumps(menu)
    assert configured.default_id == "deepseek"
    with pytest.raises(ProviderUnavailable):
        configured.model("user-controlled-endpoint")


def test_browser_cannot_supply_credentials_or_another_user():
    for field in ("api_key", "url", "user_id", "provider"):
        with pytest.raises(ValidationError):
            SelectionInput(
                model_id="claude",
                expected_version=0,
                request_id="request",
                **{field: "arbitrary"},
            )
