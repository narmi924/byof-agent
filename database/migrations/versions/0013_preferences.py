"""Confirmed preferences resolve to one factory-wide objective contract."""

from alembic import op

revision = "0013_preferences"
down_revision = "0012_notifications"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE byof.preference_states (
        factory_id VARCHAR(160) PRIMARY KEY, version INTEGER NOT NULL CHECK(version>=0),
        coordination_id VARCHAR(160))""")
    op.execute("""CREATE TABLE byof.preference_proposals (
        proposal_id VARCHAR(160) PRIMARY KEY, factory_id VARCHAR(160) NOT NULL,
        scope_type VARCHAR(20) NOT NULL, scope_id VARCHAR(160) NOT NULL,
        proposer_id VARCHAR(100) NOT NULL, state VARCHAR(20) NOT NULL,
        document JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL)""")
    op.execute("""CREATE TABLE byof.preference_revisions (
        preference_id VARCHAR(160) PRIMARY KEY, factory_id VARCHAR(160) NOT NULL,
        scope_type VARCHAR(20) NOT NULL, scope_id VARCHAR(160) NOT NULL,
        version INTEGER NOT NULL CHECK(version>0), document JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL, UNIQUE(factory_id,scope_type,scope_id,version))""")
    op.execute("""CREATE TABLE byof.preference_heads (
        factory_id VARCHAR(160) NOT NULL, scope_type VARCHAR(20) NOT NULL,
        scope_id VARCHAR(160) NOT NULL, version INTEGER NOT NULL CHECK(version>0),
        preference_id VARCHAR(160) NOT NULL, active BOOLEAN NOT NULL,
        PRIMARY KEY(factory_id,scope_type,scope_id))""")
    op.execute("""CREATE TABLE byof.preference_actions (
        factory_id VARCHAR(160) NOT NULL, request_id VARCHAR(160) NOT NULL,
        actor_id VARCHAR(100) NOT NULL, kind VARCHAR(30) NOT NULL,
        payload_hash VARCHAR(64) NOT NULL, result JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL, PRIMARY KEY(factory_id,request_id))""")
    op.execute("""CREATE TABLE byof.preference_coordinations (
        coordination_id VARCHAR(160) PRIMARY KEY, factory_id VARCHAR(160) NOT NULL,
        context_hash VARCHAR(64) NOT NULL, document JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL)""")
    op.execute("""CREATE TABLE byof.objective_contracts (
        objective_version VARCHAR(160) PRIMARY KEY, factory_id VARCHAR(160) NOT NULL,
        document JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL)""")
    for table in (
        "preference_proposals",
        "preference_revisions",
        "preference_coordinations",
        "objective_contracts",
    ):
        op.execute(f"CREATE INDEX ix_{table}_factory_id ON byof.{table}(factory_id)")
    for table in (
        "preference_revisions",
        "preference_actions",
        "preference_coordinations",
        "objective_contracts",
    ):
        op.execute(f"REVOKE UPDATE,DELETE ON byof.{table} FROM byof_app")
    op.execute(
        "ALTER TABLE byof.solve_jobs ADD COLUMN objective_version VARCHAR(160) NOT NULL DEFAULT 'delivery-v1'"
    )
    op.execute("ALTER TABLE byof.solve_jobs ADD COLUMN case_id VARCHAR(160)")
    op.execute("DROP INDEX byof.cases_open_scope")
    op.execute(
        "CREATE INDEX cases_open_scope ON byof.cases(factory_id,run_id) WHERE state NOT IN ('RESOLVED','HANDED_OFF','CANCELLED')"
    )


def downgrade():
    raise RuntimeError("Confirmed preference and objective evidence must be retained")
