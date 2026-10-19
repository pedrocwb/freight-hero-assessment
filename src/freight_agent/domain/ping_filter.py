"""Turns a stream of location pings into rare risk transitions.

Upgrades are confirmed quickly (acting late is costly) and downgrades slowly
(closing an obligation too early is costly), so risk flapping around a band
boundary does not open and close obligations repeatedly.
"""

from pydantic import model_validator

from freight_agent.domain.base import DomainModel
from freight_agent.domain.load import DelayRisk

_RANK = {DelayRisk.LOW: 0, DelayRisk.MEDIUM: 1, DelayRisk.HIGH: 2}


class PingFilterConfig(DomainModel):
    upgrade_confirmations: int = 1
    downgrade_confirmations: int = 2

    @model_validator(mode="after")
    def _positive(self) -> "PingFilterConfig":
        if self.upgrade_confirmations < 1 or self.downgrade_confirmations < 1:
            raise ValueError("confirmation counts must be at least 1")
        return self


class RiskTracker(DomainModel):
    band: DelayRisk
    pending: DelayRisk | None = None
    pending_count: int = 0
    # The ids of the last pings folded in, oldest first. The integration layer delivers
    # at least once, and a ping counted twice would confirm a downgrade on one reading.
    # Kept with the tracker, so the dedupe is stored in the same unit of work and holds
    # across restarts and processes.
    recent_ping_ids: tuple[str, ...] = ()

    def has_seen(self, ping_id: str) -> bool:
        return ping_id in self.recent_ping_ids

    def remember(self, ping_id: str, *, keep: int) -> "RiskTracker":
        """The tracker with `ping_id` as its newest ping id, holding at most `keep` ids."""
        kept = (*self.recent_ping_ids, ping_id)[-keep:] if keep > 0 else ()
        return self.model_copy(update={"recent_ping_ids": kept})


class RiskTransition(DomainModel):
    previous: DelayRisk
    current: DelayRisk

    @property
    def is_upgrade(self) -> bool:
        return _RANK[self.current] > _RANK[self.previous]


def observe(
    tracker: RiskTracker, risk: DelayRisk, config: PingFilterConfig
) -> tuple[RiskTracker, RiskTransition | None]:
    """Fold one ping's risk into the tracker. Returns the new tracker and a transition if any.

    Only the count changes: the remembered ping ids are the caller's (`remember`).
    """
    if risk is tracker.band:
        return _counting(tracker, tracker.band), None

    count = tracker.pending_count + 1 if risk is tracker.pending else 1
    is_upgrade = _RANK[risk] > _RANK[tracker.band]
    required = config.upgrade_confirmations if is_upgrade else config.downgrade_confirmations

    if count >= required:
        return _counting(tracker, risk), RiskTransition(previous=tracker.band, current=risk)
    return _counting(tracker, tracker.band, pending=risk, count=count), None


def _counting(
    tracker: RiskTracker, band: DelayRisk, *, pending: DelayRisk | None = None, count: int = 0
) -> RiskTracker:
    return RiskTracker(
        band=band, pending=pending, pending_count=count, recent_ping_ids=tracker.recent_ping_ids
    )
