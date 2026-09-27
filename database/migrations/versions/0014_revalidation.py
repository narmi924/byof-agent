"""Immutable certificates retain original approvals across proven normal progress."""

from alembic import op

revision = "0014_revalidation"
down_revision = "0013_preferences"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE byof.validation_certificates (
        certificate_id VARCHAR(160) PRIMARY KEY, factory_id VARCHAR(160) NOT NULL,
        candidate_id VARCHAR(160) NOT NULL, request_id VARCHAR(160) NOT NULL,
        requester_id VARCHAR(100) NOT NULL, payload_hash VARCHAR(64) NOT NULL,
        document JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL,
        UNIQUE(factory_id,request_id))""")
    op.execute(
        "CREATE INDEX ix_validation_certificates_factory_id ON byof.validation_certificates(factory_id)"
    )
    op.execute("REVOKE UPDATE,DELETE ON byof.validation_certificates FROM byof_app")


def downgrade():
    raise RuntimeError("Approval revalidation evidence must be retained")
