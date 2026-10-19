"""Follow-up timers and the rules that make stale timers harmless."""

from datetime import datetime, timedelta
from enum import StrEnum

from freight_agent.domain.base import DomainModel
from freight_agent.domain.obligations import Obligation


class TimerStatus(StrEnum):
    PENDING = "pending"
    FIRED = "fired"
    SUPERSEDED = "superseded"
    CANCELLED = "cancelled"


class Timer(DomainModel):
    timer_id: str
    load_id: str
    obligation_id: str
    due_at: datetime
    created_at: datetime
    status: TimerStatus = TimerStatus.PENDING
    reason: str = ""

    def mark(self, status: TimerStatus) -> "Timer":
        return self.model_copy(update={"status": status})


def is_stale(timer: Timer, obligation: Obligation | None) -> bool:
    """A timer may act only while it is the active timer of an open obligation."""
    if timer.status in (TimerStatus.SUPERSEDED, TimerStatus.CANCELLED):
        return True
    if obligation is None or not obligation.is_open:
        return True
    return obligation.active_timer_id != timer.timer_id


def follow_up_due_at(
    *, requested: datetime, deferred_until: datetime | None, grace: timedelta
) -> datetime:
    """Never chase before the SOP cadence, and never before the contact's promise plus grace."""
    if deferred_until is None:
        return requested
    return max(requested, deferred_until + grace)
