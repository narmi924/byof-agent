"""Retain each conversation request while sharing equivalent physical computation."""

from alembic import op

revision = "0022_shared_solves"
down_revision = "0021_user_model_selection"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE byof.solve_jobs ADD COLUMN reused_from_id VARCHAR(160)")
    op.execute("CREATE INDEX ix_solve_jobs_reused_from_id ON byof.solve_jobs(reused_from_id)")


def downgrade():
    raise RuntimeError("Shared computation attribution must be retained")
