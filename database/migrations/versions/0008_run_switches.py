"""Record explicit administrator changes of a simulated enterprise run."""

import sqlalchemy as sa
from alembic import op

revision = "0008_run_switches"
down_revision = "0007_private_replay"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "run_switches",
        sa.Column("switch_id", sa.String(160), primary_key=True),
        sa.Column("factory_id", sa.String(160), nullable=False),
        sa.Column("request_id", sa.String(160), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("previous_run_id", sa.String(160), nullable=False),
        sa.Column("run_id", sa.String(160), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("factory_id", "request_id"),
        schema="byof",
    )
    op.create_index("ix_run_switches_factory_id", "run_switches", ["factory_id"], schema="byof")
    op.execute("REVOKE UPDATE, DELETE ON byof.run_switches FROM byof_app")


def downgrade():
    raise RuntimeError("Run-switch authorization records must be retained")
