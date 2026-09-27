"""Historical production facts suppress model wakeups only with their exact snapshot."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_case_runtime_postgres import case_context as case_context
from test_dynamic_factory_postgres import control
from test_dynamic_factory_postgres import dynamic_source as dynamic_source
from test_publication_postgres import approve_and_commit
from test_publication_postgres import publishing as publishing

from packages.agent.cases import _source_input_key, create_case, ingest_sources
from packages.agent.cases_store import CaseCursor, CaseInput, CaseRecord
from packages.planning.publication import deliver_one
from packages.planning.service import synchronize


def test_backlogged_normal_progress_uses_each_revision_and_does_not_wake_model(case_context):
    context, case = case_context
    source, reader, writer, *_ = context
    engine, factory = source[3], source[2].factory_id
    approve_and_commit(context)
    assert deliver_one(engine, reader, writer)
    synchronize(engine, reader, factory)
    assert ingest_sources(engine) == 0
    assert control(source, "normal-progress", "clock.step", {"minutes": 30}).status_code == 200
    current = synchronize(engine, reader, factory)
    assert current.actuals and any(a.state == "COMPLETED" for a in current.actuals)
    assert ingest_sources(engine) == 0
    assert ingest_sources(engine) == 0
    with Session(engine) as db:
        assert db.get(CaseCursor, factory).source_revision == int(current.source.source_revision)
        inputs = list(db.scalars(select(CaseInput).where(CaseInput.case_id == case["case_id"])))
        assert [item.kind for item in inputs] == ["USER"]


def test_startup_reconciles_current_fault_and_repeated_sync_does_not_duplicate_input(case_context):
    context, case = case_context
    source, reader, writer, *_ = context
    engine, factory = source[3], source[2].factory_id
    approve_and_commit(context)
    assert deliver_one(engine, reader, writer)
    resource = source[2].resources[0].resource_id
    assert (
        control(source, "unknown-recovery", "resource.down", {"resource_id": resource}).status_code
        == 200
    )
    current = synchronize(engine, reader, factory)
    assert ingest_sources(engine) == 1
    assert ingest_sources(engine) == 0
    with Session(engine) as db:
        rows = list(
            db.scalars(
                select(CaseInput).where(
                    CaseInput.case_id == case["case_id"], CaseInput.kind == "SOURCE_RECONCILIATION"
                )
            )
        )
        assert len(rows) == 1
        impact = rows[0].payload["impact"]
        assert impact["snapshot_hash"] == current.content_hash
        assert "UNAVAILABLE_RESOURCE" in impact["classification"]["reasons"]
        assert impact["requires_full_check"] and not impact["preserves_approval"]
        assert db.get(CaseRecord, case["case_id"]).closure is None


def test_new_fault_during_wait_joins_existing_case_and_keeps_original_input(case_context):
    context, case = case_context
    source, reader, writer, *_ = context
    engine, factory = source[3], source[2].factory_id
    approve_and_commit(context)
    assert deliver_one(engine, reader, writer)
    synchronize(engine, reader, factory)
    assert ingest_sources(engine) == 0
    with Session(engine) as db, db.begin():
        row = db.get(CaseRecord, case["case_id"], with_for_update=True)
        row.state = "WAITING"
    assert control(source, "advance", "clock.step", {"minutes": 3}).status_code == 200
    assert (
        control(
            source, "failure", "resource.down", {"resource_id": source[2].resources[0].resource_id}
        ).status_code
        == 200
    )
    synchronize(engine, reader, factory)
    assert ingest_sources(engine) == 1
    with Session(engine) as db:
        cases = list(db.scalars(select(CaseRecord).where(CaseRecord.factory_id == factory)))
        assert len(cases) == 1 and cases[0].case_id == case["case_id"]
        inputs = list(db.scalars(select(CaseInput).where(CaseInput.case_id == case["case_id"])))
        assert sorted(row.kind for row in inputs) == ["SOURCE", "USER"]
        source_input = next(row for row in inputs if row.kind == "SOURCE")
        assert source_input.available_at <= source_input.created_at
        assert source_input.payload["impact"]["classification"]["urgent"]


@pytest.mark.parametrize("startup", [False, True])
def test_material_event_wakes_each_waiting_case_once_without_replacing_owner_or_history(
    case_context, startup
):
    context, first = case_context
    source, reader, writer, actor, *_ = context
    engine, factory = source[3], source[2].factory_id
    second = create_case(
        engine,
        actor,
        factory,
        "parallel-case",
        "Follow the delivery case separately",
        start_new=True,
    )
    identities = {first["case_id"], second["case_id"]}
    assert len(identities) == 2
    approve_and_commit(context)
    assert deliver_one(engine, reader, writer)
    synchronize(engine, reader, factory)
    if not startup:
        assert ingest_sources(engine) == 0
        assert control(source, "ordinary-progress", "clock.step", {"minutes": 3}).status_code == 200
    with Session(engine) as db, db.begin():
        original_inputs = {
            row.case_id: (row.input_id, row.payload_hash)
            for row in db.scalars(select(CaseInput).where(CaseInput.case_id.in_(identities)))
        }
        original = {}
        for row in db.scalars(
            select(CaseRecord)
            .where(CaseRecord.case_id.in_(identities))
            .order_by(CaseRecord.case_id)
            .with_for_update()
        ):
            row.state = "WAITING"
            original[row.case_id] = (row.owner_id, row.version)
    assert (
        control(
            source,
            "shared-resource-failure",
            "resource.down",
            {"resource_id": source[2].resources[0].resource_id},
        ).status_code
        == 200
    )
    current = synchronize(engine, reader, factory)
    assert ingest_sources(engine) == 2
    assert ingest_sources(engine) == 0
    assert synchronize(engine, reader, factory).content_hash == current.content_hash
    assert ingest_sources(engine) == 0
    kind = "SOURCE_RECONCILIATION" if startup else "SOURCE"
    with Session(engine) as db:
        rows = list(db.scalars(select(CaseRecord).where(CaseRecord.factory_id == factory)))
        assert {row.case_id for row in rows} == identities
        wakeups = list(
            db.scalars(
                select(CaseInput).where(CaseInput.factory_id == factory, CaseInput.kind == kind)
            )
        )
        assert len(wakeups) == 2 and {row.case_id for row in wakeups} == identities
        assert (
            len({row.input_id for row in wakeups}) == len({row.input_key for row in wakeups}) == 2
        )
        assert wakeups[0].payload == wakeups[1].payload
        for row in rows:
            assert (row.owner_id, row.version) == (
                original[row.case_id][0],
                original[row.case_id][1] + 1,
            )
            assert row.state == "WAITING" and row.closure is None
            assert row.context["impact"]["classification"]["material"]
        for row in wakeups:
            assert row.turn_id is None and row.available_at <= row.created_at
            assert row.payload["impact"]["classification"]["urgent"]
            assert row.payload["impact"]["requires_full_check"]
            if not startup:
                assert row.payload["impact"]["current_snapshot_hash"] == current.content_hash
                assert row.payload["event_ids"]
        preserved = {
            row.case_id: (row.input_id, row.payload_hash)
            for row in db.scalars(
                select(CaseInput).where(CaseInput.factory_id == factory, CaseInput.kind == "USER")
            )
        }
        assert preserved == original_inputs
        assert db.get(CaseCursor, factory).source_revision == int(current.source.source_revision)


def test_normal_progress_does_not_wake_either_active_case(case_context):
    context, first = case_context
    source, reader, writer, actor, *_ = context
    engine, factory = source[3], source[2].factory_id
    second = create_case(
        engine, actor, factory, "parallel-normal", "Follow normal production", start_new=True
    )
    identities = {first["case_id"], second["case_id"]}
    approve_and_commit(context)
    assert deliver_one(engine, reader, writer)
    synchronize(engine, reader, factory)
    assert ingest_sources(engine) == 0
    assert control(source, "normal-for-both", "clock.step", {"minutes": 30}).status_code == 200
    current = synchronize(engine, reader, factory)
    assert any(actual.state == "COMPLETED" for actual in current.actuals)
    assert ingest_sources(engine) == 0 and ingest_sources(engine) == 0
    with Session(engine) as db:
        rows = list(db.scalars(select(CaseInput).where(CaseInput.factory_id == factory)))
        assert len(rows) == 2 and {row.case_id for row in rows} == identities
        assert {row.kind for row in rows} == {"USER"}


def test_fanout_excludes_terminal_cases_and_cases_from_another_run(case_context):
    context, first = case_context
    source, reader, writer, actor, *_ = context
    engine, factory = source[3], source[2].factory_id
    closed = create_case(engine, actor, factory, "closed-case", "Closed case", start_new=True)
    retired = create_case(
        engine, actor, factory, "retired-run", "Case of the original run", start_new=True
    )
    with Session(engine) as db, db.begin():
        db.get(CaseRecord, closed["case_id"]).state = "CANCELLED"
        db.get(CaseRecord, retired["case_id"]).run_id = "retired-run"
    approve_and_commit(context)
    assert deliver_one(engine, reader, writer)
    assert (
        control(
            source,
            "current-only",
            "resource.down",
            {"resource_id": source[2].resources[0].resource_id},
        ).status_code
        == 200
    )
    synchronize(engine, reader, factory)
    assert ingest_sources(engine) == 1 and ingest_sources(engine) == 0
    with Session(engine) as db:
        wakeups = list(
            db.scalars(
                select(CaseInput).where(CaseInput.factory_id == factory, CaseInput.kind != "USER")
            )
        )
        assert len(wakeups) == 1 and wakeups[0].case_id == first["case_id"]
        assert db.get(CaseRecord, closed["case_id"]).state == "CANCELLED"
        assert db.get(CaseRecord, retired["case_id"]).run_id == "retired-run"


def test_source_input_identity_is_bounded_and_distinguishes_run_revision_and_case():
    first = _source_input_key("source", "r" * 160, 2**63 - 1, "c" * 160)
    assert first == _source_input_key("source", "r" * 160, 2**63 - 1, "c" * 160)
    assert len(first) <= 250
    assert (
        len(
            {
                first,
                _source_input_key("source", "other", 2**63 - 1, "c" * 160),
                _source_input_key("source", "r" * 160, 1, "c" * 160),
                _source_input_key("source", "r" * 160, 2**63 - 1, "other"),
                _source_input_key("reconcile", "r" * 160, 2**63 - 1, "c" * 160),
            }
        )
        == 5
    )
