# Reference data layer

This folder holds the static reference factory as relational tables: the products, bill of materials, routings, resources, staff, orders, stock and confirmed receipts of the bearing final assembly scenario described in [the reference scenario](../docs/DOMAIN.md). The application imports it into PostgreSQL when a factory is set up; the files themselves stay unchanged.

## Files

- `schema.sql`: 20 tables for products, BOM, routings, resources, staff, orders, stock, confirmed receipts, batches and operations, plus type, skill, calendar and policy tables. Plain `CREATE TABLE` with composite keys, `CHECK` constraints and foreign keys.
- `seed/`: 18 maintained CSV tables — 4 products, 25 materials, 28 BOM lines, 32 routing steps, 8 resources, 8 workers, 11 worker skills, 6 orders, 25 stock rows and 16 confirmed receipts.
- `generated/`: 108 production batches (5,400 pieces, 50 per batch) and 864 operations (8 per batch). The dependency order is OP10, OP20, OP30, OP60, OP40, OP50, OP70, OP80; pure operation times are 10/9/6/9/6/8/9/9 minutes, 66 minutes per batch before changeovers.
- `presets/`: checked "Plan today" schedules for the demo factory, regenerated with `scripts/generate_day_presets.py`.
- `migrations/`: the Alembic migration history of the application database.
- [`data_dictionary.md`](data_dictionary.md): meaning, example and origin of every field; [`er_diagram.md`](er_diagram.md): keys and relations.
- `source_manifest.json`: load order and expected row counts. The SHA256 of every baseline file is pinned in `data/development/skf-baseline-hashes.json`, and the application refuses a changed baseline.

Some seed text keeps its original Chinese wording (routing step names and policy notes); the interface shows English names for the routing steps.

## Checks

Python 3.10+ with the standard library only. From the repository root:

```sh
python database/scripts/generate_batches.py
python database/scripts/generate_operations.py
python database/scripts/validate_data.py
python -m unittest discover -s database/tests -v
```

`validate_data.py` loads `schema.sql` and every CSV into an in-memory SQLite database with foreign keys on, checks the pinned hashes, row counts, types, keys, BOM and routing completeness, qualified resources and staff, batch splitting, operation durations and the calendar, and writes `validation_report.json`. It does not run the scheduler.

The generators only split unstarted baseline orders: they reject quantities that are not positive multiples of 50 and orders that are not confirmed initial versions, and never overwrite batches or operations that have started. Started, cancelled, blocked or changed work is reconciled by the application's transactions, not by these scripts.

Batch IDs look like `SO-001-R001-B001` and operation IDs like `SO-001-R001-B001-OP10`; routing step IDs such as `BRG-6202-2RS1-V1.6-OP10` bind a batch to the routing it was released with.
