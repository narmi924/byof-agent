# Guide

How to run BYOF Agent on your own computer, connect a model, and walk through a working day with the two roles.

## Requirements

- Docker with Compose v2.
- [uv](https://docs.astral.sh/uv/), which installs the pinned Python 3.12 environment for the helper scripts.
- A [DeepSeek API key](https://platform.deepseek.com/) for the Agent. Without a key the Agent cannot answer, but the factory, scheduling, approval and execution still work.
- Node.js 24 only if you want to work on the web interface outside Docker.

## Start

From the repository root:

```sh
uv sync --locked
uv run --locked python -m scripts.compose_config
```

This writes `.runtime/compose.env` with random credentials for the local database and services. Run it only once: it refuses to overwrite an existing file, and a new file would not match the existing database volume.

Open `.runtime/compose.env` and put your DeepSeek key in `LLM_GATEWAY_API_KEY` (on one line). Then build the two images, start the services and set up the factories:

```sh
docker compose --env-file .runtime/compose.env build api web
docker compose --env-file .runtime/compose.env up --detach --wait --wait-timeout 300
docker compose --env-file .runtime/compose.env run --rm team-setup
```

Seven services start: PostgreSQL, the API, the factory service and its clock, the solver worker, the business worker and the web server. `team-setup` imports the [reference factory](DOMAIN.md) and creates the two identities, the manager and the disruption simulator; running it again keeps existing history.

Open two browser windows, ideally side by side:

- **Manager**: <http://127.0.0.1:18080/agent/chat>
- **Disruption simulator**: <http://127.0.0.1:18080/factory/facts>

On a fresh setup, click **Reset today** in the simulator once. It moves the scenario to today's 08:30 pre-shift state and syncs the factory facts; until then the manager page shows "Connecting".

Only port 18080 on the loopback address is published; the database and internal services are not reachable from outside.

## Models

The model menu in **Settings** (bottom left of the manager page) lists the models the server is configured for; the choice applies to later turns of your account. Keys stay on the server.

| Model | Configuration in `.runtime/compose.env` |
|---|---|
| DeepSeek Flash (default) | `LLM_GATEWAY_API_KEY` or `DEEPSEEK_API_KEY` |
| Claude Sonnet 4.5 · Gateway | `GATEWAY_URL` and `GATEWAY_API_KEY` of a hosted gateway that speaks the `/api/chat` text protocol of [gateway.py](../packages/providers/gateway.py); it is not the Anthropic API |

After changing the file, recreate the services with `docker compose --env-file .runtime/compose.env up --detach --wait`; a plain restart does not read new environment values. An option without a key is shown as unavailable, and the Agent never switches models on its own.

## A working day

The simulator plays the shop floor and only creates disruptions; the manager works with the Agent. Times below are typical on a laptop.

| Step | Who | Action | What happens |
|---|---|---|---|
| 1 | Manager | Click **Plan today** | A "Regular-shift production schedule" card: weighted tardiness 0, added overtime 0, "Preset plan · Checked" (a few seconds) |
| 2 | Manager | Optionally **Preview on the timeline**, then return | The plan can still be approved |
| 3 | Manager | Click **Approve and execute** | The receipt shows "Accepted by factory"; the top bar reads "Plan active" and the simulator starts running |
| 4 | Simulator | In **Orders and delivery**, **Change order** on SO-003, set the quantity to 1500, **Confirm order change** | "Disruptions this run" lists the change |
| 5 | Manager | Click **Field changes 1** in the top bar and open the change | A new conversation; the Agent finds the material shortage and starts comparing response options |
| 6 | Manager | Wait | A short recommendation, then numbered options with on-time quantity, completion, new cash outlay, profit impact, added overtime and resupply details (about 30 seconds) |
| 7 | Manager | Optionally **Comment** on an option | The Agent explains or compares again; chat is never approval |
| 8 | Manager | **Approve and execute** the standard resupply option | Measures applied → schedule checked → released; the Agent then reports what was done and when SO-003 will finish |
| 9 | Manager | Ask "Can SO-003 be delivered on time?" | The Agent checks delivery and materials and answers with the conclusion first |
| 10 | Manager | Open **Execution log** and **Production timeline** | One record per approval; the timeline shows plans by conversation and exports the dispatch sheet |

Comparison results can be approved for 15 minutes; after that, or after a new disruption, the card offers **Re-compare with latest facts**. When no measure can keep an order's due date, the options propose a new date for that order, and approval needs **The customer agreed to the listed due date or quantity change** ticked.

## Disruptions to try

Each works on its own from the matching group in the simulator; the manager opens it from **Field changes**. Handle one before creating the next.

| Disruption | Simulator action | What the manager gets |
|---|---|---|
| Temporary leave | **Machines and staff** → a worker → **Worker status** → **Temporary leave (known return time)** | A rescheduled plan without delays |
| Temporary machine stop | A machine → **Machine status** → **Temporary stop (known duration)** | A reschedule, or a comparison with overtime and expedited repair |
| Machine breakdown | **Machine status** → **Down (recovery time unknown)** | If it interrupted work, confirm the remaining work in **Execution and quality**; the conversation continues by itself with repair and due date options |
| Absence of a busy worker | **Worker status** → **Absent (return time unknown)** on a worker who is running an operation | After the remaining work is confirmed, qualified cover takes over the operation and keeps the part already done |
| Larger order due too early | SO-002: quantity 800 → 1000, due time today 12:00 | Options that keep other due dates and propose a new one for SO-002 |
| Rush order | **New order**, for example 200 pcs of 6205-2RS1 due today 17:30 | Resupply, due date and quantity options |
| Stock count loss | **Raw materials** → **Count stock** on SEAL-RS1-6202, halve the quantity | Resupply options |
| Late, short or cancelled receipt | **Inbound receipts** → **Update receipt** | New resupply compared with due date changes |
| Failed quality check | **Execution and quality** → **Record quality check** → Failed, then **Confirm scrap and remake** | The resupply needed for the remake |
| Random breakdowns | **More** → **Turn on random breakdowns** | A machine stops every 90 minutes while running |

## Everyday operations

```sh
docker compose --env-file .runtime/compose.env ps -a
docker compose --env-file .runtime/compose.env logs --tail 60 api solver-worker business-worker factory-sim factory-clock
docker compose --env-file .runtime/compose.env stop
docker compose --env-file .runtime/compose.env up --detach --wait
```

`stop` keeps the database volume. To start the demo over, use **Reset today** in the simulator; earlier runs are kept. Do not delete the volume (`down --volumes`) to reset.

When something does not go as expected:

- **A comparison takes a while**: the activity line shows the stage and elapsed seconds, and **Stop** is available; stopping does not pause the factory or undo approved execution.
- **"No feasible schedule found within the time limit"**: add conditions (for example allow a later due date or overtime) and let the Agent continue.
- **"The model service is busy or out of quota"**: check the key and its balance, or switch the model in Settings.
- **Field changes do not appear**: check that the simulator showed a green confirmation; the top bar refreshes every few seconds.
- **Unknown network result**: the page keeps the original request; use **Check the original request** instead of sending a new one.

## Development

The development override mounts the source into the containers and reloads the API:

```sh
docker compose --env-file .runtime/compose.env -f compose.yaml -f compose.dev.yaml build api web
docker compose --env-file .runtime/compose.env -f compose.yaml -f compose.dev.yaml up --detach --wait
```

Backend checks run in a separate project with a temporary database, without touching your data:

```sh
docker compose -f compose.test.yaml build checks
docker compose -f compose.test.yaml run --rm checks
docker compose -f compose.test.yaml down
```

The full suite includes a long continuous-factory case. To run selected tests, append them, for example `run --rm checks python -m scripts.container_checks -q tests/test_solver.py`.

Frontend checks run in `apps/web`:

```sh
npm ci
npm run check
```

Project conventions are in [AGENTS.md](../AGENTS.md) and the interface rules in [DESIGN.md](DESIGN.md).

## Limits

Prices and supply terms come from a fixed catalog; there is no real purchasing, customer contracting or ERP/MES integration, and email notifications are off. Solving has time limits and does not guarantee a feasible plan for every combination of disruptions. The local setup serves plain HTTP on the loopback address; a public deployment needs HTTPS, a matching `PUBLIC_ORIGIN` and its own backup plan.
