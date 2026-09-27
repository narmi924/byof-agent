"""The demo factory uses the complete SKF workload without changing the pinned seed."""

import json
from unittest.mock import Mock

import pytest
from sqlalchemy.engine import make_url

from packages.domain.models import batch_operations
from packages.domain.skf import load_skf_snapshot, verify_skf_baseline
from packages.settings import Settings
from scripts import setup_team

OWNER_URL = "postgresql+psycopg://byof_owner:test-only-owner@postgres:5432/byof_local"
SOURCE_URL = "postgresql+psycopg://factory_sim_app:test-only-source@postgres:5432/byof_local"


def test_workshop_has_full_workload_and_original_constraints():
    baseline_hash = verify_skf_baseline()
    original = load_skf_snapshot()
    workshop = setup_team.workshop_snapshot()
    original_batches, original_operations = batch_operations(original)
    batches, operations = batch_operations(workshop)

    assert (len(workshop.orders), sum(o.quantity for o in workshop.orders)) == (6, 5400)
    assert len(batches) == len(original_batches) == 108
    assert len(operations) == len(original_operations) == 864
    assert workshop.orders == original.orders
    expected_buffer = {
        "IR-6204": 50,
        "OR-6204": 50,
        "BALLSET-6204": 50,
        "CAGE-6204": 50,
        "SEAL-RS1-6204": 100,
    }
    assert workshop.profile.version == "V1.6.workshop-full-2"
    original_stock = {item.material_id: item for item in original.inventory}
    for item in workshop.inventory:
        before = original_stock[item.material_id]
        assert item.on_hand == before.on_hand + expected_buffer.get(item.material_id, 0)
        assert item.model_dump(exclude={"on_hand"}) == before.model_dump(exclude={"on_hand"})
    assert {key: original_stock[key].on_hand for key in expected_buffer} == {
        "IR-6204": 800,
        "OR-6204": 800,
        "BALLSET-6204": 800,
        "CAGE-6204": 800,
        "SEAL-RS1-6204": 1600,
    }
    assert setup_team.workshop_snapshot() == workshop
    assert load_skf_snapshot() == original
    assert workshop.receipts == original.receipts
    assert workshop.resources == original.resources
    assert workshop.workers == original.workers
    assert workshop.profile.routes == original.profile.routes
    assert workshop.profile.bom == original.profile.bom
    assert workshop.profile.policy.model_dump(
        exclude={"policy_version", "progress_revalidation_enabled"}
    ) == original.profile.policy.model_dump(
        exclude={"policy_version", "progress_revalidation_enabled"}
    )
    assert workshop.profile.policy.freeze_window_min == 60
    # Clock-only progress is independently checked; material changes still need a new plan.
    assert workshop.profile.policy.progress_revalidation_enabled
    assert not original.profile.policy.progress_revalidation_enabled
    assert workshop.factory_id == workshop.profile.factory_id == "skf-workshop"
    assert original.factory_id == "skf-reference"
    assert workshop.content_hash != original.content_hash
    assert verify_skf_baseline() == baseline_hash


class UnconnectedEngine:
    def __init__(self, url):
        self.url = make_url(url)
        self.disposed = False

    def dispose(self):
        self.disposed = True


def cli(monkeypatch, *, owner=OWNER_URL, source=SOURCE_URL, environment="local"):
    settings = Settings(
        _env_file=None,
        environment=environment,
        migration_database_url=owner,
        factory_database_url=source,
    )
    monkeypatch.setattr(setup_team, "Settings", lambda: settings)
    engines = []

    def connect(url):
        result = UnconnectedEngine(url)
        engines.append(result)
        return result

    factories = Mock(return_value={factory: "CREATED" for factory in setup_team.TEAM_FACTORIES})
    personas = Mock(return_value={"manager": "CREATED", "maintainer": "CREATED"})
    monkeypatch.setattr(setup_team, "connect", connect)
    monkeypatch.setattr(setup_team, "ensure_factories", factories)
    monkeypatch.setattr(setup_team, "provision", personas)
    return engines, factories, personas


@pytest.mark.parametrize(
    ("owner", "source"),
    [
        (OWNER_URL.replace("byof_local", "other_database"), SOURCE_URL),
        (OWNER_URL, SOURCE_URL.replace("byof_local", "other_database")),
        (OWNER_URL.replace("byof_owner:", "postgres:"), SOURCE_URL),
        (OWNER_URL, SOURCE_URL.replace("factory_sim_app:", "byof_owner:")),
        (OWNER_URL, SOURCE_URL.replace("@postgres", "@remote.example")),
        (OWNER_URL + "?hostaddr=192.0.2.1", SOURCE_URL),
    ],
)
def test_setup_rejects_untrusted_database_targets(monkeypatch, owner, source):
    engines, factories, personas = cli(monkeypatch, owner=owner, source=source)
    with pytest.raises(ValueError):
        setup_team.main()
    factories.assert_not_called()
    personas.assert_not_called()
    assert all(engine.disposed for engine in engines)


def test_setup_creates_sources_and_selectable_roles_without_credentials(monkeypatch, capsys):
    engines, factories, personas = cli(monkeypatch)
    assert setup_team.main() == 0
    assert len(engines) == 2 and all(engine.disposed for engine in engines)
    factories.assert_called_once_with(engines[1])
    personas.assert_called_once_with(engines[0])
    assert json.loads(capsys.readouterr().out) == {
        "factories": {factory: "CREATED" for factory in setup_team.TEAM_FACTORIES},
        "accounts": {"manager": "CREATED", "maintainer": "CREATED"},
        "model_requests": 0,
    }


def test_demo_setup_requires_local_environment(monkeypatch):
    engines, factories, personas = cli(monkeypatch, environment="production")
    with pytest.raises(ValueError, match="local"):
        setup_team.main()
    assert not engines
    factories.assert_not_called()
    personas.assert_not_called()
