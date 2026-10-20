"""`agent sop import <dir> [--broker]`: a `sops/` tree published into the SOP store.

Owner: card W3-KNW. Each layer of the tree becomes one published `SopVersion`:
`standard/` the standard layer (None), and `clients/<id>/` with its load-type folders
(`clients/<id>/types/<load_type>/`) broker `<id>`'s layer, which the broker's config then
runs (`domain.sop.adopt_sop`: `sop_version`, a new `config_version`, the rollout to 100%).
A broker the store does not have is created from the standard catalog (`new_broker`).

The import is idempotent: a layer whose sections hash to the version already published
changes nothing (no new version, no broker change), so importing the same tree twice
gives the same versions. It never deletes: a layer missing from the tree keeps its
published version.

The files are checked as `agent sop lint` checks them, except that a broker's files may
name the intents of that broker's catalog (as stored), not only the standard ones. Any
problem stops the import before anything is written, and every problem is reported.
"""

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from freight_agent.adapters.sop.files import (
    STANDARD_INTENTS,
    SopFile,
    SopFormatError,
    locate,
    outside_layout_problem,
    read_sop_file,
    sop_paths,
)
from freight_agent.adapters.sop.lint import LintIssue, Severity, duplicate_issues, extend_issues
from freight_agent.domain.broker import new_broker
from freight_agent.domain.sop import (
    SopSection,
    SopVersion,
    adopt_sop,
    layer_owner,
    search_text,
    version_hash,
)
from freight_agent.ports.clock import Clock
from freight_agent.ports.knowledge import Embedder, SectionEmbeddings
from freight_agent.ports.repositories import UnitOfWorkFactory

PUBLISHED_BY = "agent sop import"


class SopImportError(Exception):
    """The tree cannot be imported as it is. Lists every problem, by file."""

    def __init__(self, issues: Sequence[LintIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("\n".join(str(issue) for issue in self.issues))


@dataclass(frozen=True)
class ImportedLayer:
    broker_id: str | None  # None: the standard layer
    version: str
    sections: int
    changed: bool  # False: this version was already the published one

    def __str__(self) -> str:
        layer = self.broker_id or "standard"
        what = "published" if self.changed else "unchanged"
        return f"{layer}: {what} version {self.version} ({self.sections} sections)"


def layers(sections: Iterable[SopSection]) -> dict[str | None, tuple[SopSection, ...]]:
    """The sections grouped by the layer that publishes them: the standard layer (None)
    first, then each broker by id. A layer keeps the order of the sections given."""
    grouped: dict[str | None, list[SopSection]] = {}
    for section in sections:
        grouped.setdefault(section.client_id, []).append(section)
    order = sorted(grouped, key=lambda owner: (owner is not None, owner or ""))
    return {owner: tuple(grouped[owner]) for owner in order}


class SopImporter:
    """Publishes the layers of a `sops/` tree (see the module docstring).

    With an `embedder`, each published version carries its sections' embeddings; the
    vectors' width must be the store's (1536 in Postgres; `config/models.yaml`).
    """

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        clock: Clock,
        embedder: Embedder | None = None,
        published_by: str = PUBLISHED_BY,
    ) -> None:
        self._uow = uow_factory
        self._clock = clock
        self._embedder = embedder
        self._published_by = published_by

    async def import_tree(self, root: Path, *, broker: str | None = None) -> list[ImportedLayer]:
        """Import every layer, or only `broker`'s (`standard`: the standard layer).

        Raises `SopImportError` when a file has a problem or `broker` has no files.
        """
        sections = await self.read(root)
        grouped = layers(sections)
        if broker is not None:
            owner = layer_owner(broker)
            if owner not in grouped:
                folder = "standard/" if owner is None else f"clients/{owner}/"
                raise SopImportError(
                    [_error(folder, f"there are no SOP files for {broker} in {root}.")]
                )
            grouped = {owner: grouped[owner]}
        return [await self._publish(owner, group) for owner, group in grouped.items()]

    async def read(self, root: Path) -> list[SopSection]:
        """Every section of the tree, checked; raises `SopImportError` on any problem."""
        try:
            paths = sop_paths(root)
        except SopFormatError as exc:
            raise SopImportError([_error(exc.source, exc.problem)]) from None
        located = [(source, path, locate(source)) for source, path in paths]
        catalogs = await self._catalogs(loc.client_id for _, _, loc in located if loc)
        issues: list[LintIssue] = []
        files: list[SopFile] = []
        for source, path, location in located:
            if location is None:
                issues.append(_error(source, outside_layout_problem()))
                continue
            intents = catalogs.get(location.client_id, STANDARD_INTENTS)
            file = read_sop_file(path, location, intents=intents)
            files.append(file)
            issues.extend(_error(source, problem) for problem in file.problems)
        issues.extend(duplicate_issues(files))
        issues.extend(extend_issues(files))
        if issues:
            raise SopImportError(sorted(issues, key=lambda issue: issue.file))
        return [file.section for file in files if file.section is not None]

    async def _catalogs(
        self, client_ids: Iterable[str | None]
    ) -> dict[str | None, Collection[str]]:
        """The intent ids each broker's files may name: its stored catalog, if any."""
        catalogs: dict[str | None, Collection[str]] = {}
        async with self._uow() as uow:
            for client_id in sorted({c for c in client_ids if c is not None}):
                broker = await uow.brokers.get(client_id)
                if broker is not None:
                    catalogs[client_id] = tuple(i.intent_id for i in broker.intents)
        return catalogs

    async def _publish(self, owner: str | None, sections: tuple[SopSection, ...]) -> ImportedLayer:
        version = version_hash(sections)
        async with self._uow() as uow:
            published = await uow.sops.published(owner)
        if published is not None and published.version == version:
            return ImportedLayer(owner, version, len(sections), changed=False)
        embeddings = await self._embed(sections)  # outside the transaction: a network call
        async with self._uow() as uow:
            await uow.sops.save_version(
                SopVersion(
                    broker_id=owner,
                    version=version,
                    sections=sections,
                    published_at=self._clock.now(),
                    published_by=self._published_by,
                ),
                embeddings,
            )
            if owner is not None:
                current = await uow.brokers.get(owner)
                broker = current or new_broker(owner, owner.replace("_", " ").title())
                await uow.brokers.save(adopt_sop(broker, version, saved=current is not None))
            await uow.commit()
        return ImportedLayer(owner, version, len(sections), changed=True)

    async def _embed(self, sections: Sequence[SopSection]) -> SectionEmbeddings | None:
        if self._embedder is None:
            return None
        vectors = await self._embedder.embed([search_text(s) for s in sections])
        return SectionEmbeddings(
            model=self._embedder.model_id, vectors=tuple(tuple(v) for v in vectors)
        )


def _error(file: str, message: str) -> LintIssue:
    return LintIssue(Severity.ERROR, file, message)
