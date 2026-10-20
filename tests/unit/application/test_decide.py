from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from freight_agent.adapters.llm.scripted import ScriptedModel
from freight_agent.application.context import RunContext
from freight_agent.application.decide import Decider, sections_in_progress
from freight_agent.application.prompts import DECIDE_SYSTEM_PROMPT
from freight_agent.domain.actions import Plan, Purpose, SendSms
from freight_agent.domain.events import Attachment, Event, EventType
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.load import Channel, Load, Stage
from freight_agent.domain.obligations import Obligation, ObligationKind, ObligationStatus
from freight_agent.domain.runs import Usage
from freight_agent.domain.sop import CatalogEntry, ResolvedSection, ResolvedSop
from freight_agent.ports.llm import LlmError, LlmRequest, LlmResponse, ModelRole, SchemaT

LA = ZoneInfo("America/Los_Angeles")
DRIVER = "drv_20931"
QUESTION = GateDecision(intent=Intent.QUESTION, act=True, reason="asks for the PO")
REPLY = Plan(
    actions=(SendSms(contact_id=DRIVER, text="The PO is 55821.", purpose=Purpose.REPLY),),
    rationale="driver_questions: the PO is in the instructions.",
)


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 10, 13, hour, minute, tzinfo=LA)


def section(
    section_id: str,
    body: str,
    intents: tuple[Intent, ...] = (),
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


def eta_request(status: ObligationStatus = ObligationStatus.OPEN) -> Obligation:
    return Obligation(
        obligation_id="obl_1",
        load_id="481207",
        kind=ObligationKind.ETA_REQUEST,
        status=status,
        attempts=1,
        opened_at=at(7, 1),
        last_action_at=at(7, 1),
    )


def context(
    load: Load,
    *,
    obligations: tuple[Obligation, ...] = (),
    attachments: tuple[Attachment, ...] = (),
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
    )


class TranscriptModel:
    """Returns a fixed plan with a tool transcript, like a real adapter after a tool round."""

    def __init__(self) -> None:
        self.requests: list[LlmRequest] = []

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        self.requests.append(request)
        parsed = schema.model_validate(REPLY.model_dump())
        transcript = ({"tool": "read_sop_section", "args": {"section_id": "escalation"}},)
        usage = Usage(role=request.role.value, model="m", input_tokens=900, output_tokens=60)
        return LlmResponse(parsed=parsed, usage=usage, transcript=transcript)


@pytest.mark.unit
async def test_decide_sends_one_decide_request_with_tools_limits_and_timeout(load: Load) -> None:
    model = ScriptedModel("cheap-decider")
    model.push(ModelRole.AGENT, REPLY)
    decider = Decider(model, max_tool_rounds=3, timeout_s=45.0)

    result = await decider.decide(context(load), QUESTION, resolved(QUESTIONS), VISIBLE)

    assert result.plan == REPLY
    assert [u.model for u in result.usage] == ["cheap-decider"]
    assert len(model.calls) == 1
    call = model.calls[0]
    assert (call.role, call.schema) == (ModelRole.AGENT, "AgentOutcome")
    assert call.request.max_tool_rounds == 3
    assert call.request.timeout_s == 45.0
    assert [t.name for t in call.request.tools] == ["read_sop_section"]
    assert call.request.system == DECIDE_SYSTEM_PROMPT
    assert "Answer from the load metadata." in call.request.user


@pytest.mark.unit
async def test_decide_returns_usage_and_the_tool_transcript(load: Load) -> None:
    model = TranscriptModel()

    result = await Decider(model).decide(context(load), QUESTION, resolved(QUESTIONS), VISIBLE)

    assert result.plan == REPLY
    assert result.usage[0].input_tokens == 900
    assert result.transcript[0]["tool"] == "read_sop_section"


@pytest.mark.unit
async def test_read_document_is_offered_when_the_event_has_an_attachment(load: Load) -> None:
    model = ScriptedModel()
    model.push(ModelRole.AGENT, Plan())
    bol = Attachment(attachment_id="att_1", filename="bol.jpg", content_type="image/jpeg", ref="r")

    await Decider(model).decide(
        context(load, attachments=(bol,)), QUESTION, resolved(QUESTIONS), VISIBLE
    )

    assert [t.name for t in model.calls[0].request.tools] == ["read_sop_section", "read_document"]


@pytest.mark.unit
async def test_the_procedure_of_an_open_obligation_is_in_the_prompt(load: Load) -> None:
    model = ScriptedModel()
    model.push(ModelRole.AGENT, REPLY)

    await Decider(model).decide(
        context(load, obligations=(eta_request(),)), QUESTION, resolved(QUESTIONS), VISIBLE
    )

    assert HIGH_RISK_BODY in model.calls[0].request.user


@pytest.mark.unit
async def test_model_failure_propagates(load: Load) -> None:
    model = ScriptedModel()
    model.push(ModelRole.AGENT, LlmError("schema mismatch"))

    with pytest.raises(LlmError, match="schema mismatch"):
        await Decider(model).decide(context(load), QUESTION, resolved(QUESTIONS), VISIBLE)


@pytest.mark.unit
def test_open_eta_request_brings_the_follow_up_procedure(load: Load) -> None:
    ctx = context(load, obligations=(eta_request(),))

    assert sections_in_progress(ctx, resolved(QUESTIONS), VISIBLE) == (HIGH_RISK,)


@pytest.mark.unit
def test_no_procedure_in_progress_without_an_open_obligation(load: Load) -> None:
    ctx = context(load, obligations=(eta_request(ObligationStatus.SATISFIED),))

    assert sections_in_progress(ctx, resolved(QUESTIONS), VISIBLE) == ()


@pytest.mark.unit
def test_a_section_already_resolved_is_not_repeated(load: Load) -> None:
    ctx = context(load, obligations=(eta_request(),))

    assert sections_in_progress(ctx, resolved(HIGH_RISK, QUESTIONS), VISIBLE) == ()


@pytest.mark.unit
def test_procedure_in_progress_respects_the_load_stage(load: Load) -> None:
    ctx = context(load.model_copy(update={"stage": Stage.AT_PICKUP}), obligations=(eta_request(),))

    assert sections_in_progress(ctx, resolved(QUESTIONS), VISIBLE) == ()
