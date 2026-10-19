"""Deterministic scenario runner: one trial of one scenario.

It drives the same `AgentRuntime` facade the product uses, on a virtual clock,
with recording gateways and in-memory storage, and observes what each step
changed. It does not grade: graders judge the observations separately, so the
two are built and tested on their own. The timing rules are the ones in the
`evalkit.spec` docstring.
"""

import json
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict

from freight_agent.adapters.channels.fakes.recording import RecordingGateways
from freight_agent.adapters.clock import SequentialIdGenerator, VirtualClock
from freight_agent.adapters.llm.scripted import ScriptedModel
from freight_agent.adapters.observability.null import NullTracer
from freight_agent.adapters.persistence.memory.store import MemoryStore
from freight_agent.application.contracts import (
    InboundMessage,
    IngestResult,
    LoadSnapshot,
    LocationPing,
)
from freight_agent.application.runtime import AgentRuntime, RuntimeDeps
from freight_agent.domain.errors import DomainError
from freight_agent.domain.load import Load
from freight_agent.domain.runs import RunRecord, Usage
from freight_agent.evalkit.counting import CountingModel
from freight_agent.evalkit.results import Failure, StepObservation
from freight_agent.evalkit.spec import Scenario, Step
from freight_agent.ports.clock import Clock
from freight_agent.ports.gateways import Gateways
from freight_agent.ports.llm import LanguageModel, ModelRole
from freight_agent.ports.sop import SopRepository

Mode = Literal["scripted", "replay", "live"]

# Each tick fires every due timer, so a timer still due after this many ticks at
# one instant means the runtime never fires it; stop instead of looping forever.
_MAX_TICKS_AT_ONE_INSTANT = 100


class RuntimeLike(Protocol):
    """The part of the `AgentRuntime` facade the runner uses."""

    async def register_load(self, load: Load) -> None: ...

    async def ingest_message(self, message: InboundMessage) -> IngestResult: ...

    async def ingest_ping(self, ping: LocationPing) -> IngestResult: ...

    async def tick(self) -> list[RunRecord]: ...

    async def next_timer_due_at(self) -> datetime | None: ...

    async def snapshot(self, load_id: str) -> LoadSnapshot: ...


RuntimeFactory = Callable[[Clock, LanguageModel, Gateways, SopRepository], RuntimeLike]


def build_runtime(
    clock: Clock, model: LanguageModel, gateways: Gateways, sop: SopRepository
) -> RuntimeLike:
    """The product runtime on fresh in-memory storage, with deterministic ids and no tracing."""
    deps = RuntimeDeps(
        # The ignore works around a port mismatch: `UnitOfWork` declares its repositories
        # as mutable attributes, which are invariant, so the concrete memory repositories
        # do not match. Read-only properties on the protocol would fix it.
        uow_factory=MemoryStore().unit_of_work,  # pyright: ignore[reportArgumentType]
        clock=clock,
        ids=SequentialIdGenerator(),
        llm=model,
        sop=sop,
        gateways=gateways,
        tracer=NullTracer(),
    )
    return AgentRuntime(deps)


class RunOutcome(BaseModel):
    """Everything one trial produced, ungraded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario_id: str
    trial: int = 1
    observations: tuple[StepObservation, ...] = ()
    failures: tuple[Failure, ...] = ()  # script accounting only (check "script")
    usage: tuple[Usage, ...] = ()  # every successful model call, in call order
    error: str | None = None  # the scenario could not start, or the runtime raised


class RunnerError(Exception):
    """The runtime broke a rule the runner depends on."""


class ScenarioSetupError(Exception):
    """The scenario cannot start: missing or invalid fixture, unknown timezone."""


class ScenarioRunner:
    """Runs trials of scenarios. Every trial gets its own clock, storage and gateways."""

    def __init__(
        self,
        evals_dir: Path,
        sop: SopRepository,
        mode: Mode,
        model: LanguageModel | None = None,
        runtime_factory: RuntimeFactory | None = None,
    ) -> None:
        if mode == "scripted" and model is not None:
            raise ValueError("scripted mode feeds its own ScriptedModel; do not pass a model")
        if mode != "scripted" and model is None:
            raise ValueError(f"{mode} mode needs a model")
        self._evals_dir = evals_dir
        self._sop = sop
        self._model = model
        self._factory = runtime_factory or build_runtime

    async def run(self, scenario: Scenario, trial: int = 1) -> RunOutcome:
        try:
            tz = _timezone(scenario.timezone)
            load = self._read_load(scenario)
        except ScenarioSetupError as exc:
            return RunOutcome(scenario_id=scenario.id, trial=trial, error=str(exc))

        inner, script = self._trial_model()
        model = CountingModel(inner)
        clock = VirtualClock(_local(scenario.date, scenario.start, tz))
        runtime = self._factory(clock, model, RecordingGateways().as_gateways(), self._sop)
        play = _Trial(scenario, tz, load.load_id, clock, runtime, model, script)
        error = await play.run(load)
        return RunOutcome(
            scenario_id=scenario.id,
            trial=trial,
            observations=tuple(play.observations),
            failures=tuple(play.failures),
            usage=tuple(model.usage),
            error=error,
        )

    def _trial_model(self) -> tuple[LanguageModel, ScriptedModel | None]:
        """The given model in replay and live modes; a fresh ScriptedModel per trial otherwise."""
        if self._model is not None:
            return self._model, None
        script = ScriptedModel()
        return script, script

    def _read_load(self, scenario: Scenario) -> Load:
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
        try:
            return Load.from_metadata(metadata)
        except (DomainError, TypeError) as exc:  # TypeError: an override of the wrong shape
            raise ScenarioSetupError(f"{scenario.load}: {exc}") from exc


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
    ) -> None:
        self._scenario = scenario
        self._load_id = load_id
        self._clock = clock
        self._runtime = runtime
        self._model = model
        self._script = script
        self._times = [_local(scenario.date, step.at, tz) for step in scenario.steps]
        self.observations: list[StepObservation] = []
        self.failures: list[Failure] = []

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
        before = await self._runtime.snapshot(self._load_id)
        calls_before = self._model.calls
        # Pushed before the clock moves: timers that fire on the way consume decide plans.
        self._push_script(step)
        await self._advance_to(at)
        if await self._inject(index, step, at):
            self._clock.set(self._settle_time(index, at))
            await self._runtime.tick()
        after = await self._runtime.snapshot(self._load_id)
        self.observations.append(
            StepObservation(
                step_index=index,
                at=at,
                outbox=_created(before.outbox, after.outbox, key=lambda item: item.outbox_id),
                runs=_created(before.runs, after.runs, key=lambda run: run.run_id),
                llm_calls=self._model.calls - calls_before,
                snapshot=after,
            )
        )
        self.failures.extend(self._settle_script(index))

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

    def _settle_time(self, index: int, at: datetime) -> datetime:
        """When the injected event's run happens: after the latency, never past the next step."""
        settle = at + timedelta(seconds=self._scenario.run_latency_seconds)
        if index + 1 < len(self._times):
            settle = min(settle, self._times[index + 1])
        return settle

    def _push_script(self, step: Step) -> None:
        if self._script is None:
            return
        self._script.push(ModelRole.GATE, *step.script.gate)
        self._script.push(ModelRole.DECIDE, *step.script.decide)

    def _settle_script(self, index: int) -> list[Failure]:
        """Report unused and missing script entries for the step, then start the next one clean."""
        script = self._script
        if script is None:
            return []
        failures: list[Failure] = []
        for role in (ModelRole.GATE, ModelRole.DECIDE):
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
