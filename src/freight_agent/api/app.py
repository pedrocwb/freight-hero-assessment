"""The platform API: FastAPI over one in-process simulation (`docs/api.md`).

`create_app` is the only entry point. It builds the simulation from the settings through
the composition root, so the API runs the same `AgentRuntime` as the CLI and the evals,
on a virtual clock with recording gateways. Tests pass `llm` to script the agent.
"""

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from freight_agent.api.errors import install_error_handlers
from freight_agent.api.routes import ROUTERS
from freight_agent.api.simulation import DEFAULT_HEARTBEAT_S, SimulationService
from freight_agent.ports.llm import LanguageModel
from freight_agent.settings import Settings

logger = logging.getLogger(__name__)

# The Vite dev server of the platform UI (web/).
CORS_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


def create_app(
    settings: Settings | None = None,
    *,
    llm: LanguageModel | None = None,
    heartbeat_s: float = DEFAULT_HEARTBEAT_S,
) -> FastAPI:
    """The API with a fresh simulation at the default start (`POST /api/sim/reset` moves it).

    `llm` replaces the model of every simulation the app builds, whatever the mode says.
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
        app.include_router(router, prefix="/api")
    return app
