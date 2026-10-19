import pytest

from freight_agent.adapters.llm.router import NoModelForRole, RoleRouter
from freight_agent.adapters.llm.scripted import ScriptedModel
from freight_agent.domain.actions import Plan
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.ports.llm import LanguageModel, LlmError, LlmRequest, ModelRole

GATE = LlmRequest(role=ModelRole.GATE, system="s", user="u", max_tool_rounds=0)
DECIDE = LlmRequest(role=ModelRole.DECIDE, system="s", user="u")
ACK = GateDecision(intent=Intent.ACKNOWLEDGEMENT, act=False, reason="just ok")
EMPTY_PLAN = Plan(rationale="nothing to do")


@pytest.mark.unit
async def test_each_request_goes_to_the_model_of_its_role() -> None:
    gate, decide = ScriptedModel("gate-model"), ScriptedModel("decide-model")
    gate.push(ModelRole.GATE, ACK)
    decide.push(ModelRole.DECIDE, EMPTY_PLAN)
    router = RoleRouter({ModelRole.GATE: gate, ModelRole.DECIDE: decide})

    gated = await router.generate(GATE, GateDecision)
    decided = await router.generate(DECIDE, Plan)

    assert (gated.parsed, gated.usage.model) == (ACK, "gate-model")
    assert (decided.parsed, decided.usage.model) == (EMPTY_PLAN, "decide-model")
    assert [c.role for c in gate.calls] == [ModelRole.GATE]
    assert [c.role for c in decide.calls] == [ModelRole.DECIDE]


@pytest.mark.unit
async def test_a_role_with_no_model_is_an_llm_error_naming_the_role() -> None:
    router = RoleRouter({ModelRole.GATE: ScriptedModel()})

    with pytest.raises(LlmError, match="decide") as raised:
        await router.generate(DECIDE, Plan)

    assert isinstance(raised.value, NoModelForRole)
    assert raised.value.role is ModelRole.DECIDE


@pytest.mark.unit
def test_the_router_says_which_model_serves_a_role() -> None:
    gate = ScriptedModel()
    router = RoleRouter({ModelRole.GATE: gate})

    assert router.roles == frozenset({ModelRole.GATE})
    assert router.model_for(ModelRole.GATE) is gate
    with pytest.raises(NoModelForRole, match="judge"):
        router.model_for(ModelRole.JUDGE)


@pytest.mark.unit
def test_a_later_change_to_the_callers_mapping_does_not_reroute() -> None:
    models: dict[ModelRole, LanguageModel] = {ModelRole.GATE: ScriptedModel()}
    router = RoleRouter(models)

    models[ModelRole.DECIDE] = ScriptedModel()

    assert router.roles == frozenset({ModelRole.GATE})


@pytest.mark.unit
def test_a_router_needs_at_least_one_model() -> None:
    with pytest.raises(ValueError, match="at least one"):
        RoleRouter({})
