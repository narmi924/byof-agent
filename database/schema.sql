-- Portable SQLite/PostgreSQL DDL. SQLite connections must enable PRAGMA foreign_keys=ON.

-- UTC timestamps are canonical ISO 8601 text; scripts validate actual calendar dates.

-- Boolean values use INTEGER 0/1. No runtime events, solver or schedule outputs.

CREATE TABLE resource_types (
    resource_type VARCHAR(100) NOT NULL,
    PRIMARY KEY (resource_type)
);

CREATE TABLE skills (
    skill VARCHAR(100) NOT NULL,
    PRIMARY KEY (skill)
);

CREATE TABLE operation_types (
    op_code VARCHAR(100) NOT NULL,
    PRIMARY KEY (op_code)
);

CREATE TABLE priority_levels (
    priority VARCHAR(100) NOT NULL,
    tardiness_weight INTEGER NOT NULL CHECK (tardiness_weight > 0),
    PRIMARY KEY (priority)
);

CREATE TABLE products (
    product_id VARCHAR(100) NOT NULL,
    product_name TEXT NOT NULL,
    batch_size INTEGER NOT NULL CHECK (batch_size = 50),
    bore_mm INTEGER NOT NULL CHECK (bore_mm > 0),
    outer_diameter_mm INTEGER NOT NULL CHECK (outer_diameter_mm > 0),
    width_mm INTEGER NOT NULL CHECK (width_mm > 0),
    weight_kg DECIMAL(10,4) NOT NULL CHECK (weight_kg > 0),
    PRIMARY KEY (product_id),
    CHECK (outer_diameter_mm > bore_mm)
);

CREATE TABLE materials (
    material_id VARCHAR(100) NOT NULL,
    material_name TEXT NOT NULL,
    material_type TEXT NOT NULL,
    unit TEXT NOT NULL,
    PRIMARY KEY (material_id)
);

CREATE TABLE bom_items (
    product_id VARCHAR(100) NOT NULL,
    material_id VARCHAR(100) NOT NULL,
    qty_per_unit INTEGER NOT NULL CHECK (qty_per_unit > 0),
    consume_op_code VARCHAR(100) NOT NULL,
    PRIMARY KEY (product_id, material_id),
    FOREIGN KEY (product_id) REFERENCES products(product_id),
    FOREIGN KEY (material_id) REFERENCES materials(material_id),
    FOREIGN KEY (consume_op_code) REFERENCES operation_types(op_code)
);

CREATE TABLE routing_steps (
    routing_step_id VARCHAR(100) NOT NULL,
    product_id VARCHAR(100) NOT NULL,
    routing_version VARCHAR(100) NOT NULL,
    op_code VARCHAR(100) NOT NULL,
    sequence_no INTEGER NOT NULL CHECK (sequence_no BETWEEN 1 AND 8),
    operation_name TEXT NOT NULL,
    setup_min INTEGER NOT NULL CHECK (setup_min >= 0),
    cycle_sec_per_unit INTEGER NOT NULL CHECK (cycle_sec_per_unit >= 0),
    required_resource_type VARCHAR(100) NOT NULL,
    required_skill VARCHAR(100) NOT NULL,
    PRIMARY KEY (routing_step_id),
    UNIQUE (product_id, routing_version, sequence_no),
    UNIQUE (product_id, routing_version, op_code),
    UNIQUE (routing_step_id, product_id, routing_version, op_code, sequence_no),
    FOREIGN KEY (product_id) REFERENCES products(product_id),
    FOREIGN KEY (op_code) REFERENCES operation_types(op_code),
    FOREIGN KEY (required_resource_type) REFERENCES resource_types(resource_type),
    FOREIGN KEY (required_skill) REFERENCES skills(skill)
);

CREATE TABLE resources (
    resource_id VARCHAR(100) NOT NULL,
    resource_type VARCHAR(100) NOT NULL,
    capacity INTEGER NOT NULL CHECK (capacity > 0),
    status VARCHAR(20) NOT NULL CHECK (status IN ('AVAILABLE','MAINTENANCE','DOWN','UNKNOWN')),
    PRIMARY KEY (resource_id),
    FOREIGN KEY (resource_type) REFERENCES resource_types(resource_type)
);

CREATE TABLE resource_operations (
    resource_id VARCHAR(100) NOT NULL,
    op_code VARCHAR(100) NOT NULL,
    PRIMARY KEY (resource_id, op_code),
    FOREIGN KEY (resource_id) REFERENCES resources(resource_id),
    FOREIGN KEY (op_code) REFERENCES operation_types(op_code)
);

CREATE TABLE workers (
    worker_id VARCHAR(100) NOT NULL,
    status VARCHAR(20) NOT NULL CHECK (status IN ('AVAILABLE','ABSENT','UNKNOWN')),
    overtime_available INTEGER NOT NULL CHECK (overtime_available IN (0, 1)),
    PRIMARY KEY (worker_id)
);

CREATE TABLE worker_skills (
    worker_id VARCHAR(100) NOT NULL,
    skill VARCHAR(100) NOT NULL,
    PRIMARY KEY (worker_id, skill),
    FOREIGN KEY (worker_id) REFERENCES workers(worker_id),
    FOREIGN KEY (skill) REFERENCES skills(skill)
);

CREATE TABLE sales_orders (
    order_id VARCHAR(100) NOT NULL,
    product_id VARCHAR(100) NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0 AND quantity % 50 = 0),
    due_at VARCHAR(20) NOT NULL CHECK (length(due_at) = 20 AND due_at LIKE '____-__-__T__:__:__Z'),
    priority VARCHAR(100) NOT NULL,
    hard_deadline INTEGER NOT NULL CHECK (hard_deadline IN (0, 1)),
    status VARCHAR(20) NOT NULL CHECK (status IN ('CONFIRMED','IN_PROGRESS','COMPLETED','CANCELLED')),
    version INTEGER NOT NULL CHECK (version > 0),
    PRIMARY KEY (order_id),
    UNIQUE (order_id, product_id),
    FOREIGN KEY (product_id) REFERENCES products(product_id),
    FOREIGN KEY (priority) REFERENCES priority_levels(priority)
);

CREATE TABLE inventory (
    material_id VARCHAR(100) NOT NULL,
    on_hand INTEGER NOT NULL CHECK (on_hand >= 0),
    reserved INTEGER NOT NULL CHECK (reserved >= 0),
    PRIMARY KEY (material_id),
    CHECK (reserved <= on_hand),
    FOREIGN KEY (material_id) REFERENCES materials(material_id)
);

CREATE TABLE material_inbounds (
    inbound_id VARCHAR(100) NOT NULL,
    material_id VARCHAR(100) NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    eta VARCHAR(20) NOT NULL CHECK (length(eta) = 20 AND eta LIKE '____-__-__T__:__:__Z'),
    status VARCHAR(20) NOT NULL CHECK (status IN ('CONFIRMED','RECEIVED','CANCELLED')),
    PRIMARY KEY (inbound_id),
    FOREIGN KEY (material_id) REFERENCES materials(material_id)
);

CREATE TABLE production_batches (
    batch_id VARCHAR(100) NOT NULL,
    order_id VARCHAR(100) NOT NULL,
    product_id VARCHAR(100) NOT NULL,
    split_revision INTEGER NOT NULL CHECK (split_revision > 0),
    sequence_no INTEGER NOT NULL CHECK (sequence_no > 0),
    routing_version VARCHAR(100) NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity = 50),
    status VARCHAR(20) NOT NULL CHECK (status IN ('PENDING','IN_PROGRESS','COMPLETED','BLOCKED','CANCELLED')),
    PRIMARY KEY (batch_id),
    UNIQUE (order_id, split_revision, sequence_no),
    UNIQUE (batch_id, product_id, routing_version),
    FOREIGN KEY (order_id, product_id) REFERENCES sales_orders(order_id, product_id)
);

CREATE TABLE operations (
    operation_id VARCHAR(100) NOT NULL,
    batch_id VARCHAR(100) NOT NULL,
    product_id VARCHAR(100) NOT NULL,
    routing_version VARCHAR(100) NOT NULL,
    routing_step_id VARCHAR(100) NOT NULL,
    op_code VARCHAR(100) NOT NULL,
    sequence_no INTEGER NOT NULL CHECK (sequence_no BETWEEN 1 AND 8),
    duration_min INTEGER NOT NULL CHECK (duration_min > 0),
    status VARCHAR(20) NOT NULL CHECK (status IN ('PENDING','IN_PROGRESS','COMPLETED','BLOCKED','CANCELLED')),
    PRIMARY KEY (operation_id),
    UNIQUE (batch_id, op_code),
    UNIQUE (batch_id, sequence_no),
    FOREIGN KEY (batch_id, product_id, routing_version) REFERENCES production_batches(batch_id, product_id, routing_version),
    FOREIGN KEY (routing_step_id, product_id, routing_version, op_code, sequence_no) REFERENCES routing_steps(routing_step_id, product_id, routing_version, op_code, sequence_no)
);

CREATE TABLE planning_settings (
    settings_id VARCHAR(100) NOT NULL,
    domain_version TEXT NOT NULL,
    display_timezone TEXT NOT NULL,
    snapshot_clock VARCHAR(20) NOT NULL CHECK (length(snapshot_clock) = 20 AND snapshot_clock LIKE '____-__-__T__:__:__Z'),
    horizon_start VARCHAR(20) NOT NULL CHECK (length(horizon_start) = 20 AND horizon_start LIKE '____-__-__T__:__:__Z'),
    horizon_end VARCHAR(20) NOT NULL CHECK (length(horizon_end) = 20 AND horizon_end LIKE '____-__-__T__:__:__Z'),
    freeze_window_min INTEGER NOT NULL CHECK (freeze_window_min = 60),
    first_changeover_min INTEGER NOT NULL CHECK (first_changeover_min = 0),
    same_sku_changeover_min INTEGER NOT NULL CHECK (same_sku_changeover_min = 1),
    different_sku_changeover_min INTEGER NOT NULL CHECK (different_sku_changeover_min = 5),
    workers_per_operation INTEGER NOT NULL CHECK (workers_per_operation = 1),
    initial_wip_present INTEGER NOT NULL CHECK (initial_wip_present IN (0, 1)),
    initial_published_plan_present INTEGER NOT NULL CHECK (initial_published_plan_present IN (0, 1)),
    PRIMARY KEY (settings_id),
    CHECK (horizon_start < horizon_end)
);

CREATE TABLE calendar_periods (
    period_id VARCHAR(100) NOT NULL,
    period_type VARCHAR(20) NOT NULL CHECK (period_type IN ('NORMAL','BREAK','OVERTIME_WINDOW')),
    start_at VARCHAR(20) NOT NULL CHECK (length(start_at) = 20 AND start_at LIKE '____-__-__T__:__:__Z'),
    end_at VARCHAR(20) NOT NULL CHECK (length(end_at) = 20 AND end_at LIKE '____-__-__T__:__:__Z'),
    requires_manager_approval INTEGER NOT NULL CHECK (requires_manager_approval IN (0, 1)),
    PRIMARY KEY (period_id),
    UNIQUE (start_at, end_at),
    CHECK (start_at < end_at)
);

CREATE TABLE policy_rules (
    rule_id VARCHAR(100) NOT NULL,
    rule_text TEXT NOT NULL,
    source_section TEXT NOT NULL,
    PRIMARY KEY (rule_id)
);
