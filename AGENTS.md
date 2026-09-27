# AGENTS.md

Guidance for contributors and coding agents working in this repository. How to run and use the product is in the [guide](docs/GUIDE.md).

## Repository

| Folder | Contents |
|---|---|
| `apps/web` | React + TypeScript + Vite workbench for the manager and the disruption simulator, API contracts and component tests |
| `packages/domain` | Domain models, factory facts, economics and treatment actions |
| `packages/planning` | CP-SAT solver, independent Checker, option comparison, approval and release |
| `packages/agent` | Persistent conversations, restricted tool actions, option approval and the execution ledger |
| `packages/providers` | Model adapters and the server-owned model menu |
| `services` | API, business worker, solver worker, factory service and factory clock |
| `database` | Reference data, schema, presets and the single Alembic migration chain |
| `scripts` | Setup, configuration and check entry points |
| `tests`, `data/development` | Backend tests and their fixtures |

Python 3.12 with `uv.lock`; Node 24 with `apps/web/package-lock.json`; SQLAlchemy and Alembic are the only database and migration stack, on PostgreSQL.

## Checks

- Backend: `docker compose -f compose.test.yaml run --rm checks` (ruff, mypy and pytest against a temporary PostgreSQL), or selected tests with `python -m scripts.container_checks -q <tests>`. A native environment can run `uv run --locked python scripts/check.py` against an isolated test database.
- Frontend: `npm run check` in `apps/web` (lint, tests, types and build).
- Run the affected tests and the lint, type and build checks for every code change. Documentation changes run `tests/test_documentation_paths.py`.
- Never point `TEST_*` database URLs at a database in use, and never replace PostgreSQL transaction tests with SQLite.
- Normal tests use controlled model responses; calls to a real model are made deliberately, with a request budget.

## Rules

- **Agent boundaries.** Every Agent action has an explicit contract, scope and server-side authorization. The model never gets a shell, arbitrary SQL or Python, file access, arbitrary network targets, the right to approve on its own, or a silent provider switch. Every schedule passes the independent Checker; model output and chat never replace factory facts or factory receipts.
- **Data.** `database/seed` and `database/generated` stay unchanged; their SHA256 digests are pinned in `data/development/skf-baseline-hashes.json`. `skf-reference` is the untouched reference factory and `skf-workshop` the demo factory; restarts and setup never clear history. After changing the initial state or the solver model, regenerate the "Plan today" presets with `uv run --locked python -m scripts.generate_day_presets`.
- **Interface.** Follow [docs/DESIGN.md](docs/DESIGN.md). `apps/web/src/tokens.css` is the only source of colors, radii and shadows. Keep the single simulator page with four groups, single-object editing, version conflicts and submission recovery. Use plain business wording in English; no placeholder entry points, invented metrics or sample data fallbacks at runtime.
- **Configuration.** Secrets live only in the ignored `.env` and `.runtime/compose.env`; never print or commit them. Email notifications stay disabled (`SMTP_MODE=disabled`).
- **Changes.** Keep diffs focused. Do not rewrite migrations that have shipped, and do not force-push shared branches.

## Coding guidelines

1. Think before coding
- Never assume. Ask for clarification if ambiguous.
- Present trade-offs when multiple interpretations or simpler paths exist.

2. Simple first
- Minimum code that solves the problem. No speculative features.
- Do not build abstractions for one-off logic.
- Avoid unrequested "flexibility" or configs. If 200 lines can be 50, rewrite it.

3. Surgical changes
- Touch only what must be touched.
- Never "improve", reformat, or refactor adjacent unrelated code.
- Match existing project style strictly. Diff must be minimal and traceable.

4. Goal-driven execution
- Define clear success criteria upfront.
- Verify via tests or runs before declaring a task complete.
