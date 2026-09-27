"""User model selection and durable per-turn attribution; no credentials in the database."""

from alembic import op

revision = "0021_user_model_selection"
down_revision = "0020_business_options"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE byof.user_model_selections (
        user_id VARCHAR(100) PRIMARY KEY REFERENCES byof.users(user_id),
        model_id VARCHAR(80) NOT NULL, version INTEGER NOT NULL,
        request_id VARCHAR(160) NOT NULL, updated_at TIMESTAMPTZ NOT NULL
    )""")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON byof.user_model_selections TO byof_app")
    op.execute("ALTER TABLE byof.case_turns ADD COLUMN model_id VARCHAR(80)")


def downgrade():
    raise RuntimeError("Model choice attribution must be retained")
