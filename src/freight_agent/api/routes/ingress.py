"""Ingress: the contract with the integration layer (messages and location pings)."""

from typing import Annotated

from fastapi import APIRouter, Query

from freight_agent.api.deps import Service
from freight_agent.api.schemas import MessageIn, PingIn
from freight_agent.api.simulation import DEFAULT_LATENCY_S
from freight_agent.application.contracts import IngestResult

router = APIRouter(prefix="/v1", tags=["ingress"])

Latency = Annotated[
    float,
    Query(ge=0, le=86_400, description="Virtual seconds between the input and its run"),
]


@router.post("/events")
async def ingest_message(
    service: Service, message: MessageIn, latency_seconds: Latency = DEFAULT_LATENCY_S
) -> IngestResult:
    """An email, SMS or Slack message, deduplicated by `message_id`; then one tick."""
    return await service.ingest_message(message, latency_seconds)


@router.post("/pings")
async def ingest_ping(
    service: Service, ping: PingIn, latency_seconds: Latency = DEFAULT_LATENCY_S
) -> IngestResult:
    """A location ping with its delay risk; only a confirmed change to or from high runs
    the agent. Then one tick."""
    return await service.ingest_ping(ping, latency_seconds)
