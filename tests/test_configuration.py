import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from packages.settings import Settings
from scripts.bootstrap_postgres import main as bootstrap


def test_invalid_configuration_traceback_hides_secret_input():
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, database_url="sqlite://user:private-test-value@host/db")
    assert "private-test-value" not in str(error.value)
    assert "PostgreSQL" in str(error.value)


@pytest.mark.parametrize("variant", ["host", "port", "database", "driver", "role"])
def test_bootstrap_rejects_misdirected_target_before_connecting(variant):
    values = {
        "BOOTSTRAP_DATABASE_URL": "postgresql+psycopg://postgres:test-only@127.0.0.1:5432/postgres",
        "MIGRATION_DATABASE_URL": "postgresql+psycopg://byof_owner:test-only@127.0.0.1:5432/byof_test",
        "DATABASE_URL": "postgresql+psycopg://byof_app:test-only@127.0.0.1:5432/byof_test",
        "FACTORY_DATABASE_URL": "postgresql+psycopg://factory_sim_app:test-only@127.0.0.1:5432/byof_test",
    }
    if variant == "host":
        values["DATABASE_URL"] = values["DATABASE_URL"].replace("127.0.0.1", "other.invalid")
    elif variant == "port":
        values["DATABASE_URL"] = values["DATABASE_URL"].replace(":5432/", ":5433/")
    elif variant == "database":
        values = {
            key: value.replace("byof_test", "unrelated_business") for key, value in values.items()
        }
    elif variant == "driver":
        values["DATABASE_URL"] = values["DATABASE_URL"].replace("postgresql+psycopg", "mysql")
    else:
        values["DATABASE_URL"] = values["DATABASE_URL"].replace("byof_app", "postgres")
    with (
        patch.dict(os.environ, values),
        patch("scripts.bootstrap_postgres.psycopg.connect") as connection,
    ):
        with pytest.raises(RuntimeError):
            bootstrap()
        connection.assert_not_called()
