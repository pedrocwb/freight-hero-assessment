"""The eval page: scenario listing and suite runs, on the same `evalkit` the CLI uses.

Trials are graded exactly as the CLI grades them (`evalkit.suite.grade`). A run happens
in a worker thread with its own event loop, so a suite that takes seconds never stalls
the simulation or the event stream.
"""

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from freight_agent.adapters.clock import SystemClock
from freight_agent.adapters.sop.files import FileSopRepository
from freight_agent.api.errors import NotFound
from freight_agent.bootstrap import build_llm
from freight_agent.evalkit import suite as eval_suite
from freight_agent.evalkit.results import Failure, ScenarioResult
from freight_agent.evalkit.runner import RunOutcome, ScenarioRunner
from freight_agent.evalkit.spec import Scenario, load_scenario
from freight_agent.ports.llm import LanguageModel
from freight_agent.settings import Settings

EvalMode = Literal["scripted", "replay"]


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvalRunRequest(_Body):
    suite: str = "core"
    mode: EvalMode = "scripted"
    k: int = Field(default=1, ge=1, le=10, description="Trials per scenario")
    scenario: str | None = Field(default=None, description="One scenario id; default all")


class ScenarioInfo(_Body):
    id: str
    title: str
    suite: str
    tags: tuple[str, ...]


class TrialFailure(_Body):
    trial: int
    step_index: int | None
    check: str
    message: str


class ScenarioSummary(_Body):
    id: str
    title: str
    tags: tuple[str, ...]
    trials: int
    passes: int
    passed: bool = Field(description="pass^k: every trial passed")
    pass_at_k: bool = Field(description="At least one trial passed")
    pass_hat_k: bool = Field(description="Every trial passed (the same as passed)")
    cost_usd: float
    failures: tuple[TrialFailure, ...]


class SuiteSummary(_Body):
    suite: str
    mode: EvalMode
    k: int
    sop_version: str
    total: int
    passed: int
    failed: int
    pass_rate: float
    cost_usd: float
    scenarios: tuple[ScenarioSummary, ...]


def scenarios(evals_dir: Path) -> list[Scenario]:
    """Every scenario under `evals/scenarios/`, by suite then id."""
    found = [load_scenario(path) for path in sorted((evals_dir / "scenarios").rglob("*.yaml"))]
    return sorted(found, key=lambda s: (s.suite, s.id))


def select(all_scenarios: Sequence[Scenario], suite: str, scenario: str | None) -> list[Scenario]:
    chosen = [s for s in all_scenarios if s.suite == suite]
    if not chosen:
        raise NotFound(f"no scenarios in suite {suite!r}")
    if scenario is not None:
        chosen = [s for s in chosen if s.id == scenario]
        if not chosen:
            raise NotFound(f"no scenario {scenario!r} in suite {suite!r}")
    return chosen


def grade(scenario: Scenario, outcome: RunOutcome) -> ScenarioResult:
    """The CLI's verdict (`evalkit.suite.grade`). A trial that could not run also lists its
    error as a failure, so the page shows why."""
    result = eval_suite.grade(scenario, outcome)
    if outcome.error is None:
        return result
    error = Failure(step_index=None, check="error", message=outcome.error)
    return result.model_copy(update={"failures": (*result.failures, error)})


async def run_suite(
    chosen: Sequence[Scenario],
    *,
    settings: Settings,
    suite: str,
    mode: EvalMode,
    k: int,
    model: LanguageModel | None = None,
) -> SuiteSummary:
    """Run `k` trials of each scenario and summarize them for the page.

    `model` replaces the replay model (tests); scripted mode always uses the scenario's
    own script.
    """
    sop = FileSopRepository(settings.sops_dir)
    if mode == "replay" and model is None:
        model = build_llm(settings, mode="replay", clock=SystemClock())
    runner = ScenarioRunner(
        evals_dir=settings.evals_dir,
        sop=sop,
        mode=mode,
        model=None if mode == "scripted" else model,
    )
    summaries: list[ScenarioSummary] = []
    for scenario in chosen:
        results = [grade(scenario, await runner.run(scenario, trial)) for trial in range(1, k + 1)]
        summaries.append(_summarize(scenario, results))
    passed = sum(1 for s in summaries if s.passed)
    return SuiteSummary(
        suite=suite,
        mode=mode,
        k=k,
        sop_version=sop.version(),
        total=len(summaries),
        passed=passed,
        failed=len(summaries) - passed,
        pass_rate=passed / len(summaries) if summaries else 0.0,
        cost_usd=sum(s.cost_usd for s in summaries),
        scenarios=tuple(summaries),
    )


def run_suite_blocking(
    chosen: Sequence[Scenario], *, settings: Settings, suite: str, mode: EvalMode, k: int
) -> SuiteSummary:
    """`run_suite` on a fresh event loop, for a worker thread."""
    return asyncio.run(run_suite(chosen, settings=settings, suite=suite, mode=mode, k=k))


def _summarize(scenario: Scenario, results: Sequence[ScenarioResult]) -> ScenarioSummary:
    passes = sum(1 for r in results if r.passed)
    every = passes == len(results)
    return ScenarioSummary(
        id=scenario.id,
        title=scenario.title,
        tags=scenario.tags,
        trials=len(results),
        passes=passes,
        passed=every,
        pass_at_k=passes > 0,
        pass_hat_k=every,
        cost_usd=sum(r.cost_usd for r in results),
        failures=tuple(
            TrialFailure(trial=r.trial, step_index=f.step_index, check=f.check, message=f.message)
            for r in results
            for f in r.failures
        ),
    )
