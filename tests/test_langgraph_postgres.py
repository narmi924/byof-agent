"""Real PostgreSQL checkpoints survive new connections without granting runtime DDL."""

import os
from typing import TypedDict
from unittest.mock import patch
from uuid import uuid4

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from langsmith import get_tracing_context
from sqlalchemy import text

from packages.agent.checkpoints import checkpoint_session, checkpoint_thread_id
from packages.persistence import connect


class ReferenceState(TypedDict):
    case_ref: str
    snapshot_ref: str
    reply_ref: str | None
    phase: str


def graph(checkpointer):
    def waiting(state: ReferenceState):
        received = interrupt({"case_ref": state["case_ref"], "task_ref": "task-1"})
        return {"reply_ref": received["reply_ref"], "phase": "recheck_current_facts"}

    builder = StateGraph(ReferenceState)
    builder.add_node("waiting", waiting)
    builder.add_edge(START, "waiting")
    builder.add_edge("waiting", END)
    return builder.compile(checkpointer=checkpointer)


@pytest.fixture
def checkpoint_database():
    url = os.getenv("TEST_DATABASE_URL")
    owner_url = os.getenv("TEST_MIGRATION_DATABASE_URL")
    if not url or not owner_url:
        pytest.skip("Explicit PostgreSQL application and migration test URLs required")
    application, owner = connect(url), connect(owner_url)
    assert application.url.database == owner.url.database == "byof_test"
    factory_id, case_id = "checkpoint-" + uuid4().hex, "case-" + uuid4().hex
    try:
        yield application, factory_id, case_id
    finally:
        with owner.begin() as connection:
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                connection.execute(
                    text(f"DELETE FROM byof.{table} WHERE thread_id = :thread"),
                    {"thread": checkpoint_thread_id(factory_id, case_id)},
                )
        application.dispose()
        owner.dispose()


def test_interrupt_is_restored_with_new_connection_and_no_runtime_schema_privileges(
    checkpoint_database,
):
    engine, factory_id, case_id = checkpoint_database
    config = {"configurable": {"thread_id": checkpoint_thread_id(factory_id, case_id)}}
    with patch(
        "langgraph.checkpoint.postgres.PostgresSaver.setup",
        side_effect=AssertionError("Runtime must not migrate"),
    ):
        with checkpoint_session(engine, factory_id, case_id) as first:
            assert first is not None
            connection = first.conn
            row = connection.execute(
                "SELECT pg_backend_pid() AS pid, current_schema() AS schema, "
                "has_schema_privilege(current_user, 'byof', 'CREATE') AS ddl"
            ).fetchone()
            assert row["schema"] == "byof" and row["ddl"] is False
            first_pid = row["pid"]
            assert connection.autocommit
            result = graph(first).invoke(
                {
                    "case_ref": case_id,
                    "snapshot_ref": "snapshot-1",
                    "reply_ref": None,
                    "phase": "waiting",
                },
                config,
                durability="sync",
            )
            assert result["__interrupt__"][0].value == {"case_ref": case_id, "task_ref": "task-1"}
            assert result["reply_ref"] is None
        assert connection.closed
        engine.dispose()
        with checkpoint_session(engine, factory_id, case_id) as restored:
            assert restored is not None
            second_pid = restored.conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            assert second_pid != first_pid
            continued = graph(restored)
            saved = continued.get_state(config)
            assert saved.next == ("waiting",)
            assert saved.values["snapshot_ref"] == "snapshot-1"
            result = continued.invoke(
                Command(resume={"reply_ref": "response-verified-2"}), config, durability="sync"
            )
            assert result["reply_ref"] == "response-verified-2"
            assert result["phase"] == "recheck_current_facts"
            assert continued.get_state(config).next == ()
            assert set(result) == {"case_ref", "snapshot_ref", "reply_ref", "phase"}


def test_same_case_second_session_is_rejected_but_different_scopes_can_run(checkpoint_database):
    engine, factory_id, case_id = checkpoint_database
    with checkpoint_session(engine, factory_id, case_id) as first:
        assert first is not None
        with checkpoint_session(engine, factory_id, case_id) as competing:
            assert competing is None
        with checkpoint_session(engine, factory_id, case_id + "-other") as other_case:
            assert other_case is not None
            assert other_case.conn.execute("SELECT 1 AS usable").fetchone()["usable"] == 1
        with checkpoint_session(engine, factory_id + "-other", case_id) as other_factory:
            assert other_factory is not None
    with checkpoint_session(engine, factory_id, case_id) as replacement:
        assert replacement is not None


def test_exception_closes_session_and_releases_case_lock(checkpoint_database):
    engine, factory_id, case_id = checkpoint_database
    with pytest.raises(RuntimeError, match="controlled worker failure"):
        with checkpoint_session(engine, factory_id, case_id) as interrupted:
            assert interrupted is not None
            connection = interrupted.conn
            raise RuntimeError("controlled worker failure")
    assert connection.closed
    with checkpoint_session(engine, factory_id, case_id) as recovered:
        assert recovered is not None


def test_checkpoint_serializer_refuses_pickle_and_tracing_is_disabled(
    checkpoint_database, monkeypatch
):
    engine, factory_id, case_id = checkpoint_database
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    with checkpoint_session(engine, factory_id, case_id) as saver:
        assert saver is not None and get_tracing_context()["enabled"] is False
        with pytest.raises(NotImplementedError, match="Unknown serialization type: pickle"):
            saver.serde.loads_typed(("pickle", b"not-deserialized"))
        assert os.environ["LANGSMITH_TRACING"] == "true"


def test_checkpoint_identity_is_unambiguous_and_checks_input():
    assert checkpoint_thread_id("factory:a", "case") != checkpoint_thread_id("factory", "a:case")
    assert checkpoint_thread_id("factory", "case") == checkpoint_thread_id("factory", "case")
    for factory_id, case_id in (("", "case"), ("factory", " "), ("factory", True)):
        with pytest.raises(ValueError, match="scope"):
            checkpoint_thread_id(factory_id, case_id)
