"""Keep advisory business studies separate from publishable scheduling candidates."""

from alembic import op

revision = "0020_business_options"
down_revision = "0019_assistant_workbench"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE byof.solve_jobs ADD COLUMN business_request JSONB")
    op.execute("ALTER TABLE byof.solve_jobs ADD COLUMN business_result JSONB")


def downgrade():
    raise RuntimeError("Business decision evidence must be retained")
