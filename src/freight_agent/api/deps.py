"""FastAPI dependencies: the objects `create_app` keeps on `app.state`."""

from typing import Annotated

from fastapi import Depends, Request

from freight_agent.api.simulation import SimulationService
from freight_agent.settings import Settings


def _service(request: Request) -> SimulationService:
    return request.app.state.service


def _settings(request: Request) -> Settings:
    return request.app.state.service.settings


def _heartbeat(request: Request) -> float:
    return request.app.state.heartbeat_s


Service = Annotated[SimulationService, Depends(_service)]
AppSettings = Annotated[Settings, Depends(_settings)]
HeartbeatSeconds = Annotated[float, Depends(_heartbeat)]
