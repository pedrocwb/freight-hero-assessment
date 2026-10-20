"""The agent's tools: what each read tool answers, and the action tools' schemas."""

import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from freight_agent.application.agent_tools import ACTION_TOOLS, agent_tools, build_action
from freight_agent.application.context import RunContext
from freight_agent.domain.actions import Purpose, SendEmail, SendSms
from freight_agent.domain.broker import DocumentRequirement, standard_broker
from freight_agent.domain.documents import Document, DocumentStatus, FieldCheck
from freight_agent.domain.events import Attachment, Event, EventType
from freight_agent.domain.load import Channel, Load, Stage
from freight_agent.domain.obligations import Obligation, ObligationStatus
from freight_agent.domain.sop import ResolvedSection, SearchHit
from freight_agent.domain.timers import Timer
from freight_agent.ports.llm import ToolSpec
from tests.unit.application.test_runtime_harness import FakeSop

pytestmark = pytest.mark.unit

LA = ZoneInfo("America/Los_Angeles")
DRIVER = "drv_20931"
BOL = Attachment(attachment_id="att_1", filename="bol.jpg", content_type="image/jpeg", ref="r1")
POD_AT_DELIVERY = DocumentRequirement(
    type_id="pod", at_stage=Stage.AT_DELIVERY, within_minutes=120, opens_obligation="pod"
)


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 10, 13, hour, minute, tzinfo=LA)


def sms_event(event_id: str, when: datetime, body: str, **extra: Any) -> Event:
    return Event(
        event_id=event_id,
        load_id="481207",
        type=EventType.INBOUND_MESSAGE,
        occurred_at=when,
        channel=Channel.SMS,
        sender_contact_id=DRIVER,
        body=body,
        **extra,
    )


def document(status: DocumentStatus = DocumentStatus.PROCESSED, **changes: Any) -> Document:
    base = Document(
        document_id="doc_1",
        load_id="481207",
        attachment_id="att_1",
        broker_id="northline",
        blob_ref="sha256:abc",
        filename="pod.pdf",
        content_type="application/pdf",
        status=status,
        type_id="pod" if status is not DocumentStatus.PENDING else None,
        confidence=0.93 if status is not DocumentStatus.PENDING else None,
        extracted={"receiver": "Valley DC", "po_numbers": ["55821"]},
        checks=(
            FieldCheck(field="po_numbers", expected="55821", found="55821", ok=True),
            FieldCheck(field="signed", expected="true", found="false", ok=False),
        ),
        text="Received by Valley DC. PO 55821.",
        pages=1,
        received_at=at(9, 40),
    )
    return base.model_copy(update=changes)


def context(load: Load, **changes: Any) -> RunContext:
    trigger = sms_event("msg_9", at(9, 45), "here is the POD", attachments=(BOL,))
    defaults: dict[str, Any] = {
        "event": trigger,
        "load": load,
        "now": at(9, 46),
        "obligations": (),
        "pending_timers": (),
        "recent_events": (trigger,),
        "recent_outbound": (),
    }
    return RunContext(**{**defaults, **changes})


SECTIONS = (
    ResolvedSection(
        section_id="escalation",
        title="Escalation",
        description="When to call a Hero.",
        intents=(),
        stages=(),
        body="Create a Hero task when you cannot act.\n",
        sources=("standard/escalation.md",),
    ),
)


def tools(ctx: RunContext, *, stage: Any = None, knowledge: Any = None) -> dict[str, ToolSpec]:
    specs = agent_tools(ctx, SECTIONS, knowledge or FakeSop(), stage=stage)
    return {t.name: t for t in specs}


async def call(ctx: RunContext, name: str, **args: Any) -> str:
    return await tools(ctx)[name].run(args)


# --- which tools are offered --------------------------------------------------------------


def test_the_read_tools_are_offered_and_the_action_tools_only_with_a_staging(load: Load) -> None:
    quiet = context(load, event=sms_event("m1", at(9, 0), "ok"), recent_events=())

    assert list(tools(quiet)) == [
        "search_sop",
        "read_sop_section",
        "get_history",
        "get_ledger",
        "get_load",
    ]
    staged = tools(quiet, stage=lambda name, args: ("accepted as action #1", True))
    assert list(staged)[5:] == list(ACTION_TOOLS)


def test_read_document_needs_a_document_or_an_attachment_in_view(load: Load) -> None:
    assert "read_document" in tools(context(load))
    quiet = context(load, event=sms_event("m1", at(9, 0), "ok"), recent_events=())
    assert "read_document" not in tools(quiet)
    assert "read_document" in tools(replace(quiet, documents=(document(),)))


def test_list_document_requirements_is_offered_when_the_broker_requires_one(load: Load) -> None:
    requiring = standard_broker().model_copy(update={"documents": (POD_AT_DELIVERY,)})

    assert "list_document_requirements" not in tools(context(load))
    assert "list_document_requirements" in tools(context(load, broker=requiring))


def test_every_tool_schema_is_an_object_with_properties(load: Load) -> None:
    """Gemini refuses an object schema with no properties, so even the tools that need no
    argument take an optional filter."""
    requiring = standard_broker().model_copy(update={"documents": (POD_AT_DELIVERY,)})
    every = tools(
        context(load, broker=requiring), stage=lambda name, args: ("accepted as action #1", True)
    )

    for spec in every.values():
        assert spec.parameters["type"] == "object"
        assert spec.parameters["properties"], spec.name
        assert spec.parameters["additionalProperties"] is False


# --- read tools ---------------------------------------------------------------------------


class RecordingIndex(FakeSop):
    def __init__(self, hits: Sequence[SearchHit]) -> None:
        super().__init__()
        self.hits = tuple(hits)
        self.queries: list[tuple[str, tuple[str, ...], str, int]] = []

    async def search(
        self, broker_id: str, load_types: Sequence[str], query: str, k: int = 5
    ) -> tuple[SearchHit, ...]:
        self.queries.append((broker_id, tuple(load_types), query, k))
        return self.hits


async def test_search_sop_lists_the_hits_of_the_loads_broker_best_first(load: Load) -> None:
    index = RecordingIndex(
        (
            SearchHit(section_id="escalation", title="Escalation", description="Heroes.", score=3),
            SearchHit(section_id="documents", title="Documents", description="Docs.", score=1),
        )
    )
    reefer = load.model_copy(update={"load_types": ("reefer",)})

    answer = await tools(context(reefer), knowledge=index)["search_sop"].run({"query": " lumper "})

    assert index.queries == [("northline", ("reefer",), "lumper", 5)]
    assert answer == (
        'Sections that match "lumper", best first (read one with read_sop_section):\n'
        "- escalation: Escalation. Heroes.\n"
        "- documents: Documents. Docs."
    )


async def test_search_sop_says_when_nothing_matches_or_the_query_is_missing(load: Load) -> None:
    index = RecordingIndex(())
    search = tools(context(load), knowledge=index)["search_sop"]

    assert (await search.run({"query": "x"})).startswith('No SOP section matches "x".')
    assert (await search.run({})).startswith("Missing query")
    assert len(index.queries) == 1


async def test_search_sop_reads_the_memory_index_of_the_load(load: Load) -> None:
    answer = await call(context(load), "search_sop", query="delay risk high ETA")

    assert "- delay_risk_high:" in answer


async def test_read_sop_section_returns_the_body_or_the_valid_ids(load: Load) -> None:
    ctx = context(load)

    assert await call(ctx, "read_sop_section", section_id="escalation") == (
        "# escalation: Escalation\n\nCreate a Hero task when you cannot act."
    )
    assert await call(ctx, "read_sop_section", section_id="nope") == (
        "Unknown section_id 'nope'. Valid section_id values: \"escalation\"."
    )
    assert await call(ctx, "read_sop_section") == (
        'Missing section_id. Valid section_id values: "escalation".'
    )
    spec = tools(ctx)["read_sop_section"]
    assert spec.parameters["properties"]["section_id"]["enum"] == ["escalation"]


async def test_read_sop_section_without_sections_says_so(load: Load) -> None:
    (spec,) = [
        t
        for t in agent_tools(context(load), (), FakeSop(), stage=None)
        if t.name == "read_sop_section"
    ]

    assert await spec.run({"section_id": "x"}) == "No SOP sections are available for this load."


async def test_get_history_reads_the_longer_window_in_the_transcripts_format(load: Load) -> None:
    old = [sms_event(f"m{i}", at(6, i), f"msg {i}") for i in range(40)]
    trigger = sms_event("msg_9", at(9, 45), "where do I park?")
    ctx = context(load, event=trigger, recent_events=(*old[-5:], trigger), history=(*old, trigger))

    default = await call(ctx, "get_history")
    three = await call(ctx, "get_history", limit=3)

    assert default.startswith("History (41 of the 41 events read for this run, oldest first):")
    assert '- 06:00 in sms from drv_20931 (driver): "msg 0"' in default
    assert three.splitlines() == [
        "History (3 of the 41 events read for this run, oldest first):",
        '- 06:38 in sms from drv_20931 (driver): "msg 38"',
        '- 06:39 in sms from drv_20931 (driver): "msg 39"',
        '- 09:45 in sms from drv_20931 (driver): "where do I park?"  <- this event',
    ]
    assert await call(ctx, "get_history", limit="all") == default
    assert await call(ctx, "get_history", limit=True) == default
    assert "41 events are available" in tools(ctx)["get_history"].description


async def test_get_history_falls_back_to_the_recent_events(load: Load) -> None:
    answer = await call(context(load), "get_history", limit=10)

    assert answer.startswith("History (1 of the 1 events read for this run")


async def test_get_ledger_lists_every_obligation_and_timer_or_one_kind(load: Load) -> None:
    eta = Obligation(
        obligation_id="obl_1",
        load_id="481207",
        kind="eta_request",
        attempts=2,
        opened_at=at(7, 1),
        last_action_at=at(7, 31),
        active_timer_id="timer_1",
    )
    closed = [
        eta.model_copy(
            update={
                "obligation_id": f"obl_c{i}",
                "status": ObligationStatus.SATISFIED,
                "opened_at": at(5, i),
                "active_timer_id": None,
            }
        )
        for i in range(5)
    ]
    pod = Obligation(obligation_id="obl_2", load_id="481207", kind="pod", opened_at=at(9, 0))
    timer = Timer(
        timer_id="timer_1",
        load_id="481207",
        obligation_id="obl_1",
        due_at=at(10, 0),
        created_at=at(9, 30),
    )
    ctx = context(load, obligations=(*closed, eta, pod), pending_timers=(timer,))

    ledger = await call(ctx, "get_ledger")
    only_pod = await call(ctx, "get_ledger", kind="pod")

    assert ledger.count("- eta_request: satisfied") == 5  # every closed one, not the last 3
    assert "- eta_request: attempts 2;" in ledger
    assert "- pod: attempts 0;" in ledger
    assert "- due 10:00 (in 14 min): follow-up for eta_request" in ledger
    assert "eta_request" not in only_pod
    assert only_pod.splitlines()[-1] == "- none"  # no timer for pod
    assert tools(ctx)["get_ledger"].parameters["properties"]["kind"]["enum"] == [
        "eta_request",
        "pod",
    ]


async def test_get_load_gives_the_load_or_one_part(load: Load) -> None:
    ctx = context(load)

    whole = await call(ctx, "get_load")
    stops = await call(ctx, "get_load", part="stops")
    contacts = await call(ctx, "get_load", part="contacts")

    assert "PO 55821 required at gate" in whole and "## Contacts" in whole
    assert stops.startswith("## Stops\n- stop 1 (current), pickup: Riverbend plant")
    assert "## Contacts" not in stops
    assert contacts.startswith("## Contacts") and "- drv_20931: driver; channels: sms" in contacts


async def test_read_document_returns_a_processed_document_with_its_checks(load: Load) -> None:
    ctx = context(load, documents=(document(),))

    by_attachment = await call(ctx, "read_document", id="att_1")
    by_document = await call(ctx, "read_document", id="doc_1")

    assert by_attachment == by_document
    assert by_document.splitlines() == [
        'document "doc_1" (attachment "att_1")',
        'filename: "pod.pdf"',
        "status: processed",
        'type: "pod"',
        "confidence: 0.93",
        "pages: 1",
        'values: {"po_numbers": ["55821"], "receiver": "Valley DC"}',
        "checks against the load:",
        '- po_numbers: ok; expected "55821", found "55821"',
        '- signed: MISMATCH; expected "true", found "false"',
        'text: "Received by Valley DC. PO 55821."',
    ]


async def test_read_document_says_pending_until_the_pipeline_has_read_it(load: Load) -> None:
    ctx = context(load, documents=(document(DocumentStatus.PENDING),))

    answer = await call(ctx, "read_document", id="att_1")

    assert answer.splitlines()[2:] == [
        "status: pending",
        "The document pipeline has not read it yet. A document_processed event follows when "
        "it has; that run acts on the document, not this one.",
    ]


async def test_read_document_tolerates_a_status_saved_as_plain_text(load: Load) -> None:
    raw = document().model_copy(update={"status": "needs_review", "confidence": None, "text": ""})

    answer = await call(context(load, documents=(raw,)), "read_document", id="doc_1")

    assert "status: needs_review" in answer
    assert "confidence" not in answer and "text:" not in answer


async def test_an_attachment_the_pipeline_never_saw_goes_to_a_hero(load: Load) -> None:
    answer = await call(context(load), "read_document", id="att_1")

    assert 'filename: "bol.jpg"' in answer
    assert "create a Hero task to review the document" in answer
    assert "r1" not in answer  # the storage reference stays out


async def test_read_document_lists_the_valid_ids_for_an_unknown_one(load: Load) -> None:
    ctx = context(load, documents=(document(),))

    answer = await call(ctx, "read_document", id="att_9")

    assert answer == 'Unknown id \'att_9\'. Valid id values: "att_1", "doc_1".'
    assert (await call(ctx, "read_document")).startswith("Missing id. Valid id values:")


async def test_read_document_understands_the_id_named_after_what_it_is(load: Load) -> None:
    ctx = context(load, documents=(document(),))

    by_id = await call(ctx, "read_document", id="doc_1")

    assert await call(ctx, "read_document", document_id="doc_1") == by_id
    assert await call(ctx, "read_document", attachment_id="att_1") == by_id


async def test_list_document_requirements_shows_where_the_load_stands(load: Load) -> None:
    rate_con = DocumentRequirement(type_id="rate_confirmation", at_stage=Stage.DISPATCHED)
    requiring = standard_broker().model_copy(update={"documents": (POD_AT_DELIVERY, rate_con)})
    pod = Obligation(
        obligation_id="obl_2", load_id="481207", kind="pod", attempts=1, opened_at=at(9, 0)
    )
    ctx = context(load, broker=requiring, obligations=(pod,), documents=(document(),))

    answer = await call(ctx, "list_document_requirements")
    only = await call(ctx, "list_document_requirements", type_id="bol")
    closed = await call(
        context(
            load,
            broker=requiring,
            obligations=(pod.model_copy(update={"status": ObligationStatus.SATISFIED}),),
        ),
        "list_document_requirements",
    )
    unopened = await call(context(load, broker=requiring), "list_document_requirements")

    assert answer.splitlines() == [
        "The load is to_pickup. Required documents:",
        '- pod at at_delivery within 120 min: received: processed ("doc_1"); pod open since '
        "09:00, attempts 1",
        "- rate_confirmation at dispatched: not received",
    ]
    assert only == "This broker requires no such document at any stage."
    assert "; pod satisfied" in closed
    assert "; pod not open" in unopened


# --- action tools -------------------------------------------------------------------------


async def test_an_action_tool_hands_its_call_to_the_staging(load: Load) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    def stage(name: str, args: Any) -> tuple[str, bool]:
        seen.append((name, dict(args)))
        return "accepted as action #1", True

    answer = await tools(context(load), stage=stage)["write_tms_note"].run({"text": "note"})

    assert answer == "accepted as action #1"
    assert seen == [("write_tms_note", {"text": "note"})]


def test_action_schemas_are_flat_portable_and_offer_the_loads_contacts(load: Load) -> None:
    every = tools(context(load), stage=lambda name, args: ("", True))

    sms = every["send_sms"].parameters
    assert sms == {
        "type": "object",
        "properties": {
            "contact_id": {
                "type": "string",
                "description": "The contact id, from the Contacts list.",
                "enum": ["drv_20931", "dsp_4410", "brk_0087"],
            },
            "text": {
                "type": "string",
                "minLength": 1,
                "maxLength": 600,
                "description": "The text, in plain words.",
            },
            "purpose": {
                "type": "string",
                "enum": ["eta_request", "reply", "escalation", "info"],
                "description": "Why the message is sent.",
            },
            "requests": {
                "type": "string",
                "description": "The obligation kind the message asks for, if it asks for one.",
                "enum": ["eta_request"],
            },
        },
        "required": ["contact_id", "text"],
        "additionalProperties": False,
    }
    email = every["send_email"].parameters["properties"]["to_contact_ids"]
    assert email["type"] == "array" and email["items"]["enum"][0] == "drv_20931"
    assert every["update_stage"].parameters["properties"]["stage"]["enum"][0] == "dispatched"
    assert every["close_obligation"].parameters["required"] == ["kind", "status"]
    for spec in every.values():
        assert _schema_keywords(spec.parameters).isdisjoint({"$ref", "$defs", "title", "default"})
        assert "type" not in spec.parameters["properties"] or spec.name in ACTION_TOOLS
    assert "type" not in sms["properties"]
    assert "Code checks it now" in every["send_sms"].description
    json.dumps([s.parameters for s in every.values()])


def _schema_keywords(node: Any) -> set[str]:
    """Every keyword used in a JSON schema, property names left out."""
    if not isinstance(node, dict):
        return set()
    keywords: set[str] = set()
    for key, value in node.items():
        keywords.add(key)
        if key == "properties":
            for prop in value.values():
                keywords |= _schema_keywords(prop)
        elif isinstance(value, dict):
            keywords |= _schema_keywords(value)
    return keywords


def test_build_action_reads_the_arguments_of_one_action_type() -> None:
    assert build_action("send_sms", {"contact_id": DRIVER, "text": "hi", "type": "x"}) == SendSms(
        contact_id=DRIVER, text="hi"
    )
    assert build_action(
        "send_email", {"to_contact_ids": ["dsp_4410"], "body": "b", "purpose": "eta_request"}
    ) == SendEmail(to_contact_ids=("dsp_4410",), body="b", purpose=Purpose.ETA_REQUEST)
