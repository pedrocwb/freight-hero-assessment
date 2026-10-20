"""`agent sop ...`: the SOP commands, registered by `cli.py` under `sop`.

Owner: card W3-KNW. `agent sop lint` checks the files (moved here from `cli.py`
unchanged); `agent sop import <dir> [--broker <id>]` checks that they publish into a SOP
store (`adapters/sop/importer.py`). Like `cli.py`, only typer and the
settings load at import time: each command imports what it needs when it runs.
"""

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from freight_agent.settings import Settings

if TYPE_CHECKING:
    from freight_agent.adapters.sop.importer import ImportedLayer

sop_app = typer.Typer(no_args_is_help=True, help="Check the SOP files and publish them.")


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


@sop_app.command("import")
def sop_import(
    directory: Annotated[
        Path, typer.Argument(help="A sops/ tree: standard/ and clients/<id>/ folders.")
    ],
    broker: Annotated[
        str | None,
        typer.Option(help="Only this broker's layer (standard: the standard layer)."),
    ] = None,
) -> None:
    """Publish the tree's layers into the SOP store: standard/ as the standard layer,
    clients/<id>/ as broker <id>'s (created from the standard catalog when missing).

    Idempotent: a layer whose text did not change keeps its version. The store is in
    memory, so the import is only checked: it goes away with the command. Exit code 1
    when a file has a problem (nothing is written then).
    """
    from freight_agent.adapters.sop.importer import SopImportError
    from freight_agent.settings import MissingSetting

    settings = Settings()
    try:
        imported = asyncio.run(_import(settings, directory, broker))
    except MissingSetting as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(2) from None
    except SopImportError as exc:
        for issue in exc.issues:
            typer.echo(str(issue))
        typer.echo(f"{len(exc.issues)} errors in {directory}: nothing was imported")
        raise typer.Exit(1) from None
    for layer in imported:
        typer.echo(str(layer))
    changed = sum(layer.changed for layer in imported)
    typer.echo(f"{changed} of {len(imported)} layers published to memory (checked only)")


async def _import(settings: Settings, directory: Path, broker: str | None) -> "list[ImportedLayer]":
    from freight_agent import bootstrap
    from freight_agent.adapters.clock import SystemClock
    from freight_agent.adapters.knowledge.embeddings import embedder_for
    from freight_agent.adapters.persistence.memory.store import MemoryStore
    from freight_agent.adapters.sop.importer import SopImporter

    # The composition root's embedder (`bootstrap.build_embedder`, card W3-OPS); until it
    # is on this branch, the same rule from `embedder_for`.
    build = getattr(bootstrap, "build_embedder", None)
    embedder = build(settings) if build is not None else embedder_for(settings)
    importer = SopImporter(MemoryStore().unit_of_work, clock=SystemClock(), embedder=embedder)
    return await importer.import_tree(directory, broker=broker)
