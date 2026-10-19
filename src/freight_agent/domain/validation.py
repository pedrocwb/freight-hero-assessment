"""Complete mediation: code decides what a proposed plan may do.

Each rule is a small object (open for extension: add a rule, do not edit the
validator). Violating actions are dropped and reported; they are never edited.
"""

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Protocol

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


def validate(plan: Plan, ctx: ValidationContext, rules: Sequence[Rule]) -> ValidationOutcome:
    approved: list[Action] = []
    violations: list[Violation] = []
    for index, action in enumerate(plan.actions):
        found = [v for r in rules if (v := r.check(action, index, ctx, approved)) is not None]
        if found:
            violations.extend(found)
        else:
            approved.append(action)
    return ValidationOutcome(approved=tuple(approved), violations=tuple(violations))
