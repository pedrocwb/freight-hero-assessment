"""The scenario runner: plays one eval scenario inside the platform's simulation, live.

`POST /api/scenarios/{id}/run` resets the simulation at the scenario's start, in its
timezone, registers the scenario's load (its fixture and overrides) and plays its steps
in the background with the same player the LangSmith experiments use
(`evalkit.runner.ScenarioPlayer`): the clock stops at every timer due on the way, each
message or ping runs after its latency, the judge grades a step with a rubric. So the
regular load endpoints show the scenario's load and its runs as they happen, each run
with its LangSmith trace when tracing is on.

After every step the step's checks (`evalkit.graders.step_checks`, the invariants with
the last step) go out on the event stream as a `scenario` event, the body of `GET
/api/scenarios/run`. One scenario plays at a time; it needs the model
(`Settings.model_ready`). While it plays, the simulation refuses every client's write
(`SimulationService.playing`).
"""

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from freight_agent.api.errors import ApiError, BadRequest, Conflict, NotFound
from freight_agent.api.simulation import Session, SimulationService
from freight_agent.domain.load import Load
from freight_agent.evalkit.graders import StepCheck, check_invariants, step_checks
from freight_agent.evalkit.judge import Judge
from freight_agent.evalkit.results import StepObservation
from freight_agent.evalkit.runner import (
    ScenarioPlayer,
    ScenarioSetupError,
    read_attachments,
    read_load,
    scenario_start,
    scenario_timezone,
)
from freight_agent.evalkit.spec import Scenario, StepKind, load_scenario
from freight_agent.settings import Settings

logger = logging.getLogger(__name__)

RunStatus = Literal["running", "passed", "failed", "error"]
StepStatus = Literal["pending", "running", "passed", "failed"]


class ServiceUnavailable(ApiError):
    status_code = 503


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ScenarioStep(_Body):
    index: int
    at: str = Field(description="HH:MM, the scenario's local time")
    note: str
    kind: StepKind


class ScenarioInfo(_Body):
    id: str
    title: str
    suite: str
    tags: tuple[str, ...]
    description: str
    steps: tuple[ScenarioStep, ...]


class ScenarioStarted(_Body):
    scenario_id: str
    load_id: str


class StepRun(_Body):
    index: int
    at: str
    note: str
    status: StepStatus = "pending"
    checks: tuple[StepCheck, ...] = ()
    run_ids: tuple[str, ...] = ()


class ScenarioRun(_Body):
    scenario_id: str
    load_id: str
    status: RunStatus = "running"
    error: str | None = None
    cost_usd: float = 0.0
    started_at: datetime
    finished_at: datetime | None = None
    steps: tuple[StepRun, ...]


def catalog(evals_dir: Path) -> list[Scenario]:
    """Every scenario under `evals/scenarios/`, by suite then id."""
    found = [load_scenario(path) for path in sorted((evals_dir / "scenarios").rglob("*.yaml"))]
    return sorted(found, key=lambda s: (s.suite, s.id))


def info(scenario: Scenario) -> ScenarioInfo:
    return ScenarioInfo(
        id=scenario.id,
        title=scenario.title,
        suite=scenario.suite,
        tags=scenario.tags,
        description=scenario.description,
        steps=tuple(
            ScenarioStep(index=i, at=step.at[:5], note=step.note, kind=step.kind)
            for i, step in enumerate(scenario.steps)
        ),
    )


class ScenarioRunner:
    def __init__(self, service: SimulationService) -> None:
        self._service = service
        self._run: ScenarioRun | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def settings(self) -> Settings:
        return self._service.settings

    @property
    def current(self) -> ScenarioRun:
        if self._run is None:
            raise NotFound("no scenario has run yet: POST /api/scenarios/{id}/run starts one")
        return self._run

    def scenarios(self) -> list[ScenarioInfo]:
        return [info(s) for s in catalog(self.settings.evals_dir)]

    async def start(self, scenario_id: str) -> ScenarioStarted:
        """Reset the simulation for the scenario and start playing it in the background."""
        playing = self._service.playing
        if playing is not None:
            raise Conflict(f"the scenario {playing} is still running")
        scenario = next((s for s in catalog(self.settings.evals_dir) if s.id == scenario_id), None)
        if scenario is None:
            raise NotFound(f"no scenario {scenario_id!r} (GET /api/scenarios lists them)")
        if not self.settings.model_ready:
            raise ServiceUnavailable(
                "OPENROUTER_API_KEY is not set: a scenario plays on the live model"
            )
        evals_dir = self.settings.evals_dir
        try:
            load = read_load(evals_dir, scenario)
            files = read_attachments(evals_dir, scenario)
            start, timezone = scenario_start(scenario), scenario_timezone(scenario)
        except ScenarioSetupError as exc:
            raise BadRequest(str(exc)) from exc
        self._service.playing = scenario.id  # from here on, client writes get a 409
        try:
            session = await self._service.open_scenario(start, timezone)
            player = ScenarioPlayer(
                scenario,
                load=load,
                runtime=session.simulation.runtime,
                clock=session.clock,
                model=session.model,
                blobs=session.blobs,
                files=files,
                judge=Judge(session.inner_model),
            )
            await self._service.start_scenario(session, player)
        except BaseException:
            self._service.playing = None
            raise
        self._run = ScenarioRun(
            scenario_id=scenario.id,
            load_id=load.load_id,
            started_at=datetime.now(UTC),
            steps=tuple(
                StepRun(index=i, at=step.at[:5], note=step.note)
                for i, step in enumerate(scenario.steps)
            ),
        )
        self._publish()
        self._task = asyncio.create_task(self._play(session, player))
        return ScenarioStarted(scenario_id=scenario.id, load_id=load.load_id)

    async def _play(self, session: Session, player: ScenarioPlayer) -> None:
        try:
            await self._steps(session, player)
        except asyncio.CancelledError:  # the server is shutting down
            self._finish("error", "the run was cancelled", session, player)
            raise
        finally:
            self._service.playing = None

    async def _steps(self, session: Session, player: ScenarioPlayer) -> None:
        scenario = player.scenario
        observations: list[StepObservation] = []
        index = 0
        try:
            for index, step in enumerate(scenario.steps):
                self._step(index, status="running")
                observation = await self._service.play_step(session, player, index)
                observations.append(observation)
                checks = step_checks(step, observation, timezone=scenario.timezone)
                if index == len(scenario.steps) - 1:
                    checks += _invariants(player.load, observations)
                self._cost(session, player)  # before the step's event, which carries it
                self._step(
                    index,
                    status="passed" if all(c.passed for c in checks) else "failed",
                    checks=tuple(checks),
                    run_ids=tuple(r.run_id for r in observation.runs),
                )
        except Exception as exc:  # the run stops here; the platform goes on
            logger.warning("scenario %s failed at step %d", scenario.id, index, exc_info=exc)
            self._step(index, status="failed")
            self._finish("error", f"step {index}: {type(exc).__name__}: {exc}", session, player)
            return
        steps = self.current.steps
        status: RunStatus = "passed" if all(s.status == "passed" for s in steps) else "failed"
        self._finish(status, None, session, player)

    def _step(self, index: int, **changes: object) -> None:
        run = self.current
        steps = list(run.steps)
        steps[index] = steps[index].model_copy(update=changes)
        self._run = run.model_copy(update={"steps": tuple(steps)})
        self._publish()

    def _cost(self, session: Session, player: ScenarioPlayer) -> None:
        cost = session.model.cost_usd + sum(u.cost_usd for u in player.judge_usage)
        self._run = self.current.model_copy(update={"cost_usd": cost})

    def _finish(
        self, status: RunStatus, error: str | None, session: Session, player: ScenarioPlayer
    ) -> None:
        self._cost(session, player)
        self._run = self.current.model_copy(
            update={"status": status, "error": error, "finished_at": datetime.now(UTC)}
        )
        self._publish()

    def _publish(self) -> None:
        self._service.bus.publish("scenario", self.current)


def _invariants(load: Load, observations: Sequence[StepObservation]) -> list[StepCheck]:
    """The scenario-wide checks, shown with the last step."""
    failures = check_invariants(load, observations)
    checks: list[StepCheck] = []
    for name in ("invariant.idempotency", "invariant.recipients"):
        mine = [f for f in failures if f.check == name]
        detail = "; ".join(f.message for f in mine) or None
        checks.append(StepCheck(name=name, passed=not mine, detail=detail))
    return checks
