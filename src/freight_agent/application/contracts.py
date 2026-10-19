"""Ingress contract with the integration layer, and read models for the API and evals."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from freight_agent.domain.events import Attachment, Event
from freight_agent.domain.load import Channel, DelayRisk, Load
from freight_agent.domain.obligations import Obligation
from freight_agent.domain.outbox import OutboxItem
from freight_agent.domain.runs import RunRecord
from freight_agent.domain.timers import Timer


class _Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class InboundMessage(_Contract):
    """A message from email, SMS or Slack, already resolved to a contact id."""

    message_id: str = Field(description="Stable id from the integration layer; the dedupe key")
    load_id: str
    channel: Channel
    sender_contact_id: str | None = Field(
        default=None, description="None when the sender is not a known contact of the load"
    )
    body: str = Field(description="Only the new text: no quoted reply, no signature")
    occurred_at: datetime
    attachments: tuple[Attachment, ...] = ()


class LocationPing(_Contract):
    ping_id: str
    load_id: str
    lat: float
    lon: float
    occurred_at: datetime
    delay_risk: DelayRisk


class IngestResult(_Contract):
    accepted: bool
    duplicate: bool = False
    event_id: str | None = None
    reason: str | None = None


class LoadSnapshot(_Contract):
    load: Load
    obligations: tuple[Obligation, ...]
    timers: tuple[Timer, ...]
    events: tuple[Event, ...]
    outbox: tuple[OutboxItem, ...]
    runs: tuple[RunRecord, ...]
