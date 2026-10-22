"""Serve the broker workspace UI and forward live operations to Temporal ingress."""

import logging
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.routing import Match, Mount
from starlette.types import Scope

from freight_agent.api.routes.durable import router
from freight_agent.settings import Settings

logger = logging.getLogger(__name__)

# The Vite dev server of the platform UI (web/).
CORS_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")

API_PREFIX = "/api"
# The UI bundle `vite build` writes. A relative path resolves against the working
# directory, the repository root for `uv run agent serve` and /app in the image.
ASSETS_DIR = "assets"


def create_app(
    settings: Settings | None = None,
    *,
    web_dist: Path | None = None,
) -> FastAPI:
    """UI and fixed-upstream proxy; no local database, model or virtual clock.

    `web_dist` overrides WEB_DIST. A folder without index.html serves only the API.
    """
    settings = Settings() if settings is None else settings

    app = FastAPI(
        title="Freight agent platform",
        description="Live broker operations backed by Temporal workflows.",
        version="0.1.0",
    )
    app.state.durable_api_url = settings.durable_api_url
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(CORS_ORIGINS),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        # The exception text can hold a connection string, a prompt or a load's data: it
        # goes to the server log only, under an id the client can quote.
        error_id = uuid.uuid4().hex[:12]
        logger.error(
            "unexpected error %s on %s %s",
            error_id,
            request.method,
            request.url.path,
            exc_info=exc,
        )
        detail = f"internal error {error_id}: the server log has the details"
        return JSONResponse(status_code=500, content={"detail": detail, "error_id": error_id})

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        # Liveness only. /api/durable/health checks the separate ingress service.
        return {"status": "ok", "mode": "workspace-proxy"}

    app.include_router(router, prefix=API_PREFIX)
    serve_web_ui(app, settings.web_dist if web_dist is None else web_dist)
    return app


def serve_web_ui(app: FastAPI, dist: Path) -> None:
    """Mount the built UI at the root, after the API, when `dist` holds an `index.html`."""
    if not (dist / "index.html").is_file():
        logger.debug("no web UI build at %s: serving the API only", dist)
        return
    dist = dist.resolve()
    app.router.routes.append(
        _OutsideApiMount("/", app=_SinglePageFiles(directory=dist, html=True), name="web")
    )
    logger.info("serving the web UI from %s", dist)


class _SinglePageFiles(StaticFiles):
    """The UI's files, and `index.html` for any other path, so a deep link or a reload of a
    client-side route opens the app.

    A missing file under `assets/` stays a 404: a browser asking for an old hashed script
    must not get HTML back. Starlette keeps every lookup inside the folder.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or Path(path).parts[:1] == (ASSETS_DIR,):
                raise
            return await super().get_response("index.html", scope)


class _OutsideApiMount(Mount):
    """A root mount that never matches a path under /api (nor a websocket).

    So an unknown API path, a wrong method or a trailing slash gets exactly the answer it
    gets without the UI (404, 405, a redirect) instead of the page.
    """

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope["type"] != "http" or _is_api_path(_route_path(scope)):
            return Match.NONE, {}
        return super().matches(scope)


def _route_path(scope: Scope) -> str:
    """The request path without the ASGI `root_path` the app may be served under."""
    path: str = scope["path"]
    root: str = scope.get("root_path", "")
    return path[len(root) :] if root and path.startswith(root) else path


def _is_api_path(path: str) -> bool:
    return path == API_PREFIX or path.startswith(f"{API_PREFIX}/")
