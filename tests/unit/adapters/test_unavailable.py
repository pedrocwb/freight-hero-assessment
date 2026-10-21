"""The model of a process without one: every call fails with the reason, as a model
failure, so a run escalates to a Hero saying why."""

import pytest

from freight_agent.adapters.llm.unavailable import UnavailableModel
from freight_agent.domain.actions import AgentOutcome
from freight_agent.domain.intents import GateDecision
from freight_agent.ports.llm import ChatRequest, LanguageModel, LlmError, LlmRequest, ModelRole

pytestmark = pytest.mark.unit

REASON = "OPENROUTER_API_KEY is not set (environment or .env); the agent's model needs it"


async def test_a_forced_answer_fails_with_the_reason() -> None:
    model: LanguageModel = UnavailableModel(REASON)

    with pytest.raises(LlmError) as raised:
        await model.generate(LlmRequest(ModelRole.GATE, "s", "u"), GateDecision)

    assert str(raised.value) == REASON


async def test_an_agent_round_fails_with_the_reason() -> None:
    model: LanguageModel = UnavailableModel(REASON)

    with pytest.raises(LlmError) as raised:
        await model.chat(ChatRequest(ModelRole.AGENT, "s", "u"), AgentOutcome)

    assert str(raised.value) == REASON


def test_the_reason_is_kept_for_whoever_shows_it() -> None:
    assert UnavailableModel(REASON).reason == REASON
