"""Create missing project database roles via an explicitly supplied administrative connection."""

import os

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url


def main() -> None:
    admin = make_url(os.environ["BOOTSTRAP_DATABASE_URL"])
    owner = make_url(os.environ["MIGRATION_DATABASE_URL"])
    application = make_url(os.environ["DATABASE_URL"])
    simulator = make_url(os.environ["FACTORY_DATABASE_URL"])
    if [owner.username, application.username, simulator.username] != [
        "byof_owner",
        "byof_app",
        "factory_sim_app",
    ]:
        raise RuntimeError("Bootstrap expects the documented separate project roles")
    if not owner.database or len({url.database for url in (owner, application, simulator)}) != 1:
        raise RuntimeError("Project URLs must refer to one named database")
    if owner.database not in {"byof", "byof_local", "byof_test"}:
        raise RuntimeError("Bootstrap may only create a documented BYOF project database")
    targets = (admin, owner, application, simulator)
    if len({(url.host, url.port or 5432) for url in targets}) != 1:
        raise RuntimeError("Administrative and runtime URLs must target the same PostgreSQL server")
    if any(url.drivername != "postgresql+psycopg" for url in targets):
        raise RuntimeError("Bootstrap requires explicit PostgreSQL psycopg URLs")
    with psycopg.connect(
        host=admin.host,
        port=admin.port or 5432,
        user=admin.username,
        password=admin.password,
        dbname=admin.database,
        autocommit=True,
    ) as connection:
        for url in (owner, application, simulator):
            if not url.username or not url.password:
                raise RuntimeError("Each project role requires explicit credentials")
            if not connection.execute(
                "SELECT 1 FROM pg_roles WHERE rolname=%s", (url.username,)
            ).fetchone():
                connection.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(url.username), sql.Literal(url.password)
                    )
                )
        if not connection.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (owner.database,)
        ).fetchone():
            connection.execute(
                sql.SQL("CREATE DATABASE {} OWNER byof_owner").format(
                    sql.Identifier(owner.database)
                )
            )
            connection.execute(
                sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(owner.database)
                )
            )
            connection.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO byof_app, factory_sim_app").format(
                    sql.Identifier(owner.database)
                )
            )
    print("Project database roles verified; existing passwords and data were not changed")


if __name__ == "__main__":
    main()
