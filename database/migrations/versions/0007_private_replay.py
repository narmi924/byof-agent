"""Private replay cursor belongs only to the simulated enterprise."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_private_replay"
down_revision = "0006_sync_receipts"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("worlds", sa.Column("replay_state", JSONB()), schema="factory_sim")


def downgrade():
    raise RuntimeError("Replay provenance must be retained; restore a verified backup")
