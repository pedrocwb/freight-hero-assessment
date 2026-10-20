"""Complete mediation: code decides what a proposed plan may do.

Each rule is a small object (open for extension: add a rule, do not edit the
validator). Violating actions are dropped and reported; they are never edited.
`check_action` judges one action, so an agent tool can answer "accepted" or "refused
by <rule>" at the call; `validate` folds it over a whole plan.
"""

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import Field

from freight_agent.domain.actions import Action, Plan
from freight_agent.domain.base import DomainModel
from freight_agent.domain.load import Channel, Load
from freight_agent.domain.obligations import DeferralPolicy, Obligation


class Severity(StrEnum):
    LOW = "low"
    HIGH = "high"


class Violation(DomainModel):
    rule: str
    action_index: int
    message: str
    severity: Severity = Severity.LOW


class OutboundRecord(DomainModel):
    contact_id: str
    channel: Channel
    at: datetime


class ValidationConfig(DomainModel):
    sms_max_per_window: int = 3
    sms_window_minutes: int = 60
    deferral: DeferralPolicy = DeferralPolicy()
    # A safety net over every SOP, per obligation kind: the SOP still sets the real number
    # of requests per client. `Broker.validation_config` fills it from the catalog.
    max_attempts: dict[str, int] = Field(default_factory=lambda: {"eta_request": 2})

    def attempt_cap(self, kind: str) -> int | None:
        """The most requests an obligation of `kind` may get; None: no cap."""
        return self.max_attempts.get(kind)


class ValidationContext(DomainModel):
    load: Load
    now: datetime
    open_obligations: tuple[Obligation, ...] = ()
    recent_outbound: tuple[OutboundRecord, ...] = ()
    config: ValidationConfig = ValidationConfig()


class Rule(Protocol):
    name: str

    def check(
        self,
        action: Action,
        index: int,
        ctx: ValidationContext,
        approved: Sequence[Action],
    ) -> Violation | None: ...


class ValidationOutcome(DomainModel):
    approved: tuple[Action, ...]
    violations: tuple[Violation, ...] = ()

    @property
    def has_high_severity(self) -> bool:
        return any(v.severity is Severity.HIGH for v in self.violations)


def check_action(
    action: Action,
    ctx: ValidationContext,
    approved: Sequence[Action],
    rules: Sequence[Rule],
    *,
    index: int | None = None,
) -> tuple[Violation, ...]:
    """Every violation of one action, judged after the `approved` ones. Empty: approved.

    `index` is the action's position in its plan (default: right after `approved`).
    """
    at = len(approved) if index is None else index
    return tuple(v for r in rules if (v := r.check(action, at, ctx, approved)) is not None)


def validate(plan: Plan, ctx: ValidationContext, rules: Sequence[Rule]) -> ValidationOutcome:
    approved: list[Action] = []
    violations: list[Violation] = []
    for index, action in enumerate(plan.actions):
        found = check_action(action, ctx, approved, rules, index=index)
        if found:
            violations.extend(found)
        else:
            approved.append(action)
    return ValidationOutcome(approved=tuple(approved), violations=tuple(violations))
