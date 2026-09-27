"""Human review evidence and short public reasons for actual model proposals."""

from alembic import op

revision = "0015_human_review"
down_revision = "0014_revalidation"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE byof.approval_reviews (
        review_id VARCHAR(160) PRIMARY KEY, factory_id VARCHAR(160) NOT NULL,
        candidate_id VARCHAR(160) NOT NULL, approval_id VARCHAR(160) UNIQUE NOT NULL,
        document JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL)""")
    op.execute("CREATE INDEX ix_approval_reviews_factory_id ON byof.approval_reviews(factory_id)")
    op.execute(
        "CREATE INDEX ix_approval_reviews_candidate_id ON byof.approval_reviews(candidate_id)"
    )
    op.execute("REVOKE UPDATE,DELETE ON byof.approval_reviews FROM byof_app")
    op.execute("ALTER TABLE byof.case_operations ADD COLUMN reason_summary VARCHAR(500)")


def downgrade():
    raise RuntimeError("Human approvals and public action reasons must be retained")
