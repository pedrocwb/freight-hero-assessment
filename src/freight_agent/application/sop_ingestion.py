"""SOP onboarding: upload a broker's document, compile a draft, lint it, publish it.

Owner: card W3-KNW.

1. `ingest_document`: the bytes go to the blob store, the page reader splits them into
   pages, and a `SopDocument` is saved (the same bytes uploaded again are the same
   document).
2. `compile_draft`: a page too thin to read (a scan) is transcribed first (role
   `extract`, the page as an image); then one `compile` call turns the document into a
   `CompiledDraft`: sections (title, description, the text word for word, proposed
   intents and stages from the broker's catalog), proposed new intents with their
   evidence, document requirements and obligations. It is merged into the broker's one
   draft, which an editor reviews (`save_draft`).
3. `lint_draft`: what a publish would refuse (errors) or accept with a note (warnings).
4. `publish_draft`: lint, the published sections and their content hash (the
   version), their embeddings, then in one unit of work the `SopVersion` and the broker's
   next config (`domain.sop.with_proposals` and `adopt_sop`: the accepted intents, the
   obligations and requirements of the draft, `sop_version`, `config_version + 1`, the
   rollout to `percent` with the previous version).

Model and embedding calls happen outside any unit of work; a publish checks that the
draft and the broker did not change meanwhile. Errors an editor can act on are
`SopIngestionError`s.
"""

import hashlib
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import Field

from freight_agent.application.prompts_knowledge import (
    COMPILE_SYSTEM_PROMPT,
    TRANSCRIBE_SYSTEM_PROMPT,
    render_compile_prompt,
    render_transcribe_prompt,
)
from freight_agent.domain.base import DomainModel
from freight_agent.domain.broker import (
    STANDARD_BROKER_ID,
    Broker,
    DocumentRequirement,
    EscalationTarget,
    ObligationDef,
    new_broker,
    standard_broker,
)
from freight_agent.domain.documents import Page
from freight_agent.domain.errors import DomainError
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Stage
from freight_agent.domain.sop import (
    DraftSection,
    MergeMode,
    ProposedIntent,
    ResolvedSection,
    SopDocument,
    SopDraft,
    SopLayer,
    SopSection,
    SopVersion,
    SourceRef,
    adopt_sop,
    effective_sections,
    layer_owner,
    search_text,
    section_from_draft,
    version_hash,
    with_proposals,
)
from freight_agent.ports.clock import Clock, IdGenerator
from freight_agent.ports.documents import BlobStore, PageReader
from freight_agent.ports.knowledge import Embedder, SectionEmbeddings, SopIndex
from freight_agent.ports.llm import ImageInput, LanguageModel, LlmError, LlmRequest, ModelRole
from freight_agent.ports.repositories import UnitOfWorkFactory

logger = logging.getLogger(__name__)

_SNAKE_CASE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")


# --- the compile role's answer -----------------------------------------------------------


class CompiledSection(DomainModel):
    section_id: str = Field(description="snake_case; a standard section's id to replace it")
    title: str = Field(description="The topic's heading, without its number")
    description: str = Field(description="One line: when the section applies")
    body: str = Field(description="The topic's text, word for word, steps and numbering kept")
    intents: tuple[str, ...] = Field(
        default=(), description="Catalog or proposed intent ids; empty: every event"
    )
    stages: tuple[Stage, ...] = Field(default=(), description="Empty: every stage")
    pages: tuple[int, ...] = Field(default=(), description="The pages its text is on")
    mode: MergeMode = MergeMode.REPLACE
    load_type: str | None = Field(default=None, description="Only for one load type alone")


class CompiledIntent(DomainModel):
    intent_id: str = Field(description="snake_case, not in the catalog")
    title: str
    description: str = Field(description="What the message says, with examples in quotes")
    examples: tuple[str, ...] = ()
    must_act: bool = True
    evidence: str = Field(description="The sentence of the document that shows it")


class CompiledRequirement(DomainModel):
    type_id: str = Field(description="A document type of the catalog")
    at_stage: Stage
    within_minutes: int | None = None
    opens_obligation: str | None = Field(
        default=None, description="The obligation kind that chases the document"
    )


class CompiledObligation(DomainModel):
    kind: str = Field(description="eta_request, or a new snake_case kind such as pod")
    title: str
    description: str
    follow_up_minutes: int = Field(description="The wait after each request")
    max_attempts: int = Field(description="The most requests sent")
    on_exhausted: tuple[EscalationTarget, ...] = Field(
        description="Who takes over after the last request, in order"
    )


class CompiledDraft(DomainModel):
    """Structured output of the `compile` role."""

    sections: tuple[CompiledSection, ...]
    proposed_intents: tuple[CompiledIntent, ...] = ()
    proposed_requirements: tuple[CompiledRequirement, ...] = ()
    proposed_obligations: tuple[CompiledObligation, ...] = ()


class PageText(DomainModel):
    """Structured output of a page transcription (role `extract`)."""

    text: str = Field(description="The page's text, line by line, exactly as printed")


# --- results and errors ------------------------------------------------------------------


class SopIngestionError(Exception):
    """A request about a broker's SOP that cannot be done as it is. Says why."""


class NoDraft(SopIngestionError):
    pass


class UnknownDocument(SopIngestionError):
    pass


class DraftChanged(SopIngestionError):
    """The draft or the broker changed while a publish was embedding: publish again."""


class DraftInvalid(SopIngestionError):
    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("the draft cannot be published: " + "; ".join(self.errors))


@dataclass(frozen=True)
class DraftLint:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def problems(self) -> tuple[str, ...]:
        """Every problem as `error: ...` or `warning: ...`, errors first."""
        return (*(f"error: {e}" for e in self.errors), *(f"warning: {w}" for w in self.warnings))


@dataclass(frozen=True)
class IngestedDocument:
    document: SopDocument
    pages: tuple[Page, ...]
    readable: bool  # False: the page reader could not open it (stored, with 0 pages)


UploadStatus = Literal["compiled", "stored", "failed"]


@dataclass(frozen=True)
class Upload:
    """An upload's outcome: `compiled` (the draft has its sections), `stored` (the compile
    failed; `error` says why), `failed` (the file could not be read)."""

    document: SopDocument
    status: UploadStatus
    draft: SopDraft | None = None
    error: str | None = None


@dataclass(frozen=True)
class Published:
    version: str
    config_version: int
    warnings: tuple[str, ...] = field(default=())


# --- the service -------------------------------------------------------------------------


@dataclass(frozen=True)
class SopIngestion:
    """SOP onboarding over the ports. `knowledge` gives the standard layer a draft builds
    on; `published_by` names who publishes from this service (an editor, the API)."""

    uow_factory: UnitOfWorkFactory
    clock: Clock
    ids: IdGenerator
    llm: LanguageModel
    blobs: BlobStore
    pages: PageReader
    embedder: Embedder
    knowledge: SopIndex
    timeout_s: float = 120.0

    async def upload(self, broker_id: str, filename: str, content_type: str, data: bytes) -> Upload:
        """Ingest a document and compile it into the broker's draft."""
        ingested = await self.ingest_document(broker_id, filename, content_type, data)
        if not ingested.readable:
            return Upload(ingested.document, "failed", error="the file could not be read")
        try:
            draft = await self.compile_draft(broker_id, ingested.document.document_id)
        except LlmError as exc:  # the editor may upload again: the document stays stored
            logger.warning("compiling %s failed: %s", ingested.document.document_id, exc)
            return Upload(ingested.document, "stored", error=f"{type(exc).__name__}: {exc}")
        return Upload(ingested.document, "compiled", draft=draft)

    async def ingest_document(
        self, broker_id: str, filename: str, content_type: str, data: bytes
    ) -> IngestedDocument:
        owner = _broker_owner(broker_id)
        digest = hashlib.sha256(data).hexdigest()
        try:
            pages = tuple(self.pages.pages(data, content_type))
            readable = True
        except ValueError as exc:  # the reader's way of saying it cannot open the bytes
            logger.info("%s cannot be read: %s", filename, exc)
            pages, readable = (), False
        async with self.uow_factory() as uow:
            for known in await uow.sops.list_documents(owner):
                if known.sha256 == digest:
                    return IngestedDocument(known, pages, readable)
        ref = await self.blobs.put(data, content_type, name=filename)
        document = SopDocument(
            document_id=self.ids.new("sopdoc"),
            broker_id=owner,
            filename=filename,
            content_type=content_type,
            blob_ref=ref,
            pages=len(pages),
            sha256=digest,
            uploaded_at=self.clock.now(),
        )
        async with self.uow_factory() as uow:
            await uow.sops.save_document(document)
            await uow.commit()
        return IngestedDocument(document, pages, readable)

    async def compile_draft(self, broker_id: str, document_id: str) -> SopDraft:
        """Compile one uploaded document into the broker's draft (merged into the draft
        there is: a section of the same id and load type is replaced) and save it."""
        owner = _broker_owner(broker_id)
        async with self.uow_factory() as uow:
            document = await uow.sops.get_document(document_id)
            broker = await uow.brokers.get(owner) or standard_broker()
        if document is None or document.broker_id != owner:
            raise UnknownDocument(f"broker {owner} has no uploaded document {document_id}")
        data = await self.blobs.get(document.blob_ref)
        pages = [
            await self._readable(document, page)
            for page in self.pages.pages(data, document.content_type)
        ]
        standard = await self.knowledge.visible(STANDARD_BROKER_ID, ())
        request = LlmRequest(
            role=ModelRole.COMPILE,
            system=COMPILE_SYSTEM_PROMPT,
            user=render_compile_prompt(broker, standard, document, pages),
            max_tool_rounds=0,
            timeout_s=self.timeout_s,
        )
        response = await self.llm.generate(request, CompiledDraft)
        logger.info("compiled %s: %s", document_id, response.usage)
        compiled = draft_from_compiled(
            response.parsed, owner, broker, document, updated_at=self.clock.now()
        )
        async with self.uow_factory() as uow:
            existing = await uow.sops.get_draft(owner)
            draft = compiled if existing is None else merge_drafts(existing, compiled)
            await uow.sops.save_draft(draft)
            await uow.commit()
        return draft

    async def _readable(self, document: SopDocument, page: Page) -> Page:
        """The page with text: a page that came with an image is transcribed from it."""
        if page.image_png is None:
            return page
        request = LlmRequest(
            role=ModelRole.EXTRACT,
            system=TRANSCRIBE_SYSTEM_PROMPT,
            user=render_transcribe_prompt(document, page),
            max_tool_rounds=0,
            timeout_s=self.timeout_s,
            images=(ImageInput(data=page.image_png, media_type="image/png"),),
        )
        response = await self.llm.generate(request, PageText)
        return page.model_copy(update={"text": response.parsed.text})

    async def get_draft(self, broker_id: str) -> SopDraft:
        async with self.uow_factory() as uow:
            draft = await uow.sops.get_draft(_broker_owner(broker_id))
        if draft is None:
            raise NoDraft(f"broker {broker_id} has no SOP draft: upload a document first")
        return draft

    async def save_draft(
        self,
        broker_id: str,
        *,
        sections: Sequence[DraftSection],
        proposed_intents: Sequence[ProposedIntent] = (),
        proposed_requirements: Sequence[DocumentRequirement] = (),
        proposed_obligations: Sequence[ObligationDef] = (),
    ) -> SopDraft:
        """The editor's whole draft replaces the broker's; the uploads it came from stay."""
        owner = _broker_owner(broker_id)
        async with self.uow_factory() as uow:
            existing = await uow.sops.get_draft(owner)
            draft = SopDraft(
                broker_id=owner,
                sections=tuple(sections),
                proposed_intents=tuple(proposed_intents),
                proposed_requirements=tuple(proposed_requirements),
                proposed_obligations=tuple(proposed_obligations),
                document_ids=() if existing is None else existing.document_ids,
                updated_at=self.clock.now(),
            )
            await uow.sops.save_draft(draft)
            await uow.commit()
        return draft

    async def lint_draft(self, draft: SopDraft) -> DraftLint:
        owner = _broker_owner(draft.broker_id or STANDARD_BROKER_ID)
        async with self.uow_factory() as uow:
            broker = await uow.brokers.get(owner) or _new(owner)
        standard = await self.knowledge.visible(STANDARD_BROKER_ID, ())
        return check_draft(draft, broker, standard)

    async def publish_draft(
        self, broker_id: str, *, percent: int = 100, published_by: str = "editor"
    ) -> Published:
        owner = _broker_owner(broker_id)
        async with self.uow_factory() as uow:
            draft = await uow.sops.get_draft(owner)
            current = await uow.brokers.get(owner)
            documents = {d.document_id: d for d in await uow.sops.list_documents(owner)}
        if draft is None:
            raise NoDraft(f"broker {owner} has no SOP draft to publish")
        broker = current or _new(owner)
        lint = check_draft(draft, broker, await self.knowledge.visible(STANDARD_BROKER_ID, ()))
        if lint.errors:
            raise DraftInvalid(lint.errors)
        sections = tuple(
            section_from_draft(s, owner, _source(s, documents)) for s in draft.sections
        )
        version = version_hash(sections)
        try:
            adopted = adopt_sop(
                with_proposals(broker, draft), version, saved=current is not None, percent=percent
            )
        except ValueError as exc:  # the catalog's own validation (card W3-BRK)
            raise DraftInvalid([str(exc)]) from None
        embeddings = await self._embed(sections)
        warnings = tuple(f"warning: {w}" for w in lint.warnings)
        async with self.uow_factory() as uow:
            if await uow.sops.get_draft(owner) != draft or await uow.brokers.get(owner) != current:
                raise DraftChanged(
                    f"the draft or the broker {owner} changed while publishing: publish again"
                )
            await uow.sops.save_version(
                SopVersion(
                    broker_id=owner,
                    version=version,
                    sections=sections,
                    published_at=self.clock.now(),
                    published_by=published_by,
                    lint=warnings,
                ),
                embeddings,
            )
            await uow.brokers.save(adopted)
            await uow.commit()
        return Published(version=version, config_version=adopted.config_version, warnings=warnings)

    async def _embed(self, sections: Sequence[SopSection]) -> SectionEmbeddings:
        vectors = await self.embedder.embed([search_text(s) for s in sections])
        return SectionEmbeddings(self.embedder.model_id, tuple(tuple(v) for v in vectors))


def _broker_owner(broker_id: str) -> str:
    """A broker's layer; the standard layer is published from `sops/standard/` with
    `agent sop import`, not uploaded."""
    owner = layer_owner(broker_id)
    if owner is None:
        raise SopIngestionError(
            "the standard layer is published from sops/standard/ with `agent sop import`; "
            "upload a broker's SOP under that broker"
        )
    return owner


def _new(broker_id: str) -> Broker:
    """A broker the store does not have yet: the standard catalog under its id."""
    return new_broker(broker_id, broker_id.replace("_", " ").title())


def _source(section: DraftSection, documents: dict[str, SopDocument]) -> str:
    """Where a section's text came from, as a run shows it (`ResolvedSection.sources`)."""
    if section.source is None:
        return "editor"
    document = documents.get(section.source.document_id)
    name = document.filename if document else f"upload {section.source.document_id}"
    pages = section.source.pages
    if not pages:
        return name
    label = "p." if len(pages) == 1 else "pp."
    return f"{name} {label} {', '.join(str(p) for p in pages)}"


# --- compiled output into a draft --------------------------------------------------------


def draft_from_compiled(
    compiled: CompiledDraft,
    broker_id: str,
    broker: Broker,
    document: SopDocument,
    *,
    updated_at: datetime,
) -> SopDraft:
    """The draft a compile proposes. Every section and proposal waits for an editor; an
    intent the catalog has, or an obligation it already has as proposed, is not proposed."""
    sections = tuple(
        DraftSection(
            section_id=s.section_id,
            layer=SopLayer.LOAD_TYPE if s.load_type else SopLayer.CLIENT,
            load_type=s.load_type or None,
            title=s.title,
            description=s.description,
            intents=tuple(dict.fromkeys(s.intents)),
            stages=tuple(dict.fromkeys(s.stages)),
            mode=s.mode,
            body=s.body,
            source=SourceRef(document_id=document.document_id, pages=s.pages),
            proposed=True,
        )
        for s in compiled.sections
    )
    intents = tuple(
        ProposedIntent(
            intent_id=i.intent_id,
            title=i.title,
            description=i.description,
            examples=i.examples,
            must_act=i.must_act,
            evidence=i.evidence,
        )
        for i in compiled.proposed_intents
        if broker.intent(i.intent_id) is None
    )
    requirements = tuple(
        DocumentRequirement(
            type_id=r.type_id,
            at_stage=r.at_stage,
            within_minutes=r.within_minutes,
            opens_obligation=r.opens_obligation,
        )
        for r in compiled.proposed_requirements
    )
    obligations = tuple(
        o
        for o in (
            ObligationDef(
                kind=c.kind,
                title=c.title,
                description=c.description,
                follow_up_minutes=c.follow_up_minutes,
                max_attempts=c.max_attempts,
                follow_up_intent=_known_follow_up(broker, c.kind),
                on_exhausted=c.on_exhausted,
            )
            for c in compiled.proposed_obligations
        )
        if broker.obligation(o.kind) != o
    )
    return SopDraft.model_validate(
        {
            "broker_id": broker_id,
            "sections": sections,
            "proposed_intents": intents,
            "proposed_requirements": requirements,
            "proposed_obligations": obligations,
            "document_ids": (document.document_id,),
            "updated_at": updated_at,
        }
    )


def _known_follow_up(broker: Broker, kind: str) -> str:
    """A kind the catalog has keeps its follow-up intent (eta_request's is named)."""
    known = broker.obligation(kind)
    return "" if known is None else known.follow_up_intent


def merge_drafts(existing: SopDraft, new: SopDraft) -> SopDraft:
    """`new` compiled into `existing`: a section of the same id and load type, an intent
    of the same id, an obligation of the same kind, a requirement of the same type and
    stage are replaced by the new one (an intent keeps the editor's decision); the rest
    is kept, in order."""

    sections = {(s.section_id, s.load_type): s for s in existing.sections}
    sections.update({(s.section_id, s.load_type): s for s in new.sections})
    intents = {i.intent_id: i for i in existing.proposed_intents}
    for intent in new.proposed_intents:
        decided = intents.get(intent.intent_id)
        accepted = None if decided is None else decided.accepted
        intents[intent.intent_id] = intent.model_copy(update={"accepted": accepted})
    obligations = {o.kind: o for o in existing.proposed_obligations}
    obligations.update({o.kind: o for o in new.proposed_obligations})
    requirements = {(r.type_id, r.at_stage): r for r in existing.proposed_requirements}
    requirements.update({(r.type_id, r.at_stage): r for r in new.proposed_requirements})
    return existing.model_copy(
        update={
            "sections": tuple(sections.values()),
            "proposed_intents": tuple(intents.values()),
            "proposed_obligations": tuple(obligations.values()),
            "proposed_requirements": tuple(requirements.values()),
            "document_ids": tuple(dict.fromkeys((*existing.document_ids, *new.document_ids))),
            "updated_at": new.updated_at,
        }
    )


# --- the lint ------------------------------------------------------------------------------


def check_draft(draft: SopDraft, broker: Broker, standard: Sequence[ResolvedSection]) -> DraftLint:
    """What publishing the draft for `broker`, over the standard layer `standard`, would
    refuse (errors) or accept with a note (warnings)."""
    candidate = with_proposals(broker, draft)
    known = {i.intent_id for i in candidate.intents}
    proposed = {i.intent_id: i for i in draft.proposed_intents}
    standard_ids = {s.section_id for s in standard}
    client_ids = {
        s.section_id for s in draft.sections if s.layer is SopLayer.CLIENT and not s.load_type
    }
    errors: list[str] = [*catalog_problems(candidate)]
    seen: set[tuple[str, SopLayer, str | None]] = set()
    for section in draft.sections:
        errors += _section_problems(section, known, proposed, standard_ids, client_ids)
        key = (section.section_id, section.layer, section.load_type)
        if key in seen:
            errors.append(f"section {section.section_id} appears twice in the same layer")
        seen.add(key)
    if not errors:
        errors += _coverage_problems(draft, candidate, standard)
    warnings = [
        f"intent {i.intent_id} was proposed and not reviewed: it is not added"
        for i in draft.proposed_intents
        if i.accepted is None
    ]
    warnings += [
        f"section {s.section_id} was compiled and not reviewed"
        for s in draft.sections
        if s.proposed
    ]
    return DraftLint(tuple(errors), tuple(warnings))


def _section_problems(
    section: DraftSection,
    known: set[str],
    proposed: dict[str, ProposedIntent],
    standard_ids: set[str],
    client_ids: set[str],
) -> list[str]:
    where = f"section {section.section_id}"
    problems: list[str] = []
    if not _SNAKE_CASE.fullmatch(section.section_id):
        problems.append(
            f"{where}: the id must be snake_case (lowercase letters, digits and underscores, "
            "starting with a letter)"
        )
    if section.layer is SopLayer.STANDARD:
        problems.append(f"{where}: a broker's section is in the client or the load type layer")
    elif (section.layer is SopLayer.LOAD_TYPE) != bool(section.load_type):
        problems.append(f"{where}: a load type section names its load type, and only it does")
    extends = section.mode is MergeMode.EXTEND
    if not extends and not (section.title.strip() and section.description.strip()):
        problems.append(f"{where}: give it a title and a description (the agent picks by them)")
    if not section.body.strip():
        problems.append(f"{where}: the section has no text")
    for intent in section.intents:
        if intent in known:
            continue
        if intent in proposed:
            problems.append(
                f"{where}: intent {intent} is proposed and not accepted: accept it, or remove "
                "it from the section"
            )
        else:
            problems.append(f"{where}: intent {intent} is not in the broker's catalog")
    bases = standard_ids | (client_ids if section.layer is SopLayer.LOAD_TYPE else set[str]())
    if extends and section.section_id not in bases:
        problems.append(
            f"{where}: it extends a section that no less specific layer has; use mode replace"
        )
    return problems


def _coverage_problems(
    draft: SopDraft, candidate: Broker, standard: Sequence[ResolvedSection]
) -> list[str]:
    """Every intent of the catalog but `other` has a section that names it, for a load of
    any of the draft's load types and for a load of none."""
    owner = candidate.broker_id
    sections = [
        *(_as_standard(s) for s in standard),
        *(section_from_draft(s, owner, "draft") for s in draft.sections),
    ]
    load_types = sorted({s.load_type for s in draft.sections if s.load_type})
    problems: list[str] = []
    for types in ((), *((t,) for t in load_types)):
        try:
            merged = effective_sections(sections, client_id=owner, load_types=types)
        except DomainError as exc:  # the section checks report what the merge refuses
            return [str(exc)]  # pragma: no cover
        named = {intent for s in merged for intent in s.intents}
        loads = f"a {types[0]} load" if types else "a load"
        problems += [
            f"intent {intent.intent_id} has no section for {loads}: add one that lists it"
            for intent in candidate.intents
            if intent.intent_id != Intent.OTHER and intent.intent_id not in named
        ]
    return problems


def _as_standard(section: ResolvedSection) -> SopSection:
    return SopSection(
        section_id=section.section_id,
        layer=SopLayer.STANDARD,
        title=section.title,
        description=section.description,
        intents=section.intents,
        stages=section.stages,
        body=section.body,
        source=section.sources[-1] if section.sources else "standard",
    )


def catalog_problems(broker: Broker) -> tuple[str, ...]:
    """What keeps the broker's catalog after a publish from working.

    Card W3-BRK's `domain.broker.catalog_problems` checks the whole catalog; until both
    cards are on one branch this checks what a draft can change: the new intents, the
    obligations and the requirements. (`publish_draft` validates the broker in full.)
    """
    problems: list[str] = []
    for intent in broker.intents:
        if not _SNAKE_CASE.fullmatch(intent.intent_id):
            problems.append(f"intent id {intent.intent_id!r} must be snake_case")
        if not (intent.title.strip() and intent.description.strip()):
            problems.append(f"intent {intent.intent_id} needs a title and a description")
    kinds = {o.kind for o in broker.obligations}
    for obligation in broker.obligations:
        if not _SNAKE_CASE.fullmatch(obligation.kind):
            problems.append(f"obligation kind {obligation.kind!r} must be snake_case")
        if obligation.follow_up_minutes < 1 or obligation.max_attempts < 1:
            problems.append(
                f"obligation {obligation.kind}: follow_up_minutes and max_attempts must be "
                "at least 1"
            )
        if not obligation.on_exhausted:
            problems.append(f"obligation {obligation.kind}: on_exhausted names nobody")
    types = {d.type_id for d in broker.document_types}
    for requirement in broker.documents:
        where = f"requirement {requirement.type_id} at {requirement.at_stage}"
        if requirement.type_id not in types:
            problems.append(f"{where}: unknown document type {requirement.type_id}")
        opens = requirement.opens_obligation
        if opens is not None and opens not in kinds:
            problems.append(f"{where}: unknown obligation kind {opens}")
        if requirement.within_minutes is not None and opens is None:
            problems.append(f"{where}: within_minutes needs opens_obligation")
    return tuple(problems)
