"""The gate on a decision model (the `jev` engine): probabilities in, a policy in code.

The `llm` engine (`gate.Gate`) asks a chat model to write its decision. This one asks a
decision model (`interfaces.decisions`: TypeSafe's Jev) two questions about the same context
the gate prompt renders (the load, the open obligations, the recent events, the
message), in one call:

- `intent`, a choice over the broker's message intents, in their priority order, each
  described by its catalog description;
- `act`, yes or no: does the message need the agent to act now.

The model answers with probabilities and writes no reason. The policy is code: the run
ends as a no-op only when the most likely intent is one the catalog lets end a run
without action (`noop_eligible`, and not `must_act`) and P(act) is under the threshold
(`GATE_ACT_THRESHOLD`, 0.2 by default). Everything else goes on to the agent, so an
uncertain reading fails safe: the agent reads the message, and it can still decide to
do nothing. The probabilities go on the run record (`RunRecord.gate_scores`) and in the
trace; the decision's reason is a sentence code writes from them, which the agent reads.

The texts are prompt text, versioned like the other templates (`prompts/gate_decision.yaml`,
role `gate_decision`): the system message is the state's preamble (rendered from the
broker, with the gate's system variables), the user message the context (the gate's user
variables), and the template's `tools` hold the questions' texts by key: `intent` (the
choice's instructions), `act`, `act_yes` and `act_no` (the yes/no question and its
criteria).
"""

from freight_agent.application.context import RunContext
from freight_agent.application.gate import (
    UNTRIAGED,
    GateEngine,
    GateResult,
    calls_gate_model,
    system_decision,
)
from freight_agent.application.prompts import (
    GATE_SYSTEM_VARIABLES,
    GATE_USER_VARIABLES,
    check_variables,
    render_gate_prompt,
    render_gate_system_prompt,
)
from freight_agent.domain.broker import Broker
from freight_agent.domain.intents import GateDecision, GateScores, Intent
from freight_agent.interfaces.decisions import (
    Choice,
    ChoiceAnswer,
    DecisionModel,
    DecisionRequest,
    YesNo,
    YesNoAnswer,
)
from freight_agent.interfaces.llm import ModelRole
from freight_agent.interfaces.prompts import ChatPrompt, PromptError

INTENT_QUESTION = "intent"
ACT_QUESTION = "act"
# The question texts the template must set in `tools`, and nothing else.
QUESTION_TEXTS = frozenset({INTENT_QUESTION, ACT_QUESTION, "act_yes", "act_no"})
DEFAULT_ACT_THRESHOLD = 0.2


def check_gate_decision_prompt(prompt: ChatPrompt) -> ChatPrompt:
    """`prompt`, or `PromptError` when it names a variable the gate does not compute or
    does not set exactly the question texts (`QUESTION_TEXTS`)."""
    check_variables(prompt, system=GATE_SYSTEM_VARIABLES, user=GATE_USER_VARIABLES)
    keys = frozenset(prompt.tools)
    if keys != QUESTION_TEXTS:
        missing, extra = sorted(QUESTION_TEXTS - keys), sorted(keys - QUESTION_TEXTS)
        raise PromptError(
            f"the {prompt.role.value} prompt {prompt.describe()} does not fit the code: its "
            f"tools must be the question texts {', '.join(sorted(QUESTION_TEXTS))}"
            + (f"; missing: {', '.join(missing)}" if missing else "")
            + (f"; unknown: {', '.join(extra)}" if extra else "")
        )
    return prompt


class DecisionGate:
    """The `jev` engine. `DecisionError` (an `LlmError`) propagates: the runtime
    escalates a model failure to a Hero, as for the `llm` engine."""

    engine = GateEngine.JEV

    def __init__(
        self,
        decisions: DecisionModel,
        prompt: ChatPrompt,
        *,
        act_threshold: float = DEFAULT_ACT_THRESHOLD,
        timeout_s: float = 60.0,
    ) -> None:
        """`PromptError` when the template does not fit (`check_gate_decision_prompt`)."""
        if not 0.0 <= act_threshold <= 1.0:
            raise ValueError(f"the act threshold must be between 0 and 1, not {act_threshold}")
        self._decisions = decisions
        self.prompt = check_gate_decision_prompt(prompt)
        self.act_threshold = act_threshold
        self._timeout_s = timeout_s

    @staticmethod
    def calls_model(ctx: RunContext) -> bool:
        return calls_gate_model(ctx)

    def request(self, ctx: RunContext) -> DecisionRequest:
        """The one decision call for a message of `ctx`."""
        texts = self.prompt.tools
        return DecisionRequest(
            role=ModelRole.GATE,
            state=(
                render_gate_system_prompt(self.prompt, ctx.broker),
                render_gate_prompt(self.prompt, ctx),
            ),
            questions={
                INTENT_QUESTION: Choice(
                    instructions=texts[INTENT_QUESTION],
                    options={i.intent_id: i.description for i in ctx.broker.message_intents()},
                ),
                ACT_QUESTION: YesNo(
                    instructions=texts[ACT_QUESTION], yes=texts["act_yes"], no=texts["act_no"]
                ),
            },
            timeout_s=self._timeout_s,
            prompt_version=self.prompt.version,
        )

    async def classify(self, ctx: RunContext) -> GateResult:
        decision = system_decision(ctx)
        if decision is not None:
            return GateResult(decision=decision, broker=ctx.broker)
        if not ctx.broker.triage:
            return GateResult(decision=UNTRIAGED, broker=ctx.broker)
        response = await self._decisions.decide(self.request(ctx))
        intent = response.answer(INTENT_QUESTION, ChoiceAnswer)
        act = response.answer(ACT_QUESTION, YesNoAnswer)
        scores = GateScores(
            model=response.usage.model,
            intents=dict(intent.probabilities),
            act=act.yes,
            threshold=self.act_threshold,
            intent_confidence=intent.confidence,
        )
        return GateResult(
            decision=decide(ctx.broker, intent.choice, scores),
            usage=response.usage,
            broker=ctx.broker,
            engine=self.engine,
            scores=scores,
        )


def decide(broker: Broker, intent: str, scores: GateScores) -> GateDecision:
    """The policy. A quiet intent (`noop_eligible`, not `must_act`) with P(act) under the
    threshold does not act; anything else acts. A label the catalog's message intents do
    not have is `other`, which acts."""
    if intent not in {i.intent_id for i in broker.message_intents()}:
        return GateDecision(
            intent=Intent.OTHER,
            act=True,
            reason=f"the decision model chose {intent}, which is not a message intent",
        )
    p_intent = scores.intents.get(intent, 0.0)
    reading = (
        f"read as {intent} (probability {p_intent:.2f}); "
        f"needs action now with probability {scores.act:.2f}"
    )
    if broker.must_act(intent):
        return GateDecision(intent=intent, act=True, reason=f"{reading}; {intent} always acts")
    if broker.noop_eligible(intent) and scores.act < scores.threshold:
        return GateDecision(
            intent=intent,
            act=False,
            reason=f"{reading}, under the {scores.threshold:g} threshold: nothing to do",
        )
    return GateDecision(intent=intent, act=True, reason=f"{reading}: the agent reads it")
