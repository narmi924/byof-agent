"""Create private local Compose configuration once without starting containers."""

import argparse
import os
import secrets
from pathlib import Path

from packages.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / ".runtime" / "compose.env"


def quoted(value: str) -> str:
    if any(character in value for character in ("\r", "\n", "\x00")):
        raise ValueError("Compose configuration values must be single-line strings")
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def create_config(path: Path = OUTPUT, *, gateway_from_local: bool = False) -> None:
    if path.exists():
        raise FileExistsError(
            "Compose configuration already exists; refusing to replace credentials"
        )
    passwords = {role: secrets.token_urlsafe(32) for role in ("postgres", "owner", "app", "sim")}
    values = {
        "POSTGRES_PASSWORD": passwords["postgres"],
        "BOOTSTRAP_DATABASE_URL": (
            f"postgresql+psycopg://postgres:{passwords['postgres']}@postgres:5432/postgres"
        ),
        "MIGRATION_DATABASE_URL": (
            f"postgresql+psycopg://byof_owner:{passwords['owner']}@postgres:5432/byof_local"
        ),
        "DATABASE_URL": (
            f"postgresql+psycopg://byof_app:{passwords['app']}@postgres:5432/byof_local"
        ),
        "FACTORY_DATABASE_URL": (
            f"postgresql+psycopg://factory_sim_app:{passwords['sim']}@postgres:5432/byof_local"
        ),
        "FACTORY_API_TOKEN": secrets.token_urlsafe(32),
        "FACTORY_EXECUTION_TOKEN": secrets.token_urlsafe(32),
        "FACTORY_CONTROL_TOKEN": secrets.token_urlsafe(32),
        "LLM_GATEWAY_URL": "https://api.deepseek.com",
        "LLM_GATEWAY_API_KEY": "",
        "LLM_MODEL": "deepseek-flash",
        "LLM_PROVIDER": "deepseek",
    }
    if gateway_from_local:
        settings = Settings()
        if not settings.llm_gateway_api_key.get_secret_value():
            raise ValueError("LLM_GATEWAY_API_KEY is missing from the configured local environment")
        if settings.llm_provider not in {"gateway", "deepseek"}:
            raise ValueError("Only the verified gateway and DeepSeek providers are supported")
        from packages.providers.registry import ProviderUnavailable, configured_model

        try:
            configured_model(settings)  # Validate without making a request.
        except ProviderUnavailable:
            raise ValueError(
                "Selected provider configuration is invalid; check URL and model"
            ) from None
        values.update(
            LLM_GATEWAY_URL=settings.llm_gateway_url,
            LLM_GATEWAY_API_KEY=settings.llm_gateway_api_key.get_secret_value(),
            LLM_MODEL=settings.llm_model,
            LLM_PROVIDER=settings.llm_provider,
            GATEWAY_URL=settings.gateway_url,
            GATEWAY_API_KEY=settings.gateway_api_key.get_secret_value(),
            DEEPSEEK_API_KEY=settings.deepseek_api_key.get_secret_value(),
        )
    content = "# Local Compose credentials; keep this ignored file private.\n" + "".join(
        f"{key}={quoted(value)}\n" for key, value in values.items()
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateway-from-local",
        action="store_true",
        help="Explicitly copy the configured verified provider into the new private file",
    )
    args = parser.parse_args()
    try:
        create_config(gateway_from_local=args.gateway_from_local)
    except (FileExistsError, ValueError) as exc:
        print(str(exc))
        return 1
    print(
        "Created .runtime/compose.env; secrets were not printed. Existing databases were not changed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
