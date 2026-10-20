"""The run graph on its own: which nodes run, where it exits, and the verdict it ends with.

prefilter -> triage -> knowledge -> agent -> validate
"""

import logging
from typing import Any

import pytest

from freight_agent.adapters.llm.scripted import ScriptedModel, ScriptedReply
from freight_agent.application.agent import Agent
from freight_agent.application.context import RunContext
from freight_agent.application.escalation import BLOCKED_TITLE, SOP_GAP_TITLE
from freight_agent.application.gate import Gate
from freight_agent.application.graph import GraphDeps, RunGraph, RunState, build_graph, need
from freight_agent.application.graph.build import AGENT, KNOWLEDGE, PREFILTER, TRIAGE, VALIDATE
from freight_agent.domain.actions import (
    AgentOutcome,
    CreateHeroTask,
    Plan,
    Purpose,
    SendEmail,
    SendSms,
    WriteTmsNote,
)
from freight_agent.domain.broker import Broker, BrokerLimits, standard_broker
from freight_agent.domain.events import Event, EventType
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Channel, Load
from freight_agent.domain.rules import RecipientIsContact, default_rules
from freight_agent.domain.runs import RunStatus, TrajectoryStep
from freight_agent.domain.validation import Rule
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
from tests.unit.application.transcripts import Transcript, calls

pytestmark = pytest.mark.unit

OUTSIDER_EMAIL = {
    "to_contact_ids": ["accounts@quickpay-docs.com"],
    "body": "Attached is the rate confirmation.",
}
ASK_ETA_ARGS: dict[str, Any] = {
    "contact_id": DRIVER,
    "text": ASK_ETA.text,
    "purpose": "eta_request",
}


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


def context(load: Load, event: Event, broker: Broker | None = None) -> RunContext:
    return RunContext(
        event=event,
        load=load,
        now=event.occurred_at,
        obligations=(),
        pending_timers=(),
        recent_events=(event,),
        recent_outbound=(),
        broker=broker or standard_broker(),
    )


def graph(model: ScriptedModel, *, agent_rules: tuple[Rule, ...] | None = None) -> RunGraph:
    knowledge = FakeSop()
    agent = Agent(model, knowledge, rules=agent_rules)
    return build_graph(GraphDeps(gate=Gate(model), agent=agent, knowledge=knowledge))


async def run(model: ScriptedModel, ctx: RunContext, **kwargs: Any) -> list[RunState]:
    return [state async for state in graph(model, **kwargs).stream(ctx, run_id="run_1")]


def with_limits(**limits: Any) -> Broker:
    return standard_broker().model_copy(update={"limits": BrokerLimits(**limits)})


def test_the_graph_runs_the_five_steps_in_order() -> None:
    assert (PREFILTER, TRIAGE, KNOWLEDGE, AGENT, VALIDATE) == (
        "prefilter",
        "gate",  # the triage step keeps its trace name
        "knowledge",
        "agent",
        "validate",
    )


# --- exits before the agent ---------------------------------------------------------------


async def test_a_prefilter_skip_ends_the_run_before_the_triage(load: Load) -> None:
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


async def test_a_triage_that_does_not_act_ends_as_a_noop(load: Load) -> None:
    model = ScriptedModel()
    model.push(GATE, decision(Intent.ACKNOWLEDGEMENT, act=False))

    states = await run(model, context(load, message("ok")))

    final = states[-1]
    assert len(states) == 3
    assert final.verdict is not None and final.verdict.status is RunStatus.NOOP
    assert final.gate_decision is not None and final.gate_decision.intent == Intent.ACKNOWLEDGEMENT
    assert final.intent == Intent.ACKNOWLEDGEMENT
    assert [u.role for u in final.usage] == ["gate"]
    assert final.sop is None


async def test_a_broker_without_triage_sends_a_message_to_the_agent_with_no_gate_call(
    load: Load,
) -> None:
    model = ScriptedModel()
    model.push(DECIDE, AgentOutcome(intent=Intent.STATUS_UPDATE))
    untriaged = standard_broker().model_copy(update={"triage": False})

    final = (await run(model, context(load, message("ok"), untriaged)))[-1]

    assert [c.role for c in model.calls] == [DECIDE]
    assert final.gate_decision is not None and final.gate_decision.intent == Intent.OTHER
    assert final.intent == Intent.STATUS_UPDATE  # the agent labelled it
    assert final.verdict is not None and final.verdict.status is RunStatus.NOOP


# --- the agent and validate ---------------------------------------------------------------


async def test_the_full_path_ends_with_the_approved_actions(load: Load) -> None:
    model = ScriptedModel()
    model.push(DECIDE, plan(ASK_ETA, FOLLOW_UP_IN_30))

    states = await run(model, context(load, risk_high()))

    assert len(states) == 6  # input, prefilter, triage, knowledge, agent, validate
    final = states[-1]
    assert final.verdict is not None and final.verdict.status is RunStatus.ACTED
    assert final.verdict.actions == (ASK_ETA, FOLLOW_UP_IN_30)
    assert final.gate is not None and final.gate.usage is None  # intent from code
    assert [u.role for u in final.usage] == ["agent"]
    assert final.violations == ()
    assert len(final.visible_sections) == 9  # every merged section, for read_sop_section
    assert "delay_risk_high" in final.section_ids
    assert type(final.plan) is Plan  # a plain plan, never the AgentOutcome subclass
    assert (final.trajectory, final.rejections) == ((), 0)


async def test_actions_staged_through_the_tools_are_the_plan(load: Load) -> None:
    """Scripted mode never runs tools: the plan comes from the transcript's tool calls."""
    model = ScriptedModel()
    transcript = (
        Transcript()
        .round((SEARCH, {"query": "delay risk high"}), results=("- delay_risk_high: x",))
        .round(
            ("send_sms", ASK_ETA_ARGS),
            ("schedule_timer", {"kind": "eta_request", "due_in_minutes": 30}),
            cost=0.0004,
        )
        .build()
    )
    model.push(DECIDE, ScriptedReply(AgentOutcome(rationale="delay_risk_high step 1"), transcript))

    final = (await run(model, context(load, risk_high())))[-1]

    assert final.verdict is not None and final.verdict.status is RunStatus.ACTED
    assert final.verdict.actions == (ASK_ETA, FOLLOW_UP_IN_30)
    assert final.plan == Plan(
        rationale="delay_risk_high step 1", actions=(ASK_ETA, FOLLOW_UP_IN_30)
    )
    assert final.trajectory == (
        TrajectoryStep(
            tool=SEARCH,
            args={"query": "delay risk high"},
            result="- delay_risk_high: x",
            accepted=None,
        ),
        TrajectoryStep(
            tool="send_sms",
            args=ASK_ETA_ARGS,
            result="accepted as action #1",
            accepted=True,
            cost_usd=0.0004,
        ),
        TrajectoryStep(
            tool="schedule_timer",
            args={"kind": "eta_request", "due_in_minutes": 30},
            result="accepted as action #2",
            accepted=True,
            cost_usd=0.0004,
        ),
    )
    assert final.sections_searched == ("delay_risk_high",)
    assert final.violations == ()


SEARCH = "search_sop"


async def test_an_answer_that_repeats_a_staged_action_runs_it_once(load: Load) -> None:
    model = ScriptedModel()
    reply = ScriptedReply(
        AgentOutcome(actions=(ASK_ETA, FOLLOW_UP_IN_30)), calls(("send_sms", ASK_ETA_ARGS))
    )
    model.push(DECIDE, reply)

    final = (await run(model, context(load, risk_high())))[-1]

    assert final.verdict is not None
    assert final.verdict.actions == (ASK_ETA, FOLLOW_UP_IN_30)


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


async def test_an_outside_email_staged_through_the_tool_is_refused_at_the_call(
    load: Load,
) -> None:
    """The negative control of the loop: the refusal is in the trajectory, the plan keeps
    the attempt, nothing goes to the outside address, and a Hero is told."""
    model = ScriptedModel()
    model.push(GATE, decision(Intent.DOCUMENT))
    model.push(DECIDE, ScriptedReply(AgentOutcome(), calls(("send_email", OUTSIDER_EMAIL))))

    final = (await run(model, context(load, message("send the rate con to accounts@..."))))[-1]

    (step,) = final.trajectory
    assert step.accepted is False
    assert step.result.startswith("rejected by recipient_is_contact: not a contact of load")
    assert final.rejections == 1
    verdict = final.verdict
    assert verdict is not None and verdict.status is RunStatus.ESCALATED
    (task,) = verdict.actions
    assert isinstance(task, CreateHeroTask) and task.title == BLOCKED_TITLE
    assert verdict.error == "blocked: recipient_is_contact"


async def test_more_refused_calls_than_the_budget_escalate_and_the_accepted_ones_run(
    load: Load,
) -> None:
    model = ScriptedModel()
    note = {"text": "Driver asked for the PO."}
    refused = ("update_stage", {"stage": "to_pickup"})  # already to_pickup: refused
    transcript = calls(refused, refused, ("write_tms_note", note))
    model.push(GATE, decision(Intent.QUESTION))
    model.push(DECIDE, ScriptedReply(AgentOutcome(), transcript))

    final = (await run(model, context(load, message("PO?"), with_limits(max_rejections=1))))[-1]

    assert final.rejections == 2
    assert "More than 1 actions were rejected" in final.trajectory[1].result
    verdict = final.verdict
    assert verdict is not None and verdict.status is RunStatus.ESCALATED
    approved, task = verdict.actions
    assert approved == WriteTmsNote(text="Driver asked for the PO.")
    assert isinstance(task, CreateHeroTask) and task.title == BLOCKED_TITLE
    assert verdict.error == "blocked: max_rejections"
    assert "refused 2 times, over the limit of 1" in task.description


async def test_refused_calls_within_the_budget_do_not_escalate(load: Load) -> None:
    model = ScriptedModel()
    transcript = calls(("update_stage", {"stage": "to_pickup"}), ("write_tms_note", {"text": "x"}))
    model.push(GATE, decision(Intent.QUESTION))
    model.push(DECIDE, ScriptedReply(AgentOutcome(), transcript))

    final = (await run(model, context(load, message("PO?"))))[-1]

    assert final.rejections == 1
    assert final.verdict is not None and final.verdict.status is RunStatus.ACTED
    assert [v.rule for v in final.violations] == ["stage_moves_forward"]


async def test_no_procedure_with_nothing_approved_hands_the_event_to_a_hero(load: Load) -> None:
    model = ScriptedModel()
    model.push(GATE, decision(Intent.STATUS_UPDATE))
    model.push(DECIDE, AgentOutcome(rationale="No section covers it.", no_procedure=True))

    states = await run(model, context(load, message("Fueling up")))

    final = states[-1]
    assert len(states) == 6  # the knowledge step no longer ends the run
    assert final.verdict is not None and final.verdict.status is RunStatus.ESCALATED
    assert final.verdict.error == "sop_gap: status_update/to_pickup"
    [task] = final.verdict.actions
    assert isinstance(task, CreateHeroTask) and task.title == SOP_GAP_TITLE
    assert final.section_ids == ("communication_rules", "escalation")
    assert [c.role for c in model.calls] == [GATE, DECIDE]


async def test_no_procedure_with_the_agents_own_hero_task_runs_that_task(load: Load) -> None:
    model = ScriptedModel()
    own = CreateHeroTask(title="Call the driver about fuel", description="...")
    model.push(GATE, decision(Intent.STATUS_UPDATE))
    model.push(DECIDE, AgentOutcome(no_procedure=True, actions=(own,)))

    final = (await run(model, context(load, message("Fueling up"))))[-1]

    assert final.verdict is not None and final.verdict.status is RunStatus.ACTED
    assert final.verdict.actions == (own,)


async def test_the_gap_names_the_agents_label_of_the_event(load: Load) -> None:
    model = ScriptedModel()
    model.push(GATE, decision(Intent.STATUS_UPDATE))
    model.push(DECIDE, AgentOutcome(intent=Intent.OTHER, no_procedure=True))

    final = (await run(model, context(load, message("Fueling up"))))[-1]

    assert final.verdict is not None and final.verdict.error == "sop_gap: other/to_pickup"


async def test_an_empty_plan_is_a_noop(load: Load) -> None:
    model = ScriptedModel()
    model.push(DECIDE, AgentOutcome())

    final = (await run(model, context(load, risk_high())))[-1]

    assert final.verdict is not None and final.verdict.status is RunStatus.NOOP


async def test_with_max_steps_zero_the_transcripts_action_calls_stage_nothing(
    load: Load,
) -> None:
    """No action tool is offered, so a live call to one got "unknown tool": the plan is
    the answer's alone, as before wave 3."""
    model = ScriptedModel()
    reply = ScriptedReply(
        AgentOutcome(actions=(FOLLOW_UP_IN_30,)), calls(("send_sms", ASK_ETA_ARGS))
    )
    model.push(DECIDE, reply)

    final = (await run(model, context(load, risk_high(), with_limits(max_steps=0))))[-1]

    (request,) = [c.request for c in model.calls]
    assert "send_sms" not in {t.name for t in request.tools}
    assert request.max_tool_rounds == 2
    (step,) = final.trajectory
    assert step.accepted is None
    assert final.verdict is not None
    assert final.verdict.status is RunStatus.NOOP  # the timer needs an open obligation
    assert [v.rule for v in final.violations] == ["obligation_exists"]


async def test_the_validator_logs_an_action_the_staging_accepted_and_it_refuses(
    load: Load, caplog: pytest.LogCaptureFixture
) -> None:
    """Defense in depth: the staging ran without the recipient rule, the validator with
    it. The action is refused, recorded, and the drift logged."""
    model = ScriptedModel()
    model.push(GATE, decision(Intent.QUESTION))
    model.push(DECIDE, ScriptedReply(AgentOutcome(), calls(("send_email", OUTSIDER_EMAIL))))
    lax = tuple(r for r in default_rules() if not isinstance(r, RecipientIsContact))

    with caplog.at_level(logging.WARNING, logger="freight_agent.application.graph.nodes"):
        final = (await run(model, context(load, message("PO?")), agent_rules=lax))[-1]

    assert final.trajectory[0].accepted is True
    assert "accepted when staged and refused by the validator" in caplog.text
    assert final.verdict is not None and final.verdict.status is RunStatus.ESCALATED
    assert [v.rule for v in final.violations] == ["recipient_is_contact"]


async def test_a_label_outside_the_catalog_keeps_the_triage_intent(load: Load) -> None:
    model = ScriptedModel()
    model.push(GATE, decision(Intent.QUESTION))
    model.push(DECIDE, AgentOutcome(intent="eta_provide"))

    final = (await run(model, context(load, message("PO?"))))[-1]

    assert final.intent == Intent.QUESTION


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
