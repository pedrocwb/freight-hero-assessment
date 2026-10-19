"""Loads, fixtures and the per-load panels: snapshot, conversation, TMS notes, runs."""

from typing import Any

from fastapi import APIRouter

from freight_agent.api.deps import AppSettings, Service
from freight_agent.api.fixtures import fixture_catalog
from freight_agent.api.schemas import (
    ConversationChannel,
    ConversationEntry,
    Fixture,
    LoadSummary,
    RunView,
    TmsNote,
)
from freight_agent.application.contracts import LoadSnapshot

router = APIRouter(tags=["loads"])


@router.post("/loads", status_code=201)
async def create_load(service: Service, metadata: dict[str, Any]) -> LoadSummary:
    """Register a load from the assignment's metadata JSON (plus optional `load_types`)."""
    return await service.create_load(metadata)


@router.get("/loads")
async def list_loads(service: Service) -> list[LoadSummary]:
    return await service.loads()


@router.get("/loads/{load_id}")
async def get_load(service: Service, load_id: str) -> LoadSnapshot:
    return await service.snapshot(load_id)


@router.get("/fixtures")
async def fixtures(settings: AppSettings) -> list[Fixture]:
    return fixture_catalog(settings.evals_dir)


@router.get("/loads/{load_id}/conversation", response_model_exclude_none=True)
async def conversation(
    service: Service, load_id: str, channel: ConversationChannel | None = None
) -> list[ConversationEntry]:
    """Inbound messages and what the agent sent, oldest first; every channel by default."""
    return await service.conversation(load_id, channel)


@router.get("/loads/{load_id}/tms")
async def tms(service: Service, load_id: str) -> list[TmsNote]:
    return await service.tms(load_id)


@router.get("/loads/{load_id}/runs")
async def runs(service: Service, load_id: str) -> list[RunView]:
    """The run inspector: what each run saw, decided and cost."""
    return await service.runs(load_id)
