from alembic import context

from packages.persistence import Base, connect
from packages.settings import Settings

target_metadata = Base.metadata


def run_migrations() -> None:
    settings = Settings()
    url = settings.migration_database_url.get_secret_value()
    if not url:
        raise RuntimeError("MIGRATION_DATABASE_URL is required; application roles cannot migrate")
    engine = connect(url)
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, include_schemas=True
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations()
