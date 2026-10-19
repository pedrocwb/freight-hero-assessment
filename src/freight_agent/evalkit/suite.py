"""Suites: find a suite's scenarios, run k trials of each, grade them and add up.

A suite is a folder, `evals/scenarios/<suite>/*.yaml`. Each trial runs on a fresh
runtime with a fresh model (the runner builds both per trial) and is graded on its own.
A scenario passes when every trial passes (pass^k): a live model that gets a case right
two times out of three would get it wrong for one load in three. pass@k (at least one
trial passed) is reported next to it, because the gap between the two is the flakiness.

Scenarios tagged `capability` are measured, not enforced: they start low and graduate to
`regression` once they pass reliably. Every other scenario gates the run.

Exit codes of a run (`ExitCode`):
- 0: every gating scenario passed;
- 1: a gating scenario failed;
- 2: a trial could not run (a missing cassette, a bad fixture, a crash): the harness,
  not the agent, is broken, so no verdict is possible;
- 3: the run passed its cost cap and stopped early.
"""

from collections.abc import Callable, Iterable, Sequence
from enum import IntEnum, StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, computed_field

from freight_agent.evalkit.graders import Budget, check_invariants, grade_step
from freight_agent.evalkit.metadata import RunMetadata
from freight_agent.evalkit.results import Failure, ScenarioResult
from freight_agent.evalkit.runner import (
    Mode,
    RunOutcome,
    RuntimeFactory,
    RuntimeLike,
    ScenarioRunner,
    build_runtime,
)
from freight_agent.evalkit.spec import Scenario, load_scenario
from freight_agent.ports.clock import Clock
from freight_agent.ports.gateways import Gateways
from freight_agent.ports.llm import LanguageModel, LlmRequest, LlmResponse, SchemaT
from freight_agent.ports.sop import SopRepository
from freight_agent.settings import LlmMode

REGRESSION = "regression"
CAPABILITY = "capability"
_FIRST_GROUPS = (REGRESSION, CAPABILITY)

# Builds the model of one trial on that trial's virtual clock (circuit breakers read it).
ModelFactory = Callable[[Clock], LanguageModel]

# The runner only asks whether the model is its own script; recording is a live run whose
# model also saves the answers.
_RUNNER_MODES: dict[LlmMode, Mode] = {
    "scripted": "scripted",
    "replay": "replay",
    "live": "live",
    "record": "live",
}


class ExitCode(IntEnum):
    PASSED = 0
    FAILED = 1
    HARNESS_ERROR = 2
    COST_CAP = 3


class SuiteError(Exception):
    """The suite cannot run as asked: no such suite, a bad scenario file, an empty selection."""


class ScenarioStatus(StrEnum):
    PASSED = "pass"  # every trial passed
    FAILED = "fail"
    ERROR = "error"  # a trial could not run: harness error
    INCOMPLETE = "incomplete"  # the cost cap stopped the run before its k trials


class _Model(BaseModel):
    # Computed fields are written to results.json for its readers and recomputed on
    # load, so a stored copy of them is ignored rather than rejected.
    model_config = ConfigDict(frozen=True, extra="ignore")


class ScenarioSummary(_Model):
    scenario_id: str
    title: str
    tags: tuple[str, ...]
    k: int  # trials planned
    trials: tuple[ScenarioResult, ...]

    @computed_field
    @property
    def status(self) -> ScenarioStatus:
        if any(t.error is not None for t in self.trials):
            return ScenarioStatus.ERROR
        if len(self.trials) < self.k:
            return ScenarioStatus.INCOMPLETE
        if all(t.passed for t in self.trials):
            return ScenarioStatus.PASSED
        return ScenarioStatus.FAILED

    @computed_field
    @property
    def passed(self) -> bool:
        """pass^k: all k trials ran and passed."""
        return self.status is ScenarioStatus.PASSED

    @computed_field
    @property
    def pass_at_k(self) -> bool:
        """At least one trial passed."""
        return any(t.passed for t in self.trials)

    @computed_field
    @property
    def trials_passed(self) -> int:
        return sum(t.passed for t in self.trials)

    @computed_field
    @property
    def gating(self) -> bool:
        """Whether a failure fails the run: every scenario but a `capability` one."""
        return CAPABILITY not in self.tags

    @computed_field
    @property
    def failure_count(self) -> int:
        return sum(len(t.failures) + (t.error is not None) for t in self.trials)

    @computed_field
    @property
    def cost_usd(self) -> float:
        return sum(t.cost_usd for t in self.trials)

    @computed_field
    @property
    def tokens(self) -> int:
        return sum(u.input_tokens + u.output_tokens for t in self.trials for u in t.usage)

    @computed_field
    @property
    def model_calls(self) -> int:
        return sum(o.llm_calls for t in self.trials for o in t.observations)


class GroupSummary(_Model):
    """The scenarios that share one tag, as fractions of scenarios."""

    tag: str
    scenarios: int
    pass_at_k: float
    pass_hat_k: float  # pass^k


class SuiteResult(_Model):
    suite: str
    mode: LlmMode
    k: int
    metadata: RunMetadata
    scenarios: tuple[ScenarioSummary, ...]
    not_run: tuple[str, ...] = ()  # scenario ids the cost cap left out
    cost_cap_usd: float | None = None
    stopped: str | None = None  # why the run stopped early
    warnings: tuple[str, ...] = ()

    @computed_field
    @property
    def groups(self) -> tuple[GroupSummary, ...]:
        """One group per tag: `regression` and `capability` first, then the rest by name."""
        tags = {tag for s in self.scenarios for tag in s.tags}
        ordered = [t for t in _FIRST_GROUPS if t in tags] + sorted(tags - set(_FIRST_GROUPS))
        groups: list[GroupSummary] = []
        for tag in ordered:
            members = [s for s in self.scenarios if tag in s.tags]
            groups.append(
                GroupSummary(
                    tag=tag,
                    scenarios=len(members),
                    pass_at_k=sum(s.pass_at_k for s in members) / len(members),
                    pass_hat_k=sum(s.passed for s in members) / len(members),
                )
            )
        return tuple(groups)

    @computed_field
    @property
    def cost_usd(self) -> float:
        return sum(s.cost_usd for s in self.scenarios)

    @computed_field
    @property
    def tokens(self) -> int:
        return sum(s.tokens for s in self.scenarios)

    @computed_field
    @property
    def model_calls(self) -> int:
        return sum(s.model_calls for s in self.scenarios)

    @computed_field
    @property
    def exit_code(self) -> ExitCode:
        if any(s.status is ScenarioStatus.ERROR for s in self.scenarios):
            return ExitCode.HARNESS_ERROR
        if self.stopped is not None:
            return ExitCode.COST_CAP
        if any(s.gating and not s.passed for s in self.scenarios):
            return ExitCode.FAILED
        return ExitCode.PASSED


# --- discovery and selection ----------------------------------------------------------


def suites(evals_dir: Path) -> list[str]:
    """The suite folders under `evals/scenarios`, by name."""
    root = evals_dir / "scenarios"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and any(p.glob("*.yaml")))


def discover(evals_dir: Path, suite: str) -> list[Scenario]:
    """Every scenario of the suite, in file-name order.

    All bad files are reported in one error, so a broken suite is fixed in one pass. A
    scenario must name the suite of its folder, and ids are unique within a suite.
    """
    folder = evals_dir / "scenarios" / suite
    paths = sorted(folder.glob("*.yaml")) if folder.is_dir() else []
    if not paths:
        known = ", ".join(suites(evals_dir)) or "none"
        raise SuiteError(f"no suite {suite!r} in {evals_dir / 'scenarios'} (suites: {known})")
    scenarios: list[Scenario] = []
    problems: list[str] = []
    first_file: dict[str, str] = {}
    for path in paths:
        try:
            scenario = load_scenario(path)
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            problems.append(f"{path.name}: {_describe_error(exc)}")
            continue
        if scenario.suite != suite:
            problems.append(
                f"{path.name}: suite is {scenario.suite!r}, but the file is in {suite}/"
            )
        elif scenario.id in first_file:
            problems.append(
                f"{path.name}: the id {scenario.id!r} is also used by {first_file[scenario.id]}"
            )
        else:
            first_file[scenario.id] = path.name
            scenarios.append(scenario)
    if problems:
        raise SuiteError(f"bad scenario files in {folder}:\n  " + "\n  ".join(problems))
    return scenarios


def select(
    scenarios: Sequence[Scenario], *, tags: Iterable[str] = (), ids: Iterable[str] = ()
) -> list[Scenario]:
    """The scenarios that have any of `tags` and are among `ids`; an empty filter keeps all."""
    wanted_tags, wanted_ids = set(tags), set(ids)
    unknown = sorted(wanted_ids - {s.id for s in scenarios})
    if unknown:
        raise SuiteError(
            f"no scenario {', '.join(repr(i) for i in unknown)} in this suite "
            "(`agent eval list` shows the ids)"
        )
    chosen = [
        s
        for s in scenarios
        if (not wanted_tags or wanted_tags & set(s.tags)) and (not wanted_ids or s.id in wanted_ids)
    ]
    if not chosen:
        raise SuiteError("no scenario matches the filters")
    return chosen


# --- grading --------------------------------------------------------------------------


def grade(
    scenario: Scenario, outcome: RunOutcome, *, budget: Budget | None = None
) -> ScenarioResult:
    """One trial's verdict: script accounting, every step's checks and the invariants.

    A trial that stopped early is graded on the steps it reached, so the report still
    shows how far it got; it fails whatever those steps say.
    """
    failures: list[Failure] = list(outcome.failures)
    for step, observation in zip(scenario.steps, outcome.observations, strict=False):
        failures.extend(grade_step(step, observation, timezone=scenario.timezone))
    if outcome.observations:
        load = outcome.observations[-1].snapshot.load
        failures.extend(check_invariants(load, outcome.observations, budget=budget))
    return ScenarioResult(
        scenario_id=scenario.id,
        trial=outcome.trial,
        passed=not failures and outcome.error is None,
        failures=tuple(failures),
        observations=outcome.observations,
        usage=outcome.usage,
        error=outcome.error,
    )


# --- running --------------------------------------------------------------------------


class TrialModel:
    """The model of the trial in progress, rebuilt for every trial on its virtual clock.

    The runner takes one model for all trials and builds each trial's clock itself, so
    this stands in for the model and the runtime factory starts a new one per trial:
    circuit breakers and the router never carry state from one trial into the next.
    """

    def __init__(self, build: ModelFactory) -> None:
        self._build = build
        self._current: LanguageModel | None = None

    def start(self, clock: Clock) -> None:
        self._current = self._build(clock)

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        if self._current is None:
            raise RuntimeError("no trial started: the runtime factory starts the trial model")
        return await self._current.generate(request, schema)


class SuiteRunner:
    """Runs k trials of each scenario and aggregates them into a `SuiteResult`.

    `scripted` mode feeds each trial the scenario's own script; the other modes need a
    `model_factory`. `record` runs like `live` (the factory's model records as it goes).
    """

    def __init__(
        self,
        *,
        evals_dir: Path,
        sop: SopRepository,
        mode: LlmMode,
        model_factory: ModelFactory | None = None,
        budget: Budget | None = None,
        runtime_factory: RuntimeFactory | None = None,
    ) -> None:
        if mode == "scripted" and model_factory is not None:
            raise ValueError("scripted mode feeds the scenario scripts; do not pass a model")
        if mode != "scripted" and model_factory is None:
            raise ValueError(f"{mode} mode needs a model factory")
        self._mode: LlmMode = mode
        self._budget = budget
        inner = runtime_factory or build_runtime
        trial_model = TrialModel(model_factory) if model_factory is not None else None

        def factory(
            clock: Clock, model: LanguageModel, gateways: Gateways, sop: SopRepository
        ) -> RuntimeLike:
            if trial_model is not None:
                trial_model.start(clock)
            return inner(clock, model, gateways, sop)

        self._runner = ScenarioRunner(
            evals_dir=evals_dir,
            sop=sop,
            mode=_RUNNER_MODES[mode],
            model=trial_model,
            runtime_factory=factory,
        )

    async def run(
        self,
        scenarios: Sequence[Scenario],
        *,
        suite: str,
        metadata: RunMetadata,
        k: int = 1,
        cost_cap_usd: float | None = None,
        on_scenario: Callable[[ScenarioSummary], None] | None = None,
    ) -> SuiteResult:
        """Scenarios in the given order. The cost cap is checked after every trial, so a
        run overshoots it by at most one trial's cost."""
        if k < 1:
            raise ValueError("k must be at least 1")
        summaries: list[ScenarioSummary] = []
        spent = 0.0
        stopped: str | None = None
        not_run: tuple[str, ...] = ()
        for index, scenario in enumerate(scenarios):
            trials: list[ScenarioResult] = []
            for trial in range(1, k + 1):
                try:
                    outcome = await self._runner.run(scenario, trial=trial)
                except Exception as exc:  # outside the trial's own guard: building its model
                    error = f"{type(exc).__name__}: {exc}"
                    outcome = RunOutcome(scenario_id=scenario.id, trial=trial, error=error)
                result = grade(scenario, outcome, budget=self._budget)
                trials.append(result)
                spent += result.cost_usd
                if cost_cap_usd is not None and spent > cost_cap_usd:
                    stopped = (
                        f"stopped by the cost cap: ${spent:.4f} spent, over the cap of "
                        f"${cost_cap_usd:.2f}, during {scenario.id} (trial {trial} of {k})"
                    )
                    break
            summary = ScenarioSummary(
                scenario_id=scenario.id,
                title=scenario.title,
                tags=scenario.tags,
                k=k,
                trials=tuple(trials),
            )
            summaries.append(summary)
            if on_scenario is not None:
                on_scenario(summary)
            if stopped is not None:
                not_run = tuple(s.id for s in scenarios[index + 1 :])
                break
        return SuiteResult(
            suite=suite,
            mode=self._mode,
            k=k,
            metadata=metadata,
            scenarios=tuple(summaries),
            not_run=not_run,
            cost_cap_usd=cost_cap_usd,
            stopped=stopped,
            warnings=self._warnings(k),
        )

    def _warnings(self, k: int) -> tuple[str, ...]:
        if self._mode == "scripted" and k > 1:
            return (
                f"scripted mode is deterministic: the {k} trials of each scenario are the "
                "same trial, so k > 1 only repeats it",
            )
        return ()


def _describe_error(exc: Exception) -> str:
    """One line per file: pydantic's own message spans many lines and links to its docs."""
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'file'}: {error['msg']}"
            for error in exc.errors()
        )
    return " ".join(str(exc).split())
