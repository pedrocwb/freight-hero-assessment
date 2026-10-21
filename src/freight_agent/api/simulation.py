"""The simulation service: one in-process simulation that people drive through the API.

It holds the current `Session` (a runtime on a virtual clock with recording gateways) and
an `asyncio.Lock` that serializes everything that moves the clock or feeds the agent.
Reads take no lock: each one reads the store through its own unit of work.

The store is the process's one platform store (`bootstrap.build_platform_store`):
in memory by default, or Postgres with `PERSISTENCE=postgres`. A reset (and a scenario,
which resets first) empties it. On Postgres the loads, runs and ledger survive a
restart: the new process resumes them (`resume`), its clock at the latest time they
hold, and attachment bytes go to `BLOBS_DIR` so the documents' files survive too.

The facility memory is the process's one platform memory too
(`bootstrap.build_platform_memory`, on the same `PERSISTENCE`): every session's runtime
reads and writes it, a reset empties it with the store, and on Postgres its notes survive
a restart, which also queues the extraction of any delivered load whose job was lost
(`AgentRuntime.queue_missed_extractions`).

While a scenario plays (`api.scenarios`) every write from a client is refused (409):
another client would break the run.

Time moves the way the scenario player moves it (`evalkit.runner`, `docs/decisions.md`):
advancing stops at every timer due on the way and ticks there with zero latency; an
ingested message or ping runs `latency_seconds` after it, on one tick. After each change
the new runs, the new or updated outbox items, the changed load summaries and the clock
are published on the event bus, in that order.

The model is always the live one (`bootstrap.platform_llm`), counted per session so a
scenario played on the platform (`api.scenarios`) checks its model calls as an eval
does. Without `OPENROUTER_API_KEY` the platform still starts and says so
(`SimState.model_ready`); a run that needs the model then escalates to a Hero.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from freight_agent.adapters.channels.fakes.recording import RecordingGateways
from freight_agent.adapters.clock import VirtualClock
from freight_agent.adapters.documents.memory_blobs import MemoryBlobStore
from freight_agent.adapters.memory.store import StoreFacilityMemory
from freight_agent.adapters.observability.langsmith import LangSmithLinks
from freight_agent.api import views
from freight_agent.api.bus import EventBus, sse_message
from freight_agent.api.errors import BadRequest, Conflict, NotFound
from freight_agent.api.schemas import (
    AdvanceRequest,
    AttachmentUploadResult,
    LangSmithState,
    LoadSummary,
    MessageIn,
    PingIn,
    ResetRequest,
    RunView,
    SimState,
    StepResult,
    StopNotesView,
)
from freight_agent.application.contracts import IngestResult, LoadSnapshot
from freight_agent.bootstrap import (
    BootstrapError,
    PlatformMemory,
    PlatformStore,
    Simulation,
    build_blobs,
    build_knowledge,
    build_platform_memory,
    build_platform_store,
    build_runtime,
    build_tracer,
    gate_decisions,
    platform_llm,
)
from freight_agent.domain.errors import DomainError
from freight_agent.domain.events import Attachment
from freight_agent.domain.load import Channel, Load
from freight_agent.domain.outbox import OutboxStatus
from freight_agent.domain.runs import RunRecord
from freight_agent.domain.timers import later
from freight_agent.evalkit.counting import CountingModel
from freight_agent.evalkit.results import StepObservation
from freight_agent.evalkit.runner import ScenarioPlayer
from freight_agent.interfaces.documents import BlobStore
from freight_agent.interfaces.knowledge import SopIndex
from freight_agent.interfaces.llm import LanguageModel
from freight_agent.interfaces.repositories import UnitOfWorkFactory
from freight_agent.interfaces.tracing import Tracer
from freight_agent.settings import MissingSetting, Settings

DEFAULT_TIMEZONE = "America/Los_Angeles"
# The assignment's morning: five minutes before the 07:00 ping.
DEFAULT_START = datetime(2026, 10, 13, 6, 55, tzinfo=ZoneInfo(DEFAULT_TIMEZONE))
DEFAULT_LATENCY_S = 60
DEFAULT_HEARTBEAT_S = 15.0
# Each tick fires every due timer, so a timer still due after this many ticks at one
# instant means the runtime never fires it; stop instead of looping forever.
_MAX_TICKS_AT_ONE_INSTANT = 100


@dataclass
class Session:
    """One simulation and the platform state that is reset with it."""

    simulation: Simulation
    uow: UnitOfWorkFactory  # the runtime's store, for the read side the facade lacks
    timezone: ZoneInfo
    model: CountingModel  # the runtime's model, counted (a scenario checks its calls)
    inner_model: LanguageModel  # the same model uncounted, for the judge
    knowledge: SopIndex  # the SOP index the session's runs read
    blobs: BlobStore  # the session's attachment bytes, gone with a reset
    published_outbox: dict[str, OutboxStatus] = field(default_factory=dict)
    published_loads: dict[str, LoadSummary] = field(default_factory=dict)

    @property
    def clock(self) -> VirtualClock:
        return self.simulation.clock

    def now(self) -> datetime:
        return self.clock.now().astimezone(self.timezone)

    def local(self, at: datetime) -> datetime:
        """`at` in the simulation's timezone; a time without offset is read as local."""
        return (
            at.replace(tzinfo=self.timezone) if at.tzinfo is None else at.astimezone(self.timezone)
        )


@dataclass(frozen=True)
class AttachmentUpload:
    """One file sent to a load through the API (`POST /api/loads/{load_id}/attachments`),
    with the message that carries it; the ids and the time are optional, as for
    `MessageIn`."""

    load_id: str
    data: bytes
    filename: str
    content_type: str
    channel: Channel = Channel.SMS
    sender_contact_id: str | None = None
    body: str = ""
    message_id: str | None = None
    occurred_at: datetime | None = None


def open_session(
    settings: Settings,
    *,
    start: datetime,
    timezone: ZoneInfo,
    persistence: UnitOfWorkFactory,
    blobs: BlobStore,
    llm: LanguageModel | None = None,
    tracer: Tracer | None = None,
    memory: StoreFacilityMemory | None = None,
) -> Session:
    """A simulation (`bootstrap.Simulation`) on `persistence` and `blobs`. It reads what
    the store holds: emptying it first (a reset) is the caller's part.

    The model is the live one (`platform_llm`, on the session's clock for its circuit
    breakers) unless `llm` is given, which tests use to play the agent's rounds.
    """
    clock = VirtualClock(start.astimezone(timezone))
    gateways = RecordingGateways()
    try:
        inner = llm if llm is not None else platform_llm(settings, clock=clock)
        decisions = gate_decisions(settings)
        model = CountingModel(inner, decisions)  # the jev gate's calls are counted too
        knowledge = build_knowledge(settings)
        runtime = build_runtime(
            settings,
            clock=clock,
            gateways=gateways.as_gateways(),
            llm=model,
            persistence=persistence,
            knowledge=knowledge,
            blobs=blobs,
            tracer=tracer,
            decisions=model if decisions is not None else None,
            memory=memory,
        )
    except (MissingSetting, BootstrapError, ValueError) as exc:
        raise BadRequest(str(exc)) from exc
    return Session(
        simulation=Simulation(runtime=runtime, clock=clock, gateways=gateways),
        uow=persistence,
        timezone=timezone,
        model=model,
        inner_model=inner,
        knowledge=knowledge,
        blobs=blobs,
    )


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise BadRequest(
            f"unknown timezone {name!r}: use an IANA name such as {DEFAULT_TIMEZONE}"
        ) from exc


class SimulationService:
    def __init__(
        self, settings: Settings, *, llm: LanguageModel | None = None, bus: EventBus | None = None
    ) -> None:
        self._settings = settings
        self._llm = llm
        self.bus = bus or EventBus()
        self._lock = asyncio.Lock()
        self._tracer = build_tracer(settings)
        self._store: PlatformStore = build_platform_store(settings)
        self._memory: PlatformMemory = build_platform_memory(settings)
        # Durable attachment bytes go with a durable store; in memory they reset with it.
        self._durable_blobs = build_blobs(settings) if self._store.durable else None
        self._session = self._open(ResetRequest())
        self.playing: str | None = None  # the scenario playing (`api.scenarios`), if any

    def _writable(self) -> None:
        """A client's write while a scenario plays would break the run: refused."""
        if self.playing is not None:
            raise Conflict(
                f"the scenario {self.playing} is playing: wait for it to finish "
                "(GET /api/scenarios/run)"
            )

    @property
    def settings(self) -> Settings:
        return self._settings

    # The current session's interfaces, for the routes the runtime facade does not cover
    # (documents, the SOP viewer). A reset replaces them: read them per request.

    @property
    def uow_factory(self) -> UnitOfWorkFactory:
        return self._session.uow

    @property
    def knowledge(self) -> SopIndex:
        return self._session.knowledge

    @property
    def blobs(self) -> BlobStore:
        return self._session.blobs

    @property
    def session(self) -> Session:
        return self._session

    def _open(self, request: ResetRequest) -> Session:
        timezone = zone(request.timezone or DEFAULT_TIMEZONE)
        start = request.start or DEFAULT_START
        start = start.replace(tzinfo=timezone) if start.tzinfo is None else start
        return self._open_at(start, timezone)

    def _open_at(self, start: datetime, timezone: ZoneInfo) -> Session:
        return open_session(
            self._settings,
            start=start,
            timezone=timezone,
            persistence=self._store.uow_factory,
            blobs=self._durable_blobs or MemoryBlobStore(),
            llm=self._llm,
            tracer=self._tracer,
            memory=self._memory.memory,
        )

    # --- lifecycle ------------------------------------------------------------------------

    async def resume(self) -> SimState:
        """At startup: on a durable store that holds loads, the simulation goes on from the
        latest time they hold (the clock is not stored, and timers due before it would
        otherwise fire again on the next advance); otherwise nothing changes. On a durable
        facility memory, a delivered load whose extraction job was lost is queued again;
        the next tick runs it."""
        async with self._lock:
            session = self._session
            if self._store.durable:
                latest = _latest(await self._snapshots(session))
                if latest is not None and _after(latest, session.clock.now()):
                    session.clock.set(session.local(latest))
            if self._memory.durable:
                await session.simulation.runtime.queue_missed_extractions()
            return await self._publish(session, ())

    async def close(self) -> None:
        """At shutdown: release the stores (the Postgres pools)."""
        await self._store.close()
        await self._memory.close()

    # --- simulation -----------------------------------------------------------------------

    async def reset(self, request: ResetRequest) -> SimState:
        """A fresh simulation on an emptied store; the old one stays, store included, if
        the new one cannot be built."""
        self._writable()
        async with self._lock:
            session = self._open(request)
            await self._store.clear()
            await self._memory.clear()
            self._session = session
            return await self._publish(self._session, ())

    async def state(self) -> SimState:
        session = self._session
        return await self._state(session, await self._snapshots(session))

    async def advance(self, request: AdvanceRequest) -> StepResult:
        self._writable()
        async with self._lock:
            session = self._session
            now = session.now()
            if request.to is not None:
                target = session.local(request.to)
            else:
                target = later(now, timedelta(minutes=request.minutes or 0))
            if _after(now, target):
                raise BadRequest(
                    f"the clock cannot move backwards: the simulation is at {now.isoformat()}"
                )
            runs = await self._run_timers_until(session, target)
            runs += await session.simulation.runtime.tick()
            state = await self._publish(session, runs)
        return StepResult(state=state, runs=tuple(RunView.of(r) for r in runs))

    async def tick(self) -> StepResult:
        self._writable()
        async with self._lock:
            session = self._session
            runs = await session.simulation.runtime.tick()
            state = await self._publish(session, runs)
        return StepResult(state=state, runs=tuple(RunView.of(r) for r in runs))

    # --- scenarios (api.scenarios) --------------------------------------------------------

    async def open_scenario(self, start: datetime, timezone: ZoneInfo) -> Session:
        """A fresh simulation whose clock starts at a scenario's start, in its timezone."""
        async with self._lock:
            session = self._open_at(start, timezone)
            await self._store.clear()
            await self._memory.clear()
            self._session = session
            await self._publish(self._session, ())
            return self._session

    async def start_scenario(self, session: Session, player: ScenarioPlayer) -> None:
        async with self._lock:
            await player.start()
            await self._publish(session, ())

    async def play_step(
        self, session: Session, player: ScenarioPlayer, index: int
    ) -> StepObservation:
        """One step of a scenario, then what changed goes out on the bus, as for any input."""
        async with self._lock:
            observation = await player.play(index)
            await self._publish(session, observation.runs)
        return observation

    async def _run_timers_until(self, session: Session, target: datetime) -> list[RunRecord]:
        """Stop at every pending timer due by `target`, in due order, and tick there; then
        set the clock to `target` (without a tick: the caller decides)."""
        runtime, clock = session.simulation.runtime, session.clock
        runs: list[RunRecord] = []
        ticks_at_instant = 0
        while (due := await runtime.next_timer_due_at()) is not None and _not_after(due, target):
            if _after(due, clock.now()):  # an overdue timer fires at the current time
                clock.set(session.local(due))
                ticks_at_instant = 0
            ticks_at_instant += 1
            if ticks_at_instant > _MAX_TICKS_AT_ONE_INSTANT:
                raise RuntimeError(f"the runtime did not fire the timer due at {due.isoformat()}")
            runs += await runtime.tick()
        clock.set(target)
        return runs

    # --- loads ----------------------------------------------------------------------------

    async def create_load(self, metadata: Mapping[str, Any]) -> LoadSummary:
        self._writable()
        load = Load.from_metadata(metadata)  # DomainError: a 400 with the reason
        async with self._lock:
            session = self._session
            if load.load_id in await self._load_ids(session):
                raise Conflict(
                    f"load {load.load_id!r} already exists: reset the simulation or use "
                    "another load_id"
                )
            await session.simulation.runtime.register_load(load)
            await self._publish(session, ())
        return views.load_summary(await self.snapshot(load.load_id))

    async def snapshot(self, load_id: str) -> LoadSnapshot:
        try:
            return await self._session.simulation.runtime.snapshot(load_id)
        except DomainError as exc:
            raise NotFound(f"unknown load {load_id!r}") from exc

    async def facility_memory(self, load_id: str) -> list[StopNotesView]:
        """Each stop of the load with its client's facility notes."""
        try:
            stops = await self._session.simulation.runtime.facility_facts(load_id)
        except DomainError as exc:
            raise NotFound(f"unknown load {load_id!r}") from exc
        return [
            StopNotesView(
                sequence=s.stop.sequence,
                type=s.stop.type,
                name=s.stop.name,
                address=s.stop.address,
                facility_key=s.facility_key,
                facts=s.facts,
            )
            for s in stops
        ]

    async def runs(self, load_id: str) -> list[RunView]:
        return [RunView.of(run) for run in (await self.snapshot(load_id)).runs]

    # --- ingress --------------------------------------------------------------------------

    async def ingest_message(self, message: MessageIn, latency_s: float) -> IngestResult:
        """A redelivered message (a message id already logged) changes nothing: the
        runtime reports it as a duplicate, and the clock neither moves nor ticks."""
        self._writable()
        async with self._lock:
            session = self._session
            await self._require_load(session, message.load_id)
            message_id = message.message_id or str(uuid.uuid4())
            runs, at = await self._arrive(
                session, message.occurred_at, move=not await _logged(session, message_id)
            )
            contract = message.to_contract(message_id=message_id, occurred_at=at)
            result = await session.simulation.runtime.ingest_message(contract)
            if not result.duplicate:
                runs += await self._settle(session, latency_s)
            await self._publish(session, runs)
        return result

    async def ingest_ping(self, ping: PingIn, latency_s: float) -> IngestResult:
        """A redelivered ping (an id the load's risk tracker remembers) changes nothing,
        the clock included."""
        self._writable()
        async with self._lock:
            session = self._session
            await self._require_load(session, ping.load_id)
            ping_id = ping.ping_id or str(uuid.uuid4())
            runs, at = await self._arrive(
                session, ping.occurred_at, move=not await _pinged(session, ping.load_id, ping_id)
            )
            contract = ping.to_contract(ping_id=ping_id, occurred_at=at)
            result = await session.simulation.runtime.ingest_ping(contract)
            if not result.duplicate:
                runs += await self._settle(session, latency_s)
            await self._publish(session, runs)
        return result

    async def upload_attachment(self, upload: AttachmentUpload) -> AttachmentUploadResult:
        """A file sent to the load: its bytes go to the session's blob store, then it
        arrives as a message with that attachment, through `ingest_message`, which saves
        its document as pending (card W3-DOC).

        Nothing runs yet: the next tick (`POST /api/sim/tick`, or any input) reads the
        document first, then runs the message and the document's `document_processed`
        event. A message id already logged is a 409, before anything is stored.
        """
        self._writable()
        async with self._lock:
            session = self._session
            await self._require_load(session, upload.load_id)
            message_id = upload.message_id or str(uuid.uuid4())
            if await _logged(session, message_id):
                raise Conflict(f"message {message_id!r} was already received")
            ref = await session.blobs.put(upload.data, upload.content_type, name=upload.filename)
            attachment = Attachment(
                attachment_id=f"att_{uuid.uuid4().hex[:12]}",
                filename=upload.filename,
                content_type=upload.content_type,
                ref=ref,
            )
            runs, at = await self._arrive(session, upload.occurred_at)
            message = MessageIn(
                load_id=upload.load_id,
                channel=upload.channel,
                sender_contact_id=upload.sender_contact_id,
                body=upload.body,
                attachments=(attachment,),
            )
            result = await session.simulation.runtime.ingest_message(
                message.to_contract(message_id=message_id, occurred_at=at)
            )
            async with session.uow() as uow:
                document = await uow.documents.for_attachment(
                    upload.load_id, attachment.attachment_id
                )
            await self._publish(session, runs)
        if document is None or result.event_id is None:  # pragma: no cover - checked above
            raise RuntimeError(f"the upload of {upload.filename!r} saved no document")
        return AttachmentUploadResult(
            attachment_id=attachment.attachment_id,
            document_id=document.document_id,
            event_id=result.event_id,
        )

    async def _arrive(
        self, session: Session, occurred_at: datetime | None, *, move: bool = True
    ) -> tuple[list[RunRecord], datetime]:
        """When the input happens: now by default; a later time is reached first, firing
        the timers due on the way (as an eval step does). An earlier time is kept as the
        event's time, but the clock does not go back. Without `move` (a redelivery) the
        clock stays wherever the time is."""
        if occurred_at is None:
            return [], session.now()
        at = session.local(occurred_at)
        if not move or _not_after(at, session.clock.now()):
            return [], at
        return await self._run_timers_until(session, at), at

    async def _settle(self, session: Session, latency_s: float) -> list[RunRecord]:
        """The run of an input happens `latency_s` after it, on one tick. A timer that comes
        due in that window fires on the same tick, after the input's run."""
        session.clock.advance(timedelta(seconds=latency_s))
        return await session.simulation.runtime.tick()

    async def _require_load(self, session: Session, load_id: str) -> None:
        if load_id not in await self._load_ids(session):
            raise NotFound(f"unknown load {load_id!r}")

    # --- stream ---------------------------------------------------------------------------

    def stream(self, *, heartbeat_s: float = DEFAULT_HEARTBEAT_S) -> AsyncIterator[str]:
        """The SSE body for one subscriber: the current clock first, then every change."""

        async def initial() -> list[str]:
            return [sse_message("clock", await self.state())]

        return self.bus.stream(initial, heartbeat_s=heartbeat_s)

    # --- read side ------------------------------------------------------------------------

    @staticmethod
    async def _load_ids(session: Session) -> list[str]:
        async with session.uow() as uow:
            return await uow.loads.list_ids()

    async def _snapshots(self, session: Session) -> list[LoadSnapshot]:
        runtime = session.simulation.runtime
        return [await runtime.snapshot(load_id) for load_id in await self._load_ids(session)]

    async def _state(self, session: Session, snapshots: list[LoadSnapshot]) -> SimState:
        runtime = session.simulation.runtime
        due = await runtime.next_timer_due_at()
        async with session.uow() as uow:
            queued = await uow.queue.size()
        return SimState(
            now=session.now(),
            timezone=session.timezone.key,
            next_timer_due_at=None if due is None else session.local(due),
            queue_size=queued,
            loads=tuple(views.load_summary(s) for s in snapshots),
            langsmith=await self._langsmith(),
            model_ready=self._settings.model_ready,
        )

    async def _langsmith(self) -> LangSmithState:
        tracer = self._tracer
        if not isinstance(tracer, LangSmithLinks):
            return LangSmithState(enabled=False, project_url=None)
        return LangSmithState(enabled=True, project_url=await tracer.cached_project_url())

    async def _publish(self, session: Session, runs: Sequence[RunRecord]) -> SimState:
        """Publish what changed: runs, new or updated outbox items, changed loads, the clock."""
        for run in runs:
            self.bus.publish("run", RunView.of(run))
        snapshots = await self._snapshots(session)
        for snapshot in snapshots:
            for item in snapshot.outbox:
                if session.published_outbox.get(item.outbox_id) is not item.status:
                    session.published_outbox[item.outbox_id] = item.status
                    self.bus.publish("outbox", item)
        state = await self._state(session, snapshots)
        for summary in state.loads:
            if session.published_loads.get(summary.load_id) != summary:
                session.published_loads[summary.load_id] = summary
                self.bus.publish("load", summary)
        self.bus.publish("clock", state)
        return state


def _latest(snapshots: Sequence[LoadSnapshot]) -> datetime | None:
    """The latest instant the loads hold: an event, a run, an outbox item."""
    times = [
        moment
        for snapshot in snapshots
        for moment in (
            *(event.occurred_at for event in snapshot.events),
            *(run.finished_at or run.started_at for run in snapshot.runs),
            *(item.created_at for item in snapshot.outbox),
        )
    ]
    return max(times, key=lambda moment: moment.astimezone(UTC)) if times else None


async def _logged(session: Session, event_id: str) -> bool:
    """Whether the runtime would take this message as a duplicate (its id is logged)."""
    async with session.uow() as uow:
        return await uow.events.get(event_id) is not None


async def _pinged(session: Session, load_id: str, ping_id: str) -> bool:
    """Whether the runtime would take this ping as a duplicate (its tracker has the id)."""
    async with session.uow() as uow:
        tracker = await uow.loads.get_tracker(load_id)
    return tracker is not None and tracker.has_seen(ping_id)


# Compare instants: two times in one zone compare by wall clock, which DST breaks.


def _not_after(first: datetime, second: datetime) -> bool:
    return first.astimezone(UTC) <= second.astimezone(UTC)


def _after(first: datetime, second: datetime) -> bool:
    return first.astimezone(UTC) > second.astimezone(UTC)
