"""Persist physical execution, source event watermarks and idempotent source actions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_dynamic_factory"
down_revision = "0003_agent_requests"
branch_labels = None
depends_on = None


def upgrade():
    for column in (
        sa.Column("active_candidate", JSONB()),
        sa.Column("mode", sa.String(20), nullable=False, server_default="PAUSED"),
        sa.Column("interval_ms", sa.Integer(), nullable=False, server_default="1000"),
        sa.Column("next_tick_at", sa.DateTime(timezone=True)),
    ):
        op.add_column("worlds", column, schema="factory_sim")
    op.create_table(
        "changes",
        sa.Column("run_id", sa.String(160), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("factory_id", sa.String(160), nullable=False),
        sa.Column("document", JSONB(), nullable=False),
        schema="factory_sim",
    )
    op.create_table(
        "actions",
        sa.Column("action_id", sa.String(160), primary_key=True),
        sa.Column("factory_id", sa.String(160), nullable=False),
        sa.Column("run_id", sa.String(160), nullable=False),
        sa.Column("operation_id", sa.String(160), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(50), nullable=False),
        sa.Column("request", JSONB(), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.UniqueConstraint("factory_id", "run_id", "operation_id"),
        schema="factory_sim",
    )
    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(160), primary_key=True),
        sa.Column("factory_id", sa.String(160), nullable=False),
        sa.Column("initial_snapshot", JSONB(), nullable=False),
        sa.Column("replay_of", sa.String(160)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="factory_sim",
    )
    for table in ("changes", "actions", "runs"):
        op.create_index(f"ix_{table}_factory_id", table, ["factory_id"], schema="factory_sim")
        op.execute(f"REVOKE UPDATE, DELETE ON factory_sim.{table} FROM factory_sim_app")


def downgrade():
    raise RuntimeError("Restore verified backup; physical execution history cannot be dropped")
