"""Versioned factory contacts and fenced SMTP notification attempts."""

from alembic import op

revision = "0012_notifications"
down_revision = "0011_human_tasks"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE byof.notification_contacts (
        factory_id VARCHAR(160) NOT NULL, role VARCHAR(30) NOT NULL,
        user_id VARCHAR(100) NOT NULL, email VARCHAR(254) NOT NULL,
        version INTEGER NOT NULL CHECK(version>0), enabled BOOLEAN NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL, PRIMARY KEY(factory_id,role))""")
    op.execute("""CREATE TABLE byof.notification_contact_actions (
        factory_id VARCHAR(160) NOT NULL, request_id VARCHAR(160) NOT NULL,
        actor_id VARCHAR(100) NOT NULL, payload_hash VARCHAR(64) NOT NULL,
        result JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY(factory_id,request_id))""")
    op.execute("""CREATE TABLE byof.notifications (
        notification_id VARCHAR(160) PRIMARY KEY, factory_id VARCHAR(160) NOT NULL,
        case_id VARCHAR(160) NOT NULL, task_id VARCHAR(160) NOT NULL,
        task_version INTEGER NOT NULL, kind VARCHAR(30) NOT NULL,
        dedupe_key VARCHAR(240) NOT NULL UNIQUE, reminder_id VARCHAR(160),
        role VARCHAR(30) NOT NULL, contact_version INTEGER, recipient_id VARCHAR(100),
        recipient_email VARCHAR(254), message_id VARCHAR(240) NOT NULL UNIQUE,
        send_state VARCHAR(30) NOT NULL CHECK(send_state IN
          ('QUEUED','CLAIMED','SENDING','PROVIDER_ACCEPTED','FAILED','UNKNOWN','CANCELLED',
           'NOT_ENABLED','NOT_CONFIGURED')),
        attempts INTEGER NOT NULL CHECK(attempts>=0 AND attempts<=3),
        lease_token VARCHAR(160), lease_until TIMESTAMPTZ,
        error_code VARCHAR(100), created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL)""")
    op.execute("CREATE INDEX ix_notifications_factory_id ON byof.notifications(factory_id)")
    op.execute("CREATE INDEX ix_notifications_task_id ON byof.notifications(task_id)")
    op.execute("CREATE INDEX ix_notifications_state ON byof.notifications(send_state,created_at)")
    op.execute("REVOKE UPDATE,DELETE ON byof.notification_contact_actions FROM byof_app")


def downgrade():
    raise RuntimeError("Notification and contact evidence must be retained")
