"""The eval page: scenario listing and suite runs, on the same `evalkit` the CLI uses.

Trials are graded exactly as the CLI grades them (`evalkit.suite.SuiteRunner`). A run
happens in a worker thread with its own event loop, so a suite that takes seconds never
stalls the simulation or the event stream.

Two entry points:
- `POST /api/evals/run`: a suite in scripted or replay mode, on the scenarios as written;
- `POST /api/brokers/{broker_id}/evals/run` (`api.schemas.BrokerEvalRunRequest`): a suite
  on one broker's current configuration, read from the session's store (its catalog and
  its published SOP version, as a broker export, `evalkit.brokers`), in scripted, replay
  or live mode, stopped by the request's cost cap. A live run is recorded as a LangSmith
  experiment when `LANGSMITH_API_KEY` is set; the summary then carries its link. LangSmith
  is a convenience here: when it is down the run goes on without it.
"""

import asyncio
import logging
import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from freight_agent.adapters.clock import SystemClock
from freight_agent.adapters.knowledge.memory import MemorySopIndex
from freight_agent.api.errors import BadRequest, NotFound
from freight_agent.bootstrap import BootstrapError, build_llm
from freight_agent.domain.broker import STANDARD_BROKER_ID, standard_broker
from freight_agent.domain.sop import SopLayer
from freight_agent.evalkit.brokers import BrokerExport, broker_index
from freight_agent.evalkit.langsmith_experiment import (
    ExperimentClient,
    LangSmithExperiment,
    LangSmithUnavailable,
)
from freight_agent.evalkit.metadata import collect_metadata
from freight_agent.evalkit.results import ScenarioResult
from freight_agent.evalkit.spec import Scenario, load_scenario
from freight_agent.evalkit.suite import ModelFactory, SuiteResult, SuiteRunner
from freight_agent.evalkit.suite import ScenarioSummary as SuiteScenario
from freight_agent.ports.clock import Clock
from freight_agent.ports.llm import LanguageModel
from freight_agent.ports.repositories import UnitOfWorkFactory
from freight_agent.settings import LlmMode, Settings

logger = logging.getLogger(__name__)

EvalMode = Literal["scripted", "replay"]
BrokerEvalMode = Literal["scripted", "replay", "live"]


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
    broker: str | None = Field(default=None, description="`acme@v2` for a broker's run")
    stopped: str | None = Field(default=None, description="Why the cost cap stopped the run")
    not_run: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    langsmith_url: str | None = Field(
        default=None, description="The LangSmith experiment of a live run, when recorded"
    )


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


# --- a broker's configuration, as the evals play it ----------------------------------------


async def session_broker(uow_factory: UnitOfWorkFactory, broker_id: str) -> BrokerExport:
    """The broker's current version and its published sections (its own layers), read in
    one unit of work of the session's store. `standard` is the standard catalog until a
    broker of that id is saved."""
    async with uow_factory() as uow:
        broker = await uow.brokers.get(broker_id)
        published = None if broker is None else await uow.sops.published(broker_id)
    if broker is None:
        if broker_id != STANDARD_BROKER_ID:
            raise NotFound(f"no broker {broker_id!r}")
        broker = standard_broker()
    sections = tuple(
        s
        for s in (published.sections if published is not None else ())
        if s.layer is not SopLayer.STANDARD and s.client_id == broker_id
    )
    return BrokerExport(broker=broker, sections=sections)


# --- running ------------------------------------------------------------------------------

# Builds the LangSmith client of a live run, or None when LangSmith is not configured.
ClientFactory = Callable[[Settings], ExperimentClient | None]


def langsmith_client(settings: Settings) -> ExperimentClient | None:
    key = settings.langsmith_api_key
    if key is None or not key.get_secret_value().strip():
        return None
    from langsmith import Client

    client: Any = Client(
        api_key=key.get_secret_value(), api_url=os.environ.get("LANGSMITH_ENDPOINT") or None
    )
    return client


async def run_suite(
    chosen: Sequence[Scenario],
    *,
    settings: Settings,
    suite: str,
    mode: LlmMode,
    k: int,
    model: LanguageModel | None = None,
    broker: BrokerExport | None = None,
    cost_cap_usd: float | None = None,
    experiment_client: ClientFactory | None = None,
) -> SuiteSummary:
    """Run `k` trials of each scenario and summarize them for the page.

    `model` replaces the replay or live model (tests); scripted mode always uses the
    scenario's own script. `broker` plays every scenario on that broker.
    """
    if mode == "record":
        raise BadRequest("record mode writes cassettes: run it from the CLI")
    knowledge = MemorySopIndex.from_files(settings.sops_dir)
    played = knowledge if broker is None else broker_index(knowledge, broker)
    sop_version = getattr(played, "content_version", knowledge.content_version)
    factory = _model_factory(settings, mode, model)
    metadata = collect_metadata(
        created_at=datetime.now(UTC),
        mode=mode,
        model_set="default",
        sop_version=sop_version,
        models_config=settings.models_config,
        repo=Path.cwd(),
        settings=settings.describe(),
        broker=None if broker is None else broker.label(),
    )
    experiment = (
        _experiment(
            (experiment_client or langsmith_client)(settings),
            suite,
            chosen,
            metadata,
            k,
            cost_cap_usd,
        )
        if mode == "live"
        else None
    )
    runner = SuiteRunner(
        evals_dir=settings.evals_dir,
        knowledge=knowledge,
        mode=mode,
        model_factory=factory,
        observer=experiment,
        broker=broker,
    )
    result = await runner.run(
        chosen, suite=suite, metadata=metadata, k=k, cost_cap_usd=cost_cap_usd
    )
    url = experiment.finish(result) if experiment is not None else None
    return summarize(result, sop_version=sop_version, langsmith_url=url)


def _model_factory(
    settings: Settings, mode: LlmMode, model: LanguageModel | None
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


def _experiment(
    client: ExperimentClient | None,
    suite: str,
    chosen: Sequence[Scenario],
    metadata: Any,
    k: int,
    cost_cap_usd: float | None,
) -> LangSmithExperiment | None:
    """The live run's experiment, or None: LangSmith is not configured or not reachable."""
    if client is None:
        return None
    experiment = LangSmithExperiment(
        client, suite=suite, dataset_scenarios=chosen, metadata=metadata, k=k,
        cost_cap_usd=cost_cap_usd,
    )  # fmt: skip
    try:
        experiment.start()
    except LangSmithUnavailable as exc:
        logger.warning("LangSmith unavailable, the eval runs without an experiment: %s", exc)
        return None
    return experiment


def run_suite_blocking(
    chosen: Sequence[Scenario],
    *,
    settings: Settings,
    suite: str,
    mode: LlmMode,
    k: int,
    broker: BrokerExport | None = None,
    cost_cap_usd: float | None = None,
) -> SuiteSummary:
    """`run_suite` on a fresh event loop, for a worker thread."""
    return asyncio.run(
        run_suite(
            chosen,
            settings=settings,
            suite=suite,
            mode=mode,
            k=k,
            broker=broker,
            cost_cap_usd=cost_cap_usd,
        )
    )


def summarize(
    result: SuiteResult, *, sop_version: str, langsmith_url: str | None = None
) -> SuiteSummary:
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
        broker=result.metadata.broker,
        stopped=result.stopped,
        not_run=result.not_run,
        warnings=result.warnings,
        langsmith_url=langsmith_url,
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
