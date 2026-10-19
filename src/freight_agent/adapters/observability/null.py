"""Tracer that exports nothing. The default in tests and when tracing is off."""

from freight_agent.domain.runs import RunRecord


class NullTracer:
    async def record_run(self, run: RunRecord) -> str | None:
        return None
