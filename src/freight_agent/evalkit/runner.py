"""Deterministic scenario runner: one trial of one scenario.

It drives the same `AgentRuntime` facade the product uses, on a virtual clock,
with recording gateways and in-memory storage, and observes what each step
changed. It does not grade: graders judge the observations separately, so the
two are built and tested on their own. The timing rules are the ones in the
`evalkit.spec` docstring.

Per trial the runner builds the runtime from `TrialDeps` (clock, counting model,
gateways, SOP index, a blob store with the scenario's attachments and, for a broker
export, the broker to seed). Two kinds of factory build it:

- a `TrialFactory(deps)`: the default, `build_trial_runtime`, runs the product runtime
  on a fresh memory store with the document pipeline when card W3-DOC provides it
  (`partial(build_trial_runtime, uow_factory=store.unit_of_work)` runs it on another
  store, a Postgres scratch schema);
- a `RuntimeFactory(clock, model, gateways, knowledge)`, the pre-wave-3 contract, still
  accepted (a test's fake runtime, another store): it gets no blob store and cannot seed
  a broker, so a scenario that needs one is a setup error with it.

The judge (`evalkit.judge`) runs after a step that has a rubric, on the trial's inner
model: its calls never pass through the counting wrapper, so `llm_calls` counts the
agent's calls only.
"""

import importlib
import json
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict

from freight_agent.adapters.channels.fakes.recording import RecordingGateways
from freight_agent.adapters.clock import SequentialIdGenerator, VirtualClock
from freight_agent.adapters.documents.memory_blobs import MemoryBlobStore
from freight_agent.adapters.llm.scripted import ScriptedModel, ScriptedReply
from freight_agent.adapters.observability.null import NullTracer
from freight_agent.adapters.persistence.memory.store import MemoryStore
from freight_agent.application.contracts import (
    InboundMessage,
    IngestResult,
    LoadSnapshot,
    LocationPing,
)
from freight_agent.application.documents import NullDocumentProcessor
from freight_agent.application.runtime import AgentRuntime, RuntimeConfig, RuntimeDeps
from freight_agent.domain.actions import AgentOutcome
from freight_agent.domain.broker import Broker
from freight_agent.domain.errors import DomainError
from freight_agent.domain.events import Attachment
from freight_agent.domain.load import Load
from freight_agent.domain.outbox import OutboxItem
from freight_agent.domain.runs import RunRecord, Usage
from freight_agent.evalkit.brokers import (
    BrokerExport,
    BrokerFileError,
    broker_index,
    load_metadata,
    read_broker_export,
)
from freight_agent.evalkit.counting import CountingModel
from freight_agent.evalkit.judge import Judge, JudgeResult, skipped
from freight_agent.evalkit.observe import NullObserver, Observer
from freight_agent.evalkit.results import Failure, StepObservation
from freight_agent.evalkit.spec import AgentTurn, Scenario, ScriptedToolCall, Step
from freight_agent.ports.clock import Clock
from freight_agent.ports.documents import BlobStore, DocumentProcessor
from freight_agent.ports.gateways import Gateways
from freight_agent.ports.knowledge import SopIndex
from freight_agent.ports.llm import LanguageModel, ModelRole
from freight_agent.ports.repositories import UnitOfWorkFactory

Mode = Literal["scripted", "replay", "live"]

# Each tick fires every due timer, so a timer still due after this many ticks at
# one instant means the runtime never fires it; stop instead of looping forever.
_MAX_TICKS_AT_ONE_INSTANT = 100

# What a scripted tool call answered, when the scenario does not say: tools never run.
SCRIPTED_TOOL_RESULT = "(scripted: the tool did not run)"

# Roles whose script entries the runner pushes and accounts for, per step.
_SCRIPTED_ROLES = (ModelRole.GATE, ModelRole.AGENT, ModelRole.EXTRACT, ModelRole.JUDGE)

# The document pipeline and the page reader of card W3-DOC, found by name like the
# composition root does (`bootstrap.DOCUMENT_PIPELINE`, `bootstrap.PAGE_READER`), so the
# eval reads documents exactly as the product does once they exist.
_PIPELINE = ("freight_agent.application.documents", "DocumentPipeline")
_PAGE_READER = ("freight_agent.adapters.documents.pdfium", "PdfiumPageReader")


class RuntimeLike(Protocol):
    """The part of the `AgentRuntime` facade the runner uses."""

    async def register_load(self, load: Load) -> None: ...

    async def ingest_message(self, message: InboundMessage) -> IngestResult: ...

    async def ingest_ping(self, ping: LocationPing) -> IngestResult: ...

    async def tick(self) -> list[RunRecord]: ...

    async def next_timer_due_at(self) -> datetime | None: ...

    async def snapshot(self, load_id: str) -> LoadSnapshot: ...


@dataclass(frozen=True)
class TrialDeps:
    """What the runner gives the runtime of one trial."""

    clock: Clock
    model: LanguageModel  # the counting model: every call the runtime makes counts
    gateways: Gateways
    knowledge: SopIndex
    blobs: BlobStore  # holds the bytes of the scenario's attachments
    broker: Broker | None = None  # a broker export's broker, saved before the load


TrialFactory = Callable[[TrialDeps], RuntimeLike]
RuntimeFactory = Callable[[Clock, LanguageModel, Gateways, SopIndex], RuntimeLike]


def build_trial_runtime(
    deps: TrialDeps, *, uow_factory: UnitOfWorkFactory | None = None
) -> RuntimeLike:
    """The product runtime on a fresh memory store (or on `uow_factory`, another store such
    as a Postgres scratch schema), with deterministic ids and no tracing.

    Documents are read by the product's pipeline once card W3-DOC provides it (until then
    by nothing). A broker export's broker is saved in the store before the load.
    """
    uow_factory = uow_factory or MemoryStore().unit_of_work
    runtime_deps = RuntimeDeps(
        uow_factory=uow_factory,
        clock=deps.clock,
        ids=SequentialIdGenerator(),
        llm=deps.model,
        knowledge=deps.knowledge,
        gateways=deps.gateways,
        tracer=NullTracer(),
        blobs=deps.blobs,
        # An eval must fail loudly on a harness error (a missing cassette, a bug), not
        # record it as a failed run and move on.
        config=RuntimeConfig(reraise_unexpected=True),
    )
    runtime_deps = replace(runtime_deps, documents=document_processor(runtime_deps))
    runtime = AgentRuntime(runtime_deps)
    if deps.broker is None:
        return runtime
    return BrokerSeededRuntime(runtime, uow_factory, deps.broker)


def build_runtime(
    clock: Clock, model: LanguageModel, gateways: Gateways, knowledge: SopIndex
) -> RuntimeLike:
    """`build_trial_runtime` through the pre-wave-3 `RuntimeFactory` signature."""
    return build_trial_runtime(
        TrialDeps(
            clock=clock,
            model=model,
            gateways=gateways,
            knowledge=knowledge,
            blobs=MemoryBlobStore(),
        )
    )


def from_runtime_factory(factory: RuntimeFactory) -> TrialFactory:
    """A `TrialFactory` over a pre-wave-3 `RuntimeFactory`, which cannot seed a broker."""

    def build(deps: TrialDeps) -> RuntimeLike:
        if deps.broker is not None:
            raise ScenarioSetupError(
                f"the broker {deps.broker.broker_id} cannot be seeded by this runtime "
                "factory: pass a trial_factory (evalkit.runner.TrialFactory)"
            )
        return factory(deps.clock, deps.model, deps.gateways, deps.knowledge)

    return build


def document_processor(deps: RuntimeDeps) -> DocumentProcessor:
    pipeline, pages = _adapter(*_PIPELINE), _adapter(*_PAGE_READER)
    if pipeline is None or pages is None:
        return NullDocumentProcessor()
    return pipeline(
        uow_factory=deps.uow_factory,
        clock=deps.clock,
        ids=deps.ids,
        llm=deps.llm,
        blobs=deps.blobs,
        pages=pages(),
        timeout_s=deps.config.llm_timeout_s,
    )


def _adapter(module: str, attribute: str) -> Any | None:
    """The attribute, or None while its card has not landed (as `bootstrap.LazyAdapter`)."""
    try:
        found = importlib.import_module(module)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if module != missing and not module.startswith(f"{missing}."):
            raise
        return None
    return getattr(found, attribute, None)


class BrokerSeededRuntime:
    """A runtime whose store gets a broker before the load: `register_load` saves it first.

    The rest of the facade goes to the runtime unchanged."""

    def __init__(self, inner: RuntimeLike, uow_factory: UnitOfWorkFactory, broker: Broker) -> None:
        self.inner = inner
        self._uow_factory = uow_factory
        self.broker = broker

    async def register_load(self, load: Load) -> None:
        async with self._uow_factory() as uow:
            await uow.brokers.save(self.broker)
            await uow.commit()
        await self.inner.register_load(load)

    async def ingest_message(self, message: InboundMessage) -> IngestResult:
        return await self.inner.ingest_message(message)

    async def ingest_ping(self, ping: LocationPing) -> IngestResult:
        return await self.inner.ingest_ping(ping)

    async def tick(self) -> list[RunRecord]:
        return await self.inner.tick()

    async def next_timer_due_at(self) -> datetime | None:
        return await self.inner.next_timer_due_at()

    async def snapshot(self, load_id: str) -> LoadSnapshot:
        return await self.inner.snapshot(load_id)


# --- scripted answers --------------------------------------------------------------------


class ScriptedJson(BaseModel):
    """A scripted answer written as plain JSON (`script.extract`): the schema the model is
    asked for validates it at the call, so the scenario does not import that schema."""

    model_config = ConfigDict(extra="allow", frozen=True)


def scripted_transcript(calls: Sequence[ScriptedToolCall]) -> tuple[dict[str, Any], ...]:
    """The tool calls as the OpenRouter adapter's transcript (wave 3 spec, section 1.5):
    one assistant round per call, then the tool's answer. The final answer is the
    reply's `parsed` output, not a transcript entry."""
    entries: list[dict[str, Any]] = []
    for number, call in enumerate(calls, 1):
        call_id = f"call_{number}"
        entries.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": call_id, "name": call.name, "args": dict(call.args)}],
            }
        )
        entries.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "name": call.name,
                "content": SCRIPTED_TOOL_RESULT if call.result is None else call.result,
            }
        )
    return tuple(entries)


def scripted_reply(turn: AgentTurn) -> AgentOutcome | ScriptedReply:
    """What the scripted model answers for one agent turn: the outcome, with its tool
    calls as the transcript when it made any."""
    outcome = turn.outcome()
    if not turn.tool_calls:
        return outcome
    return ScriptedReply(parsed=outcome, transcript=scripted_transcript(turn.tool_calls))


# --- the runner --------------------------------------------------------------------------


class RunOutcome(BaseModel):
    """Everything one trial produced, ungraded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario_id: str
    trial: int = 1
    observations: tuple[StepObservation, ...] = ()
    failures: tuple[Failure, ...] = ()  # script accounting only (check "script")
    usage: tuple[Usage, ...] = ()  # every successful model call, in call order
    judge_usage: tuple[Usage, ...] = ()  # the judge's calls, not counted in `usage`
    warnings: tuple[str, ...] = ()
    error: str | None = None  # the scenario could not start, or the runtime raised


class RunnerError(Exception):
    """The runtime broke a rule the runner depends on."""


class ScenarioSetupError(Exception):
    """The scenario cannot start: missing or invalid fixture, unknown timezone."""


class ScenarioRunner:
    """Runs trials of scenarios. Every trial gets its own clock, storage and gateways.

    `broker` (an export, `agent eval run --broker-file`) puts every scenario on that
    broker; otherwise a scenario's own `broker_file` does.
    """

    def __init__(
        self,
        evals_dir: Path,
        knowledge: SopIndex,
        mode: Mode,
        model: LanguageModel | None = None,
        runtime_factory: RuntimeFactory | None = None,
        observer: Observer | None = None,
        *,
        trial_factory: TrialFactory | None = None,
        broker: BrokerExport | None = None,
    ) -> None:
        if mode == "scripted" and model is not None:
            raise ValueError("scripted mode feeds its own ScriptedModel; do not pass a model")
        if mode != "scripted" and model is None:
            raise ValueError(f"{mode} mode needs a model")
        if runtime_factory is not None and trial_factory is not None:
            raise ValueError("pass a runtime_factory or a trial_factory, not both")
        self._evals_dir = evals_dir
        self._knowledge = knowledge
        self._model = model
        if trial_factory is not None:
            self._factory = trial_factory
        elif runtime_factory is not None:
            self._factory = from_runtime_factory(runtime_factory)
        else:
            self._factory = build_trial_runtime
        self._observer: Observer = observer or NullObserver()
        self._broker = broker
        self._exports: dict[str, BrokerExport] = {}

    async def run(self, scenario: Scenario, trial: int = 1) -> RunOutcome:
        try:
            tz = _timezone(scenario.timezone)
            export = self._export(scenario)
            load = self._read_load(scenario, export)
            files = self._read_attachments(scenario)
            knowledge = self._knowledge if export is None else broker_index(self._knowledge, export)
        except (ScenarioSetupError, BrokerFileError) as exc:
            return RunOutcome(scenario_id=scenario.id, trial=trial, error=str(exc))

        inner, script = self._trial_model()
        model = CountingModel(inner)
        clock = VirtualClock(_local(scenario.date, scenario.start, tz))
        blobs = MemoryBlobStore()
        deps = TrialDeps(
            clock=clock,
            model=model,
            gateways=RecordingGateways().as_gateways(),
            knowledge=knowledge,
            blobs=blobs,
            broker=None if export is None else export.broker,
        )
        try:
            runtime = self._factory(deps)
        except ScenarioSetupError as exc:
            return RunOutcome(scenario_id=scenario.id, trial=trial, error=str(exc))
        play = _Trial(
            scenario,
            tz,
            load.load_id,
            clock,
            runtime,
            model,
            script,
            self._observer,
            judge=Judge(inner),
            blobs=blobs,
            files=files,
        )
        error = await play.run(load)
        return RunOutcome(
            scenario_id=scenario.id,
            trial=trial,
            observations=tuple(play.observations),
            failures=tuple(play.failures),
            usage=tuple(model.usage),
            judge_usage=tuple(play.judge_usage),
            warnings=tuple(play.warnings),
            error=error,
        )

    def _trial_model(self) -> tuple[LanguageModel, ScriptedModel | None]:
        """The given model in replay and live modes; a fresh ScriptedModel per trial otherwise."""
        if self._model is not None:
            return self._model, None
        script = ScriptedModel()
        return script, script

    def _export(self, scenario: Scenario) -> BrokerExport | None:
        if self._broker is not None:
            return self._broker
        name = scenario.broker_file
        if name is None:
            return None
        if name not in self._exports:
            path = self._evals_dir / name
            if not path.is_file():
                raise ScenarioSetupError(f"broker file not found: {name}")
            self._exports[name] = read_broker_export(path)
        return self._exports[name]

    def _read_load(self, scenario: Scenario, export: BrokerExport | None) -> Load:
        try:
            with (self._evals_dir / scenario.load).open(encoding="utf-8") as handle:
                data: Any = json.load(handle)
        except FileNotFoundError as exc:
            raise ScenarioSetupError(f"load fixture not found: {scenario.load}") from exc
        except (OSError, ValueError) as exc:
            raise ScenarioSetupError(f"cannot read load fixture {scenario.load}: {exc}") from exc
        if not isinstance(data, dict):
            raise ScenarioSetupError(f"load fixture {scenario.load} must be a JSON object")
        metadata = {**cast(dict[str, Any], data), **scenario.load_overrides}
        if export is not None:
            metadata.update(load_metadata(export))
        try:
            return Load.from_metadata(metadata)
        except DomainError as exc:
            raise ScenarioSetupError(f"{scenario.load}: {exc}") from exc

    def _read_attachments(self, scenario: Scenario) -> dict[str, bytes]:
        """Every attachment fixture's bytes, read before the trial starts: a missing file
        is a setup error, not a failure halfway through."""
        files: dict[str, bytes] = {}
        for step in scenario.steps:
            for attachment in step.message.attachments if step.message is not None else ():
                if attachment.fixture in files:
                    continue
                try:
                    files[attachment.fixture] = (self._evals_dir / attachment.fixture).read_bytes()
                except OSError as exc:
                    raise ScenarioSetupError(
                        f"attachment fixture not found: {attachment.fixture}"
                    ) from exc
        return files


class _Trial:
    """One pass through a scenario's steps: moves the clock, injects, observes."""

    def __init__(
        self,
        scenario: Scenario,
        tz: ZoneInfo,
        load_id: str,
        clock: VirtualClock,
        runtime: RuntimeLike,
        model: CountingModel,
        script: ScriptedModel | None,
        observer: Observer,
        *,
        judge: Judge,
        blobs: BlobStore,
        files: dict[str, bytes],
    ) -> None:
        self._scenario = scenario
        self._load_id = load_id
        self._clock = clock
        self._runtime = runtime
        self._model = model
        self._script = script
        self._observer = observer
        self._judge = judge
        self._blobs = blobs
        self._files = files
        self._times = [_local(scenario.date, step.at, tz) for step in scenario.steps]
        self.observations: list[StepObservation] = []
        self.failures: list[Failure] = []
        self.judge_usage: list[Usage] = []
        self.warnings: list[str] = []

    async def run(self, load: Load) -> str | None:
        """Play every step. Returns an error string when the runtime raised."""
        where = "register_load"
        try:
            await self._runtime.register_load(load)
            for index, step in enumerate(self._scenario.steps):
                where = f"step {index}"
                await self._step(index, step)
        except Exception as exc:  # the trial ends here; other scenarios and trials still run
            return f"{where}: {type(exc).__name__}: {exc}"
        return None

    async def _step(self, index: int, step: Step) -> None:
        at = self._times[index]
        async with self._observer.step(self._scenario, index, step, at) as span:
            before = await self._runtime.snapshot(self._load_id)
            calls_before = self._model.calls
            # Pushed before the clock moves: timers that fire on the way consume agent plans.
            self._push_script(step)
            await self._advance_to(at)
            if await self._inject(index, step, at):
                self._clock.set(self._settle_time(index, at))
                await self._runtime.tick()
            after = await self._runtime.snapshot(self._load_id)
            outbox = _created(before.outbox, after.outbox, key=lambda item: item.outbox_id)
            llm_calls = self._model.calls - calls_before  # before the judge, which is apart
            observation = StepObservation(
                step_index=index,
                at=at,
                outbox=outbox,
                runs=_created(before.runs, after.runs, key=lambda run: run.run_id),
                llm_calls=llm_calls,
                snapshot=after,
                judge=await self._judged(index, step, outbox),
            )
            self.observations.append(observation)
            span.observed(observation)
        self.failures.extend(self._settle_script(index))

    async def _judged(
        self, index: int, step: Step, outbox: Sequence[OutboxItem]
    ) -> JudgeResult | None:
        spec = step.expect.judge
        if spec is None:
            return None
        if self._script is not None and not step.script.judge:
            self.warnings.append(
                f"step {index}: the rubric was not judged (scripted mode, no script.judge)"
            )
            return skipped(spec, "scripted mode: the step has no script.judge verdict")
        result = await self._judge.grade(spec, outbox)
        if result.usage is not None:
            self.judge_usage.append(result.usage)
        return result

    async def _advance_to(self, at: datetime) -> None:
        """Stop at every pending timer due by `at`, in due order, and tick there (zero latency)."""
        ticks_at_instant = 0
        while (due := await self._runtime.next_timer_due_at()) is not None and due <= at:
            if due > self._clock.now():  # an overdue timer fires at the current time
                self._clock.set(due)
                ticks_at_instant = 0
            ticks_at_instant += 1
            if ticks_at_instant > _MAX_TICKS_AT_ONE_INSTANT:
                raise RunnerError(f"the runtime did not fire the timer due at {due.isoformat()}")
            await self._runtime.tick()
        self._clock.set(at)

    async def _inject(self, index: int, step: Step, at: datetime) -> bool:
        """Ingest the step's message or ping, if any. Returns whether something was ingested."""
        derived_id = f"{self._scenario.id}:{index}"
        if step.message is not None:
            message = InboundMessage(
                message_id=step.message.message_id or derived_id,
                load_id=self._load_id,
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
                load_id=self._load_id,
                lat=step.ping.lat,
                lon=step.ping.lon,
                occurred_at=at,
                delay_risk=step.ping.delay_risk,
            )
            await self._runtime.ingest_ping(ping)
            return True
        return False

    async def _attachments(self, step: Step, derived_id: str) -> tuple[Attachment, ...]:
        """The message's fixture files, stored in the trial's blob store."""
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
        settle = at + timedelta(seconds=self._scenario.run_latency_seconds)
        if index + 1 < len(self._times):
            settle = min(settle, self._times[index + 1])
        return settle

    def _push_script(self, step: Step) -> None:
        script = self._script
        if script is None:
            return
        script.push(ModelRole.GATE, *step.script.gate)
        script.push(ModelRole.AGENT, *(scripted_reply(turn) for turn in step.script.agent))
        script.push(ModelRole.EXTRACT, *(ScriptedJson(**entry) for entry in step.script.extract))
        # The judge runs after the step; queued now so that a verdict no rubric uses is
        # reported like any other unused entry.
        script.push(ModelRole.JUDGE, *step.script.judge)

    def _settle_script(self, index: int) -> list[Failure]:
        """Report unused and missing script entries for the step, then start the next one clean."""
        script = self._script
        if script is None:
            return []
        failures: list[Failure] = []
        for role in _SCRIPTED_ROLES:
            if left := script.remaining(role):
                message = f"{left} scripted {role} output(s) were not used"
                failures.append(Failure(step_index=index, check="script", message=message))
        missing = Counter(call.role for call in script.exhausted)
        for role, count in missing.items():
            message = f"the {role} model was called {count} time(s) with nothing scripted"
            failures.append(Failure(step_index=index, check="script", message=message))
        script.reset()
        return failures


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScenarioSetupError(f"unknown timezone {name!r}") from exc


def _local(day: date, clock_time: str, tz: ZoneInfo) -> datetime:
    """A scenario time (HH:MM or HH:MM:SS) on the scenario's date, as an aware datetime."""
    return datetime.combine(day, time.fromisoformat(clock_time), tzinfo=tz)


def _created[T](before: Iterable[T], after: Iterable[T], key: Callable[[T], str]) -> tuple[T, ...]:
    """Items of `after` whose id is not in `before`, in `after`'s order."""
    seen = {key(item) for item in before}
    return tuple(item for item in after if key(item) not in seen)
