"""Command line entry point: `agent eval run|list`, `agent sop ...` (in `cli_sop.py`),
`agent prompts ...` (in `cli_prompts.py`), `agent memory ...` (in `cli_memory.py`),
`agent serve` and `agent db migrate`.

Paths come from `Settings` (environment or `.env`; the defaults are relative to the
repository root), so the CLI, the API and the evals read the same folders.

Only typer and the settings are imported at module level. The eval harness pulls in the
runtime and LangGraph, and a live model LangChain, so each command imports what it needs
when it runs: `agent --help` starts fast and nothing touches the network on import.
"""

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from freight_agent.cli_memory import memory_app
from freight_agent.cli_prompts import prompts_app
from freight_agent.cli_sop import sop_app
from freight_agent.cli_temporal import temporal_app
from freight_agent.settings import MissingSetting, Settings

if TYPE_CHECKING:
    from langchain_core.embeddings import Embeddings

    from freight_agent.adapters.knowledge.memory import MemorySopIndex
    from freight_agent.application.runtime import RuntimeConfig
    from freight_agent.evalkit.experiment import CostMeter, ExperimentRun, ScenarioTarget
    from freight_agent.evalkit.metadata import RunMetadata
    from freight_agent.evalkit.spec import Scenario
    from freight_agent.interfaces.decisions import DecisionModel
    from freight_agent.interfaces.prompts import Prompts

app = typer.Typer(no_args_is_help=True, help="Freight load agent.", add_completion=False)
eval_app = typer.Typer(
    no_args_is_help=True, help="Run the scenario evals as LangSmith experiments, list them."
)
app.add_typer(eval_app, name="eval")
app.add_typer(sop_app, name="sop")
app.add_typer(prompts_app, name="prompts")
app.add_typer(memory_app, name="memory")
app.add_typer(temporal_app, name="temporal")

# Exit codes of `agent eval run`.
PASSED = 0
FAILED = 1  # a regression scenario failed pass^k
HARNESS_ERROR = 2  # the run could not start: a bad scenario, no key, LangSmith down
COST_CAP = 3  # the cost cap stopped the run before every example played


class UsageProblem(Exception):
    """The command cannot run as asked. The message says what to change."""


def _show_version(value: bool) -> None:
    if not value:
        return
    try:
        typer.echo(f"freight-agent {package_version('freight-agent')}")
    except PackageNotFoundError:
        typer.echo("freight-agent (not installed)")
    raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_show_version, is_eager=True, help="Show the version and exit."
        ),
    ] = False,
) -> None:
    """Freight load agent."""


# --- agent eval run ---------------------------------------------------------------------


@eval_app.command("run")
def eval_run(
    suite: Annotated[str, typer.Option(help="Scenario folder under evals/scenarios.")] = "core",
    scenario: Annotated[
        list[str] | None,
        typer.Option(help="Only these scenario ids. Repeat or comma-separate."),
    ] = None,
    k: Annotated[
        int,
        typer.Option("--k", min=1, help="Repetitions per scenario: pass^k needs all to pass."),
    ] = 1,
    cost_cap_usd: Annotated[
        float,
        typer.Option(min=0, help="Stop playing examples once the experiment spent this."),
    ] = 0.25,
    concurrency: Annotated[int, typer.Option(min=1, help="Examples played at the same time.")] = 4,
    prompt_tag: Annotated[
        str | None,
        typer.Option(
            help="Run every prompt at this Hub tag (a candidate) instead of its pin in "
            "config/prompts.yaml.",
            show_default="the pins",
        ),
    ] = None,
    gate_engine: Annotated[
        str | None,
        typer.Option(
            help="The gate's engine: llm (the gate's chat model) or jev (the decision model).",
            show_default="GATE_ENGINE, else jev",
        ),
    ] = None,
) -> None:
    """Run a suite live, as a LangSmith experiment, and print its pass^k table.

    Spends from OPENROUTER_API_KEY and needs LANGSMITH_API_KEY.

    Exit code 0: every regression scenario passed pass^k. 1: one failed.

    Exit code 2: the run could not start (a bad scenario, a missing key, LangSmith down).

    Exit code 3: the cost cap stopped the run before every example played.
    """
    settings = Settings()
    if prompt_tag:
        settings = settings.model_copy(update={"prompts_tag": prompt_tag})
    if gate_engine:
        if gate_engine not in ("llm", "jev"):
            typer.echo(f"error: --gate-engine is llm or jev, not {gate_engine!r}", err=True)
            raise typer.Exit(HARNESS_ERROR)
        settings = settings.model_copy(update={"gate_engine": gate_engine})
    try:
        code = _run(
            settings,
            suite=suite,
            ids=_split(scenario),
            k=k,
            cost_cap_usd=cost_cap_usd,
            concurrency=concurrency,
        )
    except (UsageProblem, MissingSetting) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(HARNESS_ERROR) from None
    raise typer.Exit(code)


@dataclass(frozen=True)
class _Trials:
    """What every trial of a run shares: the SOPs, the prompts, the gate, the embeddings."""

    knowledge: "MemorySopIndex"
    prompts: "Prompts"
    decisions: "DecisionModel | None"
    config: "RuntimeConfig"
    embeddings: "Embeddings"


def _check_ready(settings: Settings) -> None:
    """The checks that need no network: the OpenRouter key (`MissingSetting`), the models
    file and the SOP folder (`UsageProblem`)."""
    from freight_agent.adapters.clock import SystemClock
    from freight_agent.bootstrap import build_llm

    settings.require_openrouter_key()
    build_llm(settings, clock=SystemClock())  # a broken models file stops the run here
    if not settings.sops_dir.is_dir():
        raise UsageProblem(f"the SOP folder {settings.sops_dir} does not exist (SOPS_DIR)")


def _prepare_trials(settings: Settings) -> _Trials:
    """Build what the trials share; `UsageProblem` when a prompt cannot be had."""
    from freight_agent.adapters.knowledge.memory import MemorySopIndex
    from freight_agent.bootstrap import (
        BootstrapError,
        build_embeddings,
        build_prompts,
        build_runtime_config,
        gate_decisions,
    )

    return _Trials(
        knowledge=MemorySopIndex.from_files(settings.sops_dir),
        prompts=_or_usage(BootstrapError, build_prompts, settings),  # each pin pulled once
        decisions=_or_usage(BootstrapError, gate_decisions, settings),  # jev's model, or None
        config=build_runtime_config(settings),
        embeddings=build_embeddings(settings),  # the facility memory's index, for every trial
    )


def _or_usage[T](error: type[Exception], build: "Callable[[Settings], T]", settings: Settings) -> T:
    try:
        return build(settings)
    except error as exc:
        raise UsageProblem(str(exc)) from exc


def _target(
    settings: Settings,
    trials: _Trials,
    scenarios: "Iterable[Scenario]",
    meter: "CostMeter",
    on_output: "Callable[[Scenario, dict[str, Any]], None] | None" = None,
) -> "ScenarioTarget":
    from freight_agent.adapters.clock import SystemClock
    from freight_agent.bootstrap import build_llm
    from freight_agent.evalkit.experiment import ScenarioTarget

    return ScenarioTarget(
        {s.id: s for s in scenarios},
        evals_dir=settings.evals_dir,
        knowledge=trials.knowledge,
        prompts=trials.prompts,
        model_factory=lambda: build_llm(settings, clock=SystemClock()),
        meter=meter,
        config=trials.config,
        decisions=trials.decisions,
        embeddings=trials.embeddings,
        on_output=on_output,
    )


def _metadata(
    settings: Settings, trials: _Trials, *, suite: str, k: int, cost_cap_usd: float
) -> "RunMetadata":
    from freight_agent.evalkit.metadata import collect_metadata

    return collect_metadata(
        created_at=datetime.now(UTC),
        suite=suite,
        k=k,
        cost_cap_usd=cost_cap_usd,
        sop_version=trials.knowledge.content_version,
        models_config=settings.models_config,
        prompts=trials.prompts,
        repo=Path.cwd(),
        gate_engine=trials.config.gate_engine.value,
        gate_act_threshold=trials.config.gate_act_threshold,
    )


def _langsmith_client(settings: Settings) -> Any:
    import os

    from langsmith import Client

    key = settings.require_langsmith_key()
    return Client(
        api_key=key.get_secret_value(),
        api_url=os.environ.get("LANGSMITH_ENDPOINT") or None,
    )


def _describe_trials(trials: _Trials) -> list[str]:
    return [
        "prompts: " + ", ".join(p.describe() for p in trials.prompts.all()),
        f"gate engine: {trials.config.gate_engine.value}",
    ]


def _embeddings_spent(trials: _Trials) -> str | None:
    spent = getattr(trials.embeddings, "spent_usd", None)
    if isinstance(spent, float) and spent:
        return f"facility memory embeddings: ${spent:.6f} (not in the cost cap)"
    return None


def _run(
    settings: Settings,
    *,
    suite: str,
    ids: list[str],
    k: int,
    cost_cap_usd: float,
    concurrency: int,
) -> int:
    from freight_agent.bootstrap import enable_native_tracing
    from freight_agent.evalkit.experiment import CostMeter, LangSmithUnavailable, run_experiment
    from freight_agent.evalkit.suite import SuiteError, discover, select

    settings.require_langsmith_key()
    _check_ready(settings)
    try:
        suite_scenarios = discover(settings.evals_dir, suite)
        chosen = select(suite_scenarios, ids=ids)
    except SuiteError as exc:
        raise UsageProblem(str(exc)) from exc
    trials = _prepare_trials(settings)
    # The examples' traces carry each graph node and model round: LangChain traces them
    # into the experiment through LangSmith's context.
    enable_native_tracing(settings)
    metadata = _metadata(settings, trials, suite=suite, k=k, cost_cap_usd=cost_cap_usd)
    target = _target(settings, trials, suite_scenarios, CostMeter(cap_usd=cost_cap_usd))
    client = _langsmith_client(settings)
    typer.echo(
        f"{suite}: {len(chosen)} scenarios, k={k}, live on OpenRouter, cost cap ${cost_cap_usd:.2f}"
    )
    for line in _describe_trials(trials):
        typer.echo(line)
    try:
        run = asyncio.run(
            run_experiment(
                client,
                suite=suite,
                suite_scenarios=suite_scenarios,
                chosen=chosen,
                target=target,
                metadata=metadata,
                k=k,
                agent=settings.langsmith_project,
                concurrency=concurrency,
            )
        )
    except LangSmithUnavailable as exc:
        raise UsageProblem(f"LangSmith: {exc}; nothing was run") from exc
    typer.echo(render_table(run, k))
    if spent := _embeddings_spent(trials):
        typer.echo(spent)
    return exit_code(run)


def render_table(run: "ExperimentRun", k: int) -> str:
    """One line per scenario (pass^k, the repetitions that passed, the first failure),
    then the totals and the experiment's link."""
    lines = [f"experiment {run.name}"]
    width = max((len(o.scenario.id) for o in run.outcomes), default=10)
    for o in run.outcomes:
        verdict = "FAIL" if not all(o.passed) else ("pass" if o.pass_hat_k else "not run")
        line = f"  {o.scenario.id:<{width}}  {verdict:<7}  {sum(o.passed)}/{k}  ${o.cost_usd:.4f}"
        first = next((f for fs in o.failures for f in fs), None)
        if first is not None:
            line += f"  {first.describe()[:160]}"
        lines.append(line)
    passed = sum(o.pass_hat_k for o in run.outcomes)
    lines.append(f"pass^{k}: {passed}/{len(run.outcomes)} scenarios, ${run.cost_usd:.4f} spent")
    if run.not_run:
        lines.append(f"not run (cost cap): {', '.join(run.not_run)}")
    lines.append(f"  {run.url}" if run.url else "  (no experiment link)")
    return "\n".join(lines)


def exit_code(run: "ExperimentRun") -> int:
    from freight_agent.evalkit.experiment import regressions

    if regressions(run.outcomes):
        return FAILED
    if run.not_run:
        return COST_CAP
    return PASSED


def _split(values: Iterable[str] | None) -> list[str]:
    """`--scenario a,b --scenario c` -> [a, b, c]."""
    return [part.strip() for value in values or () for part in value.split(",") if part.strip()]


# --- agent eval list ------------------------------------------------------------------


@eval_app.command("list")
def eval_list(
    suite: Annotated[
        str | None, typer.Option(help="Only this suite.", show_default="every suite")
    ] = None,
) -> None:
    """List the scenarios of each suite with their tags."""
    from freight_agent.evalkit.suite import SuiteError, discover, suites

    evals_dir = Settings().evals_dir
    names = [suite] if suite is not None else suites(evals_dir)
    if not names:
        typer.echo(f"no suites in {evals_dir / 'scenarios'}")
    try:
        for name in names:
            scenarios = discover(evals_dir, name)
            typer.echo(f"{name} ({len(scenarios)} scenarios)")
            for s in scenarios:
                typer.echo(f"  {s.id:<40} {', '.join(s.tags)}")
    except SuiteError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(HARNESS_ERROR) from None


# --- agent play -------------------------------------------------------------------------


@app.command("play")
def play(
    scenario_ids: Annotated[
        list[str] | None,
        typer.Argument(
            metavar="[SCENARIO_ID]...",
            help="Scenarios to play, by id (searched in every suite). Comma-separate or list.",
            show_default=False,
        ),
    ] = None,
    suite: Annotated[
        str | None, typer.Option(help="Only scenarios of this suite (folder of evals/scenarios).")
    ] = None,
    tag: Annotated[
        list[str] | None,
        typer.Option(help="Only scenarios with this tag, e.g. sop:driver_questions. Repeatable."),
    ] = None,
    k: Annotated[
        int, typer.Option("--k", min=1, help="Plays per scenario: it passes when all pass.")
    ] = 1,
    cost_cap_usd: Annotated[
        float, typer.Option(min=0, help="Stop starting new plays once this much is spent.")
    ] = 0.05,
    no_upload: Annotated[
        bool,
        typer.Option(
            "--no-upload",
            help="Play locally only: no LangSmith experiment and no traces. Needs no "
            "LANGSMITH_API_KEY; without it the prompts are the copy in prompts/.",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show every check and each step's note."),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the result as JSON instead of the transcript.")
    ] = False,
) -> None:
    """Play scenarios live and show, step by step, what came in, what the agent did and
    why, and which checks passed.

    The same player and graders as `agent eval run`. Spends from OPENROUTER_API_KEY.

    The results go to LangSmith as an experiment (LANGSMITH_API_KEY) unless --no-upload.

    Exit code 0: every scenario passed (all its plays). 1: one failed.

    Exit code 2: it could not start. 3: the cost cap stopped it before every play ran.
    """
    settings = Settings()
    try:
        code = _play(
            settings,
            ids=_split(scenario_ids),
            suite=suite,
            tags=_split(tag),
            k=k,
            cost_cap_usd=cost_cap_usd,
            upload=not no_upload,
            verbose=verbose,
            as_json=as_json,
        )
    except (UsageProblem, MissingSetting) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(HARNESS_ERROR) from None
    raise typer.Exit(code)


def _play(
    settings: Settings,
    *,
    ids: list[str],
    suite: str | None,
    tags: list[str],
    k: int,
    cost_cap_usd: float,
    upload: bool,
    verbose: bool,
    as_json: bool,
) -> int:
    import json
    import os

    from freight_agent.bootstrap import enable_native_tracing
    from freight_agent.evalkit.experiment import (
        CostCapReached,
        CostMeter,
        LangSmithUnavailable,
        ScenarioOutcome,
        outcomes,
        run_experiment,
    )
    from freight_agent.evalkit.spec import Scenario
    from freight_agent.evalkit.suite import SuiteError, discover, find
    from freight_agent.evalkit.transcript import (
        Experiment,
        PlayResult,
        play_json,
        render_play,
        render_summary,
    )

    try:  # the choice first: it needs no key
        chosen = find(settings.evals_dir, ids=ids, suite=suite, tags=tags)
    except SuiteError as exc:
        raise UsageProblem(str(exc)) from exc
    if upload:
        settings.require_langsmith_key()
    _check_ready(settings)
    trials = _prepare_trials(settings)
    say = (lambda text: typer.echo(text, err=True)) if as_json else typer.echo
    where = "uploaded to LangSmith" if upload else "local only"
    plural = "s" if len(chosen) != 1 else ""
    say(
        f"playing {len(chosen)} scenario{plural}, k={k}, live on OpenRouter, "
        f"cost cap ${cost_cap_usd:.2f}, {where}"
    )
    for line in _describe_trials(trials):
        say(line)

    played: dict[str, list[PlayResult]] = {s.id: [] for s in chosen}

    def show(scenario: Scenario, outputs: dict[str, Any]) -> None:
        result = PlayResult.from_outputs(scenario, outputs, len(played[scenario.id]) + 1)
        played[scenario.id].append(result)
        if not as_json:
            typer.echo("")
            typer.echo(render_play(result, k=k, verbose=verbose))

    meter = CostMeter(cap_usd=cost_cap_usd)
    experiments: list[Experiment] = []

    async def upload_each_suite() -> list[ScenarioOutcome]:
        client = _langsmith_client(settings)
        found: list[ScenarioOutcome] = []
        for name in dict.fromkeys(s.suite for s in chosen):  # one experiment per suite
            suite_scenarios = discover(settings.evals_dir, name)
            try:
                run = await run_experiment(
                    client,
                    suite=name,
                    suite_scenarios=suite_scenarios,
                    chosen=[s for s in chosen if s.suite == name],
                    target=_target(settings, trials, suite_scenarios, meter, show),
                    metadata=_metadata(
                        settings, trials, suite=name, k=k, cost_cap_usd=cost_cap_usd
                    ),
                    k=k,
                    agent=settings.langsmith_project,
                    concurrency=1,  # one play at a time: the transcripts in order
                    description=f"agent play, suite {name}, k={k}",
                )
            except LangSmithUnavailable as exc:
                raise UsageProblem(f"LangSmith: {exc}; nothing more was run") from exc
            found += run.outcomes
            experiments.append(Experiment(suite=name, name=run.name, url=run.url))
        return found

    async def play_locally() -> list[ScenarioOutcome]:
        target = _target(settings, trials, chosen, meter, show)
        rows: list[tuple[dict[str, Any], dict[str, Any] | None, str | None]] = []
        for scenario in chosen:
            for _ in range(k):
                inputs = {"scenario_id": scenario.id}
                try:
                    rows.append((inputs, await target(inputs), None))
                except CostCapReached as exc:
                    rows.append((inputs, None, f"{type(exc).__name__}: {exc}"))
        return outcomes({s.id: s for s in chosen}, rows)

    if upload:
        enable_native_tracing(settings)
        found = asyncio.run(upload_each_suite())
    else:
        os.environ["LANGSMITH_TRACING"] = "false"  # local only: no trace leaves the machine
        found = asyncio.run(play_locally())
    order = {s.id: n for n, s in enumerate(chosen)}
    found.sort(key=lambda o: order[o.scenario.id])
    failed = any(not all(o.passed) for o in found)
    code = FAILED if failed else COST_CAP if any(o.not_run for o in found) else PASSED
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "k": k,
                    "exit_code": code,
                    "passed": code == PASSED,
                    "cost_usd": meter.spent_usd,
                    "scenarios": [
                        {
                            "id": o.scenario.id,
                            "suite": o.scenario.suite,
                            "title": o.scenario.title,
                            "passed": o.pass_hat_k,
                            "not_run": o.not_run,
                            "cost_usd": o.cost_usd,
                            "plays": [play_json(r) for r in played[o.scenario.id]],
                        }
                        for o in found
                    ],
                    "experiments": [
                        {"suite": e.suite, "name": e.name, "url": e.url} for e in experiments
                    ],
                },
                indent=2,
            )
        )
    else:
        typer.echo("")
        typer.echo(render_summary(found, k=k, cost_usd=meter.spent_usd, experiments=experiments))
    if spent := _embeddings_spent(trials):
        say(spent)
    return code


# --- agent serve --------------------------------------------------------------------------


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", help="Interface to listen on."),
    port: int = typer.Option(8000, help="Port of the platform API (the web UI proxies to it)."),
) -> None:
    """Serve the simulation platform's API (the web UI in web/ talks to it)."""
    from freight_agent.api.__main__ import main as serve_api

    serve_api(["--host", host, "--port", str(port)])


db_app = typer.Typer(no_args_is_help=True, help="The Postgres database (PERSISTENCE=postgres).")
app.add_typer(db_app, name="db")


@db_app.command("migrate")
def db_migrate() -> None:
    """Apply the pending migrations of migrations/ to DATABASE_URL (environment or .env),
    then set up the facility memory's LangGraph store there (its tables and vector index).

    Each file is applied once and recorded in schema_migrations, so a rerun applies only
    new files; the store's setup keeps its own migration counters and is idempotent too.
    Exit code 2: no DATABASE_URL, an unreachable database, a bad migration or no models
    config.
    """
    from psycopg import OperationalError

    from freight_agent.adapters.memory.postgres import setup_store
    from freight_agent.adapters.persistence.postgres.migrate import MigrationError, migrate
    from freight_agent.bootstrap import BootstrapError, memory_dims

    try:
        settings = Settings()
        url = settings.require_database_url().get_secret_value()
        applied = migrate(url)
        dims = memory_dims(settings)
        asyncio.run(setup_store(url, dims))
    except MissingSetting as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(2) from exc
    except (MigrationError, BootstrapError) as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(2) from exc
    except OperationalError as exc:  # the message names host and port, never the password
        typer.echo(f"error: cannot reach the database: {str(exc).strip().splitlines()[0]}")
        raise typer.Exit(2) from exc
    typer.echo(
        f"applied {len(applied)} migration(s)" + (f": {', '.join(applied)}" if applied else "")
    )
    typer.echo(f"the facility memory store is set up ({dims}-dimension vectors)")
