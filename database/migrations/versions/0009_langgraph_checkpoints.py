"""Pin LangGraph PostgreSQL checkpoint storage to the project's Alembic lifecycle.

DDL follows langgraph-checkpoint-postgres 3.1.2 (MIT), migrations 0 through 9.
New empty tables use transactional indexes; runtime roles never run saver.setup().
"""

from alembic import op

revision = "0009_langgraph_checkpoints"
down_revision = "0008_run_switches"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE TABLE byof.checkpoint_migrations (v INTEGER PRIMARY KEY)")
    op.execute("""CREATE TABLE byof.checkpoints (
        thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '',
        checkpoint_id TEXT NOT NULL, parent_checkpoint_id TEXT, type TEXT,
        checkpoint JSONB NOT NULL, metadata JSONB NOT NULL DEFAULT '{}',
        PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id))""")
    op.execute("""CREATE TABLE byof.checkpoint_blobs (
        thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '',
        channel TEXT NOT NULL, version TEXT NOT NULL, type TEXT NOT NULL, blob BYTEA,
        PRIMARY KEY (thread_id, checkpoint_ns, channel, version))""")
    op.execute("""CREATE TABLE byof.checkpoint_writes (
        thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '',
        checkpoint_id TEXT NOT NULL, task_id TEXT NOT NULL, idx INTEGER NOT NULL,
        channel TEXT NOT NULL, type TEXT, blob BYTEA NOT NULL, task_path TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx))""")
    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
        op.execute(f"CREATE INDEX {table}_thread_id_idx ON byof.{table}(thread_id)")
    op.execute("INSERT INTO byof.checkpoint_migrations(v) SELECT generate_series(0, 9)")
    op.execute("REVOKE INSERT, UPDATE, DELETE ON byof.checkpoint_migrations FROM byof_app")


def downgrade():
    raise RuntimeError("Agent checkpoints must be retained for recovery")
