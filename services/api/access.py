from fastapi import Request
from sqlalchemy.engine import Engine

from packages import auth
from packages.settings import Settings


def session_cookie_name(request: Request) -> str:
    # The header selects a cookie, never grants authority. Each token is authenticated below.
    surface = request.headers.get("x-byof-surface")
    if surface is None:
        return "byof_session"
    if surface not in {"agent", "simulator"}:
        raise auth.AccessError("INVALID_SURFACE", "The page entry is not recognized.", 400)
    return f"byof_{surface}_session"


def principal(request: Request, *, mutation: bool = False) -> auth.Principal:
    token = request.cookies.get(session_cookie_name(request))
    if not token:
        raise auth.AccessError("UNAUTHENTICATED", "Sign in first.", 401)
    config: Settings = request.app.state.settings
    engine: Engine | None = request.app.state.engine
    if engine is None:
        raise auth.AccessError(
            "SETUP_REQUIRED", "The database is not connected yet; contact the administrator.", 503
        )
    if mutation and request.headers.get("origin") != config.public_origin:
        raise auth.AccessError("INVALID_ORIGIN", "The request origin is not trusted.")
    csrf = request.headers.get("x-csrf-token", "") if mutation else None
    return auth.authenticate(engine, token, csrf)
