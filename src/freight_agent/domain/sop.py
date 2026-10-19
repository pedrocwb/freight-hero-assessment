"""SOP sections and how layers combine.

Layers, from least to most specific: standard, client, client + load type.
Sections are merged per `section_id`: a more specific layer either replaces
the section or extends it (its body is appended after the inherited body).
This is how "the standard procedure applies to all clients until a client
changes it" is enforced: a client only ever writes the sections it changes.
"""

from collections.abc import Sequence
from enum import StrEnum

from freight_agent.domain.base import DomainModel
from freight_agent.domain.errors import DomainError
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
    """Merge the layers visible to this client and load. Ordered by section id.

    Visible: every standard section, this client's sections, and this client's
    load-type sections for the given load types, applied in the order given. Other
    clients' sections are never read, so an override cannot leak across clients.

    `sources` lists the files that shaped the result, least specific first. A
    `replace` starts the list over (the replaced files no longer say anything), and
    each later `extend` appends its file. `mode` is ignored in the standard layer,
    which has nothing to extend. An `extend` with no base raises `DomainError`: the
    SOP lint reports it earlier, but silently dropping an editor's text would hide
    the mistake at run time.
    """
    merged: dict[str, ResolvedSection] = {}
    for scope in _visible_scopes(sections, client_id=client_id, load_types=load_types):
        for section in scope:
            merged[section.section_id] = _apply(merged.get(section.section_id), section)
    return tuple(merged[section_id] for section_id in sorted(merged))


def resolve(
    sections: Sequence[SopSection],
    *,
    client_id: str,
    load_types: Sequence[str],
    intent: Intent,
    stage: Stage,
) -> ResolvedSop:
    """Sections that apply to this intent and stage, plus the catalog of all visible sections.

    Filtering uses the merged metadata, so a client that narrows a section's intents
    or stages with a `replace` changes when it applies. The catalog lets the model ask
    for a section the filter did not pick.
    """
    visible = effective_sections(sections, client_id=client_id, load_types=load_types)
    return ResolvedSop(
        sections=tuple(s for s in visible if _applies(s, intent=intent, stage=stage)),
        catalog=tuple(
            CatalogEntry(section_id=s.section_id, title=s.title, description=s.description)
            for s in visible
        ),
    )


def _visible_scopes(
    sections: Sequence[SopSection], *, client_id: str, load_types: Sequence[str]
) -> tuple[tuple[SopSection, ...], ...]:
    """The visible sections grouped by scope, least specific first.

    A scope holds at most one section per id; two files with the same id in one scope
    have no defined order, so that is an error rather than a silent last-one-wins.
    """
    own = [s for s in sections if s.client_id == client_id]
    scopes = (
        tuple(s for s in sections if s.layer is SopLayer.STANDARD),
        tuple(s for s in own if s.layer is SopLayer.CLIENT),
        *(
            tuple(s for s in own if s.layer is SopLayer.LOAD_TYPE and s.load_type == type_)
            for type_ in dict.fromkeys(load_types)  # a type listed twice applies once
        ),
    )
    for scope in scopes:
        _check_unique_ids(scope)
    return scopes


def _check_unique_ids(scope: Sequence[SopSection]) -> None:
    seen: dict[str, SopSection] = {}
    for section in scope:
        first = seen.setdefault(section.section_id, section)
        if first is not section:
            raise DomainError(
                f"SOP section {section.section_id!r} is defined twice in the same layer: "
                f"{first.source} and {section.source}"
            )


def _apply(inherited: ResolvedSection | None, section: SopSection) -> ResolvedSection:
    extends = section.mode is MergeMode.EXTEND and section.layer is not SopLayer.STANDARD
    if not extends:
        return ResolvedSection(
            section_id=section.section_id,
            title=section.title,
            description=section.description,
            intents=section.intents,
            stages=section.stages,
            body=section.body,
            sources=(section.source,),
        )
    if inherited is None:
        raise DomainError(
            f"SOP section {section.section_id!r} in {section.source} extends a section "
            f"that no less specific layer defines; use mode: replace or add the base section"
        )
    return inherited.model_copy(
        update={
            "body": _join(inherited.body, section.body),
            "sources": (*inherited.sources, section.source),
        }
    )


def _join(inherited: str, addition: str) -> str:
    """Exactly one blank line between the bodies, however many newlines the files carry.

    Only leading newlines are trimmed from the addition, so an indented first line keeps
    its Markdown meaning.
    """
    return inherited.rstrip() + "\n\n" + addition.lstrip("\r\n")


def _applies(section: ResolvedSection, *, intent: Intent, stage: Stage) -> bool:
    # Empty means "every intent" or "every stage".
    intent_ok = not section.intents or intent in section.intents
    stage_ok = not section.stages or stage in section.stages
    return intent_ok and stage_ok
