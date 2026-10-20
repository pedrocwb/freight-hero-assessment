"""`SopIndex` over sections held in memory or read from a `sops/` tree: tests, evals and
the local platform.

Owner: card W3-KNW (C0 wrote a working version).

`by_applies` is `domain.sop.resolve` and `visible` is `domain.sop.effective_sections`,
so the run reads exactly the sections the file repository gave it before wave 3.
`search` ranks the visible sections by the query words they contain (title and
description count twice); it needs no embedding, so it works offline.

`from_files` keeps the pre-wave-3 behaviour of `FileSopRepository`: the tree is read on
every call, so an edited file applies to the next run; a broken file fails the runs of
the brokers that see it (`SopFormatError`), never the others, and never the start of the
app; the version every run records is the tree's content hash.
"""

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from pathlib import Path

from freight_agent.adapters.sop.files import FileSopRepository
from freight_agent.domain.load import Stage
from freight_agent.domain.sop import (
    ResolvedSection,
    ResolvedSop,
    SearchHit,
    SopSection,
    effective_sections,
    resolve,
)

_WORD = re.compile(r"[a-z0-9]+")
_VERSION_LENGTH = 12


def words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def content_hash(sections: Iterable[SopSection]) -> str:
    """Short sha256 of the sections' content, in order."""
    dumped = [s.model_dump(mode="json") for s in sections]
    text = json.dumps(dumped, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_VERSION_LENGTH]


class MemorySopIndex:
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
        """The sections a broker may see, every layer; `domain.sop` keeps the others out."""
        return self._sections

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
        wanted = words(query)
        if not wanted or k <= 0:
            return ()
        scored: list[tuple[float, ResolvedSection]] = []
        for section in await self.visible(broker_id, load_types):
            heading = words(f"{section.title} {section.description}")
            body = words(section.body)
            score = float(2 * len(wanted & heading) + len(wanted & body))
            if score > 0:
                scored.append((score, section))
        scored.sort(key=lambda hit: (-hit[0], hit[1].section_id))
        return tuple(
            SearchHit(
                section_id=s.section_id, title=s.title, description=s.description, score=score
            )
            for score, s in scored[:k]
        )

    async def read(
        self, broker_id: str, load_types: Sequence[str], section_id: str
    ) -> ResolvedSection | None:
        visible = await self.visible(broker_id, load_types)
        return next((s for s in visible if s.section_id == section_id), None)

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
