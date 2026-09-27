"""Real concurrent PostgreSQL commits must not leak beyond an observed change-feed watermark."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

from sqlalchemy import event
from sqlalchemy.orm import Session
from test_dynamic_factory_postgres import dynamic_source as dynamic_source

from packages.domain.execution import SimulatorCommand
from services.factory_sim.service import changes, command
from services.factory_sim.storage import World


def step(engine, initial, request_id, minutes=1):
    return command(
        engine,
        initial.factory_id,
        SimulatorCommand(
            request_id=request_id,
            run_id=initial.run_id,
            kind="clock.step",
            payload={"minutes": minutes},
        ),
    )


def test_tick_committing_between_world_read_and_feed_read_is_deferred_to_next_page(dynamic_source):
    _, _, initial, _, engine = dynamic_source
    world_read, writer_committed = Event(), Event()
    backend_ids = {}

    def after_execute(connection, cursor, statement, parameters, context, executemany):
        if "FROM factory_sim.worlds" not in statement or not statement.lstrip().startswith(
            "SELECT"
        ):
            return
        if "FOR UPDATE" in statement:
            backend_ids["writer"] = connection.exec_driver_sql(
                "SELECT pg_backend_pid()"
            ).scalar_one()
        elif not world_read.is_set():
            backend_ids["reader"] = connection.exec_driver_sql(
                "SELECT pg_backend_pid()"
            ).scalar_one()
            # The completed SELECT fixes its returned World revision; the next SQL
            # statement in READ COMMITTED can see the concurrently committed tick.
            world_read.set()
            assert writer_committed.wait(5), "Concurrent tick did not commit"

    event.listen(engine, "after_cursor_execute", after_execute)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            reading = pool.submit(changes, engine, initial.factory_id, initial.run_id, 1)
            try:
                assert world_read.wait(5), "Reader did not reach the controlled barrier"
                advanced = step(engine, initial, "concurrent-tick")
            finally:
                writer_committed.set()
            first_page = reading.result(timeout=5)
    finally:
        event.remove(engine, "after_cursor_execute", after_execute)

    assert backend_ids["reader"] != backend_ids["writer"]
    assert advanced["source_revision"] == "2"
    assert first_page["watermark"] == first_page["next_cursor"] == "1"
    assert first_page["changes"] == [] and first_page["has_more"] is False
    next_page = changes(engine, initial.factory_id, initial.run_id, int(first_page["next_cursor"]))
    assert next_page["watermark"] == next_page["next_cursor"] == "2"
    assert [int(row["revision"]) for row in next_page["changes"]] == [2]
    assert next_page["has_more"] is False
    with Session(engine) as db:
        stored = db.get(World, initial.factory_id)
        assert stored.business_clock == initial.snapshot_clock + timedelta(minutes=1)
        assert stored.revision == 2


def test_pagination_keeps_every_committed_tick_within_watermark_and_no_repeated_effect(
    dynamic_source,
):
    _, _, initial, _, engine = dynamic_source
    first = step(engine, initial, "three-ticks", minutes=3)
    assert step(engine, initial, "three-ticks", minutes=3) == first
    first_page = changes(engine, initial.factory_id, initial.run_id, after=1, limit=2)
    assert first_page["watermark"] == "4" and first_page["next_cursor"] == "3"
    assert first_page["has_more"] is True
    step(engine, initial, "next-tick")
    second_page = changes(engine, initial.factory_id, initial.run_id, after=3, limit=2)
    assert second_page["watermark"] == second_page["next_cursor"] == "5"
    assert second_page["has_more"] is False
    revisions = []
    for page in (first_page, second_page):
        assert all(int(row["revision"]) <= int(page["watermark"]) for row in page["changes"])
        revisions.extend(int(row["revision"]) for row in page["changes"])
    assert revisions == [2, 3, 4, 5]
    assert len(set(revisions)) == 4
    with Session(engine) as db:
        stored = db.get(World, initial.factory_id)
        assert stored.business_clock == initial.snapshot_clock + timedelta(minutes=4)
