"""Request and response bodies of the platform API (`docs/api.md`).

Domain objects (loads, snapshots, runs, outbox items, SOP sections) go out as they are,
dumped with `model_dump(mode="json")`; the models here are the views the platform adds.
Datetimes are ISO 8601 with their offset, in the simulation's timezone.

The wave 3 bodies at the end (brokers, documents) were written with the contracts (C0),
before their routes: the cards build their routes on them and the web UI its types, so
they change only with every user of them.
"""

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from freight_agent.application.contracts import InboundMessage, LocationPing
from freight_agent.domain.broker import (
    Broker,
    BrokerLimits,
    DocumentRequirement,
    DocumentTypeDef,
    IntentDef,
    ObligationDef,
    Rollout,
)
from freight_agent.domain.documents import Document, DocumentStatus, FieldCheck
from freight_agent.domain.events import Attachment
from freight_agent.domain.load import Channel, DelayRisk, Stage
from freight_agent.domain.outbox import OutboxStatus
from freight_agent.domain.runs import RunRecord
from freight_agent.domain.sop import ResolvedSection, SearchHit
from freight_agent.settings import LlmMode

ConversationChannel = Literal["sms", "email", "slack"]
TaskStatus = Literal["open", "done"]


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- simulation -------------------------------------------------------------------------------


class ResetRequest(_Body):
    start: datetime | None = Field(
        default=None, description="Default 2026-10-13T06:55 in the timezone; no offset: local"
    )
    llm_mode: LlmMode | None = Field(default=None, description="Default: the LLM_MODE setting")
    timezone: str | None = Field(default=None, description="IANA name; America/Los_Angeles")


class AdvanceRequest(_Body):
    minutes: float | None = Field(default=None, ge=0)
    to: datetime | None = Field(default=None, description="No offset: the simulation timezone")

    @model_validator(mode="after")
    def _one_target(self) -> Self:
        if (self.minutes is None) == (self.to is None):
            raise ValueError('give exactly one of "minutes" and "to"')
        return self


class LoadSummary(_Body):
    load_id: str
    broker_id: str
    broker_name: str
    stage: Stage
    delay_risk: DelayRisk
    open_obligations: int
    pending_timers: int
    cost_usd: float


class SimState(_Body):
    now: datetime
    timezone: str
    next_timer_due_at: datetime | None
    llm_mode: str
    queue_size: int
    loads: tuple[LoadSummary, ...]


class RunView(RunRecord):
    """A run record plus its total model cost, which the record computes but does not store."""

    @computed_field  # pyright: ignore[reportUnknownMemberType]
    @property
    def cost_usd(self) -> float:
        return sum(u.cost_usd for u in self.usage)

    @classmethod
    def of(cls, run: RunRecord) -> "RunView":
        return cls.model_validate(run.model_dump())


class StepResult(_Body):
    state: SimState
    runs: tuple[RunView, ...]


# --- ingress ----------------------------------------------------------------------------------


class MessageIn(_Body):
    """`InboundMessage` with the ids and the time optional: the API fills them."""

    message_id: str | None = Field(default=None, description="Default: a new uuid")
    load_id: str
    channel: Channel
    sender_contact_id: str | None = None
    body: str
    occurred_at: datetime | None = Field(default=None, description="Default: simulation now")
    attachments: tuple[Attachment, ...] = ()

    def to_contract(self, *, message_id: str, occurred_at: datetime) -> InboundMessage:
        return InboundMessage(
            message_id=message_id,
            load_id=self.load_id,
            channel=self.channel,
            sender_contact_id=self.sender_contact_id,
            body=self.body,
            occurred_at=occurred_at,
            attachments=self.attachments,
        )


class PingIn(_Body):
    """`LocationPing` with the id and the time optional: the API fills them."""

    ping_id: str | None = Field(default=None, description="Default: a new uuid")
    load_id: str
    lat: float
    lon: float
    occurred_at: datetime | None = Field(default=None, description="Default: simulation now")
    delay_risk: DelayRisk

    def to_contract(self, *, ping_id: str, occurred_at: datetime) -> LocationPing:
        return LocationPing(
            ping_id=ping_id,
            load_id=self.load_id,
            lat=self.lat,
            lon=self.lon,
            occurred_at=occurred_at,
            delay_risk=self.delay_risk,
        )


# --- channels and the Hero console ------------------------------------------------------------


class ConversationEntry(_Body):
    at: datetime
    direction: Literal["in", "out"]
    channel: ConversationChannel
    who: str = Field(description='The sender\'s contact id, or "agent"')
    role: str = Field(description="driver, dispatcher, broker, agent or unknown")
    text: str
    subject: str | None = None
    status: OutboxStatus | None = Field(default=None, description="Delivery, outbound only")
    to: tuple[str, ...] | None = Field(default=None, description="Recipients, SMS and email out")


class TmsNote(_Body):
    at: datetime
    text: str


class HeroTask(_Body):
    task_id: str = Field(description="The outbox id of the agent's create_hero_task")
    load_id: str
    title: str
    description: str
    urgent: bool
    created_at: datetime
    status: TaskStatus
    done_at: datetime | None = None
    note: str | None = None


class CompleteTaskRequest(_Body):
    note: str | None = None


class Fixture(_Body):
    name: str
    metadata: dict[str, Any]


# --- SOPs -------------------------------------------------------------------------------------


class SopView(_Body):
    version: str
    client_id: str
    load_types: tuple[str, ...]
    sections: tuple[ResolvedSection, ...]


class SearchHitView(SearchHit):
    """One result of `GET /api/sops/search?q=` (`broker_id`, `load_types` in the query)."""


# --- brokers (card W3-BRK) -------------------------------------------------------------------


class BrokerIn(_Body):
    """Create a broker (`POST /api/brokers`: a copy of the standard catalog, then these
    fields) or change one (`PUT /api/brokers/{broker_id}`, which saves a new
    `config_version`). A field left out keeps its value."""

    broker_id: str | None = Field(
        default=None, description="Required to create; on update, the path's id or left out"
    )
    name: str | None = Field(default=None, description="Required to create")
    triage: bool | None = None
    intents: tuple[IntentDef, ...] | None = None
    obligations: tuple[ObligationDef, ...] | None = None
    document_types: tuple[DocumentTypeDef, ...] | None = None
    documents: tuple[DocumentRequirement, ...] | None = None
    limits: BrokerLimits | None = None


class BrokerView(Broker):
    """A broker's current version, as stored (`GET /api/brokers/{broker_id}`)."""


class BrokerVersionView(_Body):
    """One saved version of a broker (`GET /api/brokers/{broker_id}/versions`)."""

    config_version: int
    name: str
    sop_version: str | None
    rollout: Rollout
    current: bool = Field(description="The version `GET /api/brokers/{broker_id}` returns")
    intents: tuple[str, ...] = Field(description="The intent ids of the version's catalog")
    obligations: tuple[str, ...] = Field(description="The obligation kinds of the version")

    @classmethod
    def of(cls, broker: Broker, *, current: bool) -> "BrokerVersionView":
        return cls(
            config_version=broker.config_version,
            name=broker.name,
            sop_version=broker.sop_version,
            rollout=broker.rollout,
            current=current,
            intents=tuple(i.intent_id for i in broker.intents),
            obligations=tuple(o.kind for o in broker.obligations),
        )


class RolloutIn(_Body):
    """`POST /api/brokers/{broker_id}/rollout`: the share of loads on the current version.
    0 is a rollback. 409 when `previous_version` names a version that was never saved."""

    percent: int = Field(ge=0, le=100)
    previous_version: int | None = Field(
        default=None, description="Default: the rollout's current previous version"
    )


# --- documents (card W3-DOC) -----------------------------------------------------------------


class AttachmentUploadResult(_Body):
    """`POST /api/loads/{load_id}/attachments` (multipart): the file is stored, logged as
    a message with that attachment, and its document is pending."""

    attachment_id: str
    document_id: str
    event_id: str


class DocumentView(_Body):
    """A document as the inspector shows it (`GET /api/documents/{document_id}`); the
    blob reference stays internal."""

    document_id: str
    load_id: str
    attachment_id: str
    broker_id: str
    filename: str
    content_type: str
    status: DocumentStatus
    type_id: str | None
    confidence: float | None
    extracted: dict[str, Any]
    checks: tuple[FieldCheck, ...]
    text: str
    pages: int
    received_at: datetime
    processed_at: datetime | None

    @classmethod
    def of(cls, document: Document) -> "DocumentView":
        return cls.model_validate(document.model_dump(exclude={"blob_ref"}))
