"""Persist observed connector guarantees separately from immutable enterprise facts."""

from alembic import op

revision = "0016_connector_guarantees"
down_revision = "0015_human_review"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE byof.factory_states ADD COLUMN connector_capabilities JSONB")
    op.execute("ALTER TABLE byof.factory_states ADD COLUMN capabilities_observed_at TIMESTAMPTZ")


def downgrade():
    raise RuntimeError("Connector execution guarantees cannot be silently discarded")
