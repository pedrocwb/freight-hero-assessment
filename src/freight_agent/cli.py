"""Command line entry point: `agent eval run|list|report|lint|draft`, `agent sop ...` (in
`cli_sop.py`), `agent serve` and `agent db migrate`.

`--broker-file` (a broker export, `evals/brokers/README.md`) plays a suite on one broker's
catalog and published sections; `agent eval lint` lists the broker's intents no scenario
covers, and `agent eval draft` drafts scenarios for them on the `compile` role.

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

from freight_agent.cli_sop import sop_app
from freight_agent.settings import LlmMode, Settings

if TYPE_CHECKING:
    from collections.abc import Sequence

    from freight_agent.domain.broker import Broker
    from freight_agent.evalkit.brokers import BrokerExport, LintIssue
    from freight_agent.evalkit.graders import Budget
    from freight_agent.evalkit.langsmith_experiment import LangSmithExperiment
    from freight_agent.evalkit.metadata import RunMetadata
    from freight_agent.evalkit.spec import Scenario
    from freight_agent.evalkit.suite import ModelFactory

app = typer.Typer(no_args_is_help=True, help="Freight load agent.", add_completion=False)
eval_app = typer.Typer(no_args_is_help=True, help="Run, list and report the scenario evals.")
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
    langsmith: Annotated[
        bool,
        typer.Option(
            "--langsmith",
            help="Record the run as a LangSmith experiment (live and record modes only): the "
            "suite's scenarios as a dataset, one row per trial with its steps and model calls, "
            "and the graders' verdicts as feedback. Needs LANGSMITH_API_KEY.",
        ),
    ] = False,
    broker_file: Annotated[
        Path | None,
        typer.Option(
            help="Play every scenario on this broker export (evals/brokers/*.json): its "
            "catalog in the trial's store, its published sections in the SOP index."
        ),
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
            langsmith=langsmith,
            broker=_read_broker(broker_file),
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
    langsmith: bool = False,
    broker: "BrokerExport | None" = None,
) -> int:
    from freight_agent.adapters.knowledge.memory import MemorySopIndex
    from freight_agent.evalkit.brokers import broker_index
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

    if langsmith and mode not in ("live", "record"):
        raise UsageProblem(
            "--langsmith records live runs only: scripted and replay runs measure the code, "
            "not the model (use --mode live)"
        )
    if langsmith and settings.langsmith_api_key is None:
        raise UsageProblem("--langsmith needs LANGSMITH_API_KEY (environment or .env)")
    models_config = models_config_for(settings, model_set)
    # The flags win over the environment, and the metadata shows what actually ran.
    settings = settings.model_copy(update={"models_config": models_config, "llm_mode": mode})
    if not settings.sops_dir.is_dir():
        raise UsageProblem(f"the SOP folder {settings.sops_dir} does not exist (SOPS_DIR)")
    try:
        suite_scenarios = discover(settings.evals_dir, suite)
        scenarios = select(suite_scenarios, tags=tags, ids=ids)
        before = read_baseline(baseline) if baseline is not None else None
    except (SuiteError, ReportError) as exc:
        raise UsageProblem(str(exc)) from exc
    factory = _checked_model_factory(settings, mode)
    knowledge = MemorySopIndex.from_files(settings.sops_dir)
    sop_version = knowledge.content_version
    if broker is not None:
        played = broker_index(knowledge, broker)
        sop_version = getattr(played, "content_version", sop_version)
    metadata = collect_metadata(
        created_at=datetime.now(UTC),
        mode=mode,
        model_set=model_set,
        sop_version=sop_version,
        models_config=models_config,
        repo=Path.cwd(),
        settings=settings.describe(),
        broker=None if broker is None else broker.label(),
    )
    experiment = (
        _langsmith_experiment(settings, suite, suite_scenarios, metadata, k, cost_cap_usd)
        if langsmith
        else None
    )
    runner = SuiteRunner(
        evals_dir=settings.evals_dir,
        knowledge=knowledge,
        mode=mode,
        model_factory=factory,
        budget=budget,
        observer=experiment,
        broker=broker,
    )
    on_broker = "" if broker is None else f", broker {broker.label()}"
    typer.echo(f"{suite}: {len(scenarios)} scenarios, mode {mode}, k={k}{on_broker}")
    if experiment is not None:
        linked = f", agent {settings.langsmith_project}" if experiment.linked_to_agent else ""
        typer.echo(f"langsmith experiment: {experiment.name}{linked}")
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
    if experiment is not None:
        url = experiment.finish(result)
        typer.echo(f"langsmith: {experiment.name}" + (f"\n  {url}" if url else ""))
    return int(result.exit_code)


def _langsmith_experiment(
    settings: Settings,
    suite: str,
    scenarios: "Sequence[Scenario]",
    metadata: "RunMetadata",
    k: int,
    cost_cap_usd: float,
) -> "LangSmithExperiment":
    """The experiment, created before the first trial: a live run never spends money for
    an experiment LangSmith cannot store."""
    import os

    from langsmith import Client

    from freight_agent.evalkit.langsmith_experiment import (
        LangSmithExperiment,
        LangSmithUnavailable,
    )

    key = settings.langsmith_api_key
    assert key is not None  # checked with the other flags
    client = Client(
        api_key=key.get_secret_value(), api_url=os.environ.get("LANGSMITH_ENDPOINT") or None
    )
    experiment = LangSmithExperiment(
        client,  # pyright: ignore[reportArgumentType]
        suite=suite,
        dataset_scenarios=scenarios,
        metadata=metadata,
        k=k,
        cost_cap_usd=cost_cap_usd,
        agent=settings.langsmith_project,  # the agent page of the tracing project
    )
    try:
        experiment.start()
    except LangSmithUnavailable as exc:
        raise UsageProblem(f"LangSmith: {exc}; nothing was run") from exc
    return experiment


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


def _read_broker(path: Path | None) -> "BrokerExport | None":
    if path is None:
        return None
    from freight_agent.evalkit.brokers import BrokerFileError, read_broker_export

    try:
        return read_broker_export(path)
    except BrokerFileError as exc:
        raise UsageProblem(str(exc)) from exc


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


# --- agent eval lint / draft (a broker's coverage) ------------------------------------------


@eval_app.command("lint")
def eval_lint(
    broker_file: Annotated[
        Path | None,
        typer.Option(
            help="The broker export to check (evals/brokers/*.json).",
            show_default="the standard catalog",
        ),
    ] = None,
) -> None:
    """Check that every intent of a broker's catalog has a scenario.

    Exit code 1 when an intent has none (`agent eval draft` drafts one), 2 when the
    scenarios or the broker file cannot be read.
    """
    try:
        broker = _read_broker(broker_file)
        issues, catalog, scenarios = _lint(Settings().evals_dir, broker)
    except UsageProblem as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(HARNESS_ERROR) from None
    for issue in issues:
        typer.echo(str(issue))
    typer.echo(
        f"{len(issues)} errors: {catalog.broker_id}, {len(catalog.intents)} intents, "
        f"{scenarios} scenarios"
    )
    raise typer.Exit(1 if issues else 0)


def _lint(evals_dir: Path, broker: "BrokerExport | None") -> "tuple[list[LintIssue], Broker, int]":
    from freight_agent.domain.broker import standard_broker
    from freight_agent.evalkit.brokers import exports_by_file, lint_broker
    from freight_agent.evalkit.suite import SuiteError, discover, suites

    catalog = standard_broker() if broker is None else broker.broker
    try:
        every = [s for name in suites(evals_dir) for s in discover(evals_dir, name)]
    except SuiteError as exc:
        raise UsageProblem(str(exc)) from exc
    issues = lint_broker(catalog, every, exports_by_file(evals_dir, every))
    return issues, catalog, len(every)


@eval_app.command("draft")
def eval_draft(
    broker_file: Annotated[
        Path, typer.Option(help="The broker export to draft for (evals/brokers/*.json).")
    ],
    mode: Annotated[
        LlmMode,
        typer.Option(
            help="live (or record) calls the compile model, about $0.002 per intent; replay "
            "reads recorded answers. Scripted has no model to draft with."
        ),
    ],
    intent: Annotated[
        list[str] | None,
        typer.Option(
            help="Intents to draft. Repeat or comma-separate.",
            show_default="the intents `agent eval lint` reports",
        ),
    ] = None,
    model_set: Annotated[
        str, typer.Option(help="'default' is config/models.yaml; NAME is models.NAME.yaml.")
    ] = DEFAULT_MODEL_SET,
    cost_cap_usd: Annotated[
        float, typer.Option(min=0, help="Stop drafting once this much was spent.")
    ] = 0.05,
    out: Annotated[
        Path | None,
        typer.Option(help="Scenario folder root.", show_default="EVALS_DIR/scenarios"),
    ] = None,
) -> None:
    """Draft scenarios for a broker's intents from its sections, on the compile role.

    Each draft goes to <out>/<broker>/_draft_<intent>.yaml, tagged draft (and capability,
    so it never gates a run). An existing draft is never overwritten. Exit code 1 when a
    draft failed, 2 when the command cannot run.
    """
    try:
        code = _draft(
            settings=Settings(),
            broker_file=broker_file,
            mode=mode,
            intents=_split(intent),
            model_set=model_set,
            cost_cap_usd=cost_cap_usd,
            out=out,
        )
    except UsageProblem as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(HARNESS_ERROR) from None
    raise typer.Exit(code)


def _draft(
    *,
    settings: Settings,
    broker_file: Path,
    mode: LlmMode,
    intents: list[str],
    model_set: str,
    cost_cap_usd: float,
    out: Path | None,
) -> int:
    from freight_agent.adapters.clock import SystemClock
    from freight_agent.adapters.knowledge.memory import MemorySopIndex
    from freight_agent.evalkit.draft import draft_scenarios

    if mode == "scripted":
        raise UsageProblem("drafting calls the compile model: use --mode live, record or replay")
    broker = _read_broker(broker_file)
    assert broker is not None
    if not intents:
        issues, _, _ = _lint(settings.evals_dir, broker)
        intents = [issue.intent for issue in issues]
    if not intents:
        typer.echo(f"nothing to draft: every intent of {broker.broker_id} has a scenario")
        return 0
    settings = settings.model_copy(
        update={"models_config": models_config_for(settings, model_set), "llm_mode": mode}
    )
    factory = _checked_model_factory(settings, mode)
    assert factory is not None
    results, usage = asyncio.run(
        draft_scenarios(
            factory(SystemClock()),
            broker,
            intents=intents,
            knowledge=MemorySopIndex.from_files(settings.sops_dir),
            evals_dir=settings.evals_dir,
            broker_file=_relative_to(broker_file, settings.evals_dir),
            out_dir=out,
            cost_cap_usd=cost_cap_usd,
        )
    )
    for result in results:
        where = str(result.path) if result.path is not None else result.message
        detail = f" ({result.message})" if result.path is not None and result.message else ""
        typer.echo(f"{result.status:<8} {result.intent:<30} {where}{detail}")
    typer.echo(f"{len(usage)} compile call(s), ${sum(u.cost_usd for u in usage):.4f}")
    return 1 if any(r.status == "failed" for r in results) else 0


def _relative_to(path: Path, folder: Path) -> str:
    """How a scenario under `folder` names `path`: relative when it is inside."""
    try:
        return path.resolve().relative_to(folder.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


# --- agent serve and agent db -------------------------------------------------------------


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", help="Interface to listen on."),
    port: int = typer.Option(8000, help="Port of the platform API (the web UI proxies to it)."),
) -> None:
    """Serve the simulation platform's API (the web UI in web/ talks to it)."""
    from freight_agent.api.__main__ import main as serve_api

    serve_api(["--host", host, "--port", str(port)])


db_app = typer.Typer(no_args_is_help=True, help="Postgres schema migrations.")
app.add_typer(db_app, name="db")


@db_app.command("migrate")
def db_migrate() -> None:
    """Apply pending migrations to DATABASE_URL_DIRECT (or DATABASE_URL), from env or .env."""
    from freight_agent.adapters.persistence.postgres.migrate import MigrationError, migrate

    settings = Settings()
    url = settings.database_url_direct or settings.database_url
    from psycopg import OperationalError

    try:
        applied = migrate(url.get_secret_value() if url is not None else None)
    except MigrationError as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(2) from exc
    except OperationalError as exc:  # the message names host and port, never the password
        typer.echo(f"error: cannot reach the database: {str(exc).strip().splitlines()[0]}")
        raise typer.Exit(2) from exc
    typer.echo(
        f"applied {len(applied)} migration(s)" + (f": {', '.join(applied)}" if applied else "")
    )
