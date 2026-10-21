"""The jev gate: one decision call per message, and the policy in code.

The decision model is the fake of `tests/fakes.py`; the template is the repository copy,
`prompts/gate_decision.yaml`. The policy under test: a run ends as a no-op only when the
chosen intent is quiet (`noop_eligible`, not `must_act`) and P(act) is under the
threshold; anything else goes on to the agent.
"""

from dataclasses import replace

import pytest

from freight_agent.application.gate import GateEngine
from freight_agent.application.gate_decision import (
    DEFAULT_ACT_THRESHOLD,
    DecisionGate,
    check_gate_decision_prompt,
)
from freight_agent.application.prompts import render_gate_prompt, render_gate_system_prompt
from freight_agent.domain.broker import IntentDef, standard_broker
from freight_agent.domain.events import EventType
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Load
from freight_agent.interfaces.decisions import Choice, DecisionError, YesNo
from freight_agent.interfaces.llm import LlmError, ModelRole
from freight_agent.interfaces.prompts import PromptError
from tests.fakes import FakeDecisions, gate_answers
from tests.prompts import PROMPTS
from tests.unit.application.test_gate import ETA_REQUEST, context, event, sms

pytestmark = pytest.mark.unit


def gate(decisions: FakeDecisions, **kwargs: float) -> DecisionGate:
    return DecisionGate(decisions, PROMPTS.gate_decision, **kwargs)


async def test_a_message_is_one_decision_on_the_gate_prompts_context(load: Load) -> None:
    decisions = FakeDecisions()
    decisions.push(gate_answers(Intent.QUESTION, 0.98))
    ctx = context(load, sms("What's the PO number?"), obligations=(ETA_REQUEST,))

    await gate(decisions).classify(ctx)

    (request,) = decisions.requests
    prompt = PROMPTS.gate_decision
    assert request.role is ModelRole.GATE
    assert request.state == (
        render_gate_system_prompt(prompt, ctx.broker),
        render_gate_prompt(prompt, ctx),
    )
    context_part = request.state[1]
    assert isinstance(context_part, str)
    assert 'text: "What\'s the PO number?"' in context_part
    assert "- eta_request: attempts 1" in context_part
    assert request.prompt_version == prompt.version
    intent, act = request.questions["intent"], request.questions["act"]
    assert isinstance(intent, Choice) and isinstance(act, YesNo)
    assert intent.instructions == prompt.tools["intent"]
    assert dict(intent.options) == {
        i.intent_id: i.description for i in standard_broker().message_intents()
    }
    assert next(iter(intent.options)) == Intent.ETA_PROVIDED  # the catalog's priority order
    assert (act.instructions, act.yes, act.no) == (
        prompt.tools["act"],
        prompt.tools["act_yes"],
        prompt.tools["act_no"],
    )


async def test_a_quiet_intent_under_the_threshold_ends_the_run(load: Load) -> None:
    decisions = FakeDecisions()
    probabilities = {"acknowledgement": 0.97, "eta_provided": 0.03}
    decisions.push(gate_answers(Intent.ACKNOWLEDGEMENT, 0.04, probabilities))

    result = await gate(decisions).classify(context(load, sms("ok")))

    assert (result.decision.intent, result.decision.act) == (Intent.ACKNOWLEDGEMENT, False)
    assert not result.should_act
    assert result.engine is GateEngine.JEV
    assert result.usage is not None and result.usage.model == "fake-jev"
    scores = result.scores
    assert scores is not None
    assert (scores.act, scores.threshold, scores.model) == (0.04, DEFAULT_ACT_THRESHOLD, "fake-jev")
    assert scores.intents == probabilities
    assert scores.intent_confidence == 0.9
    assert result.decision.reason == (
        "read as acknowledgement (probability 0.97); needs action now with probability "
        "0.04, under the 0.2 threshold: nothing to do"
    )


@pytest.mark.parametrize(
    ("intent", "p_act", "threshold"),
    [
        (Intent.ACKNOWLEDGEMENT, 0.2, 0.2),  # at the threshold is not under it
        (Intent.ACKNOWLEDGEMENT, 0.6, 0.2),
        (Intent.ACKNOWLEDGEMENT, 0.0, 0.0),  # threshold 0: nothing ends at the gate
        (Intent.STATUS_UPDATE, 0.01, 0.2),  # not must_act, but not noop_eligible either
        (Intent.OTHER, 0.01, 0.2),
    ],
)
async def test_anything_else_goes_on_to_the_agent(
    load: Load, intent: Intent, p_act: float, threshold: float
) -> None:
    decisions = FakeDecisions()
    decisions.push(gate_answers(intent, p_act))

    result = await gate(decisions, act_threshold=threshold).classify(context(load, sms("x")))

    assert result.decision.intent == intent
    assert result.decision.act and result.should_act
    assert result.decision.reason.endswith(": the agent reads it")


async def test_a_must_act_intent_acts_whatever_p_act_says(load: Load) -> None:
    decisions = FakeDecisions()
    decisions.push(gate_answers(Intent.QUESTION, 0.0))

    result = await gate(decisions).classify(context(load, sms("What's the PO number?")))

    assert result.decision.act and result.should_act
    assert result.decision.reason.endswith("; question always acts")


async def test_a_quiet_intent_that_must_act_in_the_brokers_catalog_acts(load: Load) -> None:
    """A catalog can mark an intent both: `must_act` wins."""
    standard = standard_broker()
    loud = tuple(
        i.model_copy(update={"must_act": True}) if i.intent_id == Intent.ACKNOWLEDGEMENT else i
        for i in standard.intents
    )
    broker = standard.model_copy(update={"intents": loud})
    decisions = FakeDecisions()
    decisions.push(gate_answers(Intent.ACKNOWLEDGEMENT, 0.01))

    result = await gate(decisions).classify(replace(context(load, sms("ok")), broker=broker))

    assert result.decision.act


async def test_a_brokers_own_intent_is_an_option(load: Load) -> None:
    standard = standard_broker()
    detention = IntentDef(
        intent_id="detention_request",
        title="Detention request",
        description="the carrier asks to be paid for waiting at a stop.",
        priority=55,
    )
    broker = standard.model_copy(update={"intents": (*standard.intents, detention)})
    decisions = FakeDecisions()
    decisions.push(gate_answers("detention_request", 0.9))

    result = await gate(decisions).classify(
        replace(context(load, sms("we waited 3 hours")), broker=broker)
    )

    intent = decisions.requests[0].questions["intent"]
    assert isinstance(intent, Choice)
    assert intent.options["detention_request"] == detention.description
    assert result.decision.intent == "detention_request" and result.decision.act


async def test_a_label_that_is_not_a_message_intent_is_other_and_acts(load: Load) -> None:
    decisions = FakeDecisions()
    decisions.push(gate_answers(Intent.DELAY_RISK_HIGH, 0.0))

    result = await gate(decisions).classify(context(load, sms("ok")))

    assert (result.decision.intent, result.decision.act) == (Intent.OTHER, True)
    assert "chose delay_risk_high, which is not a message intent" in result.decision.reason


async def test_system_events_and_untriaged_brokers_are_decided_by_code(load: Load) -> None:
    decisions = FakeDecisions()
    risk = event(EventType.RISK_CHANGED, data={"previous": "low", "current": "high"})
    untriaged = standard_broker().model_copy(update={"triage": False})

    by_code = await gate(decisions).classify(context(load, risk))
    unread = await gate(decisions).classify(replace(context(load, sms("ok")), broker=untriaged))

    assert decisions.requests == []
    assert (by_code.decision.intent, by_code.engine, by_code.scores) == (
        Intent.DELAY_RISK_HIGH,
        None,
        None,
    )
    assert (unread.decision.intent, unread.decision.act, unread.usage) == (Intent.OTHER, True, None)
    assert DecisionGate.calls_model(context(load, sms("ok")))
    assert not DecisionGate.calls_model(context(load, risk))


async def test_a_decision_failure_propagates_as_a_model_failure(load: Load) -> None:
    decisions = FakeDecisions()
    decisions.push(DecisionError("jev (gate): HTTP 503 after 3 attempts"))

    with pytest.raises(LlmError, match="HTTP 503"):
        await gate(decisions).classify(context(load, sms("ok")))


async def test_an_answer_missing_from_the_reply_is_a_decision_error(load: Load) -> None:
    decisions = FakeDecisions()
    decisions.push({"intent": gate_answers(Intent.QUESTION, 0.9)["intent"]})

    with pytest.raises(DecisionError, match="'act' got nothing"):
        await gate(decisions).classify(context(load, sms("PO?")))


# --- the template and the settings ------------------------------------------------------


def test_the_shipped_template_fits_and_names_its_role() -> None:
    prompt = check_gate_decision_prompt(PROMPTS.gate_decision)

    assert prompt.role.value == "gate_decision"
    assert gate(FakeDecisions()).engine is GateEngine.JEV


@pytest.mark.parametrize(
    ("tools", "message"),
    [
        ({"intent": "i", "act": "a", "act_yes": "y"}, "missing: act_no"),
        ({"intent": "i", "act": "a", "act_yes": "y", "act_no": "n", "x": "x"}, "unknown: x"),
    ],
)
def test_a_template_without_exactly_the_question_texts_is_refused(
    tools: dict[str, str], message: str
) -> None:
    with pytest.raises(PromptError, match=message):
        DecisionGate(FakeDecisions(), replace(PROMPTS.gate_decision, tools=tools))


def test_a_template_naming_a_variable_the_gate_does_not_compute_is_refused() -> None:
    template = replace(PROMPTS.gate_decision, user=PROMPTS.agent.user)

    with pytest.raises(PromptError, match="the user message names"):
        DecisionGate(FakeDecisions(), template)


@pytest.mark.parametrize("threshold", [-0.1, 1.5])
def test_a_threshold_outside_zero_to_one_is_refused(threshold: float) -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        gate(FakeDecisions(), act_threshold=threshold)
