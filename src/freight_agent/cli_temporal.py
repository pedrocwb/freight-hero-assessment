"""Durable mode entry points, imported lazily so `agent --help` stays cheap."""

import asyncio
from typing import Annotated

import typer

from freight_agent.settings import Settings

temporal_app = typer.Typer(no_args_is_help=True, help="Durable per-load workflows (Postgres).")


@temporal_app.command("worker")
def worker() -> None:
    """Run LangGraph Activities and load Workflows."""
    from freight_agent.orchestration.worker import serve_worker

    asyncio.run(serve_worker(Settings()))


@temporal_app.command("relay")
def relay() -> None:
    """Repair missed notifications and extract facility memory. Run one per DB."""
    from freight_agent.orchestration.worker import serve_relay

    asyncio.run(serve_relay(Settings()))


@temporal_app.command("serve")
def serve(
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option()] = 8001,
) -> None:
    """Serve real-clock ingress. Use a separate DB from the simulation UI."""
    import uvicorn

    uvicorn.run("freight_agent.orchestration.api:create_app", factory=True, host=host, port=port)
