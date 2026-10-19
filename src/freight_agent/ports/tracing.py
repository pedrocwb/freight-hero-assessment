"""Tracing port. Implementations must fail open: tracing never breaks a run."""

from typing import Protocol

from freight_agent.domain.runs import RunRecord


class Tracer(Protocol):
    async def record_run(self, run: RunRecord) -> str | None:
        """Export the run. Returns a trace URL when the backend has one."""
        ...
