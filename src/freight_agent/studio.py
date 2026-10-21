"""LangGraph Studio's entry point: an eval scenario played as a graph, live on OpenRouter.

`langgraph.json` serves `graph` to the Agent Server (`make studio`), and Studio draws it
with the run graph inside its `run` node:

    START -> setup -> advance
    advance -> run      an event is queued: one visit per event, the run graph inside
                        (prefilter -> gate -> knowledge -> agent <-> tools -> validate)
    advance -> deliver  the tick queued nothing
    advance -> check    the step is over
    run -> run | deliver
    deliver -> advance
    check -> advance (the next step) | END

The input names a scenario of `evals/scenarios/<suite>/`, and optionally the last step
to play (0-based):

    {"scenario_id": "a_client_a_high_risk", "until_step": 1}

or, free form, one inbound message or ping on a load fixture of `evals/fixtures/`, played
as a one-step scenario at 07:00 on the assignment's day (America/Los_Angeles):

    {"load": "load_481207", "message": {"channel": "sms", "sender": "drv_20931",
     "body": "What's the PO number?"}}
    {"load": "load_481207", "ping": {"delay_risk": "high"}}

Each node moves a fresh world, the way the scenario player of the evals and the platform
moves it (`evalkit.runner.ScenarioPlayer`), with one tick of the runtime split into its
three parts so that each run is a node visit of its own:

- `setup`: the world: the product runtime from the composition root (a memory store, a
  virtual clock at the scenario's start, recording gateways, the live model, the SOP
  files), and the scenario's load (its fixture with the overrides) registered.
- `advance`: the player's `next_tick`: the clock moves to the next instant the runtime
  must tick at (a timer due on the way; or the step's time, where its message or ping is
  ingested, and its run latency later), then what is due is queued: timers fired,
  attachments read (`AgentRuntime.queue_due_events`). Or the step is over.
- `run`: one queued event, through `AgentRuntime.process_next`: the claim, the run graph,
  the commit. Studio shows the run graph's nodes inside this one, each with the
  `RunState` after it (the event in `ctx`, the gate decision, the SOP sections, the
  conversation, the staged proposals, the trajectory, the validation, the verdict). The
  state after `run` holds the event, the run record and the ledger, timers and outbox
  after the commit.
- `deliver`: the outbox dispatched; `sent` is what the recording gateways took.
- `check`: the step's checks, as the eval graders judge them (`evalkit.graders`), the
  judge included for a step with a rubric, and after the last step the invariants.

Why the run graph is a subgraph although no node is the run graph: a LangGraph graph
streamed from inside another graph's node inherits that node's config, so it streams and
checkpoints under the node (`RunGraph.stream`). A node's `subgraphs` tells LangGraph which
graph that is, for xray drawings and for reading the subgraph's checkpoints back; the
worlds' run graphs are built per play, so `run` names one built the same way at import
(`compile_graph`, from the same `build_runtime`), on a model that is never called.

The world cannot live in a checkpoint (a store, a clock, the model's clients), so it lives
in this process, and the state holds its id and how many nodes have moved it (`moves`).
The outer state is plain JSON. A checkpoint is a picture of the world, not the world:
a fork or a replay of an earlier checkpoint fails (the world has moved on), and so does a
thread whose server restarted; Studio's interrupts on the outer nodes do work, since the
world waits for the next node. The run graph's own checkpoints hold `RunState`
(dataclasses and pydantic models): the server's serializer stores them as msgpack and
loads them back as types it was not told about (LangGraph logs a warning per type), and
tuples come back as lists. That only touches what Studio reads; the run itself uses the
`RunState` objects LangGraph streams, never one loaded back.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Annotated, Any, TypedDict, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from langgraph.graph import END, START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph.state import (  # pyright: ignore[reportMissingTypeStubs]
    CompiledStateGraph,
)
from pydantic import BaseModel, Field, ValidationError

from freight_agent.adapters.channels.fakes.recording import RecordingGateways
from freight_agent.adapters.clock import VirtualClock
from freight_agent.adapters.documents.memory_blobs import MemoryBlobStore
from freight_agent.adapters.llm.unavailable import UnavailableModel
from freight_agent.adapters.observability.null import NullTracer
from freight_agent.application.contracts import LoadSnapshot
from freight_agent.application.runtime import AgentRuntime
from freight_agent.bootstrap import (
    build_runtime,
    build_uow_factory,
    gate_decisions,
    platform_llm,
)
from freight_agent.domain.events import Event
from freight_agent.domain.runs import RunRecord
from freight_agent.evalkit.counting import CountingModel
from freight_agent.evalkit.graders import check_invariants, step_checks
from freight_agent.evalkit.judge import Judge
from freight_agent.evalkit.results import StepObservation
from freight_agent.evalkit.runner import (
    ScenarioPlayer,
    ScenarioSetupError,
    StepProgress,
    read_attachments,
    read_load,
    scenario_start,
    scenario_timezone,
)
from freight_agent.evalkit.spec import Scenario
from freight_agent.evalkit.suite import discover, suites
from freight_agent.interfaces.llm import LanguageModel
from freight_agent.interfaces.repositories import UnitOfWorkFactory
from freight_agent.settings import Settings

SETUP, ADVANCE, RUN, DELIVER, CHECK = "setup", "advance", "run", "deliver", "check"
# Every node visit is a LangGraph step: a long scenario needs far more than the default 25.
RECURSION_LIMIT = 2_000
# The worlds this process keeps, newest last; a play that ends drops its own.
KEEP_WORLDS = 8
# The free-form input's one step: the assignment's morning, as on the platform.
FREE_FORM_DATE, FREE_FORM_START, FREE_FORM_AT = date(2026, 10, 13), "06:55", "07:00"
# A free-form load names a fixture of evals/fixtures, never a path elsewhere.
_FIXTURE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


class StudioError(ValueError):
    """The input or the thread cannot be played. Says what to do instead."""


class StudioInput(TypedDict, total=False):
    """What Studio asks for: a scenario (and how far to play it), or one free-form input."""

    scenario_id: Annotated[str, Field(description="An eval scenario, e.g. a_client_a_high_risk")]
    until_step: Annotated[int, Field(description="The last step to play, 0-based; all by default")]
    load: Annotated[str, Field(description="Free form: a load fixture of evals/fixtures")]
    message: Annotated[
        dict[str, Any],
        Field(description='Free form: {"channel": "sms", "sender": "drv_20931", "body": "..."}'),
    ]
    ping: Annotated[dict[str, Any], Field(description='Free form: {"delay_risk": "high"}')]


class StudioState(TypedDict, total=False):
    """The scenario graph's state, all JSON. The world itself is in the process (`World`)."""

    scenario_id: str
    until_step: int
    load: str
    message: dict[str, Any]
    ping: dict[str, Any]
    world: str  # the world's id in this process
    moves: int  # how many nodes have moved the world; a checkpoint behind it cannot go on
    scenario: dict[str, Any]  # id, title, description, timezone and steps
    step: int  # the step being played
    clock: str  # the world's time, local
    note: str  # what the last node did, in a sentence
    step_over: bool  # `advance` found nothing more to do in the step
    finished: bool  # `check` played the last step
    queued: int  # events waiting for a run
    logged: list[dict[str, Any]]  # events the last `advance` logged
    event: dict[str, Any]  # the event of the last run
    run: dict[str, Any]  # the last run record
    runs: list[dict[str, Any]]  # every run so far, in a line each
    ledger: list[dict[str, Any]]  # the load's obligations
    timers: list[dict[str, Any]]
    outbox: list[dict[str, Any]]
    sent: list[dict[str, Any]]  # every delivery the recording gateways took
    checks: list[dict[str, Any]]  # each played step's checks
    cost_usd: float  # the play's model spend, the judge's included


@dataclass
class World:
    """One play: the runtime and what it runs on, and how far the play has gone."""

    scenario: Scenario
    last_step: int
    runtime: AgentRuntime
    clock: VirtualClock
    timezone: ZoneInfo
    gateways: RecordingGateways
    uow: UnitOfWorkFactory  # the runtime's store, for the queue's size
    model: CountingModel
    player: ScenarioPlayer
    world_id: str = field(default_factory=lambda: uuid4().hex)
    moves: int = 0
    progress: StepProgress | None = None  # the step being played
    observations: list[StepObservation] = field(default_factory=list[StepObservation])
    runs: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    checks: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    @property
    def load_id(self) -> str:
        return self.player.load.load_id

    def now(self) -> str:
        return self.clock.now().astimezone(self.timezone).isoformat()

    def time_of(self, at: datetime | None = None) -> str:
        return (at or self.clock.now()).astimezone(self.timezone).strftime("%H:%M:%S")

    async def queued(self) -> int:
        async with self.uow() as uow:
            return await uow.queue.size()

    async def snapshot(self) -> LoadSnapshot:
        return await self.runtime.snapshot(self.load_id)

    @property
    def cost_usd(self) -> float:
        return self.model.cost_usd + sum(u.cost_usd for u in self.player.judge_usage)


class Worlds:
    """The worlds of this process, by id. Past `keep`, the oldest goes."""

    def __init__(self, keep: int = KEEP_WORLDS) -> None:
        self._keep = keep
        self._worlds: dict[str, World] = {}

    def add(self, world: World) -> None:
        self._worlds[world.world_id] = world
        while len(self._worlds) > self._keep:
            del self._worlds[next(iter(self._worlds))]

    def drop(self, world: World) -> None:
        self._worlds.pop(world.world_id, None)

    def move(self, state: StudioState) -> World:
        """The state's world, for a node that is about to move it.

        The state must be the world's latest picture: a checkpoint written before the
        world's last move (a fork, a replay, a node that failed halfway) cannot go on.
        """
        world = self._worlds.get(state.get("world", ""))
        if world is None:
            raise StudioError(
                "this thread's world is gone (the server restarted or reloaded, or the play "
                "ended): start a new thread"
            )
        if state.get("moves") != world.moves:
            raise StudioError(
                "this checkpoint is behind its world (a fork, a replay or a failed node): a "
                "world cannot go back, so start a new thread"
            )
        world.moves += 1
        return world


# --- the input ------------------------------------------------------------------------------


def catalog(settings: Settings) -> dict[str, Scenario]:
    """Every eval scenario, by id."""
    evals_dir = settings.evals_dir
    return {s.id: s for suite in suites(evals_dir) for s in discover(evals_dir, suite)}


def scenario_of(state: StudioState, settings: Settings) -> Scenario:
    """The scenario the input asks for: a scenario of the evals, or the free-form input as
    a one-step scenario."""
    scenario_id, load = state.get("scenario_id"), state.get("load")
    if scenario_id and load:
        raise StudioError("give scenario_id or load (free form), not both")
    if scenario_id:
        scenarios = catalog(settings)
        if scenario_id not in scenarios:
            raise StudioError(f"no scenario {scenario_id!r}; scenarios: {', '.join(scenarios)}")
        return scenarios[scenario_id]
    if load:
        return free_form(load, message=state.get("message"), ping=state.get("ping"))
    raise StudioError('give {"scenario_id": "a_client_a_high_risk"}, or a free-form load')


def free_form(
    load: str, *, message: Mapping[str, Any] | None, ping: Mapping[str, Any] | None
) -> Scenario:
    """One message or ping on the load fixture `evals/fixtures/<load>.json`, at 07:00."""
    if (message is None) == (ping is None):
        raise StudioError("a free-form input has one message or one ping")
    name = load.removesuffix(".json")
    if not _FIXTURE_NAME.match(name):
        raise StudioError(f"load names a fixture of evals/fixtures, such as load_481207: {load!r}")
    fixture = f"fixtures/{name}.json"
    kind = "message" if message is not None else "ping"
    step: dict[str, Any] = {"at": FREE_FORM_AT, "note": f"free form: one {kind}"}
    step[kind] = message if message is not None else ping
    try:
        return Scenario.model_validate(
            {
                "id": "free_form",
                "title": f"Free form: one {kind} on {fixture}",
                "suite": "studio",
                "load": fixture,
                "date": FREE_FORM_DATE,
                "start": FREE_FORM_START,
                "steps": [step],
            }
        )
    except ValidationError as exc:
        raise StudioError(f"the free-form {kind} is not valid: {exc}") from None


def last_step(state: StudioState, scenario: Scenario) -> int:
    last = len(scenario.steps) - 1
    until = state.get("until_step", last)
    if not 0 <= until <= last:
        raise StudioError(f"until_step must be between 0 and {last} for {scenario.id}")
    return until


# --- the graph ------------------------------------------------------------------------------


# A world's model on its own clock (the circuit breakers read it).
ModelFactory = Callable[[VirtualClock], LanguageModel]


class _Nodes:
    def __init__(self, settings: Settings, model: ModelFactory, worlds: Worlds) -> None:
        self._settings = settings
        self._model = model
        self._worlds = worlds

    def _open(self, scenario: Scenario, last: int) -> World:
        """A fresh world for `scenario`, built as the platform builds its simulation
        (`api.simulation.open_session`), from the composition root's builders."""
        settings = self._settings
        try:
            load = read_load(settings.evals_dir, scenario)
            files = read_attachments(settings.evals_dir, scenario)
            timezone = scenario_timezone(scenario)
        except ScenarioSetupError as exc:
            raise StudioError(str(exc)) from exc
        clock = VirtualClock(scenario_start(scenario))
        persistence = build_uow_factory(settings)
        gateways = RecordingGateways()
        blobs = MemoryBlobStore()
        inner = self._model(clock)
        decisions = gate_decisions(settings)
        model = CountingModel(inner, decisions)  # the jev gate's calls are counted too
        runtime = build_runtime(
            settings,
            clock=clock,
            gateways=gateways.as_gateways(),
            llm=model,
            decisions=model if decisions is not None else None,
            persistence=persistence,
            blobs=blobs,
            # The Agent Server traces the whole play, the run graph nested in it, when
            # tracing is on: a run needs no link of its own.
            tracer=NullTracer(),
        )
        player = ScenarioPlayer(
            scenario,
            load=load,
            runtime=runtime,
            clock=clock,
            model=model,
            blobs=blobs,
            files=files,
            judge=Judge(inner, runtime.prompts.judge),
        )
        return World(
            scenario=scenario,
            last_step=last,
            runtime=runtime,
            clock=clock,
            timezone=timezone,
            gateways=gateways,
            uow=persistence,
            model=model,
            player=player,
        )

    async def setup(self, state: StudioState) -> StudioState:
        scenario = scenario_of(state, self._settings)
        world = self._open(scenario, last_step(state, scenario))
        await world.player.start()
        self._worlds.add(world)
        load = world.player.load
        update: StudioState = {
            "world": world.world_id,
            "moves": world.moves,
            "scenario": _scenario(scenario),
            "step": 0,
            "clock": world.now(),
            "note": (
                f"{world.time_of()}: load {load.load_id} registered (broker {load.broker_id}); "
                f"steps 0 to {world.last_step} of {len(scenario.steps)} will play"
            ),
            "finished": False,
            "step_over": False,
            "queued": 0,
            "runs": [],
            "checks": [],
            "sent": [],
            "cost_usd": 0.0,
        }
        return _with_books(update, await world.snapshot())

    async def advance(self, state: StudioState) -> StudioState:
        world = self._worlds.move(state)
        if world.progress is None:
            world.progress = await world.player.begin(state.get("step", 0))
        index = world.progress.index
        before = await world.snapshot()
        ticks = await world.player.next_tick(world.progress)
        if ticks:
            await world.runtime.queue_due_events()
        after = await world.snapshot()
        logged = _created(before.events, after.events, lambda e: e.event_id)
        queued = await world.queued()
        if not ticks:
            note = f"{world.time_of()}: step {index} is over"
        else:
            seen = ", ".join(f"{e.type.value} {e.event_id}" for e in logged) or "nothing new"
            note = f"{world.time_of()}: step {index} ticks; logged: {seen}; queued: {queued}"
        update: StudioState = {
            "moves": world.moves,
            "clock": world.now(),
            "note": note,
            "step_over": not ticks,
            "queued": queued,
            "logged": [_json(e) for e in logged],
        }
        return _with_books(update, after)

    async def run(self, state: StudioState) -> StudioState:
        world = self._worlds.move(state)
        record = await world.runtime.process_next()
        if record is None:
            raise StudioError(f"{state.get('queued')} event(s) queued but none could be claimed")
        snapshot = await world.snapshot()
        event = next((e for e in snapshot.events if e.event_id == record.event_id), None)
        world.runs.append(_run_line(record, event, world))
        staged = len(record.plan.actions) if record.plan is not None else 0
        update: StudioState = {
            "moves": world.moves,
            "clock": world.now(),
            "note": (
                f"{world.time_of(record.started_at)}: run {record.run_id} on "
                f"{event.type.value if event else 'an event'} {record.event_id}: "
                f"{record.status.value}, {staged} action(s) staged, nodes "
                f"{' -> '.join(record.nodes)}"
            ),
            "queued": await world.queued(),
            "event": _json(event) if event is not None else {},
            "run": _json(record),
            "runs": list(world.runs),
            "cost_usd": world.cost_usd,
        }
        return _with_books(update, snapshot)

    async def deliver(self, state: StudioState) -> StudioState:
        world = self._worlds.move(state)
        sent = await world.runtime.dispatch_outbox()
        update: StudioState = {
            "moves": world.moves,
            "note": f"{world.time_of()}: {sent} outbox item(s) delivered",
            "sent": [
                {"channel": d.channel, "idempotency_key": d.idempotency_key, **d.payload}
                for d in world.gateways.log.deliveries
            ],
        }
        return _with_books(update, await world.snapshot())

    async def check(self, state: StudioState) -> StudioState:
        world = self._worlds.move(state)
        progress = world.progress
        if progress is None:
            raise StudioError("no step is being played: start a new thread")
        observation = await world.player.observe(progress)
        world.progress = None
        world.observations.append(observation)
        scenario = world.scenario
        step = scenario.steps[progress.index]
        found = step_checks(step, observation, timezone=scenario.timezone)
        checks = [c.model_dump(mode="json") for c in found]
        finished = progress.index >= world.last_step
        if finished:
            checks += _invariants(world)
        passed = sum(c["passed"] for c in checks)
        world.checks.append(
            {
                "step": progress.index,
                "at": step.at,
                "note": step.note,
                "passed": passed == len(checks),
                "llm_calls": observation.llm_calls,
                "runs": [r.run_id for r in observation.runs],
                "checks": checks,
            }
        )
        if finished:
            self._worlds.drop(world)
        return {
            "moves": world.moves,
            "step": progress.index + 1,
            "finished": finished,
            "note": f"step {progress.index}: {passed} of {len(checks)} checks passed",
            "checks": list(world.checks),
            "cost_usd": world.cost_usd,
        }


def after_advance(state: StudioState) -> str:
    if state.get("step_over"):
        return CHECK
    return RUN if state.get("queued") else DELIVER


def after_run(state: StudioState) -> str:
    return RUN if state.get("queued") else DELIVER


def after_check(state: StudioState) -> str:
    return END if state.get("finished") else ADVANCE


def build_studio_graph(
    settings: Settings, *, model: ModelFactory | None = None, worlds: Worlds | None = None
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """The scenario graph. `model` gives each world its model on the world's clock: by
    default the live one (`platform_llm`), and a play without `OPENROUTER_API_KEY` stops
    at `setup` (every run would escalate); a test gives a fake one."""
    if model is None:
        model = _live_model(settings)
    nodes = _Nodes(settings, model, worlds or Worlds())
    builder = StateGraph(StudioState, input_schema=StudioInput)
    builder.add_node(SETUP, nodes.setup)
    builder.add_node(ADVANCE, nodes.advance)
    builder.add_node(RUN, nodes.run)
    builder.add_node(DELIVER, nodes.deliver)
    builder.add_node(CHECK, nodes.check)
    builder.add_edge(START, SETUP)
    builder.add_edge(SETUP, ADVANCE)
    builder.add_conditional_edges(ADVANCE, after_advance, [RUN, DELIVER, CHECK])
    builder.add_conditional_edges(RUN, after_run, [RUN, DELIVER])
    builder.add_edge(DELIVER, ADVANCE)
    builder.add_conditional_edges(CHECK, after_check, [ADVANCE, END])
    compiled = builder.compile(name="scenario")
    compiled.nodes[RUN].subgraphs = [_drawn_run_graph(settings)]
    return cast(
        CompiledStateGraph[Any, Any, Any, Any],
        compiled.with_config(recursion_limit=RECURSION_LIMIT),
    )


def _live_model(settings: Settings) -> ModelFactory:
    def live(clock: VirtualClock) -> LanguageModel:
        if not settings.model_ready:
            raise StudioError("OPENROUTER_API_KEY is not set: a scenario plays on the live model")
        return platform_llm(settings, clock=clock)

    return live


def _drawn_run_graph(settings: Settings) -> Any:
    """A run graph built as every world's is, on a model that is never called: what
    LangGraph draws inside `run` and reads the run graph's checkpoints with."""
    runtime = build_runtime(
        settings,
        llm=UnavailableModel("the drawing of the run graph never runs"),
        blobs=MemoryBlobStore(),
        tracer=NullTracer(),
    )
    return runtime.graph.compiled


# --- the state's JSON -----------------------------------------------------------------------


def _json(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _scenario(scenario: Scenario) -> dict[str, Any]:
    return {
        "id": scenario.id,
        "title": scenario.title,
        "description": scenario.description,
        "timezone": scenario.timezone,
        "date": scenario.date.isoformat(),
        "start": scenario.start,
        "steps": [
            {"index": i, "at": s.at, "kind": s.kind, "note": s.note}
            for i, s in enumerate(scenario.steps)
        ],
    }


def _with_books(update: StudioState, snapshot: LoadSnapshot) -> StudioState:
    """`update` with the load's ledger, timers and outbox, as the store holds them."""
    update["ledger"] = [_json(o) for o in snapshot.obligations]
    update["timers"] = [_json(t) for t in snapshot.timers]
    update["outbox"] = [_json(i) for i in snapshot.outbox]
    return update


def _run_line(record: RunRecord, event: Event | None, world: World) -> dict[str, Any]:
    return {
        "run_id": record.run_id,
        "at": world.time_of(record.started_at),
        "event_id": record.event_id,
        "event_type": event.type.value if event is not None else None,
        "status": record.status.value,
        "intent": record.intent,
        "staged": [a.type for a in record.plan.actions] if record.plan is not None else [],
        "nodes": list(record.nodes),
    }


def _invariants(world: World) -> list[dict[str, Any]]:
    """The scenario-wide checks, shown with the last step played."""
    failures = check_invariants(world.player.load, world.observations)
    checks: list[dict[str, Any]] = []
    for name in ("invariant.idempotency", "invariant.recipients"):
        mine = [f.message for f in failures if f.check == name]
        checks.append({"name": name, "passed": not mine, "detail": "; ".join(mine) or None})
    return checks


def _created[T](before: Sequence[T], after: Sequence[T], key: Callable[[T], str]) -> list[T]:
    seen = {key(item) for item in before}
    return [item for item in after if key(item) not in seen]


graph = build_studio_graph(Settings())
