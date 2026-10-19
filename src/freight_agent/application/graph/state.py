"""What one run carries through the graph, and the verdict it ends with.

The state is immutable: each node returns a partial update (`RunUpdate`) and
LangGraph builds the next state. Model usage accumulates across nodes; every
other key is written once. A node that ends the run writes a `Verdict`; the
conditional edges stop at the first one.
"""

import operator
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, TypedDict

from freight_agent.application.context import RunContext
from freight_agent.application.escalation import Escalation
from freight_agent.application.gate import GateResult
from freight_agent.application.prefilter import PrefilterResult
from freight_agent.domain.actions import Action, Plan
from freight_agent.domain.intents import GateDecision
from freight_agent.domain.runs import RunStatus, Usage
from freight_agent.domain.sop import ResolvedSection, ResolvedSop
from freight_agent.domain.validation import ValidationOutcome, Violation


@dataclass(frozen=True)
class Verdict:
    """How the run ends, and what the commit step executes, in order.

    `actions` are the approved actions of the plan followed by any Hero task the
    run adds; an action's index here is its outbox idempotency index, so the
    approved actions keep their positions from the approved list.
    """

    status: RunStatus
    actions: tuple[Action, ...] = ()
    skip_reason: str | None = None
    error: str | None = None  # the escalation reason, or the failure

    @classmethod
    def escalated(cls, escalation: Escalation, approved: Sequence[Action] = ()) -> "Verdict":
        return cls(
            status=RunStatus.ESCALATED,
            actions=(*approved, escalation.task),
            error=escalation.reason,
        )


@dataclass(frozen=True)
class RunState:
    ctx: RunContext
    prefilter: PrefilterResult | None = None
    gate: GateResult | None = None
    sop: ResolvedSop | None = None
    sop_version: str | None = None
    visible_sections: tuple[ResolvedSection, ...] = ()  # back the read_sop_section tool
    plan: Plan | None = None
    validation: ValidationOutcome | None = None
    usage: Annotated[tuple[Usage, ...], operator.add] = ()  # every model call, in order
    verdict: Verdict | None = None

    @property
    def gate_decision(self) -> GateDecision | None:
        return self.gate.decision if self.gate is not None else None

    @property
    def section_ids(self) -> tuple[str, ...]:
        return self.sop.section_ids if self.sop is not None else ()

    @property
    def violations(self) -> tuple[Violation, ...]:
        return self.validation.violations if self.validation is not None else ()


class RunUpdate(TypedDict, total=False):
    """What a node may write. `usage` is appended to; the other keys are set."""

    prefilter: PrefilterResult | None
    gate: GateResult
    sop: ResolvedSop
    sop_version: str
    visible_sections: tuple[ResolvedSection, ...]
    plan: Plan
    validation: ValidationOutcome
    usage: tuple[Usage, ...]
    verdict: Verdict


def need[T](value: T | None, what: str) -> T:
    """A value an earlier node must have written. Missing means the graph is miswired."""
    if value is None:
        raise RuntimeError(f"the run graph reached this step without {what}")
    return value
