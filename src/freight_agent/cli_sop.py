"""`agent sop ...`: the SOP commands, registered by `cli.py` under `sop`.

Owner: card W3-KNW. `agent sop lint` checks the files (moved here from `cli.py`
unchanged). Like `cli.py`, only typer and the settings load at import time: each command
imports what it needs when it runs.
"""

import typer

from freight_agent.settings import Settings

sop_app = typer.Typer(no_args_is_help=True, help="Check the SOP files.")


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
