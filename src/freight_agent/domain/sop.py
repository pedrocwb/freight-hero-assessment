"""SOP sections and how layers combine.

Layers, from least to most specific: standard, client, client + load type.
Sections are merged per `section_id`: a more specific layer either replaces
the section or extends it (its body is appended after the inherited body).
"""

from collections.abc import Sequence
from enum import StrEnum

from freight_agent.domain.base import DomainModel
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Stage


class SopLayer(StrEnum):
    STANDARD = "standard"
    CLIENT = "client"
    LOAD_TYPE = "load_type"


class MergeMode(StrEnum):
    REPLACE = "replace"
    EXTEND = "extend"


class SopSection(DomainModel):
    section_id: str
    layer: SopLayer
    client_id: str | None = None
    load_type: str | None = None
    title: str
    description: str
    intents: tuple[Intent, ...] = ()
    stages: tuple[Stage, ...] = ()  # empty means every stage
    mode: MergeMode = MergeMode.REPLACE
    body: str
    source: str = ""


class ResolvedSection(DomainModel):
    section_id: str
    title: str
    description: str
    intents: tuple[Intent, ...]
    stages: tuple[Stage, ...]
    body: str
    sources: tuple[str, ...]  # files that contributed, least specific first


class CatalogEntry(DomainModel):
    section_id: str
    title: str
    description: str


class ResolvedSop(DomainModel):
    sections: tuple[ResolvedSection, ...]
    catalog: tuple[CatalogEntry, ...]

    @property
    def section_ids(self) -> tuple[str, ...]:
        return tuple(s.section_id for s in self.sections)


def effective_sections(
    sections: Sequence[SopSection], *, client_id: str, load_types: Sequence[str]
) -> tuple[ResolvedSection, ...]:
    """Merge the layers visible to this client and load. Ordered by section id."""
    raise NotImplementedError  # card SOP-1


def resolve(
    sections: Sequence[SopSection],
    *,
    client_id: str,
    load_types: Sequence[str],
    intent: Intent,
    stage: Stage,
) -> ResolvedSop:
    """Sections that apply to this intent and stage, plus the catalog of all visible sections."""
    raise NotImplementedError  # card SOP-1
