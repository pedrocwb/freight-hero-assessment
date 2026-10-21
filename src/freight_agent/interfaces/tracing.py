"""Tracing interface: the link to a run's trace. Implementations must fail open: tracing
never breaks a run.

The trace itself is LangGraph's and LangChain's own tracing (each graph node, each model
round, nested under the run's root span), switched on by the environment. The runtime
gives each run's root span its id (`RunGraph.stream(trace_id=...)`), so the link can be
built from that id alone.
"""

from typing import Protocol
from uuid import UUID


class Tracer(Protocol):
    async def trace_url(self, trace_id: UUID) -> str | None:
        """The trace whose root span has `trace_id`; None when there is none to show."""
        ...
