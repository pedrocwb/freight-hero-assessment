"""The scenario player: plays a scenario's steps on a runtime and observes what each step
changed. It does not grade: graders judge the observations separately, so the two are
built and tested on their own. The timing rules are the ones in the `evalkit.spec`
docstring.

One player, three places:

- a LangSmith experiment (`evalkit.experiment`) plays each example on a fresh trial
  runtime (`play_scenario`): the product runtime on a memory store, with a virtual
  clock, recording gateways, deterministic ids and the document pipeline;
- the platform (`api.scenarios`) plays a scenario inside its simulation, after a reset,
  step by step (`ScenarioPlayer`), so its load and runs show in the UI as they happen;
- LangGraph Studio (`freight_agent.studio`) plays a step one tick at a time
  (`begin`, `next_tick`, `observe`), so that each run is a node of its graph.

The model is always the live one. The player counts its calls through `CountingModel`
(an agent loop counts once), and runs the judge (`evalkit.judge`) right after a step that
has a rubric, on the uncounted model, so `llm_calls` counts the agent's calls only.
"""

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict

from freight_agent.adapters.channels.fakes.recording import RecordingGateways
from freight_agent.adapters.clock import SequentialIdGenerator, VirtualClock
from freight_agent.adapters.documents.memory_blobs import MemoryBlobStore
from freight_agent.adapters.documents.pdfium import PdfiumPageReader
from freight_agent.adapters.observability.null import NullTracer
from freight_agent.adapters.persistence.memory.store import MemoryStore
from freight_agent.application.contracts import (
    InboundMessage,
    IngestResult,
    LoadSnapshot,
    LocationPing,
)
from freight_agent.application.documents import DocumentPipeline
from freight_agent.application.runtime import AgentRuntime, RuntimeConfig, RuntimeDeps
from freight_agent.domain.errors import DomainError
from freight_agent.domain.events import Attachment
from freight_agent.domain.load import Load
from freight_agent.domain.outbox import OutboxItem
from freight_agent.domain.runs import RunRecord, Usage
from freight_agent.evalkit.counting import CountingModel
from freight_agent.evalkit.judge import Judge, JudgeResult
from freight_agent.evalkit.results import StepObservation
from freight_agent.evalkit.spec import Scenario, Step
from freight_agent.ports.documents import BlobStore
from freight_agent.ports.knowledge import SopIndex
from freight_agent.ports.llm import LanguageModel
from freight_agent.ports.prompts import Prompts

# Each tick fires every due timer, so a timer still due after this many ticks at
# one instant means the runtime never fires it; stop instead of looping forever.
_MAX_TICKS_AT_ONE_INSTANT = 100


class RuntimeLike(Protocol):
    """The part of the `AgentRuntime` facade the player uses."""

    async def register_load(self, load: Load) -> None: ...

    async def ingest_message(self, message: InboundMessage) -> IngestResult: ...

    async def ingest_ping(self, ping: LocationPing) -> IngestResult: ...

    async def tick(self) -> list[RunRecord]: ...

    async def next_timer_due_at(self) -> datetime | None: ...

    async def snapshot(self, load_id: str) -> LoadSnapshot: ...


class RunnerError(Exception):
    """The runtime broke a rule the player depends on."""


class ScenarioSetupError(Exception):
    """The scenario cannot start: missing or invalid fixture, unknown timezone."""


# --- what a scenario needs before it starts ---------------------------------------------


def scenario_timezone(scenario: Scenario) -> ZoneInfo:
    try:
        return ZoneInfo(scenario.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScenarioSetupError(f"unknown timezone {scenario.timezone!r}") from exc


def scenario_start(scenario: Scenario) -> datetime:
    """When the scenario's clock starts, as an aware datetime."""
    return local_time(scenario.date, scenario.start, scenario_timezone(scenario))


def read_load(evals_dir: Path, scenario: Scenario) -> Load:
    """The scenario's load: its fixture with `load_overrides` merged over the top-level
    keys."""
    try:
        with (evals_dir / scenario.load).open(encoding="utf-8") as handle:
            data: Any = json.load(handle)
    except FileNotFoundError as exc:
        raise ScenarioSetupError(f"load fixture not found: {scenario.load}") from exc
    except (OSError, ValueError) as exc:
        raise ScenarioSetupError(f"cannot read load fixture {scenario.load}: {exc}") from exc
    if not isinstance(data, dict):
        raise ScenarioSetupError(f"load fixture {scenario.load} must be a JSON object")
    metadata = {**cast(dict[str, Any], data), **scenario.load_overrides}
    try:
        return Load.from_metadata(metadata)
    except DomainError as exc:
        raise ScenarioSetupError(f"{scenario.load}: {exc}") from exc


def read_attachments(evals_dir: Path, scenario: Scenario) -> dict[str, bytes]:
    """Every attachment fixture's bytes, read before the scenario starts: a missing file
    is a setup error, not a failure halfway through."""
    files: dict[str, bytes] = {}
    for step in scenario.steps:
        for attachment in step.message.attachments if step.message is not None else ():
            if attachment.fixture in files:
                continue
            try:
                files[attachment.fixture] = (evals_dir / attachment.fixture).read_bytes()
            except OSError as exc:
                raise ScenarioSetupError(
                    f"attachment fixture not found: {attachment.fixture}"
                ) from exc
    return files


# --- the player -------------------------------------------------------------------------


@dataclass
class StepProgress:
    """How far one step has been played: what its observation counts from, and whether
    the clock has reached the step's time (after which only its injection is left)."""

    index: int
    at: datetime
    before: LoadSnapshot
    llm_calls_before: int
    arrived: bool = False
    ticks_at_instant: int = field(default=0, repr=False)


class ScenarioPlayer:
    """Plays one scenario on one runtime, a step at a time: moves the clock, injects,
    observes. `model` is the runtime's own model, counted; `judge` grades the steps that
    have a rubric (None: they are not judged, and their judge check fails)."""

    def __init__(
        self,
        scenario: Scenario,
        *,
        load: Load,
        runtime: RuntimeLike,
        clock: VirtualClock,
        model: CountingModel,
        blobs: BlobStore,
        files: Mapping[str, bytes],
        judge: Judge | None,
    ) -> None:
        tz = scenario_timezone(scenario)
        self.scenario = scenario
        self.load = load
        self._runtime = runtime
        self._clock = clock
        self._model = model
        self._blobs = blobs
        self._files = files
        self._judge = judge
        self.times = [local_time(scenario.date, step.at, tz) for step in scenario.steps]
        self.judge_usage: list[Usage] = []

    async def start(self) -> None:
        await self._runtime.register_load(self.load)

    async def play(self, index: int) -> StepObservation:
        """Play step `index`: a tick at every instant `next_tick` stops at, then what the
        step changed."""
        progress = await self.begin(index)
        while await self.next_tick(progress):
            await self._runtime.tick()
        return await self.observe(progress)

    async def begin(self, index: int) -> StepProgress:
        """Start step `index`: what its observation will count from."""
        before = await self._runtime.snapshot(self.load.load_id)
        return StepProgress(
            index=index, at=self.times[index], before=before, llm_calls_before=self._model.calls
        )

    async def next_tick(self, progress: StepProgress) -> bool:
        """Move the world to the next instant of the step at which the runtime must tick,
        and say whether there is one.

        First every pending timer due by the step's time, in due order (zero latency);
        then the clock reaches the step's time and the step's message or ping, if any, is
        ingested, its tick `run_latency_seconds` later. A step that injects nothing ends
        when the clock reaches its time, without a tick.
        """
        if progress.arrived:
            return False
        due = await self._runtime.next_timer_due_at()
        if due is not None and due <= progress.at:
            if due > self._clock.now():  # an overdue timer fires at the current time
                self._clock.set(due)
                progress.ticks_at_instant = 0
            progress.ticks_at_instant += 1
            if progress.ticks_at_instant > _MAX_TICKS_AT_ONE_INSTANT:
                raise RunnerError(f"the runtime did not fire the timer due at {due.isoformat()}")
            return True
        self._clock.set(progress.at)
        progress.arrived = True
        index = progress.index
        if not await self._inject(index, self.scenario.steps[index], progress.at):
            return False
        self._clock.set(self._settle_time(index, progress.at))
        return True

    async def observe(self, progress: StepProgress) -> StepObservation:
        """What the step changed, once `next_tick` said it is over; a step with a rubric
        is judged here."""
        step = self.scenario.steps[progress.index]
        before, after = progress.before, await self._runtime.snapshot(self.load.load_id)
        outbox = _created(before.outbox, after.outbox, key=lambda item: item.outbox_id)
        llm_calls = self._model.calls - progress.llm_calls_before  # the judge is apart
        return StepObservation(
            step_index=progress.index,
            at=progress.at,
            outbox=outbox,
            runs=_created(before.runs, after.runs, key=lambda run: run.run_id),
            llm_calls=llm_calls,
            snapshot=after,
            judge=await self._judged(step, outbox),
        )

    async def _judged(self, step: Step, outbox: Sequence[OutboxItem]) -> JudgeResult | None:
        spec = step.expect.judge
        if spec is None or self._judge is None:
            return None
        result = await self._judge.grade(spec, outbox)
        if result.usage is not None:
            self.judge_usage.append(result.usage)
        return result

    async def _inject(self, index: int, step: Step, at: datetime) -> bool:
        """Ingest the step's message or ping, if any. Returns whether something was ingested."""
        derived_id = f"{self.scenario.id}:{index}"
        load_id = self.load.load_id
        if step.message is not None:
            message = InboundMessage(
                message_id=step.message.message_id or derived_id,
                load_id=load_id,
                channel=step.message.channel,
                sender_contact_id=step.message.sender,
                body=step.message.body,
                occurred_at=at,
                attachments=await self._attachments(step, derived_id),
            )
            await self._runtime.ingest_message(message)
            return True
        if step.ping is not None:
            ping = LocationPing(
                ping_id=derived_id,
                load_id=load_id,
                lat=step.ping.lat,
                lon=step.ping.lon,
                occurred_at=at,
                delay_risk=step.ping.delay_risk,
            )
            await self._runtime.ingest_ping(ping)
            return True
        return False

    async def _attachments(self, step: Step, derived_id: str) -> tuple[Attachment, ...]:
        """The message's fixture files, stored in the blob store."""
        assert step.message is not None
        attachments: list[Attachment] = []
        for number, spec in enumerate(step.message.attachments, 1):
            ref = await self._blobs.put(
                self._files[spec.fixture], spec.content_type, name=spec.filename
            )
            attachments.append(
                Attachment(
                    attachment_id=f"{derived_id}:{number}",
                    filename=spec.filename,
                    content_type=spec.content_type,
                    ref=ref,
                )
            )
        return tuple(attachments)

    def _settle_time(self, index: int, at: datetime) -> datetime:
        """When the injected event's run happens: after the latency, never past the next step."""
        settle = at + timedelta(seconds=self.scenario.run_latency_seconds)
        if index + 1 < len(self.times):
            settle = min(settle, self.times[index + 1])
        return settle


# --- one scenario on a fresh runtime ------------------------------------------------------


class Played(BaseModel):
    """Everything one play of a scenario produced, ungraded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario_id: str
    observations: tuple[StepObservation, ...] = ()
    usage: tuple[Usage, ...] = ()  # every successful model call of the runtime, in order
    judge_usage: tuple[Usage, ...] = ()  # the judge's calls, not counted in `usage`
    error: str | None = None  # the scenario could not start, or the runtime raised

    @property
    def cost_usd(self) -> float:
        """Everything the play spent, the judge included: the cost cap counts it."""
        return sum(u.cost_usd for u in (*self.usage, *self.judge_usage))


@dataclass(frozen=True)
class TrialRuntime:
    runtime: AgentRuntime
    clock: VirtualClock
    model: CountingModel
    blobs: MemoryBlobStore


def build_trial_runtime(
    model: LanguageModel, knowledge: SopIndex, start: datetime, prompts: Prompts
) -> TrialRuntime:
    """The product runtime on a fresh memory store, with a virtual clock at `start`,
    recording gateways, deterministic ids, the document pipeline, the experiment's prompt
    templates and no trace links (in an experiment the runs nest under the example's own
    trace)."""
    clock = VirtualClock(start)
    counted = CountingModel(model)
    blobs = MemoryBlobStore()
    deps = RuntimeDeps(
        uow_factory=MemoryStore().unit_of_work,
        clock=clock,
        ids=SequentialIdGenerator(),
        llm=counted,
        knowledge=knowledge,
        gateways=RecordingGateways().as_gateways(),
        tracer=NullTracer(),
        blobs=blobs,
        prompts=prompts,
        # A harness error (a bug) must end the play with its own message, not become a
        # failed run that the scenario then reports as the agent's.
        config=RuntimeConfig(reraise_unexpected=True),
    )
    pipeline = DocumentPipeline(
        uow_factory=deps.uow_factory,
        clock=clock,
        ids=deps.ids,
        llm=counted,
        blobs=blobs,
        pages=PdfiumPageReader(),
        knowledge=knowledge,
        prompt=prompts.extract,
        timeout_s=deps.config.llm_timeout_s,
    )
    deps = replace(deps, documents=pipeline)
    return TrialRuntime(AgentRuntime(deps), clock, counted, blobs)


async def play_scenario(
    scenario: Scenario,
    *,
    evals_dir: Path,
    knowledge: SopIndex,
    prompts: Prompts,
    model: LanguageModel,
    on_step: Callable[[StepObservation], None] | None = None,
) -> Played:
    """Play every step of `scenario` on a fresh trial runtime. An error stops the play
    there; the steps already played are kept, so the graders still show how far it got."""
    try:
        load = read_load(evals_dir, scenario)
        files = read_attachments(evals_dir, scenario)
        trial = build_trial_runtime(model, knowledge, scenario_start(scenario), prompts)
    except ScenarioSetupError as exc:
        return Played(scenario_id=scenario.id, error=str(exc))
    player = ScenarioPlayer(
        scenario,
        load=load,
        runtime=trial.runtime,
        clock=trial.clock,
        model=trial.model,
        blobs=trial.blobs,
        files=files,
        judge=Judge(model, prompts.judge),
    )
    observations: list[StepObservation] = []
    where = "register_load"
    error: str | None = None
    try:
        await player.start()
        for index in range(len(scenario.steps)):
            where = f"step {index}"
            observation = await player.play(index)
            observations.append(observation)
            if on_step is not None:
                on_step(observation)
    except Exception as exc:  # the play ends here; the other examples still run
        error = f"{where}: {type(exc).__name__}: {exc}"
    return Played(
        scenario_id=scenario.id,
        observations=tuple(observations),
        usage=tuple(trial.model.usage),
        judge_usage=tuple(player.judge_usage),
        error=error,
    )


def local_time(day: date, clock_time: str, tz: ZoneInfo) -> datetime:
    """A scenario time (HH:MM or HH:MM:SS) on the scenario's date, as an aware datetime."""
    return datetime.combine(day, time.fromisoformat(clock_time), tzinfo=tz)


def _created[T](before: Iterable[T], after: Iterable[T], key: Callable[[T], str]) -> tuple[T, ...]:
    """Items of `after` whose id is not in `before`, in `after`'s order."""
    seen = {key(item) for item in before}
    return tuple(item for item in after if key(item) not in seen)
