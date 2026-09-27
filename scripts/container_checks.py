"""Run locked development checks against the separate, ephemeral Compose test database."""

import os
import subprocess
import sys

from sqlalchemy.engine import make_url


def main() -> int:
    for key in (
        "DATABASE_URL",
        "MIGRATION_DATABASE_URL",
        "FACTORY_DATABASE_URL",
        "TEST_DATABASE_URL",
        "TEST_MIGRATION_DATABASE_URL",
        "TEST_FACTORY_DATABASE_URL",
    ):
        url = make_url(os.environ[key])
        if url.database != "byof_test" or url.host != "test-postgres" or url.query:
            raise ValueError("Container checks require the isolated Compose test database")
    commands = [
        [sys.executable, "-m", "scripts.bootstrap_postgres"],
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        [sys.executable, "-m", "pytest", *sys.argv[1:]]
        if sys.argv[1:]
        else [sys.executable, "scripts/check.py"],
    ]
    for command in commands:
        result = subprocess.run(command, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
