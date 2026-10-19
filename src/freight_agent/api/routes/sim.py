"""Simulation and clock: reset, state, advance and tick."""

from typing import Annotated

from fastapi import APIRouter, Body

from freight_agent.api.deps import Service
from freight_agent.api.schemas import AdvanceRequest, ResetRequest, SimState, StepResult

router = APIRouter(prefix="/sim", tags=["simulation"])


@router.post("/reset")
async def reset(
    service: Service, request: Annotated[ResetRequest | None, Body()] = None
) -> SimState:
    """A fresh simulation (empty store, new clock). The old one stays on an error."""
    return await service.reset(request or ResetRequest())


@router.get("/state")
async def state(service: Service) -> SimState:
    return await service.state()


@router.post("/advance")
async def advance(service: Service, request: AdvanceRequest) -> StepResult:
    """Move the clock by `minutes` or `to` a time, stopping at every timer due on the way."""
    return await service.advance(request)


@router.post("/tick")
async def tick(service: Service) -> StepResult:
    """Fire due timers, run the queue and deliver the outbox, without moving the clock."""
    return await service.tick()
