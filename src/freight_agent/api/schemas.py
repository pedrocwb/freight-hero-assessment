"""Request and response bodies of the platform API (`docs/api.md`).

Domain objects (loads, snapshots, runs, outbox items, SOP sections) go out as they are,
dumped with `model_dump(mode="json")`; the models here are the views the platform adds.
Datetimes are ISO 8601 with their offset, in the simulation's timezone.

The wave 3 bodies at the end (documents) were written with the contracts (C0), before
their routes: the cards build their routes on them and the web UI its types, so they
change only with every user of them.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from freight_agent.application.contracts import InboundMessage, IngestResult, LocationPing
from freight_agent.domain.documents import Document, DocumentStatus, FieldCheck
from freight_agent.domain.events import Attachment
from freight_agent.domain.load import Channel, DelayRisk, Stage, StopType
from freight_agent.domain.memory import FacilityFact
from freight_agent.domain.runs import RunRecord
from freight_agent.domain.sop import ResolvedSection, SearchHit


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- simulation -------------------------------------------------------------------------------


class ResetRequest(_Body):
    start: datetime | None = Field(
        default=None, description="Default 2026-10-13T06:55 in the timezone; no offset: local"
    )
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


class LangSmithState(_Body):
    enabled: bool = Field(description="Runs are traced to LangSmith")
    project_url: str | None = Field(description="The tracing project's page, once known")


class SimState(_Body):
    now: datetime
    timezone: str
    next_timer_due_at: datetime | None
    queue_size: int
    loads: tuple[LoadSummary, ...]
    langsmith: LangSmithState
    model_ready: bool = Field(description="False when OPENROUTER_API_KEY is not set")


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


class TurnResult(_Body):
    """An input to one load and what it caused (`POST /api/loads/{load_id}/messages` and
    `/pings`): the ingest result, the runs of the step (none for a duplicate, or a ping
    that changed no risk band), and the clock after them."""

    accepted: bool
    duplicate: bool
    event_id: str | None
    reason: str | None
    runs: tuple[RunView, ...]
    state: SimState

    @classmethod
    def of(cls, result: IngestResult, runs: Sequence[RunRecord], state: SimState) -> "TurnResult":
        return cls(
            **result.model_dump(),
            runs=tuple(RunView.of(r) for r in runs),
            state=state,
        )

    def ingest(self) -> IngestResult:
        return IngestResult(
            accepted=self.accepted,
            duplicate=self.duplicate,
            event_id=self.event_id,
            reason=self.reason,
        )


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


class LoadMessageIn(_Body):
    """A message to one load (`POST /api/loads/{load_id}/messages`): `MessageIn` without
    the load id, which the path gives."""

    message_id: str | None = Field(default=None, description="Default: a new uuid")
    channel: Channel
    sender_contact_id: str | None = None
    body: str
    occurred_at: datetime | None = Field(default=None, description="Default: simulation now")

    def for_load(self, load_id: str) -> MessageIn:
        return MessageIn(load_id=load_id, **self.model_dump())


class LoadPingIn(_Body):
    """A location ping of one load (`POST /api/loads/{load_id}/pings`); the position
    defaults to the assignment's pickup, Stockton, CA."""

    ping_id: str | None = Field(default=None, description="Default: a new uuid")
    delay_risk: DelayRisk
    lat: float = 37.9577
    lon: float = -121.2908
    occurred_at: datetime | None = Field(default=None, description="Default: simulation now")

    def for_load(self, load_id: str) -> PingIn:
        return PingIn(load_id=load_id, **self.model_dump())


# --- fixtures ---------------------------------------------------------------------------------


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


# --- documents (card W3-DOC) -----------------------------------------------------------------


class AttachmentUploadResult(_Body):
    """`POST /api/loads/{load_id}/attachments` (multipart): the file is stored, logged as
    a message with that attachment, and its document is pending."""

    attachment_id: str
    document_id: str
    event_id: str
    runs: tuple[RunView, ...] = Field(
        default=(), description="With `wait`: the runs of the tick that read the file"
    )


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


class StopNotesView(_Body):
    """One stop of a load and its client's facility notes about the stop's place
    (`GET /api/loads/{load_id}/facility-memory`), the most recently updated first."""

    sequence: int
    type: StopType
    name: str
    address: str
    facility_key: str
    facts: tuple[FacilityFact, ...]
