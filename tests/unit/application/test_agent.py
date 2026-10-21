"""The agent's two steps: one model round (`Agent.round`, what it asks the model) and the
tool calls of a round (`Agent.call_tools`, how each one is answered), the staging of one
action call (`stage`), and the context the rules and the prompt read."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from freight_agent.application.agent import (
    ANSWER_NOW,
    Agent,
    Proposal,
    catalog_intent,
    sections_in_progress,
    stage,
    validation_context,
)
from freight_agent.application.agent_tools import ACTION_TOOLS, READ_TOOLS
from freight_agent.application.context import RunContext
from freight_agent.application.prompts import render_agent_prompt, render_agent_system_prompt
from freight_agent.domain.actions import (
    FINISH,
    AgentOutcome,
    Purpose,
    ScheduleTimer,
    SendSms,
    UpdateStage,
    WriteTmsNote,
)
from freight_agent.domain.broker import Broker, BrokerLimits, ObligationDef, standard_broker
from freight_agent.domain.events import Attachment, Event, EventType
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.load import Channel, Load, Stage
from freight_agent.domain.obligations import Obligation, ObligationKind, ObligationStatus
from freight_agent.domain.rules import default_rules
from freight_agent.domain.runs import TrajectoryStep
from freight_agent.domain.sop import CatalogEntry, ResolvedSection, ResolvedSop, SearchHit
from freight_agent.ports.llm import (
    AssistantTurn,
    ChatRequest,
    LlmError,
    ModelRole,
    ToolCall,
    ToolResult,
    UserTurn,
)
from freight_agent.ports.prompts import PromptError
from tests.fakes import FakeModel, Round, call
from tests.prompts import PROMPTS
from tests.unit.application.test_runtime_harness import FakeSop

pytestmark = pytest.mark.unit

LA = ZoneInfo("America/Los_Angeles")
DRIVER = "drv_20931"
ETA = ObligationKind.ETA_REQUEST
QUESTION = GateDecision(intent=Intent.QUESTION, act=True, reason="asks for the PO")
REPLY = SendSms(contact_id=DRIVER, text="The PO is 55821.", purpose=Purpose.REPLY)
REPLY_ARGS: dict[str, Any] = {"contact_id": DRIVER, "text": "The PO is 55821.", "purpose": "reply"}
NOTE = WriteTmsNote(text="Driver asked for the PO.")
NOTE_ARGS: dict[str, Any] = {"text": NOTE.text}


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 10, 13, hour, minute, tzinfo=LA)


def section(
    section_id: str,
    body: str,
    intents: tuple[str, ...] = (),
    stages: tuple[Stage, ...] = (),
) -> ResolvedSection:
    return ResolvedSection(
        section_id=section_id,
        title=section_id.replace("_", " ").capitalize(),
        description=f"About {section_id}.",
        intents=intents,
        stages=stages,
        body=body,
        sources=(f"standard/{section_id}.md",),
    )


HIGH_RISK_BODY = "Send an SMS to the driver to get the ETA. No ETA after 30 minutes: SMS again."
COMMUNICATION = section("communication_rules", "Be brief. Never share rates.")
QUESTIONS = section("driver_questions", "Answer from the load metadata.", (Intent.QUESTION,))
HIGH_RISK = section(
    "delay_risk_high",
    HIGH_RISK_BODY,
    (Intent.DELAY_RISK_HIGH, Intent.ETA_FOLLOW_UP_DUE),
    (Stage.TO_PICKUP, Stage.TO_DELIVERY),
)
VISIBLE = (COMMUNICATION, HIGH_RISK, QUESTIONS)


def resolved(*sections: ResolvedSection) -> ResolvedSop:
    catalog = tuple(
        CatalogEntry(section_id=s.section_id, title=s.title, description=s.description)
        for s in VISIBLE
    )
    return ResolvedSop(sections=sections, catalog=catalog)


def eta_request(status: ObligationStatus = ObligationStatus.OPEN, attempts: int = 1) -> Obligation:
    return Obligation(
        obligation_id="obl_1",
        load_id="481207",
        kind=ETA,
        status=status,
        attempts=attempts,
        opened_at=at(7, 1),
        last_action_at=at(7, 1),
    )


def context(
    load: Load,
    *,
    obligations: tuple[Obligation, ...] = (),
    attachments: tuple[Attachment, ...] = (),
    broker: Broker | None = None,
) -> RunContext:
    trigger = Event(
        event_id="msg_1",
        load_id="481207",
        type=EventType.INBOUND_MESSAGE,
        occurred_at=at(7, 12),
        channel=Channel.SMS,
        sender_contact_id=DRIVER,
        body="What's the PO number?",
        attachments=attachments,
    )
    return RunContext(
        event=trigger,
        load=load,
        now=at(7, 13),
        obligations=obligations,
        pending_timers=(),
        recent_events=(trigger,),
        recent_outbound=(),
        broker=broker or standard_broker(),
    )


def with_limits(**limits: Any) -> Broker:
    return standard_broker().model_copy(update={"limits": BrokerLimits(**limits)})


def staged(
    ctx: RunContext, tool: str, args: dict[str, Any], proposals: Sequence[Proposal] = ()
) -> tuple[str, Proposal | None]:
    """Stage one call with the default rules, after `proposals`."""
    checks = validation_context(ctx)
    return stage(tool, args, checks=checks, rules=default_rules(), proposals=proposals)


async def answer(
    ctx: RunContext,
    *calls: ToolCall,
    proposals: Sequence[Proposal] = (),
    knowledge: Any = None,
    round_cost: float = 0.0,
) -> Any:
    """`Agent.call_tools` over `calls`, with the visible sections of these tests."""
    agent = Agent(FakeModel(), knowledge or FakeSop(), PROMPTS.agent)
    return await agent.call_tools(ctx, VISIBLE, calls, proposals=proposals, round_cost=round_cost)


# --- the round: what the agent asks the model -------------------------------------------


async def test_a_round_sends_one_agent_chat_with_the_prompts_the_tools_and_the_history(
    load: Load,
) -> None:
    model = FakeModel("cheap-agent")
    model.push(ModelRole.AGENT, AgentOutcome(rationale="driver_questions"))
    broker = with_limits(max_steps=5)
    ctx = context(load, broker=broker)
    agent = Agent(model, FakeSop(), PROMPTS.agent, timeout_s=45.0)
    earlier = (
        AssistantTurn(tool_calls=(ToolCall(id="c1", name="get_load"),)),
        ToolResult(call_id="c1", name="get_load", content="## Load"),
    )

    reply = await agent.round(ctx, QUESTION, resolved(QUESTIONS), VISIBLE, earlier)

    assert reply.answer == AgentOutcome(rationale="driver_questions")
    assert reply.usage.model == "cheap-agent"
    (sent,) = model.calls
    assert (sent.role, sent.schema) == (ModelRole.AGENT, "AgentOutcome")
    request = sent.request
    assert isinstance(request, ChatRequest)
    assert (request.history, request.force_answer, request.timeout_s) == (earlier, False, 45.0)
    assert request.system == render_agent_system_prompt(PROMPTS.agent, broker)
    assert "You have at most 5 rounds of tool calls" in request.system
    assert request.user == render_agent_prompt(
        PROMPTS.agent, ctx, QUESTION, resolved(QUESTIONS), ()
    )
    assert "Answer from the load metadata." in request.user
    assert (request.system, request.user) == agent.prompts(
        ctx, QUESTION, resolved(QUESTIONS), VISIBLE
    )
    names = [t.name for t in request.tools]
    assert names == [*READ_TOOLS[:5], *ACTION_TOOLS]  # no document in view, no requirement
    assert request.tools == agent.tools(ctx, VISIBLE).specs
    assert FINISH not in names  # the answer tool is the adapter's, from the schema


async def test_a_forced_round_asks_the_model_to_answer_now(load: Load) -> None:
    model = FakeModel()
    model.push(ModelRole.AGENT, AgentOutcome())
    history = (AssistantTurn(text="Thinking about it."), UserTurn(ANSWER_NOW))

    await Agent(model, FakeSop(), PROMPTS.agent).round(
        context(load), QUESTION, resolved(QUESTIONS), VISIBLE, history, force_answer=True
    )

    request = model.calls[0].request
    assert isinstance(request, ChatRequest)
    assert request.force_answer is True
    assert request.history == history
    assert ANSWER_NOW == "Now call finish with your final answer."


async def test_a_round_that_calls_tools_returns_the_calls_and_no_answer(load: Load) -> None:
    model = FakeModel()
    lookup = call("get_load", part="stops")
    model.push(ModelRole.AGENT, Round(calls=(lookup,), text="Let me check.", cost_usd=0.001))

    reply = await Agent(model, FakeSop(), PROMPTS.agent).round(
        context(load), QUESTION, resolved(QUESTIONS), VISIBLE, ()
    )

    assert reply.answer is None
    assert reply.turn == AssistantTurn(text="Let me check.", tool_calls=(lookup,))
    assert reply.usage.cost_usd == 0.001


async def test_read_document_is_offered_when_an_attachment_is_in_view(load: Load) -> None:
    model = FakeModel()
    model.push(ModelRole.AGENT, AgentOutcome())
    bol = Attachment(attachment_id="att_1", filename="bol.jpg", content_type="image/jpeg", ref="r")

    await Agent(model, FakeSop(), PROMPTS.agent).round(
        context(load, attachments=(bol,)), QUESTION, resolved(QUESTIONS), VISIBLE, ()
    )

    request = model.calls[0].request
    assert isinstance(request, ChatRequest)
    assert "read_document" in [t.name for t in request.tools]


def test_the_procedure_of_an_open_obligation_is_in_the_prompt(load: Load) -> None:
    ctx = context(load, obligations=(eta_request(),))

    _, user = Agent(FakeModel(), FakeSop(), PROMPTS.agent).prompts(
        ctx, QUESTION, resolved(QUESTIONS), VISIBLE
    )

    assert HIGH_RISK_BODY in user


async def test_model_failure_propagates(load: Load) -> None:
    model = FakeModel()
    model.push(ModelRole.AGENT, LlmError("schema mismatch"))

    with pytest.raises(LlmError, match="schema mismatch"):
        await Agent(model, FakeSop(), PROMPTS.agent).round(
            context(load), QUESTION, resolved(QUESTIONS), VISIBLE, ()
        )


def test_the_agent_stages_with_the_default_rules_unless_given_its_own() -> None:
    names = [r.name for r in Agent(FakeModel(), FakeSop(), PROMPTS.agent).rules]

    assert names == [r.name for r in default_rules()]
    assert Agent(FakeModel(), FakeSop(), PROMPTS.agent, rules=()).rules == ()


# --- stage: one action call ---------------------------------------------------------------


def test_staging_accepts_an_action_and_numbers_it(load: Load) -> None:
    ctx = context(load)

    first, reply = staged(ctx, "send_sms", REPLY_ARGS)
    second, note = staged(ctx, "write_tms_note", NOTE_ARGS, proposals=(Proposal(REPLY, True),))

    assert (first, reply) == ("accepted as action #1", Proposal(REPLY, True))
    assert (second, note) == ("accepted as action #2", Proposal(NOTE, True))


def test_staging_refuses_by_rule_and_names_every_rule(load: Load) -> None:
    text, proposal = staged(
        context(load), "send_sms", {"contact_id": "x@y.com", "text": "Call me at 555-123-4567"}
    )

    assert text.startswith("rejected by recipient_is_contact: not a contact of load 481207")
    assert "no_contact_details_in_text: outbound text contains a phone number" in text
    assert proposal is not None and not proposal.accepted
    assert proposal.action == SendSms(contact_id="x@y.com", text="Call me at 555-123-4567")
    assert [v.rule for v in proposal.violations] == [
        "recipient_is_contact",
        "no_contact_details_in_text",
    ]


def test_staging_refuses_bad_arguments_without_proposing_an_action(load: Load) -> None:
    text, proposal = staged(context(load), "send_sms", {"text": "hi"})

    assert proposal is None
    assert text == (
        "rejected: invalid arguments for send_sms: contact_id: Field required. "
        "Fix them and call it again."
    )


def test_staging_drops_unknown_fields_and_nulls_a_small_model_fills(load: Load) -> None:
    text, proposal = staged(
        context(load), "send_sms", {**REPLY_ARGS, "subject": None, "kind": None, "x": 1}
    )

    assert (text, proposal) == ("accepted as action #1", Proposal(REPLY, True))


def test_staging_judges_each_action_against_the_ones_accepted_before(load: Load) -> None:
    ctx = context(load)
    ask = {"contact_id": DRIVER, "text": "ETA?", "purpose": "eta_request"}
    timer = {"kind": "eta_request", "due_in_minutes": 30}

    _, early = staged(ctx, "schedule_timer", timer)  # nothing open yet
    _, asked = staged(ctx, "send_sms", ask)
    assert early is not None and asked is not None
    text, late = staged(ctx, "schedule_timer", timer, proposals=(early, asked))

    assert not early.accepted and asked.accepted
    assert (text, late) == (
        "accepted as action #2",
        Proposal(ScheduleTimer(kind=ETA, due_in_minutes=30), True),
    )


def test_a_refused_action_takes_no_number_but_keeps_its_place_in_the_plan(load: Load) -> None:
    """The answer counts the accepted actions; a violation names the action's position
    among every proposed one, as `validate` of the whole plan does."""
    ctx = context(load)
    refused = Proposal(UpdateStage(stage=Stage.TO_PICKUP), False)

    text, proposal = staged(ctx, "write_tms_note", NOTE_ARGS, proposals=(refused,))
    _, again = staged(ctx, "update_stage", {"stage": "to_pickup"}, proposals=(refused,))

    assert (text, proposal) == ("accepted as action #1", Proposal(NOTE, True))
    assert again is not None
    assert [(v.rule, v.action_index) for v in again.violations] == [("stage_moves_forward", 1)]


def test_the_catalog_kinds_and_the_caps_are_the_brokers(load: Load) -> None:
    pod = ObligationDef(kind="pod", title="POD", description="The POD.", max_attempts=1)
    broker = standard_broker().model_copy(
        update={"obligations": (*standard_broker().obligations, pod)}
    )
    ctx = context(load, broker=broker)
    checks = validation_context(ctx)

    assert checks.obligation_kinds == ("eta_request", "pod")
    assert checks.config.attempt_cap("pod") == 1
    ask_pod = {"contact_id": DRIVER, "text": "POD please", "requests": "pod"}
    email_pod = {"to_contact_ids": ["dsp_4410"], "body": "POD please", "requests": "pod"}

    first, sms = staged(ctx, "send_sms", ask_pod)
    assert first == "accepted as action #1" and sms is not None
    assert staged(ctx, "send_email", email_pod, proposals=(sms,))[0].startswith(
        "rejected by attempt_cap: pod was already requested 1 times (cap 1): its requests "
        "are used up. No further message may carry requests: pod."
    )


def test_a_kind_outside_the_catalog_is_refused_but_an_open_one_can_be_closed(load: Load) -> None:
    stray = Obligation(
        obligation_id="obl_9", load_id="481207", kind="old_kind", attempts=1, opened_at=at(6, 0)
    )
    ctx = context(load, obligations=(stray,))

    assert staged(ctx, "open_obligation", {"kind": "pod"})[0].startswith(
        "rejected by known_obligation_kind: pod is not an obligation kind of this broker"
    )
    _, closed = staged(ctx, "close_obligation", {"kind": "old_kind", "status": "cancelled"})
    assert closed is not None and closed.accepted


# --- call_tools: answering a round's calls ------------------------------------------------


async def test_read_tools_answer_and_action_tools_stage_in_call_order(load: Load) -> None:
    read = call("read_sop_section", section_id="driver_questions")
    sms = call("send_sms", **REPLY_ARGS)
    note = call("write_tms_note", **NOTE_ARGS)

    done = await answer(context(load), read, sms, note, round_cost=0.002)

    body = "# driver_questions: Driver questions\n\nAnswer from the load metadata."
    assert done.results == (
        ToolResult(call_id=read.id, name="read_sop_section", content=body),
        ToolResult(call_id=sms.id, name="send_sms", content="accepted as action #1"),
        ToolResult(call_id=note.id, name="write_tms_note", content="accepted as action #2"),
    )
    assert done.steps == (
        TrajectoryStep(
            tool="read_sop_section",
            args={"section_id": "driver_questions"},
            result=body,
            accepted=None,
            cost_usd=0.002,
        ),
        TrajectoryStep(
            tool="send_sms",
            args=REPLY_ARGS,
            result="accepted as action #1",
            accepted=True,
            cost_usd=0.002,
        ),
        TrajectoryStep(
            tool="write_tms_note",
            args=NOTE_ARGS,
            result="accepted as action #2",
            accepted=True,
            cost_usd=0.002,
        ),
    )
    assert done.proposals == (Proposal(REPLY, True), Proposal(NOTE, True))
    assert (done.rejections, done.sections) == (0, ("driver_questions",))


async def test_a_round_stages_after_the_actions_of_earlier_rounds(load: Load) -> None:
    """Only the new proposals come back; the earlier ones are the run's already."""
    earlier = (Proposal(REPLY, True),)

    done = await answer(
        context(load),
        call("send_sms", **REPLY_ARGS),
        call("write_tms_note", **NOTE_ARGS),
        proposals=earlier,
    )

    repeat, note = done.steps
    assert repeat.accepted is False
    assert repeat.result.startswith("rejected by no_duplicate_action")
    assert note.result == "accepted as action #2"
    assert [p.accepted for p in done.proposals] == [False, True]
    assert done.rejections == 1


async def test_bad_arguments_are_a_refused_call_with_no_proposal(load: Load) -> None:
    done = await answer(context(load), call("send_sms", text="hi"))

    (step,) = done.steps
    assert step.accepted is False
    assert step.result.startswith("rejected: invalid arguments for send_sms: contact_id")
    assert (done.proposals, done.rejections) == ((), 1)


async def test_arguments_that_are_not_json_are_answered_and_refuse_an_action_call(
    load: Load,
) -> None:
    """The adapter hands over a call whose arguments did not parse: the model is told,
    an action call counts as refused, a read tool or the answer is simply asked again."""
    broken = [
        ToolCall(id="c1", name="send_sms", error="Expecting value: line 1 column 1"),
        ToolCall(id="c2", name="get_load", error="Unterminated string"),
        ToolCall(id="c3", name=FINISH, error="Expecting ',' delimiter"),
    ]

    done = await answer(context(load), *broken)

    assert [r.content for r in done.results] == [
        "The arguments for send_sms are not valid JSON (Expecting value: line 1 column 1). "
        "Call it again with a JSON object.",
        "The arguments for get_load are not valid JSON (Unterminated string). "
        "Call it again with a JSON object.",
        "The arguments for finish are not valid JSON (Expecting ',' delimiter). "
        "Call it again with a JSON object.",
    ]
    assert [r.call_id for r in done.results] == ["c1", "c2", "c3"]
    assert [s.accepted for s in done.steps] == [False, None, None]
    assert (done.proposals, done.rejections) == ((), 1)


async def test_an_unknown_tool_is_answered_with_the_tools_available(load: Load) -> None:
    done = await answer(context(load), call("send_fax", to="x"))

    (step,) = done.steps
    assert step.result == (
        "Unknown tool 'send_fax'. Available tools: search_sop, read_sop_section, get_history, "
        "get_ledger, get_load, " + ", ".join(ACTION_TOOLS) + "."
    )
    assert step.accepted is None
    assert (done.proposals, done.rejections) == ((), 0)


class FailingIndex(FakeSop):
    async def search(
        self, broker_id: str, load_types: Sequence[str], query: str, k: int = 5
    ) -> tuple[SearchHit, ...]:
        raise RuntimeError("index down")


async def test_a_read_tool_that_raises_is_answered_and_the_round_goes_on(load: Load) -> None:
    done = await answer(
        context(load),
        call("search_sop", query="lumper"),
        call("write_tms_note", **NOTE_ARGS),
        knowledge=FailingIndex(),
    )

    failed, note = done.results
    assert failed.content == (
        "The tool search_sop failed (RuntimeError: index down). Continue without it."
    )
    assert note.content == "accepted as action #1"
    assert done.sections == ()


async def test_the_answer_call_is_left_to_the_graph(load: Load) -> None:
    done = await answer(context(load), call(FINISH, rationale="done"))

    assert (done.results, done.steps, done.proposals, done.rejections) == ((), (), (), 0)


class HitsIndex(FakeSop):
    async def search(
        self, broker_id: str, load_types: Sequence[str], query: str, k: int = 5
    ) -> tuple[SearchHit, ...]:
        return (
            SearchHit(section_id="escalation", title="Escalation", description="Heroes.", score=2),
            SearchHit(section_id="driver_questions", title="Questions", description="Q.", score=1),
        )


async def test_the_sections_searched_are_the_hits_and_the_sections_read(load: Load) -> None:
    done = await answer(
        context(load),
        call("search_sop", query="lumper"),
        call("read_sop_section", section_id="delay_risk_high"),
        call("read_sop_section", section_id=" "),
        call("get_ledger"),
        knowledge=HitsIndex(),
    )

    assert done.sections == ("escalation", "driver_questions", "delay_risk_high")


# --- labels, procedures in progress -------------------------------------------------------


def test_a_label_counts_only_when_the_catalog_has_it() -> None:
    broker = standard_broker()

    assert catalog_intent(broker, None) is None
    assert catalog_intent(broker, "question") == "question"
    assert catalog_intent(broker, " status_update ") == "status_update"
    assert catalog_intent(broker, "eta_follow_up_due") == "eta_follow_up_due"
    assert catalog_intent(broker, "detention_request") is None


def test_open_eta_request_brings_the_follow_up_procedure(load: Load) -> None:
    ctx = context(load, obligations=(eta_request(),))

    assert sections_in_progress(ctx, resolved(QUESTIONS), VISIBLE) == (HIGH_RISK,)


def test_no_procedure_in_progress_without_an_open_obligation(load: Load) -> None:
    ctx = context(load, obligations=(eta_request(ObligationStatus.SATISFIED),))

    assert sections_in_progress(ctx, resolved(QUESTIONS), VISIBLE) == ()


def test_a_section_already_resolved_is_not_repeated(load: Load) -> None:
    ctx = context(load, obligations=(eta_request(),))

    assert sections_in_progress(ctx, resolved(HIGH_RISK, QUESTIONS), VISIBLE) == ()


def test_procedure_in_progress_respects_the_load_stage(load: Load) -> None:
    ctx = context(load.model_copy(update={"stage": Stage.AT_PICKUP}), obligations=(eta_request(),))

    assert sections_in_progress(ctx, resolved(QUESTIONS), VISIBLE) == ()


# --- the template -------------------------------------------------------------------------


async def test_a_round_describes_finish_with_the_templates_text(load: Load) -> None:
    model = FakeModel()
    model.push(ModelRole.AGENT, AgentOutcome())

    await Agent(model, FakeSop(), PROMPTS.agent).round(
        context(load), QUESTION, resolved(QUESTIONS), VISIBLE, ()
    )

    request = model.calls[0].request
    assert request.answer_description == PROMPTS.agent.tools[FINISH]
    assert PROMPTS.agent.tools[FINISH].startswith("Call it once, at the end")
    assert request.prompt_version == PROMPTS.agent.version


def test_an_agent_template_without_a_finish_description_is_refused() -> None:
    template = replace(PROMPTS.agent, tools={})

    with pytest.raises(
        PromptError, match=r"does not describe the finish tool \(its tools.finish\)"
    ):
        Agent(FakeModel(), FakeSop(), template)


def test_an_agent_template_naming_an_unknown_variable_is_refused() -> None:
    class Message:
        variables = frozenset({"ledger", "mood"})

        def format(self, variables: Mapping[str, str]) -> str:
            return ""

    with pytest.raises(PromptError, match="the user message names mood"):
        Agent(FakeModel(), FakeSop(), replace(PROMPTS.agent, user=Message()))
