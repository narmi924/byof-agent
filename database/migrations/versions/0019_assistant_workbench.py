"""Durable reviewer actions and opt-in simulator scenarios."""

from alembic import op

revision = "0019_assistant_workbench"
down_revision = "0018_solve_action_time"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE byof.assistant_actions (
            action_id VARCHAR(160) PRIMARY KEY,
            factory_id VARCHAR(160) NOT NULL,
            user_id VARCHAR(100) NOT NULL,
            request_id VARCHAR(160) NOT NULL,
            run_id VARCHAR(160) NOT NULL,
            kind VARCHAR(40) NOT NULL,
            payload JSONB NOT NULL,
            state VARCHAR(30) NOT NULL,
            result JSONB,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL,
            next_attempt_at TIMESTAMPTZ NOT NULL,
            UNIQUE(factory_id, request_id)
        )
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON byof.assistant_actions TO byof_app")
    op.execute("ALTER TABLE factory_sim.worlds ADD COLUMN scenario_state JSONB")


def downgrade():
    raise RuntimeError("Reviewer decisions and simulation history must be retained")
