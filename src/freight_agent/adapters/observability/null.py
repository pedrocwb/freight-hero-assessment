"""Tracer that links nothing. The default in tests and when tracing is off."""

from uuid import UUID


class NullTracer:
    async def trace_url(self, trace_id: UUID) -> str | None:
        return None
