"""PostgreSQL graph persistence with one live graph execution per factory Case."""

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langsmith import tracing_context
from psycopg import Connection
from psycopg.rows import dict_row
from sqlalchemy.engine import Engine


def checkpoint_thread_id(factory_id: str, case_id: str) -> str:
    if any(
        type(value) is not str or not value.strip() or len(value) > 160
        for value in (factory_id, case_id)
    ):
        raise ValueError("Checkpoint scope requires a factory and Case identifier")
    identity = json.dumps([factory_id, case_id], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


@contextmanager
def checkpoint_session(
    engine: Engine, factory_id: str, case_id: str
) -> Iterator[PostgresSaver | None]:
    """Use checkpoint_thread_id as graph thread_id; fence effects and bound execution time."""
    thread_id = checkpoint_thread_id(factory_id, case_id)
    if engine.dialect.name != "postgresql":
        raise ValueError("Checkpoint persistence requires PostgreSQL")
    lock_id = int.from_bytes(bytes.fromhex(thread_id)[:8], byteorder="big", signed=True)
    uri = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    # This dedicated session owns the lock; closing it also releases the lock on errors.
    # No transaction remains open while a node waits for a model or an external result.
    with Connection.connect(
        uri,
        autocommit=True,
        row_factory=dict_row,
        prepare_threshold=0,
        connect_timeout=3,
        options="-c search_path=byof",
    ) as connection:
        row = connection.execute(
            "SELECT pg_try_advisory_lock(%s) AS acquired", (lock_id,)
        ).fetchone()
        assert row is not None
        if not row["acquired"]:
            yield None
            return
        serializer = JsonPlusSerializer(
            pickle_fallback=False, allowed_json_modules=None, allowed_msgpack_modules=None
        )
        with tracing_context(enabled=False):
            yield PostgresSaver(connection, serde=serializer)
