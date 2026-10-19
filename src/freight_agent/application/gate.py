"""The gate: what does this event mean, and does it need a decision?

System events (risk change, timer, attachment) get their intent from code.
Only inbound messages call the gate model. Intents in `MUST_ACT` always go on
to decide, whatever the model said, so a weak model cannot drop a required
action; it can only fail to skip noise.
"""

from dataclasses import dataclass

from freight_agent.application.context import RunContext
from freight_agent.domain.intents import MUST_ACT, GateDecision
from freight_agent.domain.runs import Usage
from freight_agent.ports.llm import LanguageModel


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
        raise NotImplementedError  # card APP-3
