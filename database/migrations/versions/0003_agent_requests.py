"""Persistent bounded Agent requests and approval operation identities."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_agent_requests"
down_revision = "0002_factory_planning"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("approvals", sa.Column("request_id", sa.String(160)), schema="byof")
    op.execute("UPDATE byof.approvals SET request_id = approval_id")
    op.alter_column("approvals", "request_id", nullable=False, schema="byof")
    op.create_unique_constraint(
        "uq_approval_request", "approvals", ["factory_id", "request_id"], schema="byof"
    )
    op.create_table(
        "agent_runs",
        sa.Column("run_id", sa.String(160), primary_key=True),
        sa.Column("factory_id", sa.String(160), nullable=False),
        sa.Column("request_id", sa.String(160), nullable=False),
        sa.Column("requester_id", sa.String(100), nullable=False),
        sa.Column("message", sa.String(8000), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.String(160)),
        sa.Column("model_requests", sa.Integer(), nullable=False),
        sa.Column("result", JSONB()),
        sa.Column("error_code", sa.String(80)),
        sa.UniqueConstraint("factory_id", "request_id"),
        schema="byof",
    )
    op.create_index("ix_agent_runs_factory_id", "agent_runs", ["factory_id"], schema="byof")


def downgrade():
    raise RuntimeError("Restore verified backup; automatic work history deletion disabled")
