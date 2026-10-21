"""Loads, fixtures and the per-load panels: the snapshot and the runs. The list of loads
is in `GET /api/sim/state`."""

from typing import Any

from fastapi import APIRouter

from freight_agent.api.deps import AppSettings, Service
from freight_agent.api.fixtures import fixture_catalog
from freight_agent.api.recording import RecordedStep
from freight_agent.api.routes.ingress import Latency
from freight_agent.api.schemas import (
    Fixture,
    LoadMessageIn,
    LoadPingIn,
    LoadSummary,
    RunView,
    StopNotesView,
    TurnResult,
)
from freight_agent.api.simulation import DEFAULT_LATENCY_S
from freight_agent.application.contracts import LoadSnapshot

router = APIRouter(tags=["loads"])


@router.post("/loads", status_code=201)
async def create_load(service: Service, metadata: dict[str, Any]) -> LoadSummary:
    """Register a load from the assignment's metadata JSON (plus optional `load_types`)."""
    return await service.create_load(metadata)


@router.get("/loads/{load_id}")
async def get_load(service: Service, load_id: str) -> LoadSnapshot:
    return await service.snapshot(load_id)


@router.get("/fixtures")
async def fixtures(settings: AppSettings) -> list[Fixture]:
    return fixture_catalog(settings.evals_dir)


@router.get("/loads/{load_id}/runs")
async def runs(service: Service, load_id: str) -> list[RunView]:
    """The run inspector: what each run saw, decided and cost."""
    return await service.runs(load_id)


@router.get("/loads/{load_id}/facility-memory")
async def facility_memory(service: Service, load_id: str) -> list[StopNotesView]:
    """The facility notes of each stop of the load: what earlier loads of its client
    taught about the place (read-only)."""
    return await service.facility_memory(load_id)


@router.post("/loads/{load_id}/messages")
async def send_message(
    service: Service,
    load_id: str,
    message: LoadMessageIn,
    latency_seconds: Latency = DEFAULT_LATENCY_S,
) -> TurnResult:
    """Send a message to the load and wait for what the agent does about it: the clock
    moves `latency_seconds`, the queue runs, and the runs come back with the clock."""
    return await service.message_turn(message.for_load(load_id), latency_seconds)


@router.post("/loads/{load_id}/pings")
async def send_ping(
    service: Service,
    load_id: str,
    ping: LoadPingIn,
    latency_seconds: Latency = DEFAULT_LATENCY_S,
) -> TurnResult:
    """A location ping and the run it causes, if its risk band changed."""
    return await service.ping_turn(ping.for_load(load_id), latency_seconds)


@router.get("/loads/{load_id}/recording")
async def recording(service: Service, load_id: str) -> list[RecordedStep]:
    """Every input sent to the load and every clock move that ran it, with what each
    changed: the steps a scenario saved from this conversation would have."""
    await service.snapshot(load_id)  # 404 for an unknown load
    return service.recording(load_id)
