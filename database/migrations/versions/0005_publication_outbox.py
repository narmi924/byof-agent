"""Keep local plan commits separate from conditional source acceptance."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_publication_outbox"
down_revision = "0004_dynamic_factory"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "publications",
        sa.Column("release_id", sa.String(160), primary_key=True),
        sa.Column("factory_id", sa.String(160), nullable=False),
        sa.Column("request_id", sa.String(160), nullable=False),
        sa.Column("requester_id", sa.String(100), nullable=False),
        sa.Column("candidate_id", sa.String(160), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("document", JSONB(), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.String(160)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(80)),
        sa.UniqueConstraint("factory_id", "request_id"),
        schema="byof",
    )
    op.create_index("ix_publications_factory_id", "publications", ["factory_id"], schema="byof")
    op.execute("REVOKE DELETE ON byof.publications FROM byof_app")


def downgrade():
    raise RuntimeError("Restore verified backup; publication history cannot be dropped")
