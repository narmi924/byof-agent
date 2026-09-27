"""Provision the two isolated SKF source worlds and the two demo identities."""

from __future__ import annotations

import json

from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from packages.domain.models import Snapshot
from packages.domain.skf import load_skf_snapshot
from packages.persistence import connect
from packages.settings import Settings
from scripts.import_factory import import_initial
from scripts.setup_workshop_personas import provision
from services.factory_sim.storage import World

TEAM_FACTORIES = ("skf-reference", "skf-workshop")
WORKSHOP_PROFILE_VERSION = "V1.6.workshop-full-2"
# Synthetic opening buffer for one legal BRG-6204-2RS1 rush batch (50 EA).
# These are additions to the immutable reference, not SKF factory inventory claims.
WORKSHOP_SAFETY_STOCK = {
    "IR-6204": 50,
    "OR-6204": 50,
    "BALLSET-6204": 50,
    "CAGE-6204": 50,
    "SEAL-RS1-6204": 100,
}


def workshop_snapshot() -> Snapshot:
    original = load_skf_snapshot()
    data = original.model_dump(mode="json", exclude={"content_hash"})
    data.update(
        schema_version="byof.snapshot/2", factory_id="skf-workshop", snapshot_id="workshop-initial"
    )
    data["profile"].update(factory_id="skf-workshop", version=WORKSHOP_PROFILE_VERSION)
    data["profile"]["policy"].update(
        policy_version=original.profile.policy.policy_version + ".workshop-full-1",
        progress_revalidation_enabled=True,
    )
    for stock in data["inventory"]:
        stock["on_hand"] += WORKSHOP_SAFETY_STOCK.get(stock["material_id"], 0)
    return Snapshot.model_validate(data)


def ensure_factories(engine: Engine) -> dict[str, str]:
    outcomes = {}
    for factory_id, make_snapshot in (
        ("skf-reference", load_skf_snapshot),
        ("skf-workshop", workshop_snapshot),
    ):
        with Session(engine) as db:
            existing = db.get(World, factory_id)
            if existing is not None:
                outcomes[factory_id] = "EXISTING_PRESERVED"
                continue
        import_initial(engine, make_snapshot())
        outcomes[factory_id] = "CREATED"
    return outcomes


def main() -> int:
    settings = Settings()
    if settings.environment != "local":
        raise ValueError("Team setup is limited to explicitly local workspaces")
    owner_url = make_url(settings.migration_database_url.get_secret_value())
    source_url = make_url(settings.factory_database_url.get_secret_value())
    if (
        any(url.drivername != "postgresql+psycopg" or url.query for url in (owner_url, source_url))
        or owner_url.host not in {"postgres", "127.0.0.1", "localhost", "::1"}
        or (owner_url.host, owner_url.port or 5432) != (source_url.host, source_url.port or 5432)
        or owner_url.database != "byof_local"
        or source_url.database != "byof_local"
        or owner_url.username != "byof_owner"
        or source_url.username != "factory_sim_app"
    ):
        raise ValueError("Team setup requires the same local BYOF database with separate roles")
    owner = connect(settings.migration_database_url.get_secret_value())
    source = connect(settings.factory_database_url.get_secret_value())
    try:
        factories = ensure_factories(source)
        accounts = provision(owner)
        print(json.dumps({"factories": factories, "accounts": accounts, "model_requests": 0}))
    finally:
        owner.dispose()
        source.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
