from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from freight_agent.application.context import RunContext
from freight_agent.application.tools import (
    READ_DOCUMENT,
    READ_SOP_SECTION,
    attachments_in_view,
    decide_tools,
    read_document_tool,
    read_sop_section_tool,
)
from freight_agent.domain.events import Attachment, Event, EventType
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Channel, Load
from freight_agent.domain.sop import ResolvedSection

LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 13, 7, 13, tzinfo=LA)

BOL = Attachment(
    attachment_id="att_1", filename="bol.jpg", content_type="image/jpeg", ref="s3://b/1"
)
POD = Attachment(
    attachment_id="att_2", filename="pod.pdf", content_type="application/pdf", ref="r2"
)


def section(section_id: str, body: str) -> ResolvedSection:
    return ResolvedSection(
        section_id=section_id,
        title=section_id.replace("_", " ").capitalize(),
        description=f"About {section_id}.",
        intents=(Intent.QUESTION,),
        stages=(),
        body=body,
        sources=(f"standard/{section_id}.md",),
    )


SECTIONS = (
    section("driver_questions", "Answer from the load metadata."),
    section("escalation", "Create a Hero task when you cannot act."),
)


def message(event_id: str, *attachments: Attachment) -> Event:
    return Event(
        event_id=event_id,
        load_id="481207",
        type=EventType.INBOUND_MESSAGE,
        occurred_at=NOW,
        channel=Channel.SMS,
        sender_contact_id="drv_20931",
        body="here",
        attachments=attachments,
    )


def context(load: Load, event: Event, *earlier: Event) -> RunContext:
    return RunContext(
        event=event,
        load=load,
        now=NOW,
        obligations=(),
        pending_timers=(),
        recent_events=(*earlier, event),
        recent_outbound=(),
    )


@pytest.mark.unit
async def test_read_sop_section_returns_title_and_body() -> None:
    tool = read_sop_section_tool(SECTIONS)

    text = await tool.run({"section_id": "escalation"})

    assert tool.name == READ_SOP_SECTION
    assert "escalation: Escalation" in text
    assert "Create a Hero task when you cannot act." in text


@pytest.mark.unit
async def test_read_sop_section_tolerates_surrounding_spaces() -> None:
    text = await read_sop_section_tool(SECTIONS).run({"section_id": " escalation "})

    assert "Create a Hero task" in text


@pytest.mark.unit
async def test_unknown_section_lists_the_valid_ids() -> None:
    text = await read_sop_section_tool(SECTIONS).run({"section_id": "pricing"})

    assert "pricing" in text
    assert "driver_questions, escalation" in text


@pytest.mark.unit
async def test_missing_section_argument_is_explained_not_raised() -> None:
    text = await read_sop_section_tool(SECTIONS).run({})

    assert "section_id" in text
    assert "driver_questions, escalation" in text


@pytest.mark.unit
async def test_read_sop_section_without_sections_says_so() -> None:
    text = await read_sop_section_tool(()).run({"section_id": "escalation"})

    assert "No SOP sections" in text


@pytest.mark.unit
def test_read_sop_section_schema_enumerates_the_ids() -> None:
    params = read_sop_section_tool(SECTIONS).parameters

    assert params["required"] == ["section_id"]
    assert params["properties"]["section_id"]["enum"] == ["driver_questions", "escalation"]
    assert params["additionalProperties"] is False


@pytest.mark.unit
async def test_read_document_returns_metadata_and_asks_for_a_hero_review() -> None:
    tool = read_document_tool((BOL, POD))

    text = await tool.run({"attachment_id": "att_1"})

    assert tool.name == READ_DOCUMENT
    assert "bol.jpg" in text
    assert "image/jpeg" in text
    assert "s3://b/1" in text
    assert "not available" in text
    assert "Hero task" in text
    assert tool.parameters["properties"]["attachment_id"]["enum"] == ["att_1", "att_2"]


@pytest.mark.unit
async def test_unknown_attachment_lists_the_valid_ids() -> None:
    text = await read_document_tool((BOL, POD)).run({"attachment_id": 7})

    assert "att_1, att_2" in text


@pytest.mark.unit
async def test_read_document_without_attachments_says_so() -> None:
    text = await read_document_tool(()).run({"attachment_id": "att_1"})

    assert "No attachments" in text


@pytest.mark.unit
def test_attachments_come_from_the_event_then_recent_events_without_duplicates(load: Load) -> None:
    earlier = message("msg_0", POD, BOL)
    ctx = context(load, message("msg_1", BOL), earlier)

    assert attachments_in_view(ctx) == (BOL, POD)


@pytest.mark.unit
def test_decide_tools_offer_read_document_only_when_there_is_an_attachment(load: Load) -> None:
    plain = decide_tools(context(load, message("msg_1")), SECTIONS)
    with_doc = decide_tools(context(load, message("msg_1", BOL)), SECTIONS)

    assert [t.name for t in plain] == [READ_SOP_SECTION]
    assert [t.name for t in with_doc] == [READ_SOP_SECTION, READ_DOCUMENT]
