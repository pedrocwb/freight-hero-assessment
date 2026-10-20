"""Request and response bodies of the platform API (`docs/api.md`).

Domain objects (loads, snapshots, runs, outbox items, SOP sections) go out as they are,
dumped with `model_dump(mode="json")`; the models here are the views the platform adds.
Datetimes are ISO 8601 with their offset, in the simulation's timezone.

The wave 3 bodies at the end (brokers, knowledge, documents, broker evals) were written
with the contracts (C0), before their routes: the cards build their routes on them and
the web UI its types, so they change only with every user of them.
"""

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from freight_agent.application.contracts import InboundMessage, LocationPing
from freight_agent.application.metrics import (
    CostMetrics,
    HealthLevel,
    HealthReason,
    OutboxMetrics,
    QueueMetrics,
    RunMetrics,
    StalledObligation,
    TimerLagMetrics,
)
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
from freight_agent.domain.sop import (
    DraftSection,
    ProposedIntent,
    ResolvedSection,
    SearchHit,
    SopDraft,
    SopVersion,
)
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


# --- SOPs and health --------------------------------------------------------------------------


class SopView(_Body):
    version: str
    client_id: str
    load_types: tuple[str, ...]
    sections: tuple[ResolvedSection, ...]


class HealthReport(_Body):
    status: HealthLevel
    reasons: tuple[HealthReason, ...]
    queue: QueueMetrics
    runs: RunMetrics
    cost: CostMetrics
    outbox: OutboxMetrics
    timers: TimerLagMetrics
    stalled: tuple[StalledObligation, ...]
    settings: dict[str, str] = Field(description="Settings.describe(): secrets only as set/unset")


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


# --- knowledge base (card W3-KNW) -------------------------------------------------------------


class SopUploadResult(_Body):
    """`POST /api/brokers/{broker_id}/sops` (multipart): the upload, stored and compiled."""

    document_id: str
    pages: int
    status: str = Field(description="compiled (the draft has its sections), stored or failed")


class SopDraftView(SopDraft):
    """The broker's draft (`GET /api/brokers/{broker_id}/sops/draft`), with the lint
    problems a publish would report now."""

    lint: tuple[str, ...] = ()


class SopDraftIn(_Body):
    """`PUT /api/brokers/{broker_id}/sops/draft`: the edited draft, as a whole."""

    sections: tuple[DraftSection, ...]
    proposed_intents: tuple[ProposedIntent, ...] = ()
    proposed_requirements: tuple[DocumentRequirement, ...] = ()


class SopPublishIn(_Body):
    """`POST /api/brokers/{broker_id}/sops/publish`: below 100 starts a canary."""

    percent: int = Field(default=100, ge=0, le=100)


class SopPublishResult(_Body):
    version: str = Field(description="The published SOP version (content hash)")
    config_version: int = Field(description="The broker's new config version")
    lint: tuple[str, ...] = Field(description="Warnings the publish accepted")


class SopVersionView(_Body):
    """One published SOP version (`GET /api/brokers/{broker_id}/sops/versions`)."""

    broker_id: str | None
    version: str
    published_at: datetime
    published_by: str
    section_ids: tuple[str, ...]
    lint: tuple[str, ...] = ()

    @classmethod
    def of(cls, version: SopVersion) -> "SopVersionView":
        return cls(
            broker_id=version.broker_id,
            version=version.version,
            published_at=version.published_at,
            published_by=version.published_by,
            section_ids=tuple(s.section_id for s in version.sections),
            lint=version.lint,
        )


class SearchHitView(SearchHit):
    """One result of `GET /api/sops/search?q=` (`broker_id`, `load_types` in the query)."""


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


# --- broker evals (card W3-EVK) --------------------------------------------------------------


class BrokerEvalRunRequest(_Body):
    """`POST /api/brokers/{broker_id}/evals/run`: a suite on the broker's current version."""

    suite: str = "core"
    mode: LlmMode = "scripted"
    k: int = Field(default=1, ge=1, le=10, description="Trials per scenario")
    cost_cap_usd: float = Field(default=0.10, ge=0, description="Stops a live run past it")
