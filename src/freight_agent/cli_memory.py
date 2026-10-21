"""`agent memory ...`: the facility memory, for inspection, registered by `cli.py` under
`memory`.

- `list --client C` prints every note of a client, by facility; `list --load L` prints
  the notes of each stop of a load, for its client.
- `extract --load L` runs the load's extraction now (one live `memory` model call, which
  spends from OPENROUTER_API_KEY), whether it was queued or done before, and prints what
  it added, updated, deleted and dropped, with why.

Both work on the stores the composition root builds (`bootstrap.build_runtime_deps`). An
in-memory store lives as long as its process, so this command sees what a durable store
holds (a database) and, in memory, only what the same process wrote. Like `cli.py`, only
typer and the settings load at import time.
"""

import asyncio
from typing import TYPE_CHECKING, Annotated

import typer

from freight_agent.settings import Settings

if TYPE_CHECKING:
    from freight_agent.application.runtime import RuntimeDeps
    from freight_agent.domain.load import Load
    from freight_agent.domain.memory import FacilityFact

memory_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect the facility memory: the notes per client and facility, and their extraction.",
)


def deps_for(settings: Settings) -> "RuntimeDeps":
    """The runtime's interfaces; the seam where tests put their own stores and model."""
    from freight_agent.bootstrap import build_runtime_deps

    return build_runtime_deps(settings)


def _fail(message: str) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(2)


async def _load(deps: "RuntimeDeps", load_id: str) -> "Load | None":
    async with deps.uow_factory() as uow:
        return await uow.loads.get(load_id)


def _line(fact: "FacilityFact") -> str:
    told = f"{fact.source_at:%Y-%m-%d}"
    return (
        f"  - {fact.fact_id} [{fact.kind.value}] {fact.text!r} "
        f"(load {fact.source_load_id}, {fact.source_event_id}, {told})"
    )


@memory_app.command("list")
def list_notes(
    client: Annotated[
        str | None, typer.Option(help="Every note of this client (broker id).")
    ] = None,
    load: Annotated[str | None, typer.Option(help="The notes of this load's stops.")] = None,
) -> None:
    """Print the facility notes of a client, or of a load's stops."""
    if (client is None) == (load is None):
        raise _fail("give one of --client or --load")
    deps = deps_for(Settings())
    memory = deps.memory
    if memory is None:
        raise _fail("this build has no facility memory")

    async def run() -> list[str]:
        lines: list[str] = []
        if client is not None:
            facts = await memory.facts(client)
            current = None
            for fact in facts:
                if fact.facility_key != current:
                    current = fact.facility_key
                    lines.append(f"{fact.facility_name} ({fact.facility_key})")
                lines.append(_line(fact))
            return lines or [f"no notes for client {client!r}"]
        from freight_agent.domain.memory import facility_key

        found = await _load(deps, load or "")
        if found is None:
            raise _fail(f"load {load!r} is not in the store")
        for stop in sorted(found.stops, key=lambda s: s.sequence):
            key = facility_key(stop)
            lines.append(f"stop {stop.sequence}, {stop.type.value}: {stop.name} ({key})")
            notes = await memory.recall(found.broker_id, key, limit=100)
            lines.extend(_line(f) for f in notes)
            if not notes:
                lines.append("  (no notes)")
        return lines

    for line in asyncio.run(run()):
        typer.echo(line)


@memory_app.command("extract")
def extract(
    load: Annotated[str, typer.Option(help="The load whose facility notes to extract.")],
) -> None:
    """Run the load's facility memory extraction now, on the live memory model, and print
    what it changed. Spends from OPENROUTER_API_KEY."""
    from freight_agent.application.memory import MemoryExtractor

    deps = deps_for(Settings())
    extractor = deps.memory_extraction
    if not isinstance(extractor, MemoryExtractor):
        raise _fail("this build has no facility memory extraction")

    async def run() -> list[str]:
        found = await _load(deps, load)
        if found is None:
            raise _fail(f"load {load!r} is not in the store")
        job = await extractor.extract_load(found.broker_id, load)
        lines = [
            f"load {load} ({found.broker_id}): {job.status.value}, {job.added} added, "
            f"{job.updated} updated, {job.deleted} deleted, {len(job.dropped)} dropped, "
            f"${job.cost_usd:.5f}"
        ]
        if job.error:
            lines.append(f"error: {job.error}")
        lines.extend(f"  dropped: {reason}" for reason in job.dropped)
        return lines

    for line in asyncio.run(run()):
        typer.echo(line)
