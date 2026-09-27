# Data dictionary

Origin: `reference` = defined by the reference scenario; `implementation` = technical representation chosen for the database; `derived` = computed from reference rules. Orders, operation times and stock are synthetic scenario data even where the origin is `reference`.

Times are UTC ISO 8601 text. "Changes" marks fields that the running application may update.

| Table | Field | Type | Meaning | Example | Changes | Origin |
|---|---|---|---|---|---|---|
| resource_types | resource_type | VARCHAR(100) | Resource type | ASSEMBLY_CELL | No | reference |
| skills | skill | VARCHAR(100) | Worker skill | ASSEMBLY | No | reference |
| operation_types | op_code | VARCHAR(100) | Stable operation code | OP10 | No | reference |
| priority_levels | priority | VARCHAR(100) | Priority level | NORMAL | No | reference |
| priority_levels | tardiness_weight | INTEGER | Tardiness penalty weight | 1 | No | reference |
| products | product_id | VARCHAR(100) | Product ID | BRG-6202-2RS1 | No | reference |
| products | product_name | TEXT | Reference bearing designation | 6202-2RS1 | No | reference |
| products | batch_size | INTEGER | Fixed batch size | 50 | No | reference |
| products | bore_mm | INTEGER | Bore diameter | 15 | No | reference |
| products | outer_diameter_mm | INTEGER | Outside diameter | 35 | No | reference |
| products | width_mm | INTEGER | Width | 11 | No | reference |
| products | weight_kg | DECIMAL(10,4) | Catalog reference weight | 0.0454 | No | reference |
| materials | material_id | VARCHAR(100) | Material ID | IR-6202 | No | reference |
| materials | material_name | TEXT | BOM component category | Inner Ring | No | reference |
| materials | material_type | TEXT | Component category code | INNER_RING | No | implementation |
| materials | unit | TEXT | Counting unit; GFU is not converted to grams, a ball set is one set | EA | No | reference |
| bom_items | product_id | VARCHAR(100) | Finished product | BRG-6202-2RS1 | No | reference |
| bom_items | material_id | VARCHAR(100) | Consumed material | IR-6202 | No | reference |
| bom_items | qty_per_unit | INTEGER | Quantity per finished piece | 1 | No | reference |
| bom_items | consume_op_code | VARCHAR(100) | Operation at whose start the material is consumed | OP20 | No | reference |
| routing_steps | routing_step_id | VARCHAR(100) | Stable ID of product, routing version and operation | BRG-6202-2RS1-V1.6-OP10 | No | implementation |
| routing_steps | product_id | VARCHAR(100) | Product | BRG-6202-2RS1 | No | reference |
| routing_steps | routing_version | VARCHAR(100) | Routing version; work in progress never switches routing | V1.6 | No | implementation |
| routing_steps | op_code | VARCHAR(100) | Operation code | OP10 | No | reference |
| routing_steps | sequence_no | INTEGER | Position in dependency order; OP60 is step 4 | 1 | No | derived |
| routing_steps | operation_name | TEXT | Operation name in the original wording; the interface shows an English name | (shown as Kitting and pre-assembly prep) | No | reference |
| routing_steps | setup_min | INTEGER | Setup minutes per batch | 3 | No | reference |
| routing_steps | cycle_sec_per_unit | INTEGER | Cycle seconds per piece | 8 | No | reference |
| routing_steps | required_resource_type | VARCHAR(100) | Resource type needed | KITTING | No | reference |
| routing_steps | required_skill | VARCHAR(100) | Skill needed | PRE_ASSEMBLY | No | reference |
| resources | resource_id | VARCHAR(100) | Workstation or machine ID | KIT-01 | No | reference |
| resources | resource_type | VARCHAR(100) | Workstation or machine type | KITTING | No | reference |
| resources | capacity | INTEGER | Concurrent capacity | 1 | No | reference |
| resources | status | VARCHAR(20) | Availability | AVAILABLE | Yes | reference |
| resource_operations | resource_id | VARCHAR(100) | Resource ID | KIT-01 | No | reference |
| resource_operations | op_code | VARCHAR(100) | Operation the resource may run | OP10 | No | reference |
| workers | worker_id | VARCHAR(100) | Worker ID | W01 | No | reference |
| workers | status | VARCHAR(20) | Attendance code; available on regular shifts at the start | AVAILABLE | Yes | implementation |
| workers | overtime_available | INTEGER | Worker may do overtime; not a manager approval | 1 | Yes | reference |
| worker_skills | worker_id | VARCHAR(100) | Worker ID | W01 | No | reference |
| worker_skills | skill | VARCHAR(100) | One skill | PRE_ASSEMBLY | No | reference |
| sales_orders | order_id | VARCHAR(100) | Confirmed order ID | SO-001 | No | reference |
| sales_orders | product_id | VARCHAR(100) | Ordered product | BRG-6202-2RS1 | No | reference |
| sales_orders | quantity | INTEGER | Quantity to produce | 1000 | Yes | reference |
| sales_orders | due_at | VARCHAR(20) | Due date (UTC) | 2026-09-14T09:30:00Z | Yes | reference |
| sales_orders | priority | VARCHAR(100) | Soft due date priority | NORMAL | Yes | reference |
| sales_orders | hard_deadline | INTEGER | 1 when due_at may not be missed | 0 | Yes | reference |
| sales_orders | status | VARCHAR(20) | Order lifecycle; confirmed and unstarted at the start | CONFIRMED | Yes | implementation |
| sales_orders | version | INTEGER | Order version, starting at 1 | 1 | Yes | implementation |
| inventory | material_id | VARCHAR(100) | Material ID | IR-6202 | No | reference |
| inventory | on_hand | INTEGER | Stock on hand, including unconsumed reservations | 1200 | Yes | reference |
| inventory | reserved | INTEGER | Reserved part of stock on hand; 0 at the start | 0 | Yes | reference |
| material_inbounds | inbound_id | VARCHAR(100) | Stable receipt ID (never keyed by ETA) | INB-001 | No | implementation |
| material_inbounds | material_id | VARCHAR(100) | Material received | IR-6202 | No | reference |
| material_inbounds | quantity | INTEGER | Confirmed quantity | 600 | Yes | reference |
| material_inbounds | eta | VARCHAR(20) | Confirmed expected arrival (UTC) | 2026-09-15T00:30:00Z | Yes | reference |
| material_inbounds | status | VARCHAR(20) | Receipt state; the baseline only has CONFIRMED | CONFIRMED | Yes | implementation |
| production_batches | batch_id | VARCHAR(100) | Deterministic ID from order, split revision and sequence | (generated) | No | derived |
| production_batches | order_id | VARCHAR(100) | Order | (generated) | No | reference |
| production_batches | product_id | VARCHAR(100) | Product; part of a composite key that prevents mismatches | (generated) | No | reference |
| production_batches | split_revision | INTEGER | Split revision, starting at 1 | (generated) | No | implementation |
| production_batches | sequence_no | INTEGER | Batch number within the order | (generated) | No | derived |
| production_batches | routing_version | VARCHAR(100) | Routing version of the batch | (generated) | No | implementation |
| production_batches | quantity | INTEGER | Batch quantity | (generated) | No | derived |
| production_batches | status | VARCHAR(20) | Batch lifecycle; PENDING at the start | (generated) | Yes | implementation |
| operations | operation_id | VARCHAR(100) | Deterministic ID from batch and operation code | (generated) | No | derived |
| operations | batch_id | VARCHAR(100) | Batch | (generated) | No | reference |
| operations | product_id | VARCHAR(100) | Product, for the composite key to the routing | (generated) | No | implementation |
| operations | routing_version | VARCHAR(100) | Routing version, for the composite key | (generated) | No | implementation |
| operations | routing_step_id | VARCHAR(100) | Routing step the operation follows | (generated) | No | derived |
| operations | op_code | VARCHAR(100) | Operation code | (generated) | No | derived |
| operations | sequence_no | INTEGER | Dependency order | (generated) | No | derived |
| operations | duration_min | INTEGER | ceil(setup + cycle × quantity / 60), without changeover | (generated) | No | derived |
| operations | status | VARCHAR(20) | Operation lifecycle; PENDING at the start | (generated) | Yes | implementation |
| planning_settings | settings_id | VARCHAR(100) | Single baseline settings ID | BASELINE | No | implementation |
| planning_settings | domain_version | TEXT | Version label of the reference data | V1.6 | No | implementation |
| planning_settings | display_timezone | TEXT | Display time zone | Asia/Singapore | No | reference |
| planning_settings | snapshot_clock | VARCHAR(20) | Fixed initial clock | 2026-09-14T00:30:00Z | No | reference |
| planning_settings | horizon_start | VARCHAR(20) | Planning window start | 2026-09-14T00:30:00Z | No | reference |
| planning_settings | horizon_end | VARCHAR(20) | Planning window end | 2026-09-18T09:30:00Z | No | reference |
| planning_settings | freeze_window_min | INTEGER | Freeze window length | 60 | No | reference |
| planning_settings | first_changeover_min | INTEGER | No changeover before the first task | 0 | No | reference |
| planning_settings | same_sku_changeover_min | INTEGER | Changeover between tasks of the same SKU | 1 | No | reference |
| planning_settings | different_sku_changeover_min | INTEGER | Changeover between different SKUs | 5 | No | reference |
| planning_settings | workers_per_operation | INTEGER | One worker for the whole operation | 1 | No | reference |
| planning_settings | initial_wip_present | INTEGER | No work in progress at the start | 0 | No | reference |
| planning_settings | initial_published_plan_present | INTEGER | No released plan at the start | 0 | No | reference |
| calendar_periods | period_id | VARCHAR(100) | Date and period type ID | 2026-09-14-AM | No | derived |
| calendar_periods | period_type | VARCHAR(20) | Regular shift, lunch break or possible overtime window | NORMAL | No | reference |
| calendar_periods | start_at | VARCHAR(20) | Period start (UTC), [start, end) | 2026-09-14T00:30:00Z | No | reference |
| calendar_periods | end_at | VARCHAR(20) | Period end (UTC), [start, end) | 2026-09-14T04:00:00Z | No | reference |
| calendar_periods | requires_manager_approval | INTEGER | Overtime window needs manager approval | 0 | No | reference |
| policy_rules | rule_id | VARCHAR(100) | Traceable rule ID | BATCH | No | implementation |
| policy_rules | rule_text | TEXT | Rule statement (original wording) | — | No | reference |
| policy_rules | source_section | TEXT | Section of the reference specification | 6.2 | No | implementation |
