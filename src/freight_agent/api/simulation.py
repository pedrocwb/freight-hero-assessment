"""The simulation service: one in-process simulation that people drive through the API.

It holds the current `Session` (a runtime on a virtual clock with recording gateways and
an in-memory store, plus the platform state that lives and dies with it, such as Hero
completions) and an `asyncio.Lock` that serializes everything that moves the clock or
feeds the agent. Reads take no lock: each one reads the store through its own unit of
work, which the store serializes on its own.

Time moves the way the eval runner moves it (`evalkit.runner`, `docs/decisions.md`):
advancing stops at every timer due on the way and ticks there with zero latency; an
ingested message or ping runs `latency_seconds` after it, on one tick. After each change
the new runs, the new or updated outbox items, the changed load summaries and the clock
are published on the event bus, in that order.
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
from freight_agent.api import views
from freight_agent.api.bus import EventBus, sse_message
from freight_agent.api.errors import BadRequest, Conflict, NotFound
from freight_agent.api.health import health_report
from freight_agent.api.schemas import (
    AdvanceRequest,
    ConversationChannel,
    ConversationEntry,
    HealthReport,
    HeroTask,
    LoadSummary,
    MessageIn,
    PingIn,
    ResetRequest,
    RunView,
    SimState,
    StepResult,
    TaskStatus,
    TmsNote,
)
from freight_agent.application.contracts import IngestResult, LoadSnapshot
from freight_agent.bootstrap import BootstrapError, Simulation, build_runtime, build_uow_factory
from freight_agent.domain.errors import DomainError
from freight_agent.domain.load import Load
from freight_agent.domain.outbox import OutboxStatus
from freight_agent.domain.runs import RunRecord
from freight_agent.domain.timers import later
from freight_agent.ports.llm import LanguageModel
from freight_agent.ports.repositories import UnitOfWorkFactory
from freight_agent.settings import LlmMode, MissingSetting, Settings

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
    llm_mode: str
    completions: dict[str, views.Completion] = field(default_factory=dict)
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


def _interactive_mode(settings: Settings) -> LlmMode:
    """`LLM_MODE`, whatever keys are present: the live model spends money, so it is used
    only when asked for (`LLM_MODE=live`, or `llm_mode` on reset). Replay answers only
    the prompts of recorded eval scenarios."""
    return settings.llm_mode


def open_session(
    settings: Settings,
    *,
    start: datetime,
    timezone: ZoneInfo,
    llm_mode: LlmMode | None = None,
    llm: LanguageModel | None = None,
) -> Session:
    """A fresh simulation, as `bootstrap.build_simulation` builds it, keeping its store.

    The contract runs the platform on an in-memory store, whatever PERSISTENCE says, so a
    reset really starts over. The model comes from `llm_mode` (default: the setting)
    unless `llm` is given, which tests use to script the agent.
    """
    mode = llm_mode or _interactive_mode(settings)
    effective = settings.model_copy(update={"persistence": "memory", "llm_mode": mode})
    persistence = build_uow_factory(effective)
    clock = VirtualClock(start.astimezone(timezone))
    gateways = RecordingGateways()
    try:
        runtime = build_runtime(
            effective,
            clock=clock,
            gateways=gateways.as_gateways(),
            llm=llm,
            persistence=persistence,
        )
    except (MissingSetting, BootstrapError, ValueError) as exc:
        raise BadRequest(str(exc)) from exc
    return Session(
        simulation=Simulation(runtime=runtime, clock=clock, gateways=gateways),
        uow=persistence,
        timezone=timezone,
        llm_mode=mode,
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
        self._session = self._open(ResetRequest())

    @property
    def settings(self) -> Settings:
        return self._settings

    def _open(self, request: ResetRequest) -> Session:
        timezone = zone(request.timezone or DEFAULT_TIMEZONE)
        start = request.start or DEFAULT_START
        start = start.replace(tzinfo=timezone) if start.tzinfo is None else start
        return open_session(
            self._settings, start=start, timezone=timezone, llm_mode=request.llm_mode, llm=self._llm
        )

    # --- simulation -----------------------------------------------------------------------

    async def reset(self, request: ResetRequest) -> SimState:
        """A fresh simulation; the old one stays if the new one cannot be built."""
        async with self._lock:
            self._session = self._open(request)
            return await self._publish(self._session, ())

    async def state(self) -> SimState:
        session = self._session
        return await self._state(session, await self._snapshots(session))

    async def advance(self, request: AdvanceRequest) -> StepResult:
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
        async with self._lock:
            session = self._session
            runs = await session.simulation.runtime.tick()
            state = await self._publish(session, runs)
        return StepResult(state=state, runs=tuple(RunView.of(r) for r in runs))

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

    async def loads(self) -> list[LoadSummary]:
        return [views.load_summary(s) for s in await self._snapshots(self._session)]

    async def snapshot(self, load_id: str) -> LoadSnapshot:
        try:
            return await self._session.simulation.runtime.snapshot(load_id)
        except DomainError as exc:
            raise NotFound(f"unknown load {load_id!r}") from exc

    async def conversation(
        self, load_id: str, channel: ConversationChannel | None
    ) -> list[ConversationEntry]:
        return views.conversation(await self.snapshot(load_id), channel)

    async def tms(self, load_id: str) -> list[TmsNote]:
        return views.tms_notes(await self.snapshot(load_id))

    async def runs(self, load_id: str) -> list[RunView]:
        return [RunView.of(run) for run in (await self.snapshot(load_id)).runs]

    # --- ingress --------------------------------------------------------------------------

    async def ingest_message(self, message: MessageIn, latency_s: float) -> IngestResult:
        async with self._lock:
            session = self._session
            await self._require_load(session, message.load_id)
            runs, at = await self._arrive(session, message.occurred_at)
            contract = message.to_contract(
                message_id=message.message_id or str(uuid.uuid4()), occurred_at=at
            )
            result = await session.simulation.runtime.ingest_message(contract)
            runs += await self._settle(session, latency_s)
            await self._publish(session, runs)
        return result

    async def ingest_ping(self, ping: PingIn, latency_s: float) -> IngestResult:
        async with self._lock:
            session = self._session
            await self._require_load(session, ping.load_id)
            runs, at = await self._arrive(session, ping.occurred_at)
            contract = ping.to_contract(ping_id=ping.ping_id or str(uuid.uuid4()), occurred_at=at)
            result = await session.simulation.runtime.ingest_ping(contract)
            runs += await self._settle(session, latency_s)
            await self._publish(session, runs)
        return result

    async def _arrive(
        self, session: Session, occurred_at: datetime | None
    ) -> tuple[list[RunRecord], datetime]:
        """When the input happens: now by default; a later time is reached first, firing
        the timers due on the way (as an eval step does). An earlier time is kept as the
        event's time, but the clock does not go back."""
        if occurred_at is None:
            return [], session.now()
        at = session.local(occurred_at)
        if _not_after(at, session.clock.now()):
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

    # --- Hero console ---------------------------------------------------------------------

    async def hero_tasks(self, status: TaskStatus | None) -> list[HeroTask]:
        session = self._session
        return views.hero_tasks(await self._snapshots(session), session.completions, status)

    async def complete_task(self, task_id: str, note: str | None) -> HeroTask:
        """Mark a task done. Completing a done task again changes nothing."""
        async with self._lock:
            session = self._session
            if task_id not in session.completions:
                if not any(t.task_id == task_id for t in await self.hero_tasks(None)):
                    raise NotFound(f"unknown Hero task {task_id!r}")
                session.completions[task_id] = views.Completion(done_at=session.now(), note=note)
            task = next(t for t in await self.hero_tasks(None) if t.task_id == task_id)
            self.bus.publish("hero_task", task)
        return task

    # --- health and stream ----------------------------------------------------------------

    async def health(self) -> HealthReport:
        session = self._session
        snapshots = await self._snapshots(session)
        async with session.uow() as uow:
            oldest = await uow.queue.oldest_queued()
            size = await uow.queue.size()
            dead = await uow.queue.dead_letters()
        return health_report(
            snapshots,
            oldest_queued=oldest,
            queue_size=size,
            dead_letters=dead,
            now=session.now(),
            settings=self._settings.describe(),
        )

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
            llm_mode=session.llm_mode,
            queue_size=queued,
            loads=tuple(views.load_summary(s) for s in snapshots),
        )

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


# Compare instants: two times in one zone compare by wall clock, which DST breaks.


def _not_after(first: datetime, second: datetime) -> bool:
    return first.astimezone(UTC) <= second.astimezone(UTC)


def _after(first: datetime, second: datetime) -> bool:
    return first.astimezone(UTC) > second.astimezone(UTC)
