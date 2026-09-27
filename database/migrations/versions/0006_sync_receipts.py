"""Keep full external receipts and immutable source event batches."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006_sync_receipts"
down_revision = "0005_publication_outbox"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("publications", sa.Column("source_receipt", JSONB()), schema="byof")
    op.create_table(
        "source_batches",
        sa.Column("run_id", sa.String(160), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("factory_id", sa.String(160), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("document", JSONB(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        schema="byof",
    )
    op.create_index("ix_source_batches_factory_id", "source_batches", ["factory_id"], schema="byof")
    op.execute("REVOKE UPDATE, DELETE ON byof.source_batches FROM byof_app")


def downgrade():
    raise RuntimeError(
        "Restore verified backup; external receipts and source evidence are retained"
    )
