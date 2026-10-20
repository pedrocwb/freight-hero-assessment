"""What one run carries through the graph, and the verdict it ends with.

The state is immutable: each node returns a partial update (`RunUpdate`) and LangGraph
builds the next state. The annotated keys accumulate across nodes (model usage, the
agent loop's conversation, its trajectory and its staged proposals); every other key
is set. A node that ends the run writes a `Verdict`; the conditional
edges stop at the first one.
"""

import operator
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, TypedDict

from freight_agent.application.agent import Proposal
from freight_agent.application.context import RunContext
from freight_agent.application.escalation import Escalation
from freight_agent.application.gate import GateResult
from freight_agent.application.prefilter import PrefilterResult
from freight_agent.domain.actions import FINISH, Action, AgentOutcome, Plan
from freight_agent.domain.intents import GateDecision
from freight_agent.domain.runs import RunStatus, TrajectoryStep, Usage
from freight_agent.domain.sop import ResolvedSection, ResolvedSop
from freight_agent.domain.validation import ValidationOutcome, Violation
from freight_agent.ports.llm import AssistantTurn, ModelRole, ToolCall, Turn


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
    # The agent loop: the conversation after the prompts, the model rounds made, the
    # answer (`finish`), and what the tool calls staged and refused.
    messages: Annotated[tuple[Turn, ...], operator.add] = ()
    rounds: int = 0
    outcome: AgentOutcome | None = None
    proposals: Annotated[tuple[Proposal, ...], operator.add] = ()  # action calls, in order
    rejections: int = 0  # action tool calls refused, bad arguments included
    trajectory: Annotated[tuple[TrajectoryStep, ...], operator.add] = ()  # every tool call
    sections_searched: tuple[str, ...] = ()
    validation: ValidationOutcome | None = None
    usage: Annotated[tuple[Usage, ...], operator.add] = ()  # every model call, in order
    verdict: Verdict | None = None
    # The run's reading of the event: the triage intent, then the agent's catalog label.
    intent: str | None = None
    # The graph nodes the run entered, in order, one per visit. No node writes it:
    # `RunGraph.stream` fills it from LangGraph's task events.
    nodes: tuple[str, ...] = ()

    @property
    def gate_decision(self) -> GateDecision | None:
        return self.gate.decision if self.gate is not None else None

    @property
    def section_ids(self) -> tuple[str, ...]:
        return self.sop.section_ids if self.sop is not None else ()

    @property
    def violations(self) -> tuple[Violation, ...]:
        return self.validation.violations if self.validation is not None else ()

    @property
    def plan(self) -> Plan | None:
        """Every action the agent proposed, in call order, refused ones included, with the
        answer's rationale. None until the agent made a round. A plain `Plan`: the run
        record stores it, and it compares equal to the same plan read back."""
        if self.rounds == 0:
            return None
        rationale = self.outcome.rationale if self.outcome is not None else ""
        return Plan(rationale=rationale, actions=tuple(p.action for p in self.proposals))

    @property
    def no_procedure(self) -> bool:
        """The agent said no SOP section covers the event."""
        return self.outcome is not None and self.outcome.no_procedure

    @property
    def agent_usage(self) -> tuple[Usage, ...]:
        return tuple(u for u in self.usage if u.role == ModelRole.AGENT.value)

    @property
    def pending_calls(self) -> tuple[ToolCall, ...]:
        """The tool calls of the agent's last round, until the tools node answers them
        (the answer's own call aside, unless its arguments were broken)."""
        last = self.messages[-1] if self.messages else None
        if not isinstance(last, AssistantTurn):
            return ()
        return tuple(c for c in last.tool_calls if c.name != FINISH or c.error is not None)


class RunUpdate(TypedDict, total=False):
    """What a node may write. The annotated keys of `RunState` are appended to; the
    other keys are set."""

    prefilter: PrefilterResult | None
    gate: GateResult
    sop: ResolvedSop
    sop_version: str | None
    visible_sections: tuple[ResolvedSection, ...]
    messages: tuple[Turn, ...]
    rounds: int
    outcome: AgentOutcome
    proposals: tuple[Proposal, ...]
    rejections: int
    trajectory: tuple[TrajectoryStep, ...]
    sections_searched: tuple[str, ...]
    validation: ValidationOutcome
    usage: tuple[Usage, ...]
    verdict: Verdict
    intent: str | None


def need[T](value: T | None, what: str) -> T:
    """A value an earlier node must have written. Missing means the graph is miswired."""
    if value is None:
        raise RuntimeError(f"the run graph reached this step without {what}")
    return value
