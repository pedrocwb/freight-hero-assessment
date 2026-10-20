"""SOP sections and how layers combine.

Layers, from least to most specific: standard, client, client + load type.
Sections are merged per `section_id`: a more specific layer either replaces
the section or extends it (its body is appended after the inherited body).
This is how "the standard procedure applies to all clients until a client
changes it" is enforced: a client only ever writes the sections it changes.

Intents are string ids of the broker's catalog (`domain.broker`). The knowledge base
models (documents uploaded, the draft an editor reviews, the versions published) live
here too; `ports.knowledge` stores and indexes them.
"""

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum

from freight_agent.domain.base import DomainModel
from freight_agent.domain.broker import DocumentRequirement, IntentDef, ObligationDef
from freight_agent.domain.errors import DomainError
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
    intents: tuple[str, ...] = ()  # empty means every intent
    stages: tuple[Stage, ...] = ()  # empty means every stage
    mode: MergeMode = MergeMode.REPLACE
    body: str
    source: str = ""


class ResolvedSection(DomainModel):
    section_id: str
    title: str
    description: str
    intents: tuple[str, ...]
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
    intent: str,
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


def _applies(section: ResolvedSection, *, intent: str, stage: Stage) -> bool:
    # Empty means "every intent" or "every stage".
    intent_ok = not section.intents or intent in section.intents
    stage_ok = not section.stages or stage in section.stages
    return intent_ok and stage_ok


# --- the knowledge base: retrieval, uploads, drafts and published versions ---------------


class SearchHit(DomainModel):
    """One section a search found, best first. `score` only orders hits of one search."""

    section_id: str
    title: str
    description: str
    score: float


class SopDocument(DomainModel):
    """A file an editor uploaded (a broker's SOP PDF), kept as a blob.

    `broker_id` None is the standard layer, shared by every broker.
    """

    document_id: str
    broker_id: str | None
    filename: str
    content_type: str
    blob_ref: str
    pages: int = 0
    sha256: str
    uploaded_at: datetime


class SourceRef(DomainModel):
    """Where a draft section came from: an uploaded document and its pages (1-based)."""

    document_id: str
    pages: tuple[int, ...] = ()


class DraftSection(DomainModel):
    """A section of a draft, as an editor reviews it before it is published.

    The fields of `SopSection` that an editor sets (the client is the draft's broker),
    plus where the text came from and whether the compiler proposed it and no editor has
    accepted it yet.
    """

    section_id: str
    layer: SopLayer = SopLayer.CLIENT
    load_type: str | None = None
    title: str
    description: str
    intents: tuple[str, ...] = ()
    stages: tuple[Stage, ...] = ()
    mode: MergeMode = MergeMode.REPLACE
    body: str
    source: SourceRef | None = None
    proposed: bool = True


class ProposedIntent(IntentDef):
    """An intent the compiler found in a document and the catalog does not have yet.

    `evidence` quotes the text that suggests it; `accepted` is the editor's decision
    (None: not reviewed yet).
    """

    evidence: str = ""
    accepted: bool | None = None


class SopDraft(DomainModel):
    """The one draft of a broker's SOP (None: the standard layer), edited until published.

    Besides sections, the compile step proposes catalog changes the SOP implies: new
    intents, document requirements, and obligations (a new kind such as `pod`, or new
    timing for `eta_request`). Publishing applies the accepted ones to the broker.
    """

    broker_id: str | None
    sections: tuple[DraftSection, ...] = ()
    proposed_intents: tuple[ProposedIntent, ...] = ()
    proposed_requirements: tuple[DocumentRequirement, ...] = ()
    proposed_obligations: tuple[ObligationDef, ...] = ()
    document_ids: tuple[str, ...] = ()  # the uploads the draft was compiled from
    updated_at: datetime


class SopVersion(DomainModel):
    """A published, immutable set of sections. `version` is its content hash."""

    broker_id: str | None
    version: str
    sections: tuple[SopSection, ...]
    published_at: datetime
    published_by: str
    lint: tuple[str, ...] = ()  # the warnings the publish accepted
