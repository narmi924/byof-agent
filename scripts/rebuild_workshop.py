"""Explicitly replace local workshop history with the current versioned initial snapshot."""

from sqlalchemy import MetaData, delete, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from packages.agent.cases_store import CaseRecord
from packages.agent.checkpoints import checkpoint_thread_id
from packages.demo_identities import FACTORY_ID
from packages.domain.models import Snapshot
from packages.persistence import connect
from packages.settings import Settings
from scripts.import_factory import prepare_initial
from scripts.setup_team import workshop_snapshot
from services.factory_sim.storage import World


def rebuild(engine: Engine) -> tuple[Snapshot, int]:
    metadata = MetaData()
    metadata.reflect(bind=engine, schema="byof")
    metadata.reflect(bind=engine, schema="factory_sim")
    initial = prepare_initial(workshop_snapshot())
    removed = 0
    with Session(engine) as db, db.begin():
        case_ids = tuple(
            db.scalars(select(CaseRecord.case_id).where(CaseRecord.factory_id == FACTORY_ID))
        )
        thread_ids = tuple(checkpoint_thread_id(FACTORY_ID, case_id) for case_id in case_ids)
        if thread_ids:
            for table_name in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                table = metadata.tables[f"byof.{table_name}"]
                result = db.connection().execute(
                    delete(table).where(table.c.thread_id.in_(thread_ids))
                )
                removed += result.rowcount
        for table in reversed(metadata.sorted_tables):
            if "factory_id" not in table.c or table.fullname == "byof.memberships":
                continue
            result = db.connection().execute(delete(table).where(table.c.factory_id == FACTORY_ID))
            removed += result.rowcount
        db.add(
            World(
                factory_id=FACTORY_ID,
                run_id=initial.run_id,
                revision=1,
                business_clock=initial.snapshot_clock,
                document=initial.model_dump(mode="json"),
            )
        )
    return initial, removed


def main() -> None:
    settings = Settings()
    url = make_url(settings.migration_database_url.get_secret_value())
    if (
        settings.environment != "local"
        or url.drivername != "postgresql+psycopg"
        or url.database != "byof_local"
        or url.username != "byof_owner"
        or url.host not in {"postgres", "127.0.0.1", "localhost", "::1"}
        or url.query
    ):
        raise ValueError("Workshop rebuild is limited to the local BYOF owner database")
    engine = connect(settings.migration_database_url.get_secret_value())
    try:
        initial, removed = rebuild(engine)
        print(
            f"Rebuilt {FACTORY_ID} ({initial.profile.version}) with {len(initial.orders)} orders; "
            f"cleared {removed} old factory-scoped records."
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
