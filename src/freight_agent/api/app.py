"""The platform API: FastAPI over one in-process simulation (`docs/api.md`).

`create_app` is the only entry point. It builds the simulation from the settings through
the composition root, so the API runs the same `AgentRuntime` as the CLI and the evals,
on a virtual clock with recording gateways. Tests pass `llm` to script the agent.

When the web UI has been built (`pnpm build` in `web/`, or the Docker image), the same app
serves it, so one process on one port is the whole platform. Without a build the app is
the API alone, and the UI runs on the Vite dev server.
"""

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

from freight_agent.api.errors import install_error_handlers
from freight_agent.api.routes import ROUTERS
from freight_agent.api.simulation import DEFAULT_HEARTBEAT_S, SimulationService
from freight_agent.ports.llm import LanguageModel
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
    llm: LanguageModel | None = None,
    heartbeat_s: float = DEFAULT_HEARTBEAT_S,
    web_dist: Path | None = None,
) -> FastAPI:
    """The API with a fresh simulation at the default start (`POST /api/sim/reset` moves it).

    `llm` replaces the model of every simulation the app builds, whatever the mode says.
    `web_dist` is the built UI to serve; None means `settings.web_dist` (`WEB_DIST`, from
    the environment or `.env`). A folder with no `index.html` serves nothing.
    """
    settings = Settings() if settings is None else settings
    app = FastAPI(
        title="Freight agent platform",
        description="Drive a load through time, play its contacts and the Hero team, and "
        "inspect every agent run.",
        version="0.1.0",
    )
    app.state.service = SimulationService(settings, llm=llm)
    app.state.heartbeat_s = heartbeat_s
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(CORS_ORIGINS),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    install_error_handlers(app)

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

    for router in ROUTERS:
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
