"""Explicit DeepSeek selection keeps the hosted gateway and unknown providers separate."""

from unittest.mock import patch

import pytest

from packages.providers.deepseek import DeepSeek
from packages.providers.registry import ProviderUnavailable, configured_model
from packages.settings import Settings


def configured(**changes):
    return Settings(
        _env_file=None,
        **{
            "llm_provider": "deepseek",
            "llm_gateway_url": "https://api.deepseek.com",
            "llm_gateway_api_key": "test-only-key",
            "llm_model": "deepseek-chat",
            **changes,
        },
    )


def test_deepseek_is_constructed_without_calling_or_falling_back_to_the_gateway():
    with patch("packages.providers.registry.DeepSeek", autospec=True) as deepseek:
        with patch("packages.providers.registry.Gateway") as gateway:
            adapter = configured_model(configured())
    assert adapter is deepseek.return_value
    deepseek.assert_called_once_with(
        url="https://api.deepseek.com", key="test-only-key", model="deepseek-chat"
    )
    deepseek.return_value.complete.assert_not_called()
    gateway.assert_not_called()


def test_valid_deepseek_adapter_uses_explicit_configuration():
    assert isinstance(configured_model(configured()), DeepSeek)


@pytest.mark.parametrize(
    "changes",
    [
        {"llm_gateway_api_key": ""},
        {"llm_gateway_url": "https://gateway.example"},
        {"llm_gateway_url": "https://unrelated.example"},
        {"llm_model": ""},
    ],
)
def test_deepseek_configuration_error_is_safe_and_does_not_fall_back(changes):
    with patch("packages.providers.registry.Gateway") as gateway:
        with pytest.raises(ProviderUnavailable, match="^DEEPSEEK_PROVIDER_NOT_CONFIGURED$"):
            configured_model(configured(**changes))
    gateway.assert_not_called()


def test_custom_provider_still_cannot_receive_requests():
    with patch("packages.providers.registry.DeepSeek") as deepseek:
        with pytest.raises(ProviderUnavailable, match="CUSTOM_PROVIDER_UNVERIFIED"):
            configured_model(configured(llm_provider="custom"))
    deepseek.assert_not_called()
