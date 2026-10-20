"""Shared setup for the runtime tests (no tests here).

One load in a memory store, a virtual clock, a scripted model, recording gateways
and a small in-test SOP: the standard sections the scenarios need plus the
Northline (Client A) high-risk procedure. The real `sops/` tree is not read.
"""

from collections.abc import Callable, Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from freight_agent.adapters.channels.fakes.recording import Delivery, RecordingGateways
from freight_agent.adapters.clock import SequentialIdGenerator, VirtualClock
from freight_agent.adapters.documents.memory_blobs import MemoryBlobStore
from freight_agent.adapters.knowledge.memory import MemorySopIndex
from freight_agent.adapters.llm.scripted import ScriptedModel
from freight_agent.adapters.observability.null import NullTracer
from freight_agent.adapters.persistence.memory.store import MemoryStore
from freight_agent.application.contracts import (
    InboundMessage,
    IngestResult,
    LoadSnapshot,
    LocationPing,
)
from freight_agent.application.runtime import AgentRuntime, RuntimeConfig, RuntimeDeps
from freight_agent.domain.actions import Action, Plan, Purpose, ScheduleTimer, SendSms
from freight_agent.domain.broker import Broker
from freight_agent.domain.events import Attachment
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.load import Channel, DelayRisk, Load, Stage
from freight_agent.domain.obligations import Obligation, ObligationKind
from freight_agent.domain.runs import RunRecord
from freight_agent.domain.sop import MergeMode, SopLayer, SopSection
from freight_agent.domain.timers import Timer, TimerStatus
from freight_agent.ports.gateways import Gateways
from freight_agent.ports.knowledge import SopIndex
from freight_agent.ports.llm import LanguageModel, ModelRole
from freight_agent.ports.tracing import Tracer

LA = ZoneInfo("America/Los_Angeles")
LOAD_ID = "481207"
DRIVER = "drv_20931"
DISPATCHER = "dsp_4410"
BROKER = "brk_0087"
ETA = ObligationKind.ETA_REQUEST
GATE = ModelRole.GATE
DECIDE = ModelRole.AGENT  # the role was named decide before wave 3


def at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 10, 13, hour, minute, second, tzinfo=LA)


def section(
    section_id: str,
    body: str,
    *,
    intents: Sequence[Intent] = (),
    stages: Sequence[Stage] = (),
    client_id: str | None = None,
    load_type: str | None = None,
    mode: MergeMode = MergeMode.REPLACE,
) -> SopSection:
    if load_type is not None:
        layer, source = SopLayer.LOAD_TYPE, f"clients/{client_id}/types/{load_type}"
    elif client_id is not None:
        layer, source = SopLayer.CLIENT, f"clients/{client_id}"
    else:
        layer, source = SopLayer.STANDARD, "standard"
    return SopSection(
        section_id=section_id,
        layer=layer,
        client_id=client_id,
        load_type=load_type,
        title=section_id.replace("_", " ").capitalize(),
        description=f"About {section_id}.",
        intents=tuple(intents),
        stages=tuple(stages),
        mode=mode,
        body=body,
        source=f"{source}/{section_id}.md",
    )


HIGH_RISK_INTENTS = (Intent.DELAY_RISK_HIGH, Intent.ETA_FOLLOW_UP_DUE)
STANDARD = (
    section("communication_rules", "Only the load's contacts. Short SMS. Never share rates."),
    section("escalation", "Create a Hero task when you cannot act."),
    section(
        "delay_risk_high",
        "SMS the driver for the ETA. No ETA after 30 minutes: SMS again. "
        "No ETA 30 minutes after the second SMS: create a Hero task to call the driver.",
        intents=HIGH_RISK_INTENTS,
    ),
    section(
        "delay_risk_cleared",
        "Close the open ETA request as cancelled.",
        intents=(Intent.DELAY_RISK_LOW,),
    ),
    section("eta_received", "Close the ETA request as satisfied.", intents=(Intent.ETA_PROVIDED,)),
    section(
        "eta_deferred", "Accept one deferral of up to 15 minutes.", intents=(Intent.ETA_DEFERRED,)
    ),
    section("driver_questions", "Answer from the load metadata.", intents=(Intent.QUESTION,)),
    section("acknowledgements", "Do nothing.", intents=(Intent.ACKNOWLEDGEMENT,)),
    section(
        "documents",
        "Create a Hero task to review it.",
        intents=(Intent.DOCUMENT, Intent.DOCUMENT_PROCESSED),
    ),
)
NORTHLINE = (
    section(
        "delay_risk_high",
        "SMS the driver for the ETA. No ETA after 30 minutes: SMS again. No ETA after the "
        "two SMS: email the dispatcher in the main thread, then create a Hero task.",
        intents=HIGH_RISK_INTENTS,
        client_id="northline",
    ),
)
# No `status_updates` section: a status update is the SOP gap of these tests.
SECTIONS = (*STANDARD, *NORTHLINE)


class FakeSop(MemorySopIndex):
    """The SopIndex contract over a fixed list of sections, at version "test", and the
    catalogs given (a broker not given runs on the standard catalog)."""

    def __init__(
        self, sections: Sequence[SopSection] = SECTIONS, *, brokers: Sequence[Broker] = ()
    ) -> None:
        super().__init__(sections, version="test", brokers=brokers)


ASK_ETA = SendSms(
    contact_id=DRIVER, text="What time will you arrive at the pickup?", purpose=Purpose.ETA_REQUEST
)
ASK_AGAIN = SendSms(
    contact_id=DRIVER,
    text="Checking again: what time will you arrive at the pickup?",
    purpose=Purpose.ETA_REQUEST,
)
FOLLOW_UP_IN_30 = ScheduleTimer(kind=ETA, due_in_minutes=30)


def decision(intent: Intent, act: bool = True) -> GateDecision:
    return GateDecision(intent=intent, act=act, reason=f"scripted {intent.value}")


def plan(*actions: Action) -> Plan:
    return Plan(actions=actions, rationale="scripted")


class World:
    """The runtime under test and every fake it runs on."""

    def __init__(
        self,
        load: Load,
        *,
        config: RuntimeConfig | None = None,
        llm: LanguageModel | None = None,
        knowledge: SopIndex | None = None,
        tracer: Tracer | None = None,
        wrap_gateways: Callable[[Gateways], Gateways] | None = None,
    ) -> None:
        """`wrap_gateways` puts test gateways in front of the recording ones."""
        self.load = load
        self.store = MemoryStore()
        self.clock = VirtualClock(at(6, 55))
        self.ids = SequentialIdGenerator()
        self.model = ScriptedModel()
        self.gateways = RecordingGateways()
        recording = self.gateways.as_gateways()
        self.deps = RuntimeDeps(
            uow_factory=self.store.unit_of_work,
            clock=self.clock,
            ids=self.ids,
            llm=llm or self.model,
            knowledge=knowledge or FakeSop(),
            gateways=wrap_gateways(recording) if wrap_gateways else recording,
            tracer=tracer or NullTracer(),
            blobs=MemoryBlobStore(),
            config=config or RuntimeConfig(),
        )
        self.runtime = AgentRuntime(self.deps)

    def restart(self) -> None:
        """A new runtime on the same store: a restarted or a second worker process."""
        self.runtime = AgentRuntime(self.deps)

    async def start(self) -> "World":
        await self.runtime.register_load(self.load)
        return self

    async def ping(self, when: datetime, risk: DelayRisk, ping_id: str = "p1") -> IngestResult:
        self.clock.set(when)
        return await self.runtime.ingest_ping(
            LocationPing(
                ping_id=ping_id,
                load_id=self.load.load_id,
                lat=37.95,
                lon=-121.29,
                occurred_at=when,
                delay_risk=risk,
            )
        )

    async def message(
        self,
        when: datetime,
        body: str,
        *,
        sender: str | None = DRIVER,
        channel: Channel = Channel.SMS,
        message_id: str | None = None,
        attachments: tuple[Attachment, ...] = (),
    ) -> IngestResult:
        self.clock.set(when)
        return await self.runtime.ingest_message(
            InboundMessage(
                message_id=message_id or f"msg:{when:%H%M}",
                load_id=self.load.load_id,
                channel=channel,
                sender_contact_id=sender,
                body=body,
                occurred_at=when,
                attachments=attachments,
            )
        )

    async def tick(self, when: datetime) -> list[RunRecord]:
        self.clock.set(when)
        return await self.runtime.tick()

    async def snapshot(self) -> LoadSnapshot:
        return await self.runtime.snapshot(self.load.load_id)

    async def eta_request(self) -> Obligation | None:
        snapshot = await self.snapshot()
        return next((o for o in reversed(snapshot.obligations) if o.kind == ETA), None)

    async def pending_timers(self) -> list[Timer]:
        snapshot = await self.snapshot()
        return [t for t in snapshot.timers if t.status is TimerStatus.PENDING]

    def sent(self, channel: str) -> list[Delivery]:
        return self.gateways.log.of(channel)

    def calls(self, role: ModelRole) -> int:
        return sum(1 for call in self.model.calls if call.role is role)

    async def high_risk_at_seven(self) -> list[RunRecord]:
        """07:00 a high ping; 07:01 the first ETA SMS and its 07:31 follow-up timer."""
        await self.ping(at(7, 0), DelayRisk.HIGH, "p1")
        self.model.push(DECIDE, plan(ASK_ETA, FOLLOW_UP_IN_30))
        return await self.tick(at(7, 1))
