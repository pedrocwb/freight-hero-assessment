"""The event stream."""

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from freight_agent.api.deps import HeartbeatSeconds, Service

router = APIRouter(tags=["ops"])


@router.get("/stream", response_class=StreamingResponse)
async def stream(service: Service, heartbeat_s: HeartbeatSeconds) -> StreamingResponse:
    """Server-Sent Events: `run`, `outbox`, `load`, `clock` and `scenario`, one JSON
    object per `data:` line, and a comment every 15 s of silence."""
    return StreamingResponse(
        service.stream(heartbeat_s=heartbeat_s),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
