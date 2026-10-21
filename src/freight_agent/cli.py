"""Command line entry point: `agent eval run|list`, `agent sop ...` (in `cli_sop.py`),
`agent prompts ...` (in `cli_prompts.py`), `agent serve` and `agent db migrate`.

Paths come from `Settings` (environment or `.env`; the defaults are relative to the
repository root), so the CLI, the API and the evals read the same folders.

Only typer and the settings are imported at module level. The eval harness pulls in the
runtime and LangGraph, and a live model LangChain, so each command imports what it needs
when it runs: `agent --help` starts fast and nothing touches the network on import.
"""

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from freight_agent.cli_prompts import prompts_app
from freight_agent.cli_sop import sop_app
from freight_agent.settings import MissingSetting, Settings

if TYPE_CHECKING:
    from freight_agent.evalkit.experiment import ExperimentRun

app = typer.Typer(no_args_is_help=True, help="Freight load agent.", add_completion=False)
eval_app = typer.Typer(
    no_args_is_help=True, help="Run the scenario evals as LangSmith experiments, list them."
)
app.add_typer(eval_app, name="eval")
app.add_typer(sop_app, name="sop")
app.add_typer(prompts_app, name="prompts")

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


def _run(
    settings: Settings,
    *,
    suite: str,
    ids: list[str],
    k: int,
    cost_cap_usd: float,
    concurrency: int,
) -> int:
    import os

    from langsmith import Client

    from freight_agent.adapters.clock import SystemClock
    from freight_agent.adapters.knowledge.memory import MemorySopIndex
    from freight_agent.bootstrap import (
        BootstrapError,
        build_llm,
        build_prompts,
        build_runtime_config,
        enable_native_tracing,
        gate_decisions,
    )
    from freight_agent.evalkit.experiment import (
        CostMeter,
        LangSmithUnavailable,
        ScenarioTarget,
        run_experiment,
    )
    from freight_agent.evalkit.metadata import collect_metadata
    from freight_agent.evalkit.suite import SuiteError, discover, select

    langsmith_key = settings.require_langsmith_key()
    settings.require_openrouter_key()
    build_llm(settings, clock=SystemClock())  # a broken models file stops the run here
    if not settings.sops_dir.is_dir():
        raise UsageProblem(f"the SOP folder {settings.sops_dir} does not exist (SOPS_DIR)")
    try:
        suite_scenarios = discover(settings.evals_dir, suite)
        chosen = select(suite_scenarios, ids=ids)
    except SuiteError as exc:
        raise UsageProblem(str(exc)) from exc
    try:
        prompts = build_prompts(settings)  # each pin pulled once, before anything plays
        decisions = gate_decisions(settings)  # the jev engine's model, else None
    except BootstrapError as exc:
        raise UsageProblem(str(exc)) from exc
    config = build_runtime_config(settings)
    # The examples' traces carry each graph node and model round: LangChain traces them
    # into the experiment through LangSmith's context.
    enable_native_tracing(settings)
    knowledge = MemorySopIndex.from_files(settings.sops_dir)
    metadata = collect_metadata(
        created_at=datetime.now(UTC),
        suite=suite,
        k=k,
        cost_cap_usd=cost_cap_usd,
        sop_version=knowledge.content_version,
        models_config=settings.models_config,
        prompts=prompts,
        repo=Path.cwd(),
        gate_engine=config.gate_engine.value,
        gate_act_threshold=config.gate_act_threshold,
    )
    target = ScenarioTarget(
        {s.id: s for s in suite_scenarios},
        evals_dir=settings.evals_dir,
        knowledge=knowledge,
        prompts=prompts,
        model_factory=lambda: build_llm(settings, clock=SystemClock()),
        meter=CostMeter(cap_usd=cost_cap_usd),
        config=config,
        decisions=decisions,
    )
    client = Client(
        api_key=langsmith_key.get_secret_value(),
        api_url=os.environ.get("LANGSMITH_ENDPOINT") or None,
    )
    typer.echo(
        f"{suite}: {len(chosen)} scenarios, k={k}, live on OpenRouter, cost cap ${cost_cap_usd:.2f}"
    )
    typer.echo("prompts: " + ", ".join(p.describe() for p in prompts.all()))
    typer.echo(f"gate engine: {config.gate_engine.value}")
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
    """Apply the pending migrations of migrations/ to DATABASE_URL (environment or .env).

    Each file is applied once and recorded in schema_migrations, so a rerun applies only
    new files. Exit code 2: no DATABASE_URL, an unreachable database or a bad migration.
    """
    from psycopg import OperationalError

    from freight_agent.adapters.persistence.postgres.migrate import MigrationError, migrate

    try:
        url = Settings().require_database_url().get_secret_value()
        applied = migrate(url)
    except MissingSetting as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(2) from exc
    except MigrationError as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(2) from exc
    except OperationalError as exc:  # the message names host and port, never the password
        typer.echo(f"error: cannot reach the database: {str(exc).strip().splitlines()[0]}")
        raise typer.Exit(2) from exc
    typer.echo(
        f"applied {len(applied)} migration(s)" + (f": {', '.join(applied)}" if applied else "")
    )
