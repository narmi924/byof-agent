"""Explicit project configuration; values containing credentials never enter status output."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8-sig",
        extra="ignore",
        hide_input_in_errors=True,
    )

    environment: Literal["local", "test", "production"] = "local"
    legacy_password_login_enabled: bool = False
    database_url: SecretStr = SecretStr("")
    migration_database_url: SecretStr = SecretStr("")
    factory_database_url: SecretStr = SecretStr("")
    llm_gateway_url: str = ""
    llm_gateway_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    llm_provider: Literal["gateway", "deepseek", "custom"] = "deepseek"
    gateway_url: str = ""
    gateway_api_key: SecretStr = SecretStr("")
    deepseek_api_key: SecretStr = SecretStr("")
    smtp_host: str = ""
    smtp_mode: Literal["disabled", "capture", "starttls", "tls"] = "disabled"
    smtp_timeout_seconds: int = Field(default=10, ge=1, le=30)
    smtp_port: int = 587
    smtp_username: SecretStr = SecretStr("")
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = ""
    test_email_recipient: str = ""
    real_email_allowlist: SecretStr = SecretStr("")
    allow_real_email: bool = False
    public_origin: str = "http://127.0.0.1:5173"
    factory_api_url: str = "http://127.0.0.1:8001"
    factory_api_token: SecretStr = SecretStr("")
    factory_execution_token: SecretStr = SecretStr("")
    factory_control_token: SecretStr = SecretStr("")

    @field_validator("smtp_timeout_seconds", mode="before")
    @classmethod
    def smtp_timeout_is_not_a_flag(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("SMTP timeout must be a bounded number of seconds")
        return value

    @field_validator("database_url", "migration_database_url", "factory_database_url")
    @classmethod
    def postgres_only(cls, value: SecretStr) -> SecretStr:
        if value.get_secret_value() and not value.get_secret_value().startswith(
            "postgresql+psycopg://"
        ):
            raise ValueError("Use a PostgreSQL psycopg URL")
        return value

    def presence(self) -> dict[str, bool]:
        return {
            "DATABASE_URL": bool(self.database_url.get_secret_value()),
            "MIGRATION_DATABASE_URL": bool(self.migration_database_url.get_secret_value()),
            "FACTORY_DATABASE_URL": bool(self.factory_database_url.get_secret_value()),
            "LLM_GATEWAY_URL": bool(self.llm_gateway_url),
            "LLM_GATEWAY_API_KEY": bool(self.llm_gateway_api_key.get_secret_value()),
            "LLM_MODEL": bool(self.llm_model),
            "SMTP_HOST": bool(self.smtp_host),
            "SMTP_USERNAME": bool(self.smtp_username.get_secret_value()),
            "SMTP_PASSWORD": bool(self.smtp_password.get_secret_value()),
            "SMTP_FROM": bool(self.smtp_from),
            "TEST_EMAIL_RECIPIENT": bool(self.test_email_recipient),
            "ALLOW_REAL_EMAIL": self.allow_real_email,
        }
