"""Create separate ownership boundaries and persistent login records."""

import sqlalchemy as sa
from alembic import op

revision = "0001_identity"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA byof")
    op.execute("CREATE SCHEMA factory_sim")
    op.execute("CREATE SCHEMA agent_checkpoint")
    op.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    op.create_table(
        "users",
        sa.Column("user_id", sa.String(100), primary_key=True),
        sa.Column("username", sa.String(100), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        schema="byof",
    )
    op.create_table(
        "memberships",
        sa.Column("user_id", sa.String(100), sa.ForeignKey("byof.users.user_id"), primary_key=True),
        sa.Column("factory_id", sa.String(100), primary_key=True),
        sa.Column("role", sa.String(30), primary_key=True),
        sa.CheckConstraint(
            "role IN ('planner','manager','maintainer','warehouse','team_lead','admin','sim_admin')"
        ),
        schema="byof",
    )
    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(100), sa.ForeignKey("byof.users.user_id"), nullable=False),
        sa.Column("csrf_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        schema="byof",
    )
    op.execute("GRANT USAGE ON SCHEMA byof, agent_checkpoint TO byof_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA byof TO byof_app")
    op.execute("GRANT USAGE ON SCHEMA factory_sim TO factory_sim_app")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA byof GRANT SELECT, INSERT, UPDATE, DELETE "
        "ON TABLES TO byof_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA factory_sim GRANT SELECT, INSERT, UPDATE, DELETE "
        "ON TABLES TO factory_sim_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA agent_checkpoint GRANT SELECT, INSERT, UPDATE, DELETE "
        "ON TABLES TO byof_app"
    )


def downgrade() -> None:
    raise RuntimeError("Restore a verified backup; automatic identity/history deletion is disabled")
