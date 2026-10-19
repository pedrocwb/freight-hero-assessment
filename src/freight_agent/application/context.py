"""What one run may read, loaded once at the start of the run.

Every later step (prefilter, gate, SOP resolution, decide, validate, execute)
reads this snapshot instead of querying the repositories again, so a run sees
one consistent view of the load's memory.
"""

from dataclasses import dataclass
from datetime import datetime

from freight_agent.domain.events import Event
from freight_agent.domain.load import Load
from freight_agent.domain.obligations import Obligation, ObligationKind
from freight_agent.domain.ping_filter import RiskTracker
from freight_agent.domain.timers import Timer
from freight_agent.domain.validation import OutboundRecord
from freight_agent.ports.repositories import UnitOfWork


@dataclass(frozen=True)
class RunContext:
    event: Event
    load: Load
    now: datetime
    obligations: tuple[Obligation, ...]  # every obligation of the load, oldest first
    pending_timers: tuple[Timer, ...]  # earliest first
    recent_events: tuple[Event, ...]  # the last N events of the load, oldest first, incl. `event`
    recent_outbound: tuple[OutboundRecord, ...]  # SMS, email and Slack sent in the rate window
    event_timer: Timer | None = None  # for TIMER_DUE events: the timer named in `event.data`
    tracker: RiskTracker | None = None
    already_processed: bool = False  # a run record already exists for this event

    @property
    def open_obligations(self) -> tuple[Obligation, ...]:
        return tuple(o for o in self.obligations if o.is_open)

    def open_obligation(self, kind: ObligationKind) -> Obligation | None:
        return next((o for o in self.open_obligations if o.kind is kind), None)

    def obligation(self, obligation_id: str) -> Obligation | None:
        return next((o for o in self.obligations if o.obligation_id == obligation_id), None)


async def load_run_context(
    uow: UnitOfWork,
    event: Event,
    *,
    now: datetime,
    recent_limit: int,
    outbound_window_minutes: int,
) -> RunContext:
    """Read the load, its ledger, timers, recent events and recent outbound messages."""
    raise NotImplementedError  # card APP-2
