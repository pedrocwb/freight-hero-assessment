import pytest

from freight_agent.adapters.llm.scripted import ScriptedModel, ScriptExhausted
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.ports.llm import LlmError, LlmRequest, ModelRole

GATE = LlmRequest(role=ModelRole.GATE, system="s", user="u")
ACK = GateDecision(intent=Intent.ACKNOWLEDGEMENT, act=False, reason="just ok")


@pytest.mark.unit
async def test_returns_queued_outputs_in_order_per_role() -> None:
    model = ScriptedModel()
    question = GateDecision(intent=Intent.QUESTION, act=True, reason="asks PO")
    model.push(ModelRole.GATE, ACK, question)

    first = await model.generate(GATE, GateDecision)
    second = await model.generate(GATE, GateDecision)

    assert [first.parsed.intent, second.parsed.intent] == [Intent.ACKNOWLEDGEMENT, Intent.QUESTION]
    assert len(model.calls) == 2
    assert model.remaining() == 0


@pytest.mark.unit
async def test_empty_queue_raises_and_is_recorded() -> None:
    model = ScriptedModel()

    with pytest.raises(ScriptExhausted):
        await model.generate(GATE, GateDecision)

    assert len(model.exhausted) == 1


@pytest.mark.unit
async def test_queued_exception_simulates_a_model_failure() -> None:
    model = ScriptedModel()
    model.push(ModelRole.GATE, LlmError("timeout"))

    with pytest.raises(LlmError, match="timeout"):
        await model.generate(GATE, GateDecision)


@pytest.mark.unit
async def test_reset_clears_queues_and_history() -> None:
    model = ScriptedModel()
    model.push(ModelRole.GATE, ACK)
    await model.generate(GATE, GateDecision)
    model.push(ModelRole.DECIDE, ACK)

    model.reset()

    assert model.remaining() == 0
    assert model.calls == []
