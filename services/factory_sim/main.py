"""Independent enterprise HTTP source; private simulator controls are not reader tools."""

import hmac
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from packages.domain.execution import PlanSubmission, ReplayStart, SimulatorCommand, TodayRunStart
from packages.domain.models import ConnectorCapabilities, Snapshot
from packages.persistence import connect
from packages.settings import Settings
from services.factory_sim import service
from services.factory_sim.engine import SimulationError
from services.factory_sim.storage import World


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings()
    url = config.factory_database_url.get_secret_value()
    engine = connect(url) if url else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        if engine:
            engine.dispose()

    app = FastAPI(title="BYOF simulated enterprise", version="1.8.0", lifespan=lifespan)
    app.state.engine = engine

    @app.exception_handler(SQLAlchemyError)
    async def unavailable(request: Request, exc: SQLAlchemyError):
        return JSONResponse({"code": "SOURCE_UNAVAILABLE"}, status_code=503)

    @app.exception_handler(SimulationError)
    async def invalid_action(request: Request, exc: SimulationError):
        return JSONResponse({"code": exc.code}, status_code=409)

    @app.exception_handler(ValidationError)
    async def invalid_contract(request: Request, exc: ValidationError):
        return JSONResponse(
            {
                "code": "INVALID_CONTROL_PAYLOAD",
                "issues": [
                    {"path": list(e["loc"]), "code": e["type"]}
                    for e in exc.errors(include_input=False, include_url=False)
                ],
            },
            status_code=422,
        )

    def reader(request: Request) -> None:
        expected = config.factory_api_token.get_secret_value()
        supplied = request.headers.get("authorization", "")
        if not expected or not hmac.compare_digest(supplied, "Bearer " + expected):
            raise HTTPException(401, "Source reader credential required")

    def execution_writer(request: Request) -> None:
        expected = config.factory_execution_token.get_secret_value()
        if (
            not expected
            or expected
            in {
                config.factory_api_token.get_secret_value(),
                config.factory_control_token.get_secret_value(),
            }
            or not hmac.compare_digest(
                request.headers.get("authorization", ""), "Bearer " + expected
            )
        ):
            raise HTTPException(403, "Execution writer credential required")

    def controller(request: Request) -> None:
        expected = config.factory_control_token.get_secret_value()
        if (
            not expected
            or expected
            in {
                config.factory_api_token.get_secret_value(),
                config.factory_execution_token.get_secret_value(),
            }
            or not hmac.compare_digest(
                request.headers.get("authorization", ""), "Bearer " + expected
            )
        ):
            raise HTTPException(403, "Simulator controller credential required")

    def snapshot(factory_id: str) -> Snapshot:
        if engine is None:
            raise HTTPException(503, "Source database unavailable")
        with Session(engine) as db:
            world = db.get(World, factory_id)
            if world is None:
                raise HTTPException(404, "Factory not found")
            return Snapshot.model_validate(world.document)

    @app.get("/health/ready")
    def ready():
        if engine is None:
            raise HTTPException(503, "Source database unavailable")
        with engine.connect() as connection:
            if not connection.scalar(text("SELECT to_regclass('factory_sim.worlds')")):
                raise HTTPException(503, "Migration required")
        return {"status": "ok"}

    @app.get("/factory/v1/capabilities", dependencies=[Depends(reader)])
    def capabilities() -> ConnectorCapabilities:
        execution_token = config.factory_execution_token.get_secret_value()
        can_accept = bool(execution_token) and execution_token not in {
            config.factory_api_token.get_secret_value(),
            config.factory_control_token.get_secret_value(),
        }
        return ConnectorCapabilities(
            read_snapshot=True,
            read_changes=True,
            query_detail=True,
            accept_plan=can_accept,
            query_action=True,
            idempotency=True,
            conditional_acceptance=can_accept,
            snapshot_consistency="ATOMIC_SNAPSHOT",
        )

    @app.get("/factory/v1/snapshot", dependencies=[Depends(reader)])
    def read_snapshot(factory_id: str = Query(min_length=1, max_length=160)) -> Snapshot:
        return snapshot(factory_id)

    @app.get("/factory/v1/changes", dependencies=[Depends(reader)])
    def read_changes(
        factory_id: str,
        run_id: str,
        after: int = Query(ge=0),
        limit: int = Query(default=20, ge=1, le=100),
    ):
        if engine is None:
            raise HTTPException(503, "Source database unavailable")
        return service.changes(engine, factory_id, run_id, after, limit)

    @app.post("/factory/v1/plans", dependencies=[Depends(execution_writer)])
    def submit(body: PlanSubmission):
        if engine is None:
            raise HTTPException(503, "Source database unavailable")
        return service.submit_plan(engine, body)

    @app.get("/factory/v1/actions/{operation_id}", dependencies=[Depends(reader)])
    def action(operation_id: str, factory_id: str, run_id: str):
        if engine is None:
            raise HTTPException(503, "Source database unavailable")
        receipt = service.query_action(engine, factory_id, run_id, operation_id)
        if receipt is None:
            raise HTTPException(404, "Action not found")
        return receipt

    @app.post("/simulator/v1/factories/{factory_id}/commands", dependencies=[Depends(controller)])
    def control(factory_id: str, body: SimulatorCommand):
        if engine is None:
            raise HTTPException(503, "Source database unavailable")
        return service.command(engine, factory_id, body)

    @app.post(
        "/simulator/v1/factories/{factory_id}/commands/cancel", dependencies=[Depends(controller)]
    )
    def cancel_treatment(factory_id: str, body: SimulatorCommand):
        if engine is None:
            raise HTTPException(503, "Source database unavailable")
        return service.cancel_treatment_command(engine, factory_id, body)

    @app.get("/simulator/v1/factories/{factory_id}", dependencies=[Depends(controller)])
    def control_status(factory_id: str):
        if engine is None:
            raise HTTPException(503, "Source database unavailable")
        with Session(engine) as db:
            world = db.get(World, factory_id)
            if world is None:
                raise HTTPException(404, "Factory not found")
            return {
                "factory_id": factory_id,
                "run_id": world.run_id,
                "mode": world.mode,
                "interval_ms": world.interval_ms,
                "scenario": world.scenario_state,
                "business_clock": world.business_clock,
                "server_time": datetime.now(UTC),
                "replay": (
                    {
                        key: world.replay_state.get(key)
                        for key in (
                            "origin_run_id",
                            "next_revision",
                            "target_revision",
                            "done",
                            "error_code",
                        )
                    }
                    if world.replay_state is not None
                    else None
                ),
            }

    @app.post("/simulator/v1/factories/{factory_id}/replays", dependencies=[Depends(controller)])
    def replay(factory_id: str, body: ReplayStart):
        from services.factory_sim.replay import start_replay

        if engine is None:
            raise HTTPException(503, "Source database unavailable")
        return start_replay(engine, factory_id, body.request_id, body.expected_run_id)

    @app.post("/simulator/v1/factories/{factory_id}/today-runs", dependencies=[Depends(controller)])
    def today_run(factory_id: str, body: TodayRunStart):
        from services.factory_sim.today_run import start_today_run

        if engine is None:
            raise HTTPException(503, "Source database unavailable")
        return start_today_run(
            engine, factory_id, body.request_id, body.expected_run_id, body.scenario_version
        )

    @app.get("/factory/v1/objects/{entity}/{identity}", dependencies=[Depends(reader)])
    def detail(entity: str, identity: str, factory_id: str):
        keys = {
            "orders": "order_id",
            "inventory": "material_id",
            "receipts": "receipt_id",
            "resources": "resource_id",
            "workers": "worker_id",
            "actuals": "operation_id",
        }
        if entity not in keys:
            raise HTTPException(404, "Unsupported entity")
        state = snapshot(factory_id)
        for item in getattr(state, entity):
            if getattr(item, keys[entity]) == identity:
                return {"record": item, "source": state.source, "run_id": state.run_id}
        raise HTTPException(404, "Object not found")

    return app


app = create_app()
