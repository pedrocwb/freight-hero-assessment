"""The gate (the graph's triage step): what does this event mean, and does it need the agent?

System events (risk change, timer, attachment, processed document) get their intent
from code; a timer's intent is the follow-up intent of its obligation kind in the
broker's catalog. Only inbound messages call the gate model, on the gate's template
(`ports.prompts`, `prompts/gate.yaml`) rendered with the broker's message intents
(`prompts.gate_system_variables`) and the message. The catalog says
which intents a message can have and which must act (`IntentDef.must_act`): those always
go on to the agent, whatever the model said, so a weak model cannot drop a required
action; it can only fail to skip noise. A broker with `triage` off skips the model: every
message goes to the agent as `other`, and the agent labels it.

This module's `Gate` is the `llm` engine: the gate model writes its decision. The `jev`
engine (`gate_decision.DecisionGate`) asks a decision model for probabilities instead,
and code decides. Both are a `Triage`, which the run graph's triage step calls, and both
leave system events and untriaged brokers to code, the same way.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from freight_agent.application.context import RunContext
from freight_agent.application.prompts import (
    GATE_SYSTEM_VARIABLES,
    GATE_USER_VARIABLES,
    check_variables,
    render_gate_prompt,
    render_gate_system_prompt,
)
from freight_agent.domain.broker import Broker, standard_broker
from freight_agent.domain.events import EventType
from freight_agent.domain.intents import GateDecision, GateScores, Intent
from freight_agent.domain.load import DelayRisk
from freight_agent.domain.runs import Usage
from freight_agent.ports.llm import LanguageModel, LlmRequest, ModelRole
from freight_agent.ports.prompts import ChatPrompt


def follow_up_intent(kind: str, broker: Broker | None = None) -> str | None:
    """What a due follow-up timer of an obligation of `kind` means, in the broker's
    catalog (default: the standard one). None for a kind the catalog does not have."""
    return (broker or standard_broker()).follow_up_intent(kind)


class GateEngine(StrEnum):
    """What reads an inbound message at the gate (the `GATE_ENGINE` setting)."""

    LLM = "llm"  # a chat model writes the decision (`Gate`)
    JEV = "jev"  # a decision model's probabilities, and a policy in code (`DecisionGate`)


# A message of a broker that skips triage: the agent reads it and labels it.
UNTRIAGED = GateDecision(
    intent=Intent.OTHER, act=True, reason="triage is off for this broker: the agent reads it"
)


@dataclass(frozen=True)
class GateResult:
    decision: GateDecision
    usage: Usage | None = None  # None when no model was called
    broker: Broker = field(default_factory=standard_broker)  # whose catalog says must_act
    engine: GateEngine | None = None  # what read the message; None when code decided
    scores: GateScores | None = None  # what a decision model weighed (the jev engine)

    @property
    def should_act(self) -> bool:
        return self.decision.act or self.broker.must_act(self.decision.intent)


class Triage(Protocol):
    """A gate engine, as the run graph's triage step calls it."""

    @property
    def engine(self) -> GateEngine: ...

    @property
    def prompt(self) -> ChatPrompt:
        """The template its model calls render; its role names it in a run record."""
        ...

    def calls_model(self, ctx: RunContext) -> bool: ...

    async def classify(self, ctx: RunContext) -> GateResult: ...


def calls_gate_model(ctx: RunContext) -> bool:
    """Whether the event goes to the gate's model: a message whose meaning code does not
    know, for a broker that triages."""
    return system_decision(ctx) is None and ctx.broker.triage


def check_gate_prompt(prompt: ChatPrompt) -> ChatPrompt:
    """`prompt`, or `PromptError` when it names a variable the gate does not compute."""
    return check_variables(prompt, system=GATE_SYSTEM_VARIABLES, user=GATE_USER_VARIABLES)


class Gate:
    """The `llm` engine: the gate model writes its decision (intent, act, reason)."""

    engine = GateEngine.LLM

    def __init__(self, llm: LanguageModel, prompt: ChatPrompt, *, timeout_s: float = 60.0) -> None:
        """`PromptError` when the template does not fit the gate (`check_gate_prompt`)."""
        self._llm = llm
        self.prompt = check_gate_prompt(prompt)
        self._timeout_s = timeout_s

    @staticmethod
    def calls_model(ctx: RunContext) -> bool:
        return calls_gate_model(ctx)

    async def classify(self, ctx: RunContext) -> GateResult:
        """`LlmError` propagates: the runtime escalates a model failure to a Hero."""
        decision = system_decision(ctx)
        if decision is not None:
            return GateResult(decision=decision, broker=ctx.broker)
        if not ctx.broker.triage:
            return GateResult(decision=UNTRIAGED, broker=ctx.broker)
        request = LlmRequest(
            role=ModelRole.GATE,
            system=render_gate_system_prompt(self.prompt, ctx.broker),
            user=render_gate_prompt(self.prompt, ctx),
            timeout_s=self._timeout_s,
            prompt_version=self.prompt.version,
        )
        response = await self._llm.generate(request, GateDecision)
        decision = _message_decision(response.parsed, ctx.broker)
        return GateResult(
            decision=decision, usage=response.usage, broker=ctx.broker, engine=self.engine
        )


def system_decision(ctx: RunContext) -> GateDecision | None:
    """The intent of a system event, from code (docs/decisions.md). None for a message."""
    event = ctx.event
    match event.type:
        case EventType.INBOUND_MESSAGE:
            if event.attachments and not event.body.strip():
                # A photo with no text: what it is does not depend on a model's reading.
                return GateDecision(
                    intent=Intent.DOCUMENT, act=True, reason="attachment with no text"
                )
            return None
        case EventType.RISK_CHANGED:
            return _risk_decision(event.data)
        case EventType.TIMER_DUE:
            return _timer_decision(ctx)
        case EventType.ATTACHMENT_RECEIVED:
            return GateDecision(intent=Intent.DOCUMENT, act=True, reason="attachment received")
        case EventType.HERO_UPDATE:
            return GateDecision(
                intent=Intent.OTHER, act=False, reason="a Hero update is handled by people"
            )
        case EventType.AGENT_ACTION:
            return GateDecision(
                intent=Intent.OTHER, act=False, reason="the agent's own action is memory only"
            )
        case EventType.DOCUMENT_PROCESSED:
            return GateDecision(
                intent=Intent.DOCUMENT_PROCESSED, act=True, reason="a document was processed"
            )


def _risk_decision(data: Mapping[str, object]) -> GateDecision:
    previous, current = _risk(data.get("previous")), _risk(data.get("current"))
    if current is DelayRisk.HIGH:
        return GateDecision(intent=Intent.DELAY_RISK_HIGH, act=True, reason="delay risk is high")
    if previous is DelayRisk.HIGH and current is not None:
        return GateDecision(
            intent=Intent.DELAY_RISK_LOW, act=True, reason=f"delay risk dropped to {current}"
        )
    return GateDecision(
        intent=Intent.OTHER, act=False, reason="the risk change does not cross high"
    )


def _risk(value: object) -> DelayRisk | None:
    try:
        return DelayRisk(value)
    except ValueError:
        return None


def _timer_decision(ctx: RunContext) -> GateDecision:
    kind = _timer_kind(ctx)
    intent = ctx.broker.follow_up_intent(kind) if kind is not None else None
    if intent is None:
        # A timer the agent set means something was meant to happen: let the agent look.
        return GateDecision(intent=Intent.OTHER, act=True, reason="timer of an unknown kind")
    return GateDecision(intent=intent, act=True, reason=f"{kind} follow-up timer is due")


def _timer_kind(ctx: RunContext) -> str | None:
    """From the event data, else from the obligation of the timer."""
    data = ctx.event.data
    raw = data.get("kind")
    if raw is not None:
        return raw if isinstance(raw, str) else None
    obligation_id = ctx.event_timer.obligation_id if ctx.event_timer else data.get("obligation_id")
    obligation = ctx.obligation(obligation_id) if isinstance(obligation_id, str) else None
    return obligation.kind if obligation is not None else None


def _message_decision(decision: GateDecision, broker: Broker) -> GateDecision:
    """A message can only have a message intent of the catalog. Any other answer (a
    system intent, an id the catalog lacks) is corrected to `other`, which still goes on
    to the agent."""
    if decision.intent in {i.intent_id for i in broker.message_intents()}:
        return decision
    return GateDecision(
        intent=Intent.OTHER,
        act=True,
        reason=f"model said {decision.intent} for a message: {decision.reason}",
    )
