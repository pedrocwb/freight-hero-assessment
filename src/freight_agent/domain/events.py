"""Events: everything that can start an agent run."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from freight_agent.domain.base import DomainModel
from freight_agent.domain.load import Channel


class EventType(StrEnum):
    INBOUND_MESSAGE = "inbound_message"
    RISK_CHANGED = "risk_changed"
    TIMER_DUE = "timer_due"
    ATTACHMENT_RECEIVED = "attachment_received"
    HERO_UPDATE = "hero_update"
    AGENT_ACTION = "agent_action"  # the agent's own action, logged as memory; never starts a run
    DOCUMENT_PROCESSED = "document_processed"  # the document pipeline read an attachment


# Event types that are queued and start a run.
RUN_TRIGGERS = frozenset(
    {
        EventType.INBOUND_MESSAGE,
        EventType.RISK_CHANGED,
        EventType.TIMER_DUE,
        EventType.ATTACHMENT_RECEIVED,
        EventType.HERO_UPDATE,
        EventType.DOCUMENT_PROCESSED,
    }
)


class Direction(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class Attachment(DomainModel):
    attachment_id: str
    filename: str
    content_type: str
    ref: str


class Event(DomainModel):
    """An immutable entry of the load's event log.

    `event_id` is the dedupe key: the integration layer's stable `message_id`
    for messages, and a derived id for pings and timers.

    `data` carries type-specific fields:
    - RISK_CHANGED: {"previous": "low", "current": "high"}
    - TIMER_DUE: {"timer_id": "...", "obligation_id": "...", "kind": "eta_request"}
    - AGENT_ACTION: {"kind": "<outbox kind>", "payload": {...}, "run_id": "...",
      "outbox_id": "..."}; direction is OUTBOUND
    - DOCUMENT_PROCESSED: {"document_id": "...", "type_id": "pod" | None,
      "status": "processed" | "needs_review" | "failed", "attachment_id": "..."};
      `event_id` is `doc:{document_id}`, so a document is announced once
    """

    event_id: str
    load_id: str
    type: EventType
    occurred_at: datetime
    channel: Channel
    direction: Direction = Direction.INBOUND
    sender_contact_id: str | None = None
    body: str = ""
    attachments: tuple[Attachment, ...] = ()
    data: dict[str, Any] = Field(default_factory=dict)
