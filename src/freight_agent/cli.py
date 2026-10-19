"""Command line entry point: `agent eval run|list|report` and `agent sop lint`.

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

from freight_agent.settings import LlmMode, Settings

if TYPE_CHECKING:
    from freight_agent.evalkit.graders import Budget
    from freight_agent.evalkit.suite import ModelFactory

app = typer.Typer(no_args_is_help=True, help="Freight load agent.", add_completion=False)
eval_app = typer.Typer(no_args_is_help=True, help="Run, list and report the scenario evals.")
sop_app = typer.Typer(no_args_is_help=True, help="Check the SOP files.")
app.add_typer(eval_app, name="eval")
app.add_typer(sop_app, name="sop")

DEFAULT_MODEL_SET = "default"
HARNESS_ERROR = 2  # `evalkit.suite.ExitCode.HARNESS_ERROR`, without importing the harness


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
    mode: Annotated[
        LlmMode,
        typer.Option(
            help="scripted: each scenario's own script, no model. replay: recorded answers "
            "(evals/cassettes). live: the models of the model set. record: live, and save "
            "the answers for replay."
        ),
    ] = "scripted",
    k: Annotated[
        int,
        typer.Option("--k", min=1, help="Trials per scenario. A scenario passes when all pass."),
    ] = 1,
    model_set: Annotated[
        str,
        typer.Option(
            help="'default' is config/models.yaml; NAME is config/models.NAME.yaml.",
        ),
    ] = DEFAULT_MODEL_SET,
    cost_cap_usd: Annotated[
        float, typer.Option(min=0, help="Stop the run once it has spent more than this.")
    ] = 5.0,
    tags: Annotated[
        list[str] | None,
        typer.Option(help="Only scenarios with any of these tags. Repeat or comma-separate."),
    ] = None,
    scenario: Annotated[
        list[str] | None,
        typer.Option(help="Only these scenario ids. Repeat or comma-separate."),
    ] = None,
    compare: Annotated[
        Path | None,
        typer.Option(help="Baseline to compare with: a results folder or its results.json."),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(help="Folder for the run folders.", show_default="EVALS_DIR/results"),
    ] = None,
    max_tokens_per_run: Annotated[
        int | None, typer.Option(min=1, help="Fail a trial when one run uses more tokens.")
    ] = None,
    max_latency_ms_per_run: Annotated[
        int | None,
        typer.Option(min=1, help="Fail a trial when one run's model calls take longer."),
    ] = None,
) -> None:
    """Run a suite, grade it, write results.json and report.md, and exit with the verdict.

    Exit code 0: every regression scenario passed. 1: a regression scenario failed.

    Exit code 2: the harness could not run (a bad scenario, a missing cassette or key).

    Exit code 3: the cost cap stopped the run.
    """
    try:
        code = _run(
            settings=Settings(),
            suite=suite,
            mode=mode,
            k=k,
            model_set=model_set,
            cost_cap_usd=cost_cap_usd,
            tags=_split(tags),
            ids=_split(scenario),
            baseline=compare,
            out=out,
            budget=_budget(max_tokens_per_run, max_latency_ms_per_run),
        )
    except UsageProblem as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(HARNESS_ERROR) from None
    raise typer.Exit(code)


def _run(
    *,
    settings: Settings,
    suite: str,
    mode: LlmMode,
    k: int,
    model_set: str,
    cost_cap_usd: float,
    tags: list[str],
    ids: list[str],
    baseline: Path | None,
    out: Path | None,
    budget: "Budget | None",
) -> int:
    from freight_agent.adapters.sop.files import FileSopRepository
    from freight_agent.evalkit.metadata import collect_metadata
    from freight_agent.evalkit.report import (
        REPORT_FILE,
        ReportError,
        compare,
        read_baseline,
        render_scenario_line,
        render_summary,
        write_run,
    )
    from freight_agent.evalkit.suite import SuiteError, SuiteRunner, discover, select

    models_config = models_config_for(settings, model_set)
    # The flags win over the environment, and the metadata shows what actually ran.
    settings = settings.model_copy(update={"models_config": models_config, "llm_mode": mode})
    if not settings.sops_dir.is_dir():
        raise UsageProblem(f"the SOP folder {settings.sops_dir} does not exist (SOPS_DIR)")
    try:
        scenarios = select(discover(settings.evals_dir, suite), tags=tags, ids=ids)
        before = read_baseline(baseline) if baseline is not None else None
    except (SuiteError, ReportError) as exc:
        raise UsageProblem(str(exc)) from exc
    factory = _checked_model_factory(settings, mode)
    sop = FileSopRepository(settings.sops_dir)
    metadata = collect_metadata(
        created_at=datetime.now(UTC),
        mode=mode,
        model_set=model_set,
        sop=sop,
        models_config=models_config,
        repo=Path.cwd(),
        settings=settings.describe(),
    )
    runner = SuiteRunner(
        evals_dir=settings.evals_dir, sop=sop, mode=mode, model_factory=factory, budget=budget
    )
    typer.echo(f"{suite}: {len(scenarios)} scenarios, mode {mode}, k={k}")
    result = asyncio.run(
        runner.run(
            scenarios,
            suite=suite,
            metadata=metadata,
            k=k,
            cost_cap_usd=cost_cap_usd,
            on_scenario=lambda summary: typer.echo(render_scenario_line(summary)),
        )
    )
    comparison = compare(result, before) if before is not None else None
    folder = write_run(result, out or settings.evals_dir / "results", comparison)
    typer.echo("")
    typer.echo(render_summary(result, comparison, report=folder / REPORT_FILE))
    return int(result.exit_code)


def models_config_for(settings: Settings, model_set: str) -> Path:
    """The models file of a model set: `default` is MODELS_CONFIG (config/models.yaml), any
    other NAME is the file next to it named models.NAME.yaml."""
    base = settings.models_config
    if model_set == DEFAULT_MODEL_SET:
        path = base
    else:
        path = base.with_name(f"{base.stem}.{model_set}{base.suffix}")
    if not path.is_file():
        raise UsageProblem(
            f"unknown model set {model_set!r}: {path} does not exist "
            f"(use --model-set {DEFAULT_MODEL_SET} for {base})"
        )
    return path


def eval_model_factory(settings: Settings, mode: LlmMode) -> "ModelFactory | None":
    """The model of each trial for replay, live and record, from the composition root.

    None in scripted mode: the runner feeds each scenario's own script. The factory gets
    each trial's virtual clock, which the circuit breakers of a live model read.
    """
    if mode == "scripted":
        return None
    from freight_agent.bootstrap import build_llm
    from freight_agent.ports.clock import Clock
    from freight_agent.ports.llm import LanguageModel

    def build(clock: Clock) -> LanguageModel:
        return build_llm(settings, mode=mode, clock=clock)

    return build


def _checked_model_factory(settings: Settings, mode: LlmMode) -> "ModelFactory | None":
    """Build one model before any trial runs, so a missing key or a broken models file
    stops the run at once with its own message instead of failing every trial."""
    factory = eval_model_factory(settings, mode)
    if factory is None:
        return None
    from freight_agent.adapters.clock import SystemClock
    from freight_agent.bootstrap import BootstrapError

    try:
        factory(SystemClock())
    except (BootstrapError, ValueError) as exc:  # MissingSetting is a ValueError
        raise UsageProblem(f"cannot build the {mode} model: {exc}") from exc
    return factory


def _budget(max_tokens: int | None, max_latency_ms: int | None) -> "Budget | None":
    if max_tokens is None and max_latency_ms is None:
        return None
    from freight_agent.evalkit.graders import Budget

    return Budget(max_tokens_per_run=max_tokens, max_latency_ms_per_run=max_latency_ms)


def _split(values: Iterable[str] | None) -> list[str]:
    """`--tags a,b --tags c` -> [a, b, c]."""
    return [part.strip() for value in values or () for part in value.split(",") if part.strip()]


# --- agent eval list / report ---------------------------------------------------------


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


@eval_app.command("report")
def eval_report(
    path: Annotated[Path, typer.Argument(help="A results folder or its results.json.")],
    compare: Annotated[
        Path | None,
        typer.Option(help="Baseline to compare with: a results folder or its results.json."),
    ] = None,
) -> None:
    """Render report.md again from a results.json, and print the summary."""
    from freight_agent.evalkit.report import (
        ReportError,
        load_results,
        render_scenario_line,
        render_summary,
        results_path,
        write_report,
    )
    from freight_agent.evalkit.report import compare as compare_with

    try:
        result = load_results(path)
        comparison = compare_with(result, compare) if compare is not None else None
    except ReportError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(HARNESS_ERROR) from None
    report = write_report(result, results_path(path).parent, comparison)
    for summary in result.scenarios:
        typer.echo(render_scenario_line(summary))
    typer.echo("")
    typer.echo(render_summary(result, comparison, report=report))


# --- agent sop lint ---------------------------------------------------------------------


@sop_app.command("lint")
def sop_lint() -> None:
    """Check every SOP file, and warn about sections no eval scenario covers.

    Exit code 1 when there is an error; warnings alone leave it at 0.
    """
    from freight_agent.adapters.sop.lint import Severity, lint
    from freight_agent.evalkit.suite import SuiteError, discover, suites

    settings = Settings()
    tags: list[str] | None
    try:
        evals = settings.evals_dir
        tags = [tag for name in suites(evals) for s in discover(evals, name) for tag in s.tags]
    except SuiteError as exc:
        typer.echo(f"warning: coverage not checked, the scenarios cannot be read: {exc}")
        tags = None
    issues = lint(settings.sops_dir, tags)
    for issue in issues:
        typer.echo(str(issue))
    errors = sum(issue.severity is Severity.ERROR for issue in issues)
    typer.echo(f"{errors} errors, {len(issues) - errors} warnings in {settings.sops_dir}")
    raise typer.Exit(1 if errors else 0)
