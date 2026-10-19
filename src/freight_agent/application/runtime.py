"""AgentRuntime: the one facade the API, the CLI and the eval harness use.

The same object runs in production, in the simulation and in evals; only the
adapters passed in differ (composition root: `freight_agent.bootstrap`).
"""

from dataclasses import dataclass, field
from datetime import datetime

from freight_agent.application.contracts import (
    InboundMessage,
    IngestResult,
    LoadSnapshot,
    LocationPing,
)
from freight_agent.domain.load import Load
from freight_agent.domain.ping_filter import PingFilterConfig
from freight_agent.domain.runs import RunRecord
from freight_agent.domain.validation import ValidationConfig
from freight_agent.ports.clock import Clock, IdGenerator
from freight_agent.ports.gateways import Gateways
from freight_agent.ports.llm import LanguageModel
from freight_agent.ports.repositories import UnitOfWorkFactory
from freight_agent.ports.sop import SopRepository
from freight_agent.ports.tracing import Tracer


@dataclass(frozen=True)
class RuntimeConfig:
    ping_filter: PingFilterConfig = field(default_factory=PingFilterConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    recent_events_limit: int = 30
    run_deadline_s: float = 240.0
    max_delivery_attempts: int = 5


@dataclass(frozen=True)
class RuntimeDeps:
    uow_factory: UnitOfWorkFactory
    clock: Clock
    ids: IdGenerator
    llm: LanguageModel
    sop: SopRepository
    gateways: Gateways
    tracer: Tracer
    config: RuntimeConfig = field(default_factory=RuntimeConfig)


class AgentRuntime:
    def __init__(self, deps: RuntimeDeps) -> None:
        self._deps = deps

    async def register_load(self, load: Load) -> None:
        """Store the load metadata and start its risk tracker at the load's current risk."""
        raise NotImplementedError  # card APP-4

    async def ingest_message(self, message: InboundMessage) -> IngestResult:
        """Dedupe by message_id, append to the event log and enqueue, in one transaction."""
        raise NotImplementedError  # card APP-4

    async def ingest_ping(self, ping: LocationPing) -> IngestResult:
        """Update the risk tracker in code (no LLM).

        Only a confirmed transition into or out of HIGH becomes an event.
        """
        raise NotImplementedError  # card APP-4

    async def fire_due_timers(self) -> int:
        """Turn pending timers with due_at <= now into TIMER_DUE events. Returns how many fired."""
        raise NotImplementedError  # card APP-4

    async def process_next(self) -> RunRecord | None:
        """Claim the next queued event and run the agent graph on it."""
        raise NotImplementedError  # card APP-4

    async def process_all(self) -> list[RunRecord]:
        raise NotImplementedError  # card APP-4

    async def dispatch_outbox(self) -> int:
        """Deliver pending outbox items through the gateways. Returns how many were sent."""
        raise NotImplementedError  # card APP-4

    async def tick(self) -> list[RunRecord]:
        """fire_due_timers, then process_all, then dispatch_outbox."""
        raise NotImplementedError  # card APP-4

    async def next_timer_due_at(self) -> datetime | None:
        raise NotImplementedError  # card APP-4

    async def snapshot(self, load_id: str) -> LoadSnapshot:
        raise NotImplementedError  # card APP-4
