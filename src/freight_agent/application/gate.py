"""The gate: what does this event mean, and does it need a decision?

System events (risk change, timer, attachment) get their intent from code.
Only inbound messages call the gate model. Intents in `MUST_ACT` always go on
to decide, whatever the model said, so a weak model cannot drop a required
action; it can only fail to skip noise.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from freight_agent.application.context import RunContext
from freight_agent.application.prompts import GATE_SYSTEM_PROMPT, render_gate_prompt
from freight_agent.domain.events import EventType
from freight_agent.domain.intents import MUST_ACT, GateDecision, Intent
from freight_agent.domain.load import DelayRisk
from freight_agent.domain.obligations import ObligationKind
from freight_agent.domain.runs import Usage
from freight_agent.ports.llm import LanguageModel, LlmRequest, ModelRole

# What a due follow-up timer of each obligation kind means.
_FOLLOW_UP_INTENTS: Mapping[ObligationKind, Intent] = {
    ObligationKind.ETA_REQUEST: Intent.ETA_FOLLOW_UP_DUE,
}

# Intents a message can have. The others come only from system events; a model that
# returns one for a message is corrected to `other`, which still goes to decide.
MESSAGE_INTENTS = frozenset(
    {
        Intent.ETA_PROVIDED,
        Intent.ETA_DEFERRED,
        Intent.QUESTION,
        Intent.ACKNOWLEDGEMENT,
        Intent.STATUS_UPDATE,
        Intent.DOCUMENT,
        Intent.DRIVER_DEPARTED,
        Intent.OTHER,
    }
)


def follow_up_intent(kind: ObligationKind) -> Intent | None:
    return _FOLLOW_UP_INTENTS.get(kind)


@dataclass(frozen=True)
class GateResult:
    decision: GateDecision
    usage: Usage | None = None  # None when no model was called

    @property
    def should_act(self) -> bool:
        return self.decision.act or self.decision.intent in MUST_ACT


class Gate:
    def __init__(self, llm: LanguageModel, *, timeout_s: float = 60.0) -> None:
        self._llm = llm
        self._timeout_s = timeout_s

    async def classify(self, ctx: RunContext) -> GateResult:
        """`LlmError` propagates: the runtime escalates a model failure to a Hero."""
        decision = system_decision(ctx)
        if decision is not None:
            return GateResult(decision=decision)
        request = LlmRequest(
            role=ModelRole.GATE,
            system=GATE_SYSTEM_PROMPT,
            user=render_gate_prompt(ctx),
            max_tool_rounds=0,
            timeout_s=self._timeout_s,
        )
        response = await self._llm.generate(request, GateDecision)
        return GateResult(decision=_message_decision(response.parsed), usage=response.usage)


def system_decision(ctx: RunContext) -> GateDecision | None:
    """The intent of a system event, from code (docs/decisions.md). None for a message."""
    event = ctx.event
    match event.type:
        case EventType.INBOUND_MESSAGE:
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
    intent = follow_up_intent(kind) if kind is not None else None
    if intent is None:
        # A timer the agent set means something was meant to happen: let decide look.
        return GateDecision(intent=Intent.OTHER, act=True, reason="timer of an unknown kind")
    return GateDecision(intent=intent, act=True, reason=f"{kind} follow-up timer is due")


def _timer_kind(ctx: RunContext) -> ObligationKind | None:
    """From the event data, else from the obligation of the timer."""
    data = ctx.event.data
    raw = data.get("kind")
    if raw is not None:
        try:
            return ObligationKind(raw)
        except ValueError:
            return None
    obligation_id = ctx.event_timer.obligation_id if ctx.event_timer else data.get("obligation_id")
    obligation = ctx.obligation(obligation_id) if isinstance(obligation_id, str) else None
    return obligation.kind if obligation is not None else None


def _message_decision(decision: GateDecision) -> GateDecision:
    if decision.intent in MESSAGE_INTENTS:
        return decision
    return GateDecision(
        intent=Intent.OTHER,
        act=True,
        reason=f"model said {decision.intent.value} for a message: {decision.reason}",
    )
