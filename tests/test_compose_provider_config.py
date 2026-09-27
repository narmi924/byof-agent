"""Private team configuration uses the explicitly selected, validated provider."""

import pytest
from dotenv import dotenv_values

from packages.settings import Settings
from scripts import compose_config


def test_deepseek_team_configuration_copies_provider_without_network(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        llm_provider="deepseek",
        llm_gateway_url="https://api.deepseek.com",
        llm_gateway_api_key="test-only-team-key",
        llm_model="deepseek-flash",
    )
    monkeypatch.setattr(compose_config, "Settings", lambda: settings)
    target = tmp_path / "compose.env"
    compose_config.create_config(target, gateway_from_local=True)
    values = dotenv_values(target)
    assert values["LLM_PROVIDER"] == "deepseek"
    assert values["LLM_MODEL"] == "deepseek-flash"
    assert values["LLM_GATEWAY_API_KEY"] == "test-only-team-key"
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        compose_config.create_config(target, gateway_from_local=True)
    assert target.read_bytes() == before


def test_deepseek_team_configuration_rejects_mismatched_endpoint(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        llm_provider="deepseek",
        llm_gateway_url="https://gateway.example",
        llm_gateway_api_key="test-only-team-key",
        llm_model="deepseek-flash",
    )
    monkeypatch.setattr(compose_config, "Settings", lambda: settings)
    target = tmp_path / "compose.env"
    with pytest.raises(ValueError, match="configuration is invalid"):
        compose_config.create_config(target, gateway_from_local=True)
    assert not target.exists()
