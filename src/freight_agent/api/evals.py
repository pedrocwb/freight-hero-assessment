"""The eval page: scenario listing and suite runs, on the same `evalkit` the CLI uses.

Trials are graded exactly as the CLI grades them (`evalkit.suite.SuiteRunner`). A run
happens in a worker thread with its own event loop, so a suite that takes seconds never
stalls the simulation or the event stream. `POST /api/evals/run` runs a suite in
scripted or replay mode, on the scenarios as written: neither spends money. Live and
record runs belong to the CLI (`agent eval run`).
"""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from freight_agent.adapters.clock import SystemClock
from freight_agent.adapters.knowledge.memory import MemorySopIndex
from freight_agent.api.errors import BadRequest, NotFound
from freight_agent.bootstrap import BootstrapError, build_llm
from freight_agent.evalkit.metadata import collect_metadata
from freight_agent.evalkit.results import ScenarioResult
from freight_agent.evalkit.spec import Scenario, load_scenario
from freight_agent.evalkit.suite import ModelFactory, SuiteResult, SuiteRunner
from freight_agent.evalkit.suite import ScenarioSummary as SuiteScenario
from freight_agent.ports.clock import Clock
from freight_agent.ports.llm import LanguageModel
from freight_agent.settings import LlmMode, Settings

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
    mode: LlmMode
    k: int
    sop_version: str
    total: int
    passed: int
    failed: int
    pass_rate: float
    cost_usd: float
    scenarios: tuple[ScenarioSummary, ...]
    not_run: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


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


# --- running ------------------------------------------------------------------------------


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
    knowledge = MemorySopIndex.from_files(settings.sops_dir)
    sop_version = knowledge.content_version
    metadata = collect_metadata(
        created_at=datetime.now(UTC),
        mode=mode,
        model_set="default",
        sop_version=sop_version,
        models_config=settings.models_config,
        repo=Path.cwd(),
        settings=settings.describe(),
    )
    runner = SuiteRunner(
        evals_dir=settings.evals_dir,
        knowledge=knowledge,
        mode=mode,
        model_factory=_model_factory(settings, mode, model),
    )
    result = await runner.run(chosen, suite=suite, metadata=metadata, k=k)
    return summarize(result, sop_version=sop_version)


def _model_factory(
    settings: Settings, mode: EvalMode, model: LanguageModel | None
) -> ModelFactory | None:
    if mode == "scripted":
        return None
    if model is not None:
        given = model

        def stand_in(clock: Clock) -> LanguageModel:
            return given

        return stand_in

    def build(clock: Clock) -> LanguageModel:
        return build_llm(settings, mode=mode, clock=clock)

    try:
        build(SystemClock())  # a missing key or a broken models file fails before any trial
    except (BootstrapError, ValueError) as exc:
        raise BadRequest(f"cannot build the {mode} model: {exc}") from exc
    return build


def run_suite_blocking(
    chosen: Sequence[Scenario], *, settings: Settings, suite: str, mode: EvalMode, k: int
) -> SuiteSummary:
    """`run_suite` on a fresh event loop, for a worker thread."""
    return asyncio.run(run_suite(chosen, settings=settings, suite=suite, mode=mode, k=k))


def summarize(result: SuiteResult, *, sop_version: str) -> SuiteSummary:
    summaries = tuple(_summarize(s) for s in result.scenarios)
    passed = sum(1 for s in summaries if s.passed)
    return SuiteSummary(
        suite=result.suite,
        mode=result.mode,
        k=result.k,
        sop_version=sop_version,
        total=len(summaries),
        passed=passed,
        failed=len(summaries) - passed,
        pass_rate=passed / len(summaries) if summaries else 0.0,
        cost_usd=result.cost_usd,
        scenarios=summaries,
        not_run=result.not_run,
        warnings=result.warnings,
    )


def _summarize(summary: SuiteScenario) -> ScenarioSummary:
    """A trial that could not run lists its error as a failure, so the page shows why."""
    return ScenarioSummary(
        id=summary.scenario_id,
        title=summary.title,
        tags=summary.tags,
        trials=len(summary.trials),
        passes=summary.trials_passed,
        passed=summary.passed,
        pass_at_k=summary.pass_at_k,
        pass_hat_k=summary.passed,
        cost_usd=summary.cost_usd,
        failures=tuple(f for r in summary.trials for f in _failures(r)),
    )


def _failures(result: ScenarioResult) -> list[TrialFailure]:
    failures = [
        TrialFailure(trial=result.trial, step_index=f.step_index, check=f.check, message=f.message)
        for f in result.failures
    ]
    if result.error is not None:
        failures.append(
            TrialFailure(trial=result.trial, step_index=None, check="error", message=result.error)
        )
    return failures
