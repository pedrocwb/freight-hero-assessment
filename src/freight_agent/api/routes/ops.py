"""Health and the event stream."""

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from freight_agent.api.deps import HeartbeatSeconds, Service
from freight_agent.api.schemas import HealthReport

router = APIRouter(tags=["ops"])


@router.get("/health")
async def health(service: Service) -> HealthReport:
    """One verdict from our own data, with the settings (secrets only as set or unset)."""
    return await service.health()


@router.get("/stream", response_class=StreamingResponse)
async def stream(service: Service, heartbeat_s: HeartbeatSeconds) -> StreamingResponse:
    """Server-Sent Events: `run`, `outbox`, `load`, `clock` and `hero_task`, one JSON
    object per `data:` line, and a comment every 15 s of silence."""
    return StreamingResponse(
        service.stream(heartbeat_s=heartbeat_s),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
