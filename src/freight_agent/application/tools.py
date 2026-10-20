"""The words of the agent's tools: their names and the descriptions the model reads.

Owner: card W3-AGT. The runnable tools are built in `application/agent_tools.py`; the
text lives here, next to `application/prompts.py`, because both shape what the model
reads: `evalkit.metadata.prompts_sha256` hashes the two modules, so a changed tool
description shows in an eval run's metadata like a changed prompt.
"""

from collections.abc import Mapping

SEARCH_SOP = "search_sop"
READ_SOP_SECTION = "read_sop_section"
GET_HISTORY = "get_history"
GET_LEDGER = "get_ledger"
GET_LOAD = "get_load"
READ_DOCUMENT = "read_document"
LIST_DOCUMENT_REQUIREMENTS = "list_document_requirements"
READ_TOOLS = (
    SEARCH_SOP,
    READ_SOP_SECTION,
    GET_HISTORY,
    GET_LEDGER,
    GET_LOAD,
    READ_DOCUMENT,
    LIST_DOCUMENT_REQUIREMENTS,
)

READ_DESCRIPTIONS: Mapping[str, str] = {
    SEARCH_SOP: (
        "Find SOP sections of this load's broker by their words, best first. Use it when the "
        "sections shown do not cover the event."
    ),
    READ_SOP_SECTION: (
        "Read the full text of one SOP section from the catalog of other SOP sections. Use it "
        "only when this event needs a section that is not shown in full."
    ),
    GET_HISTORY: (
        "The load's events further back than the transcript shows, oldest first, in the "
        "transcript's format."
    ),
    GET_LEDGER: (
        "Every obligation of the load, open and closed, and every pending timer. The prompt "
        "shows the open ones and the last closed ones."
    ),
    GET_LOAD: "The load data (stops, appointments, instructions) and the contacts.",
    READ_DOCUMENT: (
        "Read a document of the load (BOL, POD, rate confirmation, receipt) by its document id "
        "or its attachment id: its type, status, values and checks against the load."
    ),
    LIST_DOCUMENT_REQUIREMENTS: (
        "The documents this broker requires at each stage, and whether this load has them or "
        "is waiting for them."
    ),
}

ACTION_DESCRIPTIONS: Mapping[str, str] = {
    "send_sms": "Stage an SMS to a contact that has the sms channel.",
    "send_email": "Stage an email in the load's main thread.",
    "post_broker_slack": "Stage a message in the broker's Slack channel.",
    "write_tms_note": "Stage a note in the broker's TMS.",
    "create_hero_task": "Stage a task for the Hero team (phone calls, what you cannot do).",
    "post_internal_slack": "Stage an alert to the Hero team.",
    "update_stage": "Stage a move of the load to another stage.",
    "schedule_timer": "Stage a follow-up timer for an open obligation, in minutes from now.",
    "defer_obligation": "Stage a deferral: the contact promised the answer later.",
    "close_obligation": "Stage the closing of an open obligation.",
    "open_obligation": "Stage the opening of an obligation, with no message sent.",
}
ACTION_CHECKED = (
    "Code checks it now and answers accepted or rejected; nothing runs until you finish."
)

FIELD_DESCRIPTIONS: Mapping[str, str] = {
    "contact_id": "The contact id, from the Contacts list.",
    "to_contact_ids": "The contact ids of the recipients, from the Contacts list.",
    "text": "The text, in plain words.",
    "body": "The email body.",
    "subject": "The email subject.",
    "purpose": "Why the message is sent.",
    "requests": "The obligation kind the message asks for, if it asks for one.",
    "title": "A short instruction for the Hero.",
    "description": "What happened, with local times, what was tried, what the Hero should do.",
    "urgent": "True when the load is at risk and a person must act now.",
    "stage": "The new stage.",
    "kind": "The obligation kind.",
    "due_in_minutes": "Minutes from now until the follow-up.",
    "minutes": "Minutes from the message until the promised answer.",
    "status": "How the obligation ends.",
}
