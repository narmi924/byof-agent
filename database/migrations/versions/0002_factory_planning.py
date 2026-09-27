"""Persist source-owned facts, immutable projections and durable planning jobs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_factory_planning"
down_revision = "0001_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "worlds",
        sa.Column("factory_id", sa.String(160), primary_key=True),
        sa.Column("run_id", sa.String(160), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("business_clock", sa.DateTime(timezone=True), nullable=False),
        sa.Column("document", JSONB(), nullable=False),
        schema="factory_sim",
    )
    op.create_table(
        "snapshots",
        sa.Column("snapshot_id", sa.String(160), primary_key=True),
        sa.Column("factory_id", sa.String(160), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("document", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="byof",
    )
    op.create_index("ix_snapshots_factory_id", "snapshots", ["factory_id"], schema="byof")
    op.create_table(
        "factory_states",
        sa.Column("factory_id", sa.String(160), primary_key=True),
        sa.Column("snapshot_id", sa.String(160), nullable=False),
        sa.Column("run_id", sa.String(160), nullable=False),
        sa.Column("source_revision", sa.String(160), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False),
        schema="byof",
    )
    op.create_table(
        "solve_jobs",
        sa.Column("job_id", sa.String(160), primary_key=True),
        sa.Column("factory_id", sa.String(160), nullable=False),
        sa.Column("request_id", sa.String(160), nullable=False),
        sa.Column("requester_id", sa.String(100), nullable=False),
        sa.Column(
            "snapshot_id",
            sa.String(160),
            sa.ForeignKey("byof.snapshots.snapshot_id"),
            nullable=False,
        ),
        sa.Column("allow_overtime", sa.Boolean(), nullable=False),
        sa.Column("time_limit", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.String(160)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.String(160)),
        sa.Column("error_code", sa.String(80)),
        sa.UniqueConstraint("factory_id", "request_id"),
        schema="byof",
    )
    op.create_index("ix_solve_jobs_factory_id", "solve_jobs", ["factory_id"], schema="byof")
    op.create_table(
        "candidates",
        sa.Column("candidate_id", sa.String(160), primary_key=True),
        sa.Column("factory_id", sa.String(160), nullable=False),
        sa.Column(
            "snapshot_id",
            sa.String(160),
            sa.ForeignKey("byof.snapshots.snapshot_id"),
            nullable=False,
        ),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("document", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="byof",
    )
    op.create_index("ix_candidates_factory_id", "candidates", ["factory_id"], schema="byof")
    op.create_table(
        "approvals",
        sa.Column("approval_id", sa.String(160), primary_key=True),
        sa.Column("factory_id", sa.String(160), nullable=False),
        sa.Column(
            "candidate_id",
            sa.String(160),
            sa.ForeignKey("byof.candidates.candidate_id"),
            nullable=False,
        ),
        sa.Column("document", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="byof",
    )
    op.create_index("ix_approvals_factory_id", "approvals", ["factory_id"], schema="byof")
    # Immutable evidence can be inserted/read by the app; even application bugs cannot rewrite it.
    op.execute(
        "REVOKE UPDATE, DELETE ON byof.snapshots, byof.candidates, byof.approvals FROM byof_app"
    )


def downgrade():
    raise RuntimeError(
        "Restore a verified backup; automatic production history deletion is disabled"
    )
