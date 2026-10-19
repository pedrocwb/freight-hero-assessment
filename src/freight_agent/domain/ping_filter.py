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


class RiskTransition(DomainModel):
    previous: DelayRisk
    current: DelayRisk

    @property
    def is_upgrade(self) -> bool:
        return _RANK[self.current] > _RANK[self.previous]


def observe(
    tracker: RiskTracker, risk: DelayRisk, config: PingFilterConfig
) -> tuple[RiskTracker, RiskTransition | None]:
    """Fold one ping's risk into the tracker. Returns the new tracker and a transition if any."""
    if risk is tracker.band:
        return RiskTracker(band=tracker.band), None

    count = tracker.pending_count + 1 if risk is tracker.pending else 1
    is_upgrade = _RANK[risk] > _RANK[tracker.band]
    required = config.upgrade_confirmations if is_upgrade else config.downgrade_confirmations

    if count >= required:
        return RiskTracker(band=risk), RiskTransition(previous=tracker.band, current=risk)
    return RiskTracker(band=tracker.band, pending=risk, pending_count=count), None
