"""The agent: its request, its staging, and the fold of a transcript into the staged plan."""

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from freight_agent.adapters.llm.scripted import ScriptedModel, ScriptedReply
from freight_agent.application.agent import (
    READ_ONLY_ROUNDS,
    Agent,
    Proposal,
    Staged,
    Staging,
    catalog_intent,
    merge_plan,
    sections_in_progress,
    stage_from_transcript,
    validation_context,
)
from freight_agent.application.agent_tools import ACTION_TOOLS, READ_TOOLS
from freight_agent.application.context import RunContext
from freight_agent.application.prompts import render_agent_system_prompt
from freight_agent.domain.actions import (
    AgentOutcome,
    CreateHeroTask,
    Plan,
    Purpose,
    ScheduleTimer,
    SendSms,
    WriteTmsNote,
)
from freight_agent.domain.broker import Broker, BrokerLimits, ObligationDef, standard_broker
from freight_agent.domain.events import Attachment, Event, EventType
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.load import Channel, Load, Stage
from freight_agent.domain.obligations import Obligation, ObligationKind, ObligationStatus
from freight_agent.domain.rules import default_rules
from freight_agent.domain.runs import TrajectoryStep
from freight_agent.domain.sop import CatalogEntry, ResolvedSection, ResolvedSop
from freight_agent.ports.llm import LlmError, ModelRole
from tests.unit.application.test_runtime_harness import FakeSop
from tests.unit.application.transcripts import Transcript, calls

pytestmark = pytest.mark.unit

LA = ZoneInfo("America/Los_Angeles")
DRIVER = "drv_20931"
ETA = ObligationKind.ETA_REQUEST
QUESTION = GateDecision(intent=Intent.QUESTION, act=True, reason="asks for the PO")
REPLY = SendSms(contact_id=DRIVER, text="The PO is 55821.", purpose=Purpose.REPLY)
REPLY_ARGS: dict[str, Any] = {"contact_id": DRIVER, "text": "The PO is 55821.", "purpose": "reply"}
NOTE = WriteTmsNote(text="Driver asked for the PO.")


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


def staging(ctx: RunContext, max_rejections: int = 3) -> Staging:
    return Staging(validation_context(ctx), default_rules(), max_rejections)


def fold(ctx: RunContext, transcript: Any, offered: Any = ACTION_TOOLS + READ_TOOLS) -> Staged:
    return stage_from_transcript(
        transcript,
        checks=validation_context(ctx),
        rules=default_rules(),
        max_rejections=3,
        offered=offered,
    )


# --- the request ------------------------------------------------------------------------


async def test_the_agent_sends_one_agent_request_inside_the_brokers_budgets(load: Load) -> None:
    model = ScriptedModel("cheap-agent")
    model.push(ModelRole.AGENT, Plan(actions=(REPLY,), rationale="driver_questions"))
    broker = with_limits(max_steps=5, max_cost_usd=0.02)
    agent = Agent(model, FakeSop(), timeout_s=45.0)

    result = await agent.run(context(load, broker=broker), QUESTION, resolved(QUESTIONS), VISIBLE)

    assert result.plan == Plan(actions=(REPLY,), rationale="driver_questions")
    assert [u.model for u in result.usage] == ["cheap-agent"]
    (call,) = model.calls
    assert (call.role, call.schema) == (ModelRole.AGENT, "AgentOutcome")
    request = call.request
    assert (request.max_tool_rounds, request.max_cost_usd, request.timeout_s) == (5, 0.02, 45.0)
    assert request.system == render_agent_system_prompt(broker)
    assert "Answer from the load metadata." in request.user
    names = [t.name for t in request.tools]
    assert names == [*READ_TOOLS[:5], *ACTION_TOOLS]  # no document in view, no requirement


async def test_with_max_steps_zero_the_agent_has_read_tools_for_two_rounds(load: Load) -> None:
    model = ScriptedModel()
    model.push(ModelRole.AGENT, Plan())

    await Agent(model, FakeSop()).run(
        context(load, broker=with_limits(max_steps=0)), QUESTION, resolved(QUESTIONS), VISIBLE
    )

    request = model.calls[0].request
    assert request.max_tool_rounds == READ_ONLY_ROUNDS == 2
    assert not set(ACTION_TOOLS) & {t.name for t in request.tools}
    assert "You return a plan" in request.system


async def test_read_document_is_offered_when_an_attachment_is_in_view(load: Load) -> None:
    model = ScriptedModel()
    model.push(ModelRole.AGENT, Plan())
    bol = Attachment(attachment_id="att_1", filename="bol.jpg", content_type="image/jpeg", ref="r")

    await Agent(model, FakeSop()).run(
        context(load, attachments=(bol,)), QUESTION, resolved(QUESTIONS), VISIBLE
    )

    assert "read_document" in [t.name for t in model.calls[0].request.tools]


async def test_the_procedure_of_an_open_obligation_is_in_the_prompt(load: Load) -> None:
    model = ScriptedModel()
    model.push(ModelRole.AGENT, Plan())

    await Agent(model, FakeSop()).run(
        context(load, obligations=(eta_request(),)), QUESTION, resolved(QUESTIONS), VISIBLE
    )

    assert HIGH_RISK_BODY in model.calls[0].request.user


async def test_model_failure_propagates(load: Load) -> None:
    model = ScriptedModel()
    model.push(ModelRole.AGENT, LlmError("schema mismatch"))

    with pytest.raises(LlmError, match="schema mismatch"):
        await Agent(model, FakeSop()).run(context(load), QUESTION, resolved(QUESTIONS), VISIBLE)


async def test_the_live_tools_stage_and_answer_while_the_fold_decides(load: Load) -> None:
    """A model that runs the offered action tool (as a live adapter does) gets the staging's
    answer; the plan is still the fold over the transcript it returns."""

    class LiveModel(ScriptedModel):
        answers: list[str]

        async def generate(self, request: Any, schema: Any) -> Any:
            tool = next(t for t in request.tools if t.name == "send_sms")
            self.answers = [await tool.run(REPLY_ARGS), await tool.run(REPLY_ARGS)]
            self.push(
                ModelRole.AGENT,
                ScriptedReply(
                    AgentOutcome(),
                    Transcript()
                    .round(("send_sms", REPLY_ARGS), results=(self.answers[0],))
                    .round(("send_sms", REPLY_ARGS), results=(self.answers[1],))
                    .build(),
                ),
            )
            return await super().generate(request, schema)

    model = LiveModel()

    result = await Agent(model, FakeSop()).run(
        context(load), QUESTION, resolved(QUESTIONS), VISIBLE
    )

    assert model.answers[0] == "accepted as action #1"
    assert model.answers[1].startswith("rejected by no_duplicate_action")
    assert [s.result for s in result.staged.trajectory] == model.answers
    assert result.plan.actions == (REPLY, REPLY)
    assert result.staged.accepted == (REPLY,)
    assert result.staged.rejections == 1


async def test_the_answer_labels_the_event_and_says_when_no_procedure_applies(
    load: Load,
) -> None:
    model = ScriptedModel()
    model.push(ModelRole.AGENT, AgentOutcome(intent=" status_update ", no_procedure=True))

    result = await Agent(model, FakeSop()).run(
        context(load), QUESTION, resolved(QUESTIONS), VISIBLE
    )

    assert (result.intent, result.no_procedure) == (Intent.STATUS_UPDATE, True)


# --- staging ------------------------------------------------------------------------------


def test_staging_accepts_an_action_and_numbers_it(load: Load) -> None:
    stage = staging(context(load))

    assert stage.answer("send_sms", REPLY_ARGS) == ("accepted as action #1", True)
    assert stage.answer("write_tms_note", {"text": NOTE.text}) == ("accepted as action #2", True)
    assert stage.accepted == (REPLY, NOTE)
    assert stage.rejections == 0


def test_staging_refuses_by_rule_and_names_every_rule(load: Load) -> None:
    stage = staging(context(load))

    text, accepted = stage.answer(
        "send_sms", {"contact_id": "x@y.com", "text": "Call me at 555-123-4567"}
    )

    assert not accepted
    assert text.startswith("rejected by recipient_is_contact: not a contact of load 481207")
    assert "no_contact_details_in_text: outbound text contains a phone number" in text
    assert stage.rejections == 1
    assert stage.accepted == ()
    (proposal,) = stage.proposals
    assert [v.rule for v in proposal.violations] == [
        "recipient_is_contact",
        "no_contact_details_in_text",
    ]


def test_staging_refuses_bad_arguments_without_proposing_an_action(load: Load) -> None:
    stage = staging(context(load))

    text, accepted = stage.answer("send_sms", {"text": "hi"})

    assert not accepted
    assert text == (
        "rejected: invalid arguments for send_sms: contact_id: Field required. "
        "Fix them and call it again."
    )
    assert (stage.proposals, stage.rejections) == ([], 1)


def test_staging_drops_unknown_fields_and_nulls_a_small_model_fills(load: Load) -> None:
    stage = staging(context(load))

    text, _ = stage.answer("send_sms", {**REPLY_ARGS, "subject": None, "kind": None, "x": 1})

    assert text == "accepted as action #1"


def test_staging_judges_each_action_against_the_ones_accepted_before(load: Load) -> None:
    stage = staging(context(load))
    ask = {"contact_id": DRIVER, "text": "ETA?", "purpose": "eta_request"}
    timer = {"kind": "eta_request", "due_in_minutes": 30}

    assert stage.answer("schedule_timer", timer)[1] is False  # nothing open yet
    assert stage.answer("send_sms", ask)[1] is True
    assert stage.answer("schedule_timer", timer) == ("accepted as action #2", True)


def test_over_the_rejection_budget_the_answer_tells_the_model_to_finish(load: Load) -> None:
    stage = staging(context(load), max_rejections=1)
    refused = {"stage": "to_pickup"}

    first, _ = stage.answer("update_stage", refused)
    second, _ = stage.answer("update_stage", refused)
    third, _ = stage.answer("send_sms", {"text": "no contact"})

    assert "call finish" not in first
    assert second.endswith(
        ". More than 1 actions were rejected: call finish now; a person will take over the event."
    )
    assert "Fix them and call it again. More than 1" in third
    assert stage.rejections == 3


def test_the_catalog_kinds_and_the_caps_are_the_brokers(load: Load) -> None:
    pod = ObligationDef(kind="pod", title="POD", description="The POD.", max_attempts=1)
    broker = standard_broker().model_copy(
        update={"obligations": (*standard_broker().obligations, pod)}
    )
    checks = validation_context(context(load, broker=broker))

    assert checks.obligation_kinds == ("eta_request", "pod")
    assert checks.config.attempt_cap("pod") == 1
    stage = Staging(checks, default_rules(), 3)
    ask_pod = {"contact_id": DRIVER, "text": "POD please", "requests": "pod"}
    email_pod = {"to_contact_ids": ["dsp_4410"], "body": "POD please", "requests": "pod"}

    assert stage.answer("send_sms", ask_pod) == ("accepted as action #1", True)
    assert stage.answer("send_email", email_pod) == (
        "rejected by attempt_cap: pod was already requested 1 times (cap 1): its requests "
        "are used up",
        False,
    )


def test_a_kind_outside_the_catalog_is_refused_but_an_open_one_can_be_closed(load: Load) -> None:
    stray = Obligation(
        obligation_id="obl_9", load_id="481207", kind="old_kind", attempts=1, opened_at=at(6, 0)
    )
    stage = staging(context(load, obligations=(stray,)))

    assert stage.answer("open_obligation", {"kind": "pod"})[0].startswith(
        "rejected by known_obligation_kind: pod is not an obligation kind of this broker"
    )
    assert stage.answer("close_obligation", {"kind": "old_kind", "status": "cancelled"})[1]


# --- the fold -----------------------------------------------------------------------------


def test_the_fold_stages_the_action_calls_in_order_with_their_rounds_cost(load: Load) -> None:
    transcript = (
        Transcript()
        .round(("read_sop_section", {"section_id": "escalation"}), results=("# escalation",))
        .round(("send_sms", REPLY_ARGS), ("write_tms_note", {"text": NOTE.text}), cost=0.002)
        .build()
    )

    staged = fold(context(load), transcript)

    assert staged.accepted == staged.proposed == (REPLY, NOTE)
    assert staged.trajectory == (
        TrajectoryStep(
            tool="read_sop_section",
            args={"section_id": "escalation"},
            result="# escalation",
            accepted=None,
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
            args={"text": NOTE.text},
            result="accepted as action #2",
            accepted=True,
            cost_usd=0.002,
        ),
    )
    assert staged.sections_searched == ("escalation",)


def test_the_fold_is_order_dependent(load: Load) -> None:
    ask = ("send_sms", {"contact_id": DRIVER, "text": "ETA?", "purpose": "eta_request"})
    timer = ("schedule_timer", {"kind": "eta_request", "due_in_minutes": 30})

    assert fold(context(load), calls(ask, timer)).accepted == (
        SendSms(contact_id=DRIVER, text="ETA?", purpose=Purpose.ETA_REQUEST),
        ScheduleTimer(kind=ETA, due_in_minutes=30),
    )
    reversed_order = fold(context(load), calls(timer, ask))
    assert [s.accepted for s in reversed_order.trajectory] == [False, True]
    assert reversed_order.rejections == 1


def test_the_fold_skips_the_answer_and_tools_that_were_not_offered(load: Load) -> None:
    transcript = calls(
        ("send_sms", REPLY_ARGS), ("finish", {"rationale": "done"}), ("send_fax", {"to": "x"})
    )

    staged = fold(context(load), transcript, offered=READ_TOOLS)

    assert staged.proposals == ()
    assert [(s.tool, s.accepted) for s in staged.trajectory] == [
        ("send_sms", None),
        ("send_fax", None),
    ]


def test_the_fold_reads_the_hits_of_a_search_and_the_sections_read(load: Load) -> None:
    hits = (
        'Sections that match "lumper", best first (read one with read_sop_section):\n'
        "- escalation: Escalation. When to call a Hero.\n"
        "- driver_questions: Driver questions. Answer from the load."
    )
    transcript = (
        Transcript()
        .round(("search_sop", {"query": "lumper"}), results=(hits,))
        .round(("read_sop_section", {"section_id": "escalation"}), results=("# escalation",))
        .round(("read_sop_section", {"section_id": " "}), ("search_sop", {"query": "x"}))
        .build()
    )

    staged = fold(context(load), transcript)

    assert staged.sections_searched == ("escalation", "driver_questions")


def test_the_fold_tolerates_malformed_entries(load: Load) -> None:
    transcript: tuple[dict[str, Any], ...] = (
        {"role": "assistant", "content": "", "tool_calls": "not a list"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": ["x", {"args": {}}, {"name": "send_sms"}],
        },
        {"role": "assistant", "content": "", "tool_calls": [{"name": "send_sms", "args": "{}"}]},
        {"role": "assistant", "usage": {"cost_usd": True}, "tool_calls": []},
        {"role": "tool", "tool_call_id": 3, "content": "x"},
    )

    staged = fold(context(load), transcript)

    assert [s.tool for s in staged.trajectory] == ["send_sms", "send_sms"]
    assert staged.rejections == 2  # no arguments: contact_id and text are missing


def test_a_recorded_answer_that_the_rules_no_longer_give_is_logged(
    load: Load, caplog: pytest.LogCaptureFixture
) -> None:
    transcript = Transcript().round(("send_sms", REPLY_ARGS), results=("accepted as #9",)).build()

    with caplog.at_level(logging.INFO, logger="freight_agent.application.agent"):
        staged = fold(context(load), transcript)

    assert staged.trajectory[0].result == "accepted as action #1"
    assert "the rules now answer 'accepted as action #1'" in caplog.text


# --- merge, labels, procedures in progress ------------------------------------------------


def test_the_plan_is_every_staged_call_then_the_answers_own_actions() -> None:
    """Refused calls stay in the plan (the validator reports them again, at the same
    index); an answer action that repeats a staged call is not added twice."""
    staged = Staged(proposals=(Proposal(REPLY, True), Proposal(NOTE, False)))
    task = CreateHeroTask(title="t", description="d")

    merged = merge_plan(staged, AgentOutcome(rationale="r", actions=(REPLY, NOTE, task)))

    assert merged == Plan(rationale="r", actions=(REPLY, NOTE, task))
    assert type(merged) is Plan


def test_a_label_counts_only_when_the_catalog_has_it() -> None:
    broker = standard_broker()

    assert catalog_intent(broker, None) is None
    assert catalog_intent(broker, "question") == "question"
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
