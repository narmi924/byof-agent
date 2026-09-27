"""Persist Case inputs, bounded turns and controlled tool operations."""

from alembic import op

revision = "0010_durable_cases"
down_revision = "0009_langgraph_checkpoints"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE byof.cases (
        case_id VARCHAR(160) PRIMARY KEY, factory_id VARCHAR(160) NOT NULL,
        run_id VARCHAR(160) NOT NULL, owner_id VARCHAR(100) NOT NULL,
        state VARCHAR(40) NOT NULL, version INTEGER NOT NULL DEFAULT 1,
        title VARCHAR(240) NOT NULL, created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL, active_turn_id VARCHAR(160),
        snapshot_id VARCHAR(160), context JSONB NOT NULL DEFAULT '{}',
        closure JSONB, error_code VARCHAR(80))""")
    op.execute("""CREATE UNIQUE INDEX cases_open_scope ON byof.cases(factory_id,run_id)
        WHERE state NOT IN ('RESOLVED','HANDED_OFF','CANCELLED')""")
    op.execute("""CREATE TABLE byof.case_inputs (
        input_id VARCHAR(160) PRIMARY KEY, factory_id VARCHAR(160) NOT NULL,
        case_id VARCHAR(160) NOT NULL, input_key VARCHAR(250) NOT NULL,
        kind VARCHAR(50) NOT NULL, payload JSONB NOT NULL, payload_hash VARCHAR(64) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL, available_at TIMESTAMPTZ NOT NULL,
        turn_id VARCHAR(160), UNIQUE(factory_id,input_key))""")
    op.execute("""CREATE TABLE byof.case_turns (
        turn_id VARCHAR(160) PRIMARY KEY, factory_id VARCHAR(160) NOT NULL,
        case_id VARCHAR(160) NOT NULL, state VARCHAR(40) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL, deadline TIMESTAMPTZ NOT NULL,
        lease_until TIMESTAMPTZ, lease_token VARCHAR(160), model_requests INTEGER NOT NULL DEFAULT 0,
        solver_requests INTEGER NOT NULL DEFAULT 0, next_step INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0, model_pending BOOLEAN NOT NULL DEFAULT FALSE,
        error_code VARCHAR(80))""")
    op.execute("""CREATE TABLE byof.case_operations (
        operation_id VARCHAR(160) PRIMARY KEY, factory_id VARCHAR(160) NOT NULL,
        case_id VARCHAR(160) NOT NULL, turn_id VARCHAR(160) NOT NULL, step INTEGER NOT NULL,
        action VARCHAR(40) NOT NULL, parameters JSONB NOT NULL, parameter_hash VARCHAR(64) NOT NULL,
        expected_case_version INTEGER NOT NULL, snapshot_id VARCHAR(160) NOT NULL,
        state VARCHAR(40) NOT NULL, result JSONB, created_at TIMESTAMPTZ NOT NULL,
        UNIQUE(turn_id,step))""")
    op.execute("""CREATE TABLE byof.case_cursors (
        factory_id VARCHAR(160) PRIMARY KEY, run_id VARCHAR(160) NOT NULL,
        source_revision INTEGER NOT NULL)""")
    for table in ("cases", "case_inputs", "case_turns", "case_operations"):
        op.execute(f"CREATE INDEX ix_{table}_factory_id ON byof.{table}(factory_id)")
    for table in ("case_inputs", "case_turns", "case_operations"):
        op.execute(f"CREATE INDEX ix_{table}_case_id ON byof.{table}(case_id)")
    op.execute(
        "REVOKE DELETE ON byof.cases,byof.case_inputs,byof.case_turns,byof.case_operations FROM byof_app"
    )


def downgrade():
    raise RuntimeError("Case evidence must be retained")
