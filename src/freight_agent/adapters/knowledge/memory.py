"""`SopIndex` over sections held in memory, read from a `sops/` tree, or published in a
`SopStore`: tests, evals and the local platform.

Owner: card W3-KNW.

`by_applies` is `domain.sop.resolve` and `visible` is `domain.sop.effective_sections`,
so the run reads exactly the sections the file repository gave it before wave 3.
`search` ranks the visible sections by the query words they contain (title and
description count twice); it needs no embedding, so it works offline.

- `MemorySopIndex(sections)`: a fixed set of sections of every layer.
- `MemorySopIndex.from_files(root)` keeps the pre-wave-3 behaviour of
  `FileSopRepository`: the tree is read on every call, so an edited file applies to the
  next run; a broken file fails the runs of the brokers that see it (`SopFormatError`),
  never the others, and never the start of the app; the version every run records is
  the tree's content hash.
- `StoreSopIndex(uow_factory, files=root)`: the versions published in a `SopStore`
  (`agent sop import`, a broker's publish), read on every call through a short unit of
  work of its own, so a publish applies to the next run. A layer never published comes
  from the `sops/` tree when one is given.

The version of what a broker reads is its layers' versions joined by `+`: the standard
layer's, then the broker's own when it has one (`compose_version`).
"""

import re
from collections.abc import Iterable, Sequence
from pathlib import Path

from freight_agent.adapters.sop.files import FileSopRepository
from freight_agent.domain.load import Stage
from freight_agent.domain.sop import (
    ResolvedSection,
    ResolvedSop,
    SearchHit,
    SopLayer,
    SopSection,
    effective_sections,
    layer_owner,
    resolve,
    version_hash,
)
from freight_agent.ports.repositories import UnitOfWorkFactory

_WORD = re.compile(r"[a-z0-9]+")


def words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def content_hash(sections: Iterable[SopSection]) -> str:
    """Short sha256 of the sections' content, in order (`domain.sop.version_hash`)."""
    return version_hash(sections)


def compose_version(standard: str | None, own: str | None) -> str | None:
    """The version of what a broker reads: the standard layer's version, then `+` and the
    broker's own layer's when it has one. None when neither layer has a version."""
    parts = [part for part in (standard, own) if part]
    return "+".join(dict.fromkeys(parts)) or None


def word_search(sections: Sequence[ResolvedSection], query: str, k: int) -> tuple[SearchHit, ...]:
    """The `k` sections sharing the most words with `query`, best first (ties by id)."""
    wanted = words(query)
    if not wanted or k <= 0:
        return ()
    scored: list[tuple[float, ResolvedSection]] = []
    for section in sections:
        heading = words(f"{section.title} {section.description}")
        body = words(section.body)
        score = float(2 * len(wanted & heading) + len(wanted & body))
        if score > 0:
            scored.append((score, section))
    scored.sort(key=lambda hit: (-hit[0], hit[1].section_id))
    return tuple(
        SearchHit(section_id=s.section_id, title=s.title, description=s.description, score=score)
        for score, s in scored[:k]
    )


class SectionIndex:
    """`ports.knowledge.SopIndex` over the sections `sections_for` gives, merged by the
    domain. Subclasses say where the sections come from and what their version is."""

    async def sections_for(self, broker_id: str) -> Sequence[SopSection]:
        """The sections a broker may see, every layer; `domain.sop` keeps the others out."""
        raise NotImplementedError

    async def version(self, broker_id: str) -> str | None:
        raise NotImplementedError

    async def by_applies(
        self, broker_id: str, load_types: Sequence[str], intent: str, stage: Stage
    ) -> ResolvedSop:
        return resolve(
            await self.sections_for(broker_id),
            client_id=broker_id,
            load_types=load_types,
            intent=intent,
            stage=stage,
        )

    async def visible(
        self, broker_id: str, load_types: Sequence[str]
    ) -> tuple[ResolvedSection, ...]:
        return effective_sections(
            await self.sections_for(broker_id), client_id=broker_id, load_types=load_types
        )

    async def search(
        self, broker_id: str, load_types: Sequence[str], query: str, k: int = 5
    ) -> tuple[SearchHit, ...]:
        return word_search(await self.visible(broker_id, load_types), query, k)

    async def read(
        self, broker_id: str, load_types: Sequence[str], section_id: str
    ) -> ResolvedSection | None:
        visible = await self.visible(broker_id, load_types)
        return next((s for s in visible if s.section_id == section_id), None)


class MemorySopIndex(SectionIndex):
    """Implements `ports.knowledge.SopIndex` over a fixed set of sections of every layer.

    `version` defaults to a hash of the sections.
    """

    def __init__(self, sections: Iterable[SopSection], *, version: str | None = None) -> None:
        self._sections = tuple(sections)
        self._version = version if version is not None else content_hash(self._sections)

    @classmethod
    def from_files(cls, root: Path) -> "MemorySopIndex":
        """The sections of a `sops/` tree, read on every call (see the module docstring)."""
        return _FileTreeIndex(root)

    @property
    def content_version(self) -> str:
        """The version every broker's runs record (one set of sections, one version)."""
        return self._version

    async def sections_for(self, broker_id: str) -> Sequence[SopSection]:
        return self._sections

    async def version(self, broker_id: str) -> str | None:
        return self.content_version


class _FileTreeIndex(MemorySopIndex):
    """`MemorySopIndex` over a `sops/` tree that it reads on every call."""

    def __init__(self, root: Path) -> None:
        super().__init__((), version="")
        self._files = FileSopRepository(root)

    @property
    def content_version(self) -> str:
        """The tree's hash now. Raises `SopFormatError` when the folder does not exist."""
        return self._files.version()

    async def sections_for(self, broker_id: str) -> Sequence[SopSection]:
        return await self._files.sections_for_client(broker_id)


class StoreSopIndex(SectionIndex):
    """Implements `ports.knowledge.SopIndex` over the versions published in a `SopStore`.

    Each call opens a unit of work of its own, so never call it inside a unit of work of
    the same memory store (its lock is not reentrant): the run thinks outside any (F8).
    With `files`, a layer that was never published reads that `sops/` tree instead, and
    the tree's hash stands for it in the version.
    """

    def __init__(self, uow_factory: UnitOfWorkFactory, *, files: Path | None = None) -> None:
        self._uow = uow_factory
        self._files = None if files is None else FileSopRepository(files)

    async def sections_for(self, broker_id: str) -> Sequence[SopSection]:
        standard, own = await self._published(broker_id)
        from_files: list[SopSection] = []
        if self._files is not None and (standard is None or own is None):
            from_files = await self._files.sections_for_client(broker_id)
        if standard is None:
            standard = tuple(s for s in from_files if s.layer is SopLayer.STANDARD)
        if own is None:
            own = tuple(s for s in from_files if s.layer is not SopLayer.STANDARD)
        return (*standard, *own)

    async def version(self, broker_id: str) -> str | None:
        async with self._uow() as uow:
            standard = await uow.sops.published(None)
            owner = layer_owner(broker_id)
            own = None if owner is None else await uow.sops.published(owner)
        tree = None if self._files is None else self._files.version()
        return compose_version(standard.version if standard else tree, own.version if own else tree)

    async def _published(
        self, broker_id: str
    ) -> tuple[tuple[SopSection, ...] | None, tuple[SopSection, ...] | None]:
        """The sections of the published standard layer and of the broker's own layer,
        each None while that layer was never published."""
        async with self._uow() as uow:
            standard = await uow.sops.published(None)
            owner = layer_owner(broker_id)
            own = None if owner is None else await uow.sops.published(owner)
        return (
            None if standard is None else standard.sections,
            None if own is None else own.sections,
        )
