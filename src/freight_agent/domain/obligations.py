"""Obligations: what the agent still owes or is owed (prospective memory).

The ledger stores facts only (status, attempts, times). It never stores a
pointer into the English SOP; the model reads the SOP plus these facts.
"""

from datetime import UTC, datetime, timedelta
from enum import StrEnum

from freight_agent.domain.base import DomainModel
from freight_agent.domain.errors import InvariantViolation


class ObligationKind(StrEnum):
    """The kinds of the standard catalog, as constants. A kind is a string id from the
    broker's catalog (`domain.broker.ObligationDef.kind`): brokers add their own."""

    ETA_REQUEST = "eta_request"


class ObligationStatus(StrEnum):
    OPEN = "open"
    SATISFIED = "satisfied"
    EXHAUSTED = "exhausted"
    CANCELLED = "cancelled"


CLOSED_STATUSES = frozenset(
    {ObligationStatus.SATISFIED, ObligationStatus.EXHAUSTED, ObligationStatus.CANCELLED}
)


class DeferralPolicy(DomainModel):
    max_minutes: int = 15
    max_per_obligation: int = 1
    grace_minutes: int = 2


class Obligation(DomainModel):
    obligation_id: str
    load_id: str
    kind: str
    status: ObligationStatus = ObligationStatus.OPEN
    attempts: int = 0
    opened_at: datetime
    last_action_at: datetime | None = None
    deferred_until: datetime | None = None
    deferrals_used: int = 0
    active_timer_id: str | None = None
    sop_version: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status is ObligationStatus.OPEN

    @classmethod
    def open(
        cls,
        *,
        obligation_id: str,
        load_id: str,
        kind: str,
        at: datetime,
        first_attempt: bool,
        sop_version: str | None = None,
    ) -> "Obligation":
        return cls(
            obligation_id=obligation_id,
            load_id=load_id,
            kind=kind,
            opened_at=at,
            attempts=1 if first_attempt else 0,
            last_action_at=at if first_attempt else None,
            sop_version=sop_version,
        )

    def record_attempt(self, at: datetime) -> "Obligation":
        self._require_open("record an attempt")
        return self.model_copy(update={"attempts": self.attempts + 1, "last_action_at": at})

    def defer(self, *, until: datetime, now: datetime, policy: DeferralPolicy) -> "Obligation":
        self._require_open("defer")
        if self.deferrals_used >= policy.max_per_obligation:
            raise InvariantViolation("deferral limit reached for this obligation")
        # Measured on UTC instants: Python subtracts two times of the same zone by wall
        # clock, which is an hour off when the wait crosses a DST change.
        wait = until.astimezone(UTC) - now.astimezone(UTC)
        if wait <= timedelta(0):
            raise InvariantViolation("deferral must end in the future")
        if wait > timedelta(minutes=policy.max_minutes):
            raise InvariantViolation(f"deferral longer than {policy.max_minutes} minutes")
        return self.model_copy(
            update={"deferred_until": until, "deferrals_used": self.deferrals_used + 1}
        )

    def attach_timer(self, timer_id: str) -> "Obligation":
        self._require_open("attach a timer")
        return self.model_copy(update={"active_timer_id": timer_id})

    def close(self, status: ObligationStatus) -> "Obligation":
        self._require_open("close")
        if status not in CLOSED_STATUSES:
            raise InvariantViolation(f"{status} is not a closing status")
        return self.model_copy(
            update={"status": status, "active_timer_id": None, "deferred_until": None}
        )

    def _require_open(self, operation: str) -> None:
        if not self.is_open:
            raise InvariantViolation(f"cannot {operation} on a {self.status} obligation")
