"""The gate's engine end to end: the runtime on the jev engine (the fake decision model)
and on the llm engine, and what each run records."""

from dataclasses import replace

import pytest

from freight_agent.application.gate import GateEngine
from freight_agent.application.graph.build import GraphDeps
from freight_agent.application.runtime import RuntimeConfig, build_gate, graph_deps
from freight_agent.domain.actions import Purpose, SendSms
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import DelayRisk, Load
from freight_agent.domain.runs import RunStatus
from freight_agent.ports.decisions import DecisionError
from tests.fakes import FakeDecisions, gate_answers
from tests.prompts import PROMPTS
from tests.unit.application.test_runtime_harness import (
    ASK_ETA,
    DECIDE,
    DRIVER,
    FOLLOW_UP_IN_30,
    GATE,
    World,
    at,
    decision,
    plan,
)

pytestmark = pytest.mark.unit

JEV = RuntimeConfig(gate_engine=GateEngine.JEV)


async def jev_world(load: Load, decisions: FakeDecisions, threshold: float = 0.2) -> World:
    config = replace(JEV, gate_act_threshold=threshold)
    return await World(load, config=config, decisions=decisions).start()


async def test_on_jev_an_acknowledgement_ends_at_the_gate_with_its_probabilities(
    load: Load,
) -> None:
    decisions = FakeDecisions(cost_usd=0.00003)
    world = await jev_world(load, decisions)
    await world.message(at(7, 12), "ok")
    decisions.push(gate_answers(Intent.ACKNOWLEDGEMENT, 0.03))

    (run,) = await world.tick(at(7, 13))

    assert run.status is RunStatus.NOOP
    assert run.gate is not None and (run.gate.intent, run.gate.act) == ("acknowledgement", False)
    assert run.gate_engine == "jev"
    assert run.gate_scores is not None and run.gate_scores.act == 0.03
    assert run.gate_scores.intents == {"acknowledgement": 1.0}
    assert run.prompts == {"gate_decision": PROMPTS.gate_decision.version}
    assert [(u.role, u.model, u.cost_usd) for u in run.usage] == [("gate", "fake-jev", 0.00003)]
    assert world.calls(GATE) == 0  # the gate's chat model is not called
    assert world.sent("sms") == []


async def test_on_jev_a_question_goes_on_to_the_agent(load: Load) -> None:
    decisions = FakeDecisions()
    world = await jev_world(load, decisions)
    await world.message(at(7, 12), "What's the PO number?")
    decisions.push(gate_answers(Intent.QUESTION, 0.97))
    reply = SendSms(contact_id=DRIVER, text="The PO number is 55821.", purpose=Purpose.REPLY)
    world.model.push(DECIDE, plan(reply))

    (run,) = await world.tick(at(7, 13))

    assert run.status is RunStatus.ACTED
    assert run.gate_engine == "jev" and run.intent == Intent.QUESTION
    assert set(run.prompts) == {"gate_decision", "agent"}
    assert [d.payload["contact_id"] for d in world.sent("sms")] == [DRIVER]


async def test_on_jev_an_uncertain_acknowledgement_goes_on_to_the_agent(load: Load) -> None:
    decisions = FakeDecisions()
    world = await jev_world(load, decisions)
    await world.message(at(7, 12), "ok")
    decisions.push(gate_answers(Intent.ACKNOWLEDGEMENT, 0.35))
    world.model.push(DECIDE, plan())

    (run,) = await world.tick(at(7, 13))

    assert run.status is RunStatus.NOOP  # the agent's own choice: it staged nothing
    assert run.nodes[:3] == ("prefilter", "gate", "knowledge")
    assert run.gate is not None and run.gate.act


async def test_on_jev_a_decision_failure_escalates_like_a_model_failure(load: Load) -> None:
    decisions = FakeDecisions()
    world = await jev_world(load, decisions)
    await world.message(at(7, 12), "Where do I park?")
    decisions.push(DecisionError("typesafe/jev-1.14 (gate): HTTP 503 after 3 attempts"))

    (run,) = await world.tick(at(7, 13))

    assert run.status is RunStatus.ESCALATED
    assert run.error is not None and "HTTP 503 after 3 attempts" in run.error
    assert (run.gate, run.gate_engine, run.usage) == (None, None, ())
    assert run.prompts == {"gate_decision": PROMPTS.gate_decision.version}
    assert [d.payload["urgent"] for d in world.sent("hero_task")] == [True]


async def test_on_jev_a_system_event_is_decided_by_code(load: Load) -> None:
    decisions = FakeDecisions()
    world = await jev_world(load, decisions)
    await world.ping(at(7, 0), DelayRisk.HIGH, "p1")
    world.model.push(DECIDE, plan(ASK_ETA, FOLLOW_UP_IN_30))

    (run,) = await world.tick(at(7, 1))

    assert run.status is RunStatus.ACTED
    assert (run.gate_engine, run.gate_scores) == (None, None)
    assert decisions.requests == []


async def test_on_llm_the_record_names_the_engine_and_has_no_probabilities(load: Load) -> None:
    world = await World(load).start()
    await world.message(at(7, 12), "ok")
    world.model.push(GATE, decision(Intent.ACKNOWLEDGEMENT, act=False))

    (run,) = await world.tick(at(7, 13))

    assert run.status is RunStatus.NOOP
    assert (run.gate_engine, run.gate_scores) == ("llm", None)
    assert run.prompts == {"gate": PROMPTS.gate.version}


async def test_the_jev_engine_needs_a_decision_model(load: Load) -> None:
    world = World(load)

    with pytest.raises(ValueError, match="needs a decision model"):
        build_gate(replace(world.deps, config=JEV))


def test_each_runs_trace_names_the_engine_and_its_prompt(load: Load) -> None:
    world = World(load, config=JEV, decisions=FakeDecisions())
    deps: GraphDeps = graph_deps(world.deps)

    assert deps.trace_metadata() == {"gate_engine": "jev"}
    assert deps.prompt_versions() == {
        "gate_decision": PROMPTS.gate_decision.version,
        "agent": PROMPTS.agent.version,
    }
    assert graph_deps(World(load).deps).trace_metadata() == {"gate_engine": "llm"}
