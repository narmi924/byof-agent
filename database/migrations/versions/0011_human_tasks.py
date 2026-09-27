"""Authenticated human responses and independently tracked reminder intents."""

from alembic import op

revision = "0011_human_tasks"
down_revision = "0010_durable_cases"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE byof.human_tasks (
        task_id VARCHAR(160) PRIMARY KEY, factory_id VARCHAR(160) NOT NULL,
        case_id VARCHAR(160) NOT NULL, operation_id VARCHAR(160) NOT NULL,
        question VARCHAR(2000) NOT NULL, subject_id VARCHAR(160) NOT NULL,
        owner_role VARCHAR(30) NOT NULL, owner_id VARCHAR(100), requested_fields JSONB NOT NULL,
        creation_hash VARCHAR(64) NOT NULL, question_hash VARCHAR(64) NOT NULL,
        state VARCHAR(30) NOT NULL, version INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
        due_at TIMESTAMPTZ NOT NULL, next_reminder_at TIMESTAMPTZ,
        reminders_count INTEGER NOT NULL, response JSONB,
        UNIQUE(factory_id,operation_id))""")
    op.execute("""CREATE UNIQUE INDEX human_tasks_open_question ON byof.human_tasks(case_id,question_hash)
        WHERE state IN ('OPEN','ESCALATED')""")
    op.execute("""CREATE TABLE byof.task_actions (
        action_id VARCHAR(160) PRIMARY KEY, task_id VARCHAR(160) NOT NULL,
        case_id VARCHAR(160) NOT NULL, factory_id VARCHAR(160) NOT NULL,
        request_id VARCHAR(160) NOT NULL, actor_id VARCHAR(100), kind VARCHAR(30) NOT NULL,
        payload_hash VARCHAR(64) NOT NULL, payload JSONB NOT NULL, result JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL, UNIQUE(factory_id,request_id))""")
    op.execute("""CREATE TABLE byof.task_reminders (
        reminder_id VARCHAR(160) PRIMARY KEY, task_id VARCHAR(160) NOT NULL,
        case_id VARCHAR(160) NOT NULL, factory_id VARCHAR(160) NOT NULL,
        task_version INTEGER NOT NULL, ordinal INTEGER NOT NULL, state VARCHAR(20) NOT NULL,
        scheduled_at TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL,
        UNIQUE(task_id,ordinal))""")
    for table in ("human_tasks", "task_actions", "task_reminders"):
        op.execute(f"CREATE INDEX ix_{table}_factory_id ON byof.{table}(factory_id)")
        op.execute(f"CREATE INDEX ix_{table}_case_id ON byof.{table}(case_id)")
    for table in ("task_actions", "task_reminders"):
        op.execute(f"CREATE INDEX ix_{table}_task_id ON byof.{table}(task_id)")
    op.execute("REVOKE UPDATE,DELETE ON byof.task_actions FROM byof_app")


def downgrade():
    raise RuntimeError("Human response evidence must be retained")
