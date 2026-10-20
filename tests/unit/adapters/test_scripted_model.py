import pytest

from freight_agent.adapters.llm.scripted import ScriptedModel, ScriptedReply, ScriptExhausted
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
    model.push(ModelRole.AGENT, ACK)

    model.reset()

    assert model.remaining() == 0
    assert model.calls == []


@pytest.mark.unit
async def test_a_scripted_reply_returns_its_transcript_with_the_answer() -> None:
    model = ScriptedModel()
    transcript = (
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "name": "get_ledger", "args": {}}],
        },
        {
            "role": "tool",
            "tool_call_id": "c1",
            "name": "get_ledger",
            "content": "no open obligations",
        },
    )
    model.push(ModelRole.GATE, ScriptedReply(parsed=ACK, transcript=transcript))

    response = await model.generate(GATE, GateDecision)

    assert response.parsed == ACK
    assert response.transcript == transcript


@pytest.mark.unit
async def test_a_plain_output_has_an_empty_transcript() -> None:
    model = ScriptedModel()
    model.push(ModelRole.GATE, ACK)

    assert (await model.generate(GATE, GateDecision)).transcript == ()
