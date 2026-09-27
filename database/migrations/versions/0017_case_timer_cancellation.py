"""Retain cancelled timer evidence while excluding superseded wakeups from work claims."""

from alembic import op

revision = "0017_case_timer_cancellation"
down_revision = "0016_connector_guarantees"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE byof.case_inputs ADD COLUMN cancelled_at TIMESTAMPTZ")
    op.execute("ALTER TABLE byof.case_inputs ADD COLUMN cancellation_reason VARCHAR(80)")
    op.execute("""ALTER TABLE byof.case_inputs ADD CONSTRAINT case_timer_cancellation_pair CHECK (
        (cancelled_at IS NULL AND cancellation_reason IS NULL)
        OR (kind='TIMER' AND cancelled_at IS NOT NULL AND cancellation_reason IS NOT NULL))""")


def downgrade():
    raise RuntimeError("Cancelled Case timers must remain available for recovery audits")
