"""The role router: each request, forced answer or agent round, goes to its role's model."""

import pytest

from freight_agent.adapters.llm.router import NoModelForRole, RoleRouter
from freight_agent.domain.actions import AgentOutcome
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.interfaces.llm import ChatRequest, LanguageModel, LlmError, LlmRequest, ModelRole
from tests.fakes import FakeModel

pytestmark = pytest.mark.unit

GATE = LlmRequest(role=ModelRole.GATE, system="s", user="u")
ROUND = ChatRequest(role=ModelRole.AGENT, system="s", user="u")
ACK = GateDecision(intent=Intent.ACKNOWLEDGEMENT, act=False, reason="just ok")
DONE = AgentOutcome(rationale="nothing to do")


async def test_each_request_goes_to_the_model_of_its_role() -> None:
    gate, agent = FakeModel("gate-model"), FakeModel("agent-model")
    gate.push(ModelRole.GATE, ACK)
    agent.push(ModelRole.AGENT, DONE)
    router = RoleRouter({ModelRole.GATE: gate, ModelRole.AGENT: agent})

    gated = await router.generate(GATE, GateDecision)
    reply = await router.chat(ROUND, AgentOutcome)

    assert (gated.parsed, gated.usage.model) == (ACK, "gate-model")
    assert (reply.answer, reply.usage.model) == (DONE, "agent-model")
    assert [c.role for c in gate.calls] == [ModelRole.GATE]
    assert [c.role for c in agent.calls] == [ModelRole.AGENT]


async def test_a_forced_answer_for_the_agent_role_goes_to_the_agent_model() -> None:
    """`generate` and `chat` route by the request's role alone."""
    gate, agent = FakeModel("gate-model"), FakeModel("agent-model")
    agent.push(ModelRole.AGENT, DONE)
    router = RoleRouter({ModelRole.GATE: gate, ModelRole.AGENT: agent})

    response = await router.generate(LlmRequest(ModelRole.AGENT, "s", "u"), AgentOutcome)

    assert response.usage.model == "agent-model"
    assert gate.calls == []


async def test_a_role_with_no_model_is_an_llm_error_naming_the_role() -> None:
    router = RoleRouter({ModelRole.GATE: FakeModel()})

    with pytest.raises(LlmError, match="agent") as raised:
        await router.chat(ROUND, AgentOutcome)

    assert isinstance(raised.value, NoModelForRole)
    assert raised.value.role is ModelRole.AGENT
    assert "configured: gate" in str(raised.value)


async def test_a_forced_answer_for_a_role_with_no_model_is_an_llm_error_too() -> None:
    router = RoleRouter({ModelRole.AGENT: FakeModel()})

    with pytest.raises(NoModelForRole, match="gate"):
        await router.generate(GATE, GateDecision)


def test_the_router_says_which_model_serves_a_role() -> None:
    gate = FakeModel()
    router = RoleRouter({ModelRole.GATE: gate})

    assert router.roles == frozenset({ModelRole.GATE})
    assert router.model_for(ModelRole.GATE) is gate
    with pytest.raises(NoModelForRole, match="judge"):
        router.model_for(ModelRole.JUDGE)


def test_a_later_change_to_the_callers_mapping_does_not_reroute() -> None:
    models: dict[ModelRole, LanguageModel] = {ModelRole.GATE: FakeModel()}
    router = RoleRouter(models)

    models[ModelRole.AGENT] = FakeModel()

    assert router.roles == frozenset({ModelRole.GATE})


def test_a_router_needs_at_least_one_model() -> None:
    with pytest.raises(ValueError, match="at least one"):
        RoleRouter({})
