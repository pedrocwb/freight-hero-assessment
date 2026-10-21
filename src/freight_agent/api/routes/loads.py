"""Loads, fixtures and the per-load panels: the snapshot and the runs. The list of loads
is in `GET /api/sim/state`."""

from typing import Any

from fastapi import APIRouter

from freight_agent.api.deps import AppSettings, Service
from freight_agent.api.fixtures import fixture_catalog
from freight_agent.api.schemas import Fixture, LoadSummary, RunView
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
