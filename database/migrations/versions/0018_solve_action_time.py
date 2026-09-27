"""Bind requested future dispatch timing to each durable solve job."""

from alembic import op

revision = "0018_solve_action_time"
down_revision = "0017_case_timer_cancellation"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE byof.solve_jobs ADD COLUMN new_actions_not_before TIMESTAMPTZ")


def downgrade():
    raise RuntimeError("Durable solve timing must remain available for recovery audits")
