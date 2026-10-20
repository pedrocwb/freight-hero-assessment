"""The run graph on its own, with the fake model: which nodes run, where it exits, how the
agent loop goes round, the budgets that end it, and the verdict it ends with.

prefilter -> gate -> knowledge -> agent <-> tools -> validate

`RunGraph.stream` yields a state after every step and as every node starts, so the tests
read the last state, never the number of states.
"""

import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest

from freight_agent.application.agent import ANSWER_NOW, Agent
from freight_agent.application.context import RunContext
from freight_agent.application.escalation import BLOCKED_TITLE, SOP_GAP_TITLE
from freight_agent.application.gate import Gate
from freight_agent.application.graph import NODES, GraphDeps, RunGraph, RunState, build_graph, need
from freight_agent.application.graph.build import (
    AGENT,
    KNOWLEDGE,
    PREFILTER,
    TOOLS,
    TRIAGE,
    VALIDATE,
)
from freight_agent.domain.actions import (
    FINISH,
    AgentOutcome,
    CreateHeroTask,
    Plan,
    Purpose,
    SendEmail,
    SendSms,
    UpdateStage,
    WriteTmsNote,
)
from freight_agent.domain.broker import Broker, BrokerLimits, standard_broker
from freight_agent.domain.events import Event, EventType
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Channel, Load, Stage
from freight_agent.domain.rules import RecipientIsContact, default_rules
from freight_agent.domain.runs import RunStatus, TrajectoryStep
from freight_agent.domain.validation import Rule
from freight_agent.ports.llm import (
    AssistantTurn,
    ChatRequest,
    LlmError,
    ToolCall,
    ToolResult,
    UserTurn,
)
from tests.fakes import FakeModel, Round, act, call
from tests.prompts import PROMPTS
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

THROUGH_THE_AGENT = (PREFILTER, TRIAGE, KNOWLEDGE, AGENT)
OUTSIDER_EMAIL = SendEmail(
    to_contact_ids=("accounts@quickpay-docs.com",), body="Attached is the rate confirmation."
)
NOTE = WriteTmsNote(text="Driver asked for the PO.")
BACKWARDS = UpdateStage(stage=Stage.TO_PICKUP)  # the load is to_pickup already: refused


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


def graph(model: FakeModel, *, agent_rules: tuple[Rule, ...] | None = None) -> RunGraph:
    knowledge = FakeSop()
    agent = Agent(model, knowledge, PROMPTS.agent, rules=agent_rules)
    return build_graph(GraphDeps(gate=Gate(model, PROMPTS.gate), agent=agent, knowledge=knowledge))


async def run(model: FakeModel, ctx: RunContext, **kwargs: Any) -> RunState:
    """The last state of the stream: the run as the graph ended it."""
    final: RunState | None = None
    async for state in graph(model, **kwargs).stream(ctx, run_id="run_1"):
        final = state
    assert final is not None
    return final


def with_limits(**limits: Any) -> Broker:
    return standard_broker().model_copy(update={"limits": BrokerLimits(**limits)})


def requests(model: FakeModel) -> list[ChatRequest]:
    """The agent's rounds, in order."""
    sent = [c.request for c in model.rounds_of(DECIDE)]
    assert all(isinstance(r, ChatRequest) for r in sent)
    return [r for r in sent if isinstance(r, ChatRequest)]


# --- the graph itself ---------------------------------------------------------------------


def test_the_graph_has_six_nodes_and_draws_its_loop() -> None:
    drawn = graph(FakeModel())

    assert NODES == (PREFILTER, TRIAGE, KNOWLEDGE, AGENT, TOOLS, VALIDATE)
    assert NODES == ("prefilter", "gate", "knowledge", "agent", "tools", "validate")
    assert drawn.nodes() == NODES  # the triage step keeps its trace name, gate
    mermaid = drawn.mermaid()
    for edge in (
        "__start__ --> prefilter;",
        "knowledge --> agent;",
        "agent -.-> tools;",
        "tools -.-> agent;",
        "agent -.-> agent;",
        "tools -.-> validate;",
        "validate --> __end__;",
    ):
        assert edge in mermaid


def test_the_pending_calls_are_those_of_the_last_round_until_answered(load: Load) -> None:
    """The answer's own call is not pending, unless its arguments were broken."""
    state = RunState(ctx=context(load, risk_high()))
    lookup, finish = call("get_load"), call(FINISH, rationale="r")
    broken = ToolCall(id="c_bad", name=FINISH, error="Expecting value")
    asked = AssistantTurn(tool_calls=(lookup, finish))

    assert state.pending_calls == () and state.plan is None
    assert replace(state, messages=(asked,)).pending_calls == (lookup,)
    assert replace(state, messages=(AssistantTurn(tool_calls=(broken,)),)).pending_calls == (
        broken,
    )
    answered = ToolResult(call_id=lookup.id, name="get_load", content="## Load")
    assert replace(state, messages=(asked, answered)).pending_calls == ()
    assert replace(state, rounds=1).plan == Plan()  # a round made, nothing proposed


def test_need_names_what_is_missing() -> None:
    assert need(3, "a number") == 3
    with pytest.raises(RuntimeError, match="without a plan"):
        need(None, "a plan")


# --- exits before the agent ---------------------------------------------------------------


async def test_a_prefilter_skip_ends_the_run_before_the_triage(load: Load) -> None:
    model = FakeModel()

    final = await run(model, context(load, message("  ")))

    assert final.verdict is not None and final.verdict.status is RunStatus.SKIPPED
    assert final.verdict.skip_reason == "empty_message"
    assert final.verdict.actions == ()
    assert final.nodes == (PREFILTER,)
    assert model.calls == []


async def test_a_run_given_its_trace_id_streams_the_same_run(load: Load) -> None:
    """The trace id only names the root span (the runtime links the trace with it)."""
    model = FakeModel()
    states = graph(model).stream(context(load, message("  ")), run_id="run_1", trace_id=uuid4())

    final = [state async for state in states][-1]

    assert final.verdict is not None and final.verdict.skip_reason == "empty_message"
    assert final.nodes == (PREFILTER,)


async def test_an_unknown_sender_ends_with_one_hero_task(load: Load) -> None:
    model = FakeModel()

    final = await run(model, context(load, message("hello", sender="stranger")))

    assert final.verdict is not None and final.verdict.status is RunStatus.ESCALATED
    [task] = final.verdict.actions
    assert isinstance(task, CreateHeroTask) and not task.urgent
    assert final.gate is None and model.calls == []
    assert final.nodes == (PREFILTER,)


async def test_a_triage_that_does_not_act_ends_as_a_noop(load: Load) -> None:
    model = FakeModel()
    model.push(GATE, decision(Intent.ACKNOWLEDGEMENT, act=False))

    final = await run(model, context(load, message("ok")))

    assert final.verdict is not None and final.verdict.status is RunStatus.NOOP
    assert final.gate_decision is not None and final.gate_decision.intent == Intent.ACKNOWLEDGEMENT
    assert final.intent == Intent.ACKNOWLEDGEMENT
    assert [u.role for u in final.usage] == ["gate"]
    assert final.sop is None and final.plan is None
    assert final.nodes == (PREFILTER, TRIAGE)


async def test_a_broker_without_triage_sends_a_message_to_the_agent_with_no_gate_call(
    load: Load,
) -> None:
    model = FakeModel()
    model.push(DECIDE, AgentOutcome(intent=Intent.STATUS_UPDATE))
    untriaged = standard_broker().model_copy(update={"triage": False})

    final = await run(model, context(load, message("ok"), untriaged))

    assert [c.role for c in model.calls] == [DECIDE]
    assert final.gate_decision is not None and final.gate_decision.intent == Intent.OTHER
    assert final.intent == Intent.STATUS_UPDATE  # the agent labelled it
    assert final.verdict is not None and final.verdict.status is RunStatus.NOOP


# --- the loop -----------------------------------------------------------------------------


async def test_the_agent_stages_through_the_tools_then_answers(load: Load) -> None:
    model = FakeModel()
    model.push(DECIDE, plan(ASK_ETA, FOLLOW_UP_IN_30))  # a staging round, then finish

    final = await run(model, context(load, risk_high()))

    assert final.nodes == (*THROUGH_THE_AGENT, TOOLS, AGENT, VALIDATE)
    assert final.verdict is not None and final.verdict.status is RunStatus.ACTED
    assert final.verdict.actions == (ASK_ETA, FOLLOW_UP_IN_30)
    assert final.gate is not None and final.gate.usage is None  # intent from code
    assert [u.role for u in final.usage] == ["agent", "agent"]
    assert final.rounds == 2
    assert final.plan == Plan(rationale="scripted", actions=(ASK_ETA, FOLLOW_UP_IN_30))
    assert type(final.plan) is Plan
    assert [(s.tool, s.result, s.accepted) for s in final.trajectory] == [
        ("send_sms", "accepted as action #1", True),
        ("schedule_timer", "accepted as action #2", True),
    ]
    assert (final.rejections, final.violations) == (0, ())
    assert len(final.visible_sections) == 9  # every merged section, for read_sop_section
    assert "delay_risk_high" in final.section_ids
    first, second = requests(model)
    assert first.history == () and not first.force_answer
    assert second.history == final.messages[:3]  # the staging round and its two results
    assert [type(m) for m in final.messages] == [
        AssistantTurn,
        ToolResult,
        ToolResult,
        AssistantTurn,
    ]


async def test_each_round_is_a_step_with_its_own_cost(load: Load) -> None:
    model = FakeModel()
    search = call("search_sop", query="delay risk high")
    read = call("read_sop_section", section_id="delay_risk_high")
    model.push(
        DECIDE,
        Round(calls=(search, read), cost_usd=0.0001),
        Round(calls=(act(ASK_ETA), act(FOLLOW_UP_IN_30)), cost_usd=0.0004),
        Round(answer=AgentOutcome(rationale="delay_risk_high step 1"), cost_usd=0.0002),
    )

    final = await run(model, context(load, risk_high()))

    assert final.nodes == (*THROUGH_THE_AGENT, TOOLS, AGENT, TOOLS, AGENT, VALIDATE)
    assert final.verdict is not None and final.verdict.actions == (ASK_ETA, FOLLOW_UP_IN_30)
    assert final.plan == Plan(
        rationale="delay_risk_high step 1", actions=(ASK_ETA, FOLLOW_UP_IN_30)
    )
    assert [(s.tool, s.accepted, s.cost_usd) for s in final.trajectory] == [
        ("search_sop", None, 0.0001),
        ("read_sop_section", None, 0.0001),
        ("send_sms", True, 0.0004),
        ("schedule_timer", True, 0.0004),
    ]
    assert final.trajectory[2] == TrajectoryStep(
        tool="send_sms",
        args=act(ASK_ETA).args,
        result="accepted as action #1",
        accepted=True,
        cost_usd=0.0004,
    )
    assert final.sections_searched[0] == "delay_risk_high"  # the best hit, read again
    assert len(set(final.sections_searched)) == len(final.sections_searched)
    assert [u.cost_usd for u in final.agent_usage] == [0.0001, 0.0004, 0.0002]


async def test_an_answer_next_to_tool_calls_waits_for_them_then_validates(load: Load) -> None:
    model = FakeModel()
    model.push(GATE, decision(Intent.QUESTION))
    model.push(DECIDE, Round(calls=(act(NOTE),), answer=AgentOutcome(rationale="noted")))

    final = await run(model, context(load, message("PO?")))

    assert final.nodes == (*THROUGH_THE_AGENT, TOOLS, VALIDATE)
    assert len(requests(model)) == 1
    (step,) = final.trajectory  # the answer's own call is not a tool step
    assert (step.tool, step.accepted) == ("write_tms_note", True)
    assert final.verdict is not None and final.verdict.status is RunStatus.ACTED
    assert final.verdict.actions == (NOTE,)
    assert final.plan == Plan(rationale="noted", actions=(NOTE,))


async def test_a_prose_reply_is_asked_once_for_the_answer(load: Load) -> None:
    """The next round is forced, after a user turn: a trailing assistant message reads
    as a prefill to some providers."""
    model = FakeModel()
    model.push(GATE, decision(Intent.QUESTION))
    model.push(
        DECIDE,
        Round(text="The driver wants the PO; it is 55821."),
        AgentOutcome(rationale="answered in prose"),
    )

    final = await run(model, context(load, message("PO?")))

    assert final.nodes == (*THROUGH_THE_AGENT, AGENT, VALIDATE)
    first, second = requests(model)
    assert not first.force_answer and second.force_answer
    prose = AssistantTurn(text="The driver wants the PO; it is 55821.")
    assert second.history == (prose, UserTurn(ANSWER_NOW))
    assert final.messages[:2] == (prose, UserTurn(ANSWER_NOW))
    assert final.rounds == 2
    assert final.verdict is not None and final.verdict.status is RunStatus.NOOP


async def test_an_answer_whose_arguments_are_not_json_is_asked_again(load: Load) -> None:
    model = FakeModel()
    model.push(GATE, decision(Intent.QUESTION))
    broken = ToolCall(id="c_bad", name=FINISH, error="Expecting ',' delimiter")
    model.push(DECIDE, Round(calls=(broken,)), Round(calls=(act(NOTE),)), AgentOutcome())

    final = await run(model, context(load, message("PO?")))

    assert final.nodes == (*THROUGH_THE_AGENT, TOOLS, AGENT, TOOLS, AGENT, VALIDATE)
    first = final.trajectory[0]
    assert (first.tool, first.accepted) == (FINISH, None)
    assert first.result.startswith("The arguments for finish are not valid JSON")
    assert final.rejections == 0
    answered = ToolResult(call_id="c_bad", name=FINISH, content=first.result)
    assert answered in requests(model)[1].history
    assert final.verdict is not None and final.verdict.actions == (NOTE,)


# --- the budgets --------------------------------------------------------------------------


async def test_after_max_steps_rounds_the_next_round_must_answer(load: Load) -> None:
    model = FakeModel()
    model.push(
        DECIDE,
        Round(calls=(call("get_load"),)),
        Round(calls=(call("get_ledger"),)),
        AgentOutcome(rationale="forced"),
    )

    final = await run(model, context(load, risk_high(), with_limits(max_steps=2)))

    assert [r.force_answer for r in requests(model)] == [False, False, True]
    assert final.nodes == (*THROUGH_THE_AGENT, TOOLS, AGENT, TOOLS, AGENT, VALIDATE)
    assert final.outcome == AgentOutcome(rationale="forced")


async def test_a_forced_round_that_still_calls_tools_fails_the_run(load: Load) -> None:
    """The adapter raises when a forced round does not answer: the runtime escalates."""
    model = FakeModel()
    model.push(DECIDE, Round(calls=(call("get_load"),)), Round(calls=(call("get_load"),)))

    with pytest.raises(LlmError, match="did not return a finish answer"):
        await run(model, context(load, risk_high(), with_limits(max_steps=1)))


async def test_once_the_agents_rounds_cost_max_cost_usd_the_next_round_must_answer(
    load: Load,
) -> None:
    model = FakeModel()
    model.push(
        DECIDE,
        Round(calls=(call("get_load"),), cost_usd=0.006),
        Round(calls=(call("get_ledger"),), cost_usd=0.004),
        AgentOutcome(),
    )

    await run(model, context(load, risk_high(), with_limits(max_cost_usd=0.01)))

    assert [r.force_answer for r in requests(model)] == [False, False, True]


async def test_more_refused_calls_than_the_budget_escalate_and_the_accepted_ones_run(
    load: Load,
) -> None:
    """The tools node stops the loop: no round is asked to answer after it."""
    model = FakeModel()
    model.push(GATE, decision(Intent.QUESTION))
    model.push(DECIDE, Round(calls=(act(BACKWARDS), act(BACKWARDS), act(NOTE))))

    final = await run(model, context(load, message("PO?"), with_limits(max_rejections=1)))

    assert final.nodes == (*THROUGH_THE_AGENT, TOOLS, VALIDATE)
    assert model.remaining(DECIDE) == 0 and model.exhausted == []
    assert final.rejections == 2
    assert [s.accepted for s in final.trajectory] == [False, False, True]
    assert final.outcome is None
    assert final.plan == Plan(actions=(BACKWARDS, BACKWARDS, NOTE))
    verdict = final.verdict
    assert verdict is not None and verdict.status is RunStatus.ESCALATED
    approved, task = verdict.actions
    assert approved == NOTE
    assert isinstance(task, CreateHeroTask) and task.title == BLOCKED_TITLE
    assert verdict.error == "blocked: max_rejections"
    assert "refused 2 times, over the limit of 1" in task.description


async def test_refused_calls_within_the_budget_do_not_escalate(load: Load) -> None:
    model = FakeModel()
    model.push(GATE, decision(Intent.QUESTION))
    model.push(DECIDE, Round(calls=(act(BACKWARDS), act(NOTE))), AgentOutcome())

    final = await run(model, context(load, message("PO?")))

    assert final.rejections == 1
    assert final.verdict is not None and final.verdict.status is RunStatus.ACTED
    assert final.verdict.actions == (NOTE,)
    assert [(v.rule, v.action_index) for v in final.violations] == [("stage_moves_forward", 0)]


# --- the verdict --------------------------------------------------------------------------


async def test_a_high_violation_runs_the_approved_actions_then_the_hero_task(load: Load) -> None:
    model = FakeModel()
    leak = SendEmail(to_contact_ids=("stranger",), body="The rate is $2,000.")
    reply = SendSms(contact_id=DRIVER, text="Sorry, I cannot share that.", purpose=Purpose.REPLY)
    model.push(GATE, decision(Intent.QUESTION))
    model.push(DECIDE, plan(leak, reply))

    final = await run(model, context(load, message("What's the rate?")))

    leaked = final.trajectory[0]
    assert leaked.accepted is False
    assert leaked.result.startswith("rejected by recipient_is_contact: not a contact of load")
    verdict = final.verdict
    assert verdict is not None and verdict.status is RunStatus.ESCALATED
    assert verdict.actions[0] == reply
    task = verdict.actions[1]
    assert isinstance(task, CreateHeroTask) and task.title == BLOCKED_TITLE and task.urgent
    assert verdict.error == "blocked: recipient_is_contact"


async def test_no_procedure_with_nothing_approved_hands_the_event_to_a_hero(load: Load) -> None:
    model = FakeModel()
    model.push(GATE, decision(Intent.STATUS_UPDATE))
    model.push(DECIDE, AgentOutcome(rationale="No section covers it.", no_procedure=True))

    final = await run(model, context(load, message("Fueling up")))

    assert final.nodes == (*THROUGH_THE_AGENT, VALIDATE)  # knowledge never ends the run
    assert final.verdict is not None and final.verdict.status is RunStatus.ESCALATED
    assert final.verdict.error == "sop_gap: status_update/to_pickup"
    [task] = final.verdict.actions
    assert isinstance(task, CreateHeroTask) and task.title == SOP_GAP_TITLE
    assert final.section_ids == ("communication_rules", "escalation")
    assert [c.role for c in model.calls] == [GATE, DECIDE]


async def test_no_procedure_with_the_agents_own_hero_task_runs_that_task(load: Load) -> None:
    model = FakeModel()
    own = CreateHeroTask(title="Call the driver about fuel", description="...")
    model.push(GATE, decision(Intent.STATUS_UPDATE))
    model.push(DECIDE, Round(calls=(act(own),)), AgentOutcome(no_procedure=True))

    final = await run(model, context(load, message("Fueling up")))

    assert final.verdict is not None and final.verdict.status is RunStatus.ACTED
    assert final.verdict.actions == (own,)


async def test_the_gap_names_the_agents_label_of_the_event(load: Load) -> None:
    model = FakeModel()
    model.push(GATE, decision(Intent.STATUS_UPDATE))
    model.push(DECIDE, AgentOutcome(intent=Intent.OTHER, no_procedure=True))

    final = await run(model, context(load, message("Fueling up")))

    assert final.intent == Intent.OTHER
    assert final.verdict is not None and final.verdict.error == "sop_gap: other/to_pickup"


async def test_an_answer_with_nothing_staged_is_a_noop(load: Load) -> None:
    model = FakeModel()
    model.push(DECIDE, AgentOutcome())

    final = await run(model, context(load, risk_high()))

    assert final.verdict is not None and final.verdict.status is RunStatus.NOOP
    assert final.plan == Plan()


async def test_the_validator_logs_an_action_the_staging_accepted_and_it_refuses(
    load: Load, caplog: pytest.LogCaptureFixture
) -> None:
    """Defense in depth: the staging ran without the recipient rule, the validator with
    it. The action is refused, recorded, and the drift logged."""
    model = FakeModel()
    model.push(GATE, decision(Intent.QUESTION))
    model.push(DECIDE, Round(calls=(act(OUTSIDER_EMAIL),)), AgentOutcome())
    lax = tuple(r for r in default_rules() if not isinstance(r, RecipientIsContact))

    with caplog.at_level(logging.WARNING, logger="freight_agent.application.graph.nodes"):
        final = await run(model, context(load, message("PO?")), agent_rules=lax)

    assert final.trajectory[0].accepted is True
    assert "accepted when staged and refused by the validator" in caplog.text
    assert final.verdict is not None and final.verdict.status is RunStatus.ESCALATED
    assert [v.rule for v in final.violations] == ["recipient_is_contact"]


async def test_a_label_outside_the_catalog_keeps_the_triage_intent(load: Load) -> None:
    model = FakeModel()
    model.push(GATE, decision(Intent.QUESTION))
    model.push(DECIDE, AgentOutcome(intent="eta_provide"))

    final = await run(model, context(load, message("PO?")))

    assert final.intent == Intent.QUESTION


# --- a model failure ----------------------------------------------------------------------


async def test_a_model_failure_leaves_the_earlier_states_behind(load: Load) -> None:
    model = FakeModel()
    model.push(GATE, decision(Intent.QUESTION))
    model.push(DECIDE, LlmError("bad json"))
    seen: list[RunState] = []

    with pytest.raises(LlmError, match="bad json"):
        async for state in graph(model).stream(context(load, message("PO?")), run_id="run_1"):
            seen.append(state)

    last = seen[-1]
    assert last.nodes == THROUGH_THE_AGENT  # the failing node is the last one
    assert last.sop is not None and last.plan is None
    assert [u.role for u in last.usage] == ["gate"]


async def test_a_failure_in_a_later_round_keeps_what_the_run_staged(load: Load) -> None:
    model = FakeModel()
    model.push(GATE, decision(Intent.QUESTION))
    model.push(DECIDE, Round(calls=(act(NOTE),), cost_usd=0.001), LlmError("timeout"))
    seen: list[RunState] = []

    with pytest.raises(LlmError, match="timeout"):
        async for state in graph(model).stream(context(load, message("PO?")), run_id="run_1"):
            seen.append(state)

    last = seen[-1]
    assert last.nodes == (*THROUGH_THE_AGENT, TOOLS, AGENT)
    assert last.plan == Plan(actions=(NOTE,))
    assert [u.cost_usd for u in last.agent_usage] == [0.001]
    assert last.verdict is None


# --- the trace ----------------------------------------------------------------------------


class _Recorded:
    """A compiled graph that records the config of each stream and yields nothing."""

    def __init__(self) -> None:
        self.configs: list[Mapping[str, Any]] = []

    async def astream(
        self, input: RunState, config: Mapping[str, object], *, stream_mode: list[str]
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        self.configs.append(config)
        for item in ():
            yield item

    def get_graph(self) -> Any:
        raise NotImplementedError


async def test_each_runs_trace_carries_the_version_of_its_prompts(load: Load) -> None:
    compiled = _Recorded()
    graph = RunGraph(compiled, {"gate": "5b488b4298d0", "agent": "c6aeff6531a1"})

    states = [s async for s in graph.stream(context(load, message("ok")), run_id="run_1")]

    (config,) = compiled.configs
    assert config["metadata"]["prompt_gate"] == "5b488b4298d0"
    assert config["metadata"]["prompt_agent"] == "c6aeff6531a1"
    assert (config["metadata"]["run_id"], states) == ("run_1", [])


def test_the_graph_names_the_prompts_of_its_gate_and_agent() -> None:
    model, knowledge = FakeModel(), FakeSop()
    deps = GraphDeps(
        gate=Gate(model, PROMPTS.gate),
        agent=Agent(model, knowledge, PROMPTS.agent),
        knowledge=knowledge,
    )

    assert deps.prompt_versions() == {
        "gate": PROMPTS.gate.version,
        "agent": PROMPTS.agent.version,
    }
