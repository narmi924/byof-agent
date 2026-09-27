"""Import a validated initial factory once; never overwrite an existing run."""

import argparse
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from packages.domain.models import Snapshot
from packages.domain.skf import load_skf_snapshot
from packages.persistence import connect
from packages.settings import Settings
from services.factory_sim.storage import World


def prepare_initial(snapshot: Snapshot) -> Snapshot:
    data = snapshot.model_dump(mode="json", exclude={"content_hash"})
    run_id = str(uuid4())
    data.update(run_id=run_id, snapshot_id=f"{run_id}-1")
    data["source"].update(
        source_system="factory-simulator-http-v1",
        source_revision="1",
        cursor="1",
        observed_at=datetime.now(UTC).isoformat(),
        consistency="ATOMIC_SNAPSHOT",
        freshness="CURRENT",
        ownership="simulator_fact",
    )
    return Snapshot.model_validate(data)


def import_initial(engine, snapshot: Snapshot) -> Snapshot:
    initial = prepare_initial(snapshot)
    with Session(engine) as db, db.begin():
        if db.get(World, snapshot.factory_id):
            raise ValueError("Factory already exists; initial import will not overwrite a run")
        db.add(
            World(
                factory_id=initial.factory_id,
                run_id=initial.run_id,
                revision=1,
                business_clock=initial.snapshot_clock,
                document=initial.model_dump(mode="json"),
            )
        )
    return initial


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development", action="store_true")
    args = parser.parse_args()
    config = Settings()
    engine = connect(config.factory_database_url.get_secret_value())
    try:
        state = import_initial(engine, load_skf_snapshot(development=args.development))
        print(f"Imported {state.factory_id}: {len(state.orders)} orders; {state.content_hash}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
