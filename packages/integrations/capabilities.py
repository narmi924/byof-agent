"""Declared source guarantees bound automatic execution; absence never means support."""

from datetime import UTC, datetime, timedelta

from packages.auth import AccessError
from packages.domain.models import ConnectorCapabilities

REQUIRED_EXECUTION_CAPABILITIES = (
    "read_snapshot",
    "accept_plan",
    "query_action",
    "idempotency",
    "conditional_acceptance",
)


def execution_support(
    document: dict | None, observed_at: datetime | None, *, now: datetime | None = None
) -> dict:
    current = now or datetime.now(UTC)
    missing: list[str] = []
    if document is None or observed_at is None:
        reason = (
            "The execution interface capabilities are not checked yet; sync the connection first."
        )
        status = "UNKNOWN"
    else:
        try:
            capabilities = ConnectorCapabilities.model_validate(document)
        except ValueError:
            reason, status = (
                "The execution interface capability declaration does not meet the contract; ask the administrator to check the connection.",
                "INVALID",
            )
        else:
            age = current - observed_at
            missing = [
                name for name in REQUIRED_EXECUTION_CAPABILITIES if not getattr(capabilities, name)
            ]
            if capabilities.snapshot_consistency == "UNVERIFIED":
                missing.append("snapshot_consistency")
            if age < timedelta(0) or age > timedelta(seconds=30):
                reason, status = (
                    "The execution interface capability check has expired; sync again.",
                    "STALE",
                )
            elif missing:
                reason = "The execution interface lacks conditional acceptance, idempotency or original-action checks, so plans can only be exported and imported after manual review."
                status = "UNSUPPORTED"
            else:
                reason = "The interface declares conditional acceptance support; actual acceptance and effect follow the execution receipt."
                status = "DECLARED"
    return {
        "mode": "CONDITIONAL" if status == "DECLARED" else "EXPORT_ONLY",
        "status": status,
        "reason": reason,
        "missing_capabilities": missing,
        "observed_at": observed_at,
        "capabilities": document,
    }


def require_execution_support(
    document: dict | None, observed_at: datetime | None, *, now: datetime | None = None
) -> None:
    support = execution_support(document, observed_at, now=now)
    if support["mode"] != "CONDITIONAL":
        raise AccessError("EXECUTION_CAPABILITY_REQUIRED", support["reason"], 409)
