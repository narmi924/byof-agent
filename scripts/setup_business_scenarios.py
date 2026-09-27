"""Create offline development fixtures; product personas remain in the shared SKF workshop."""

import argparse
import json

from sqlalchemy.orm import Session

from packages.domain.business_scenarios import business_scenarios
from packages.integrations.factory_http import FactoryHTTP
from packages.persistence import connect
from packages.planning.service import synchronize
from packages.settings import Settings
from scripts.import_factory import import_initial
from scripts.setup_workshop_personas import provision
from services.factory_sim.storage import World


def ensure_factories(engine) -> dict[str, str]:
    result = {}
    for snapshot, _ in business_scenarios():
        with Session(engine) as db:
            exists = db.get(World, snapshot.factory_id) is not None
        if exists:
            result[snapshot.factory_id] = "EXISTING_PRESERVED"
        else:
            import_initial(engine, snapshot)
            result[snapshot.factory_id] = "CREATED"
    return result


def grant_personas(engine) -> None:
    """Compatibility entry point: provision SKF roles without granting fixture access."""
    provision(engine)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list", action="store_true", help="Show synthetic inputs without accessing a database"
    )
    parser.add_argument(
        "--personas",
        action="store_true",
        help="Ensure the two SKF workshop identities only; never grant access to these fixtures",
    )
    args = parser.parse_args()
    if args.list:
        print(
            json.dumps(
                [
                    {
                        "factory_id": snapshot.factory_id,
                        "evidence_mode": "synthetic",
                        "request": request.model_dump(mode="json")
                        if request
                        else {"quantity": 50, "event": "order.revise"},
                    }
                    for snapshot, request in business_scenarios()
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    config = Settings()
    if config.environment != "local":
        raise ValueError("Synthetic demonstrations are local only")
    source = connect(config.factory_database_url.get_secret_value())
    owner = connect(config.migration_database_url.get_secret_value())
    application = connect(config.database_url.get_secret_value())
    reader = FactoryHTTP(config.factory_api_url, config.factory_api_token.get_secret_value())
    try:
        if args.personas:
            grant_personas(owner)
        result = ensure_factories(source)
        for factory_id in result:
            synchronize(application, reader, factory_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        reader.close()
        application.dispose()
        owner.dispose()
        source.dispose()


if __name__ == "__main__":
    main()
