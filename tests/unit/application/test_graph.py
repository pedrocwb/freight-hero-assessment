"""The run graph on its own: which nodes run, where it exits, and the verdict it ends with."""

import pytest

from freight_agent.adapters.llm.scripted import ScriptedModel
from freight_agent.application.context import RunContext
from freight_agent.application.decide import Decider
from freight_agent.application.escalation import BLOCKED_TITLE, SOP_GAP_TITLE
from freight_agent.application.gate import Gate
from freight_agent.application.graph import GraphDeps, RunGraph, RunState, build_graph, need
from freight_agent.domain.actions import CreateHeroTask, Purpose, SendEmail, SendSms
from freight_agent.domain.events import Event, EventType
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Channel, Load
from freight_agent.domain.runs import RunStatus
from freight_agent.ports.llm import LlmError
from tests.unit.application.test_runtime_harness import (
    ASK_ETA,
    DECIDE,
    DRIVER,
    FOLLOW_UP_IN_30,
    GATE,
    LOAD_ID,
    FakeSop,
    at,
    decision,
    plan,
)

pytestmark = pytest.mark.unit


def message(body: str, sender: str | None = DRIVER) -> Event:
    return Event(
        event_id="m-1",
        load_id=LOAD_ID,
        type=EventType.INBOUND_MESSAGE,
        occurred_at=at(7, 12),
        channel=Channel.SMS,
        sender_contact_id=sender,
        body=body,
    )


def risk_high() -> Event:
    return Event(
        event_id="risk:p1",
        load_id=LOAD_ID,
        type=EventType.RISK_CHANGED,
        occurred_at=at(7, 0),
        channel=Channel.SYSTEM,
        data={"previous": "low", "current": "high"},
    )


def context(load: Load, event: Event) -> RunContext:
    return RunContext(
        event=event,
        load=load,
        now=event.occurred_at,
        obligations=(),
        pending_timers=(),
        recent_events=(event,),
        recent_outbound=(),
    )


def graph(model: ScriptedModel) -> RunGraph:
    return build_graph(GraphDeps(gate=Gate(model), decider=Decider(model), knowledge=FakeSop()))


async def run(model: ScriptedModel, ctx: RunContext) -> list[RunState]:
    return [state async for state in graph(model).stream(ctx, run_id="run_1")]


async def test_a_prefilter_skip_ends_the_run_before_the_gate(load: Load) -> None:
    model = ScriptedModel()

    states = await run(model, context(load, message("  ")))

    assert len(states) == 2  # the input, then the prefilter
    final = states[-1]
    assert final.verdict is not None and final.verdict.status is RunStatus.SKIPPED
    assert final.verdict.skip_reason == "empty_message"
    assert final.verdict.actions == ()
    assert model.calls == []


async def test_an_unknown_sender_ends_with_one_hero_task(load: Load) -> None:
    model = ScriptedModel()

    final = (await run(model, context(load, message("hello", sender="stranger"))))[-1]

    assert final.verdict is not None and final.verdict.status is RunStatus.ESCALATED
    [task] = final.verdict.actions
    assert isinstance(task, CreateHeroTask) and not task.urgent
    assert final.gate is None and model.calls == []


async def test_a_gate_that_does_not_act_ends_as_a_noop(load: Load) -> None:
    model = ScriptedModel()
    model.push(GATE, decision(Intent.ACKNOWLEDGEMENT, act=False))

    states = await run(model, context(load, message("ok")))

    final = states[-1]
    assert len(states) == 3
    assert final.verdict is not None and final.verdict.status is RunStatus.NOOP
    assert final.gate_decision is not None and final.gate_decision.intent == Intent.ACKNOWLEDGEMENT
    assert [u.role for u in final.usage] == ["gate"]
    assert final.sop is None


async def test_no_section_for_the_intent_escalates_before_decide(load: Load) -> None:
    model = ScriptedModel()
    model.push(GATE, decision(Intent.STATUS_UPDATE))

    final = (await run(model, context(load, message("Fueling up"))))[-1]

    assert final.verdict is not None and final.verdict.status is RunStatus.ESCALATED
    assert final.verdict.error == "sop_gap: status_update/to_pickup"
    [task] = final.verdict.actions
    assert isinstance(task, CreateHeroTask) and task.title == SOP_GAP_TITLE
    assert final.section_ids == ("communication_rules", "escalation")
    assert [c.role for c in model.calls] == [GATE]


async def test_the_full_path_ends_with_the_approved_actions(load: Load) -> None:
    model = ScriptedModel()
    model.push(DECIDE, plan(ASK_ETA, FOLLOW_UP_IN_30))

    states = await run(model, context(load, risk_high()))

    assert len(states) == 6  # input, prefilter, gate, resolve_sop, decide, validate
    final = states[-1]
    assert final.verdict is not None and final.verdict.status is RunStatus.ACTED
    assert final.verdict.actions == (ASK_ETA, FOLLOW_UP_IN_30)
    assert final.gate is not None and final.gate.usage is None  # intent from code
    assert [u.role for u in final.usage] == ["agent"]
    assert final.violations == ()
    assert len(final.visible_sections) == 9  # every merged section, for read_sop_section
    assert "delay_risk_high" in final.section_ids


async def test_a_high_violation_runs_the_approved_actions_then_the_hero_task(load: Load) -> None:
    model = ScriptedModel()
    leak = SendEmail(to_contact_ids=("stranger",), body="The rate is $2,000.")
    reply = SendSms(contact_id=DRIVER, text="Sorry, I cannot share that.", purpose=Purpose.REPLY)
    model.push(GATE, decision(Intent.QUESTION))
    model.push(DECIDE, plan(leak, reply))

    final = (await run(model, context(load, message("What's the rate?"))))[-1]

    verdict = final.verdict
    assert verdict is not None and verdict.status is RunStatus.ESCALATED
    assert verdict.actions[0] == reply
    task = verdict.actions[1]
    assert isinstance(task, CreateHeroTask) and task.title == BLOCKED_TITLE and task.urgent
    assert verdict.error == "blocked: recipient_is_contact"


async def test_a_model_failure_leaves_the_earlier_states_behind(load: Load) -> None:
    model = ScriptedModel()
    model.push(GATE, decision(Intent.QUESTION))
    model.push(DECIDE, LlmError("bad json"))
    seen: list[RunState] = []

    with pytest.raises(LlmError):
        async for state in graph(model).stream(context(load, message("PO?")), run_id="run_1"):
            seen.append(state)

    assert seen[-1].sop is not None and seen[-1].plan is None
    assert [u.role for u in seen[-1].usage] == ["gate"]


def test_need_names_what_is_missing() -> None:
    assert need(3, "a number") == 3
    with pytest.raises(RuntimeError, match="without a plan"):
        need(None, "a plan")
