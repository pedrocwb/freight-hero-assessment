"""SOP sections and how layers combine.

Layers, from least to most specific: standard, client, client + load type.
Sections are merged per `section_id`: a more specific layer either replaces
the section or extends it (its body is appended after the inherited body).
This is how "the standard procedure applies to all clients until a client
changes it" is enforced: a client only ever writes the sections it changes.

Intents are string ids of the broker's catalog (`domain.broker`). The knowledge base
models (documents uploaded, the draft an editor reviews, the versions published) live
here too; `ports.knowledge` stores and indexes them. A layer belongs to a broker, or to
nobody (`broker_id` None): the standard layer every broker inherits. The standard
catalog's id (`STANDARD_BROKER_ID`) names that layer too, so it is read as None.
"""

import hashlib
import json
from collections.abc import Iterable, Sequence
from datetime import datetime
from enum import StrEnum

from pydantic import field_validator

from freight_agent.domain.base import DomainModel
from freight_agent.domain.broker import (
    STANDARD_BROKER_ID,
    Broker,
    DocumentRequirement,
    IntentDef,
    ObligationDef,
    Rollout,
    with_follow_up_intents,
)
from freight_agent.domain.errors import DomainError
from freight_agent.domain.load import Stage

VERSION_LENGTH = 12


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
    merged = _merge(sections, client_id=client_id, load_types=load_types)
    return tuple(merged[section_id][0] for section_id in sorted(merged))


def contributors(
    sections: Sequence[SopSection], *, client_id: str, load_types: Sequence[str]
) -> dict[str, tuple[SopSection, ...]]:
    """For each visible section id, the sections whose text the merged section holds,
    least specific first: the objects given, so a caller can map them back to its rows.

    A section a `replace` shadows is not among them: its text no longer applies.
    """
    merged = _merge(sections, client_id=client_id, load_types=load_types)
    return {section_id: merged[section_id][1] for section_id in sorted(merged)}


def _merge(
    sections: Sequence[SopSection], *, client_id: str, load_types: Sequence[str]
) -> dict[str, tuple[ResolvedSection, tuple[SopSection, ...]]]:
    merged: dict[str, tuple[ResolvedSection, tuple[SopSection, ...]]] = {}
    for scope in _visible_scopes(sections, client_id=client_id, load_types=load_types):
        for section in scope:
            inherited = merged.get(section.section_id)
            resolved = _apply(inherited[0] if inherited else None, section)
            parts = (*inherited[1], section) if inherited and _extends(section) else (section,)
            merged[section.section_id] = (resolved, parts)
    return merged


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


def _extends(section: SopSection) -> bool:
    return section.mode is MergeMode.EXTEND and section.layer is not SopLayer.STANDARD


def _apply(inherited: ResolvedSection | None, section: SopSection) -> ResolvedSection:
    if not _extends(section):
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


def layer_owner(broker_id: str | None) -> str | None:
    """The owner of a layer: a broker id, or None for the standard layer (which the
    standard catalog's id also names)."""
    return None if broker_id in (None, STANDARD_BROKER_ID) else broker_id


class _LayerOwned(DomainModel):
    broker_id: str | None

    @field_validator("broker_id")
    @classmethod
    def _owner(cls, value: str | None) -> str | None:
        return layer_owner(value)


class SearchHit(DomainModel):
    """One section a search found, best first. `score` only orders hits of one search."""

    section_id: str
    title: str
    description: str
    score: float


class SopDocument(_LayerOwned):
    """A file an editor uploaded (a broker's SOP PDF), kept as a blob.

    `broker_id` None is the standard layer, shared by every broker.
    """

    document_id: str
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


class SopDraft(_LayerOwned):
    """The one draft of a broker's SOP (None: the standard layer), edited until published.

    Besides sections, the compile step proposes catalog changes the SOP implies: new
    intents, document requirements, and obligations (a new kind such as `pod`, or new
    timing for `eta_request`). Publishing applies the accepted ones to the broker
    (`with_proposals`): the intents an editor accepted, and every requirement and
    obligation still in the draft (an editor removes the ones to drop).
    """

    sections: tuple[DraftSection, ...] = ()
    proposed_intents: tuple[ProposedIntent, ...] = ()
    proposed_requirements: tuple[DocumentRequirement, ...] = ()
    proposed_obligations: tuple[ObligationDef, ...] = ()
    document_ids: tuple[str, ...] = ()  # the uploads the draft was compiled from
    updated_at: datetime


class SopVersion(_LayerOwned):
    """A published, immutable set of sections. `version` is its content hash
    (`version_hash`), so the same sections always publish as the same version."""

    version: str
    sections: tuple[SopSection, ...]
    published_at: datetime
    published_by: str
    lint: tuple[str, ...] = ()  # the warnings the publish accepted


def version_hash(sections: Iterable[SopSection]) -> str:
    """Short sha256 of the sections' content, in order: the id of a published version."""
    dumped = [s.model_dump(mode="json") for s in sections]
    text = json.dumps(dumped, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:VERSION_LENGTH]


def search_text(section: SopSection) -> str:
    """What the index embeds and searches for a section: its heading and its text."""
    return f"{section.title}\n{section.description}\n\n{section.body}"


def section_from_draft(section: DraftSection, broker_id: str | None, source: str) -> SopSection:
    """The section a draft section publishes as. `source` says where its text came from
    (an upload and its pages), for `ResolvedSection.sources`."""
    standard = layer_owner(broker_id) is None or section.layer is SopLayer.STANDARD
    return SopSection(
        section_id=section.section_id,
        layer=SopLayer.STANDARD if standard else section.layer,
        client_id=None if standard else broker_id,
        load_type=None if standard else section.load_type,
        title=section.title,
        description=section.description,
        intents=section.intents,
        stages=section.stages,
        mode=MergeMode.REPLACE if standard else section.mode,
        body=section.body,
        source=source,
    )


def with_proposals(broker: Broker, draft: SopDraft) -> Broker:
    """The broker's catalog once the draft is published, not validated yet: a draft may
    propose a catalog that does not work, which its lint reports (`revised_broker`
    validates).

    Adds the intents the editor accepted (an id already in the catalog keeps its
    definition), and puts in every obligation and requirement the draft proposes (one of
    the same kind, or the same type and stage, is replaced). Each obligation also gets
    its follow-up intent, a system intent, unless the catalog has it: its timer emits it.
    """
    intents = list(broker.intents)
    known = {i.intent_id for i in intents}
    for proposed in draft.proposed_intents:
        if proposed.accepted and proposed.intent_id not in known:
            intents.append(IntentDef(**proposed.model_dump(exclude={"evidence", "accepted"})))
            known.add(proposed.intent_id)
    proposed_kinds = {o.kind: o for o in draft.proposed_obligations}
    obligations = [proposed_kinds.pop(o.kind, o) for o in broker.obligations]
    obligations.extend(proposed_kinds.values())
    requirements = {(r.type_id, r.at_stage): r for r in broker.documents}
    requirements.update({(r.type_id, r.at_stage): r for r in draft.proposed_requirements})
    return broker.model_copy(
        update={
            "intents": follow_up_intents(intents, obligations),
            "obligations": tuple(obligations),
            "documents": tuple(requirements.values()),
        }
    )


def follow_up_intents(
    intents: Sequence[IntentDef], obligations: Sequence[ObligationDef]
) -> tuple[IntentDef, ...]:
    """`intents`, plus a system intent for each obligation whose follow-up intent is
    missing (its timer emits it): `domain.broker.with_follow_up_intents`."""
    return with_follow_up_intents(intents, obligations)


def revised_broker(broker: Broker, **changes: object) -> Broker:
    """`broker` with `changes`, validated with the whole catalog (`Broker.revised`;
    `model_copy` skips validation)."""
    return broker.revised(**changes)


def adopt_sop(broker: Broker, sop_version: str, *, saved: bool, percent: int = 100) -> Broker:
    """The broker's next configuration, running SOP `sop_version`, validated.

    A broker already saved gets a new `config_version`, and `percent` of its loads run
    on it, the rest on the version it replaces (a canary; 0 is a rollback). A broker
    saved for the first time keeps version 1, with no previous version to share with.
    """
    if not saved:
        return revised_broker(broker, sop_version=sop_version, rollout=Rollout())
    return revised_broker(
        broker,
        sop_version=sop_version,
        config_version=broker.config_version + 1,
        rollout=Rollout(percent=percent, previous_version=broker.config_version),
    )
