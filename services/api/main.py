"""HTTP entry point; long-running work belongs to durable workers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from packages import auth
from packages.domain.models import FactoryProfile, canonical_hash
from packages.integrations.factory_http import CONTROL_ERRORS, ConnectorError
from packages.persistence import connect
from packages.settings import Settings
from services.api.access import principal, session_cookie_name
from services.api.assistant import router as assistant_router
from services.api.cases import router as cases_router
from services.api.models import router as models_router
from services.api.notifications import router as notifications_router
from services.api.planning import router as planning_router
from services.api.preferences import router as preferences_router


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class RoleSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    role: Literal["manager", "maintainer"]


class SystemStatus(BaseModel):
    name: Literal["BYOF"] = "BYOF"
    version: Literal["1.0"] = "1.0"
    state: Literal["setup_required", "ready"]
    message: str


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings()
    engine: Engine | None = (
        connect(config.database_url.get_secret_value())
        if config.database_url.get_secret_value()
        else None
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        yield
        if engine:
            engine.dispose()

    app = FastAPI(title="BYOF Agent API", version="1.0.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.settings = config
    app.include_router(planning_router)
    app.include_router(assistant_router)
    app.include_router(cases_router)
    app.include_router(notifications_router)
    app.include_router(models_router)
    app.include_router(preferences_router)

    def database() -> Engine:
        if engine is None:
            raise auth.AccessError(
                "SETUP_REQUIRED",
                "The database is not connected yet; contact the administrator.",
                503,
            )
        return engine

    def require_origin(request: Request) -> None:
        if request.headers.get("origin") != config.public_origin:
            raise auth.AccessError(
                "INVALID_ORIGIN", "The request origin is not trusted; act again from the workbench."
            )

    @app.exception_handler(auth.AccessError)
    async def access_error(request: Request, exc: auth.AccessError) -> JSONResponse:
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=exc.status)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        return JSONResponse(
            {
                "code": "DATABASE_UNAVAILABLE",
                "message": "The database is temporarily unavailable; try again shortly.",
            },
            status_code=503,
        )

    @app.exception_handler(ConnectorError)
    async def source_error(request: Request, exc: ConnectorError) -> JSONResponse:
        return JSONResponse(
            {
                "code": exc.code,
                "message": CONTROL_ERRORS.get(
                    exc.code,
                    "The factory interface is temporarily unavailable or its data failed validation; check the connection and retry.",
                ),
            },
            status_code=exc.status,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic errors may contain input credentials, so only expose field locations and codes.
        return JSONResponse(
            {
                "code": "INVALID_INPUT",
                "message": "The input format is invalid; check it and retry.",
                "fields": [{"path": list(e["loc"]), "type": e["type"]} for e in exc.errors()],
            },
            status_code=422,
        )

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    def ready() -> bool:
        if engine is None:
            return False
        try:
            with engine.connect() as connection:
                return bool(connection.scalar(text("SELECT to_regclass('byof.sessions')")))
        except SQLAlchemyError:
            return False

    @app.get("/health/ready")
    def readiness(response: Response) -> dict[str, str]:
        if not ready():
            response.status_code = 503
            return {"status": "unavailable"}
        return {"status": "ok"}

    @app.get("/api/system/status", response_model=SystemStatus)
    def system_status() -> SystemStatus:
        is_ready = ready()
        return SystemStatus(
            state="ready" if is_ready else "setup_required",
            message="Service connected."
            if is_ready
            else "The database is not ready; ask the administrator to check the connection and setup.",
        )

    def set_session_cookie(response: Response, token: str, name: str = "byof_session") -> None:
        response.set_cookie(
            name,
            token,
            httponly=True,
            secure=config.environment == "production",
            samesite="strict",
            max_age=8 * 3600,
            path="/api",
        )

    @app.post("/api/role-session")
    def role_session(body: RoleSelection, request: Request, response: Response) -> dict[str, str]:
        require_origin(request)
        name = session_cookie_name(request)
        surface = request.headers.get("x-byof-surface")
        if surface and body.role != ("manager" if surface == "agent" else "maintainer"):
            raise auth.AccessError(
                "INVALID_ROLE", "The identity does not match the page entry.", 422
            )
        token, csrf = auth.select_demo_role(database(), body.role)
        set_session_cookie(response, token, name)
        return {"csrf_token": csrf}

    @app.post("/api/login")
    def login(body: LoginInput, request: Request, response: Response) -> dict[str, str]:
        require_origin(request)
        if not config.legacy_password_login_enabled:
            raise auth.AccessError(
                "PASSWORD_LOGIN_DISABLED",
                "Choose the manager or the disruption simulator from the workbench.",
                404,
            )
        token, csrf = auth.login(database(), body.username, body.password)
        set_session_cookie(response, token)
        return {"csrf_token": csrf}

    @app.get("/api/session", response_model=auth.Principal)
    def session(request: Request) -> auth.Principal:
        token = request.cookies.get(session_cookie_name(request))
        if not token:
            raise auth.AccessError("UNAUTHENTICATED", "Sign in first.", 401)
        return auth.authenticate(database(), token)

    @app.post("/api/logout")
    def logout(request: Request, response: Response) -> dict[str, str]:
        require_origin(request)
        name = session_cookie_name(request)
        token = request.cookies.get(name, "")
        auth.authenticate(database(), token, request.headers.get("x-csrf-token", ""))
        auth.logout(database(), token)
        response.delete_cookie(name, path="/api")
        return {"status": "signed_out"}

    @app.post("/api/csrf")
    def csrf(request: Request) -> dict[str, str]:
        require_origin(request)
        return {
            "csrf_token": auth.renew_csrf(
                database(), request.cookies.get(session_cookie_name(request), "")
            )
        }

    @app.post("/api/factory-profiles/validate")
    def validate_profile(body: FactoryProfile, request: Request) -> dict[str, object]:
        actor = principal(request, mutation=True)
        actor.require(body.factory_id, {"admin"})
        return {
            "status": "STRUCTURALLY_VALID",
            "profile_hash": canonical_hash(body),
            "activation_allowed": False,
            "message": "The structure check passed; runtime capability verification and administrator activation are still required.",
        }

    return app


app = create_app()
