"""The document pipeline the runtime ticks before it processes the queue.

Owner: card W3-DOC.

`DocumentPipeline` implements `interfaces.documents.DocumentProcessor`. The composition root
builds it by these names (`bootstrap.build_documents`):

    DocumentPipeline(
        uow_factory=..., clock=..., ids=..., llm=..., blobs=..., pages=..., knowledge=...,
        prompt=..., timeout_s=...
    )

`prompt` is the extract template (`Prompts.extract`).

Per pending document, oldest first, in three phases so that no unit of work is open
while the file is read or the model thinks (the memory store serializes units of work,
a database would hold the rows):

1. read the load and the seal its messages reported, in one short unit of work, and its
   broker's catalog from the knowledge index, as a run does (`SopIndex.broker`);
2. outside any transaction: the bytes from the blob store, the pages from the page
   reader (in a thread: PDF work blocks), then one `extract` call, on the extract
   template, that classifies the document among the broker's `DocumentTypeDef`s and
   reads the type's fields. A page whose text is too thin goes as an image
   (`LlmRequest.images`). The answer is checked in code
   (`domain.documents.read_extraction`);
3. one unit of work: append the `document_processed` event (`doc:{document_id}`), save
   the document, queue the event, and, for a clean `processed` document, close as
   `satisfied` the open obligation of each `DocumentRequirement` of its type (its
   pending timers are cancelled, as `close_obligation` does). A document another worker
   already finished is left alone, and an event already logged is never queued again,
   so a document is announced once.

Failures:

- The file cannot be read (no bytes, not a PDF, a page reader error) or the model fails
  (`LlmError`, a timeout): the document is `failed` and its `document_processed` event
  still goes out, with status `failed`. It is never retried, so a broken file or a model
  outage cannot loop. The event's run acts on it: `sops/standard/documents.md` hands a
  failed document to a Hero (the executor owns the outbox, so the pipeline never writes
  a Hero task itself), and if the model is still down, that run escalates on its own.
- A document whose load is unknown is saved `failed` with no event: there is no load to
  run on.
- Any other error while finishing a document (the database) leaves it pending for the
  next tick and the pipeline goes on with the next document: one bad row never stops the
  tick.

The event's `body` is `Reading.summary()`: the type, the status and why it needs review.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from freight_agent.application.prompts_documents import (
    check_extract_prompt,
    extract_system,
    extract_user,
)
from freight_agent.domain.broker import Broker
from freight_agent.domain.documents import (
    Document,
    DocumentStatus,
    Extraction,
    Page,
    Reading,
    read_extraction,
    reported_seal,
)
from freight_agent.domain.events import Event, EventType
from freight_agent.domain.load import Channel, Load
from freight_agent.domain.obligations import ObligationStatus
from freight_agent.domain.timers import TimerStatus
from freight_agent.interfaces.clock import Clock, IdGenerator
from freight_agent.interfaces.documents import BlobStore, PageReader
from freight_agent.interfaces.knowledge import SopIndex
from freight_agent.interfaces.llm import ImageInput, LanguageModel, LlmError, LlmRequest, ModelRole
from freight_agent.interfaces.prompts import ChatPrompt
from freight_agent.interfaces.repositories import UnitOfWork, UnitOfWorkFactory

logger = logging.getLogger(__name__)

# Page images sent with one request at most (the first ones); the text of every page goes.
MAX_IMAGES = 8
# The load's events read for a reported seal number.
SEAL_EVENTS_LIMIT = 200
# The stored text of one document at most (`Document.text`, for read_document).
MAX_STORED_TEXT_CHARS = 100_000
_REASON_LIMIT = 200


class NullDocumentProcessor:
    """Implements `interfaces.documents.DocumentProcessor` by reading nothing: every pending
    document stays pending (the runtime before wave 3, and the eval harness)."""

    async def process_pending(self, limit: int = 10, *, load_id: str | None = None) -> list[Event]:
        return []


@dataclass(frozen=True)
class _Inputs:
    """What the checks compare a document with, read before the model call."""

    load: Load
    broker: Broker
    seal: str | None


class DocumentPipeline:
    """Implements `interfaces.documents.DocumentProcessor`; see the module docstring.

    `ids` is part of the composition root's contract; the pipeline derives the one id it
    writes (`doc:{document_id}`) and needs no new one.
    """

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdGenerator,
        llm: LanguageModel,
        blobs: BlobStore,
        pages: PageReader,
        knowledge: SopIndex,
        prompt: ChatPrompt,
        timeout_s: float = 60.0,
    ) -> None:
        """`PromptError` when the template does not fit (`check_extract_prompt`)."""
        self._prompt = check_extract_prompt(prompt)
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._llm = llm
        self._blobs = blobs
        self._pages = pages
        self._knowledge = knowledge
        self._timeout_s = timeout_s

    def with_knowledge(self, knowledge: SopIndex) -> "DocumentPipeline":
        """A per-load catalog without changing the shared worker pipeline."""
        return DocumentPipeline(
            uow_factory=self._uow_factory,
            clock=self._clock,
            ids=self._ids,
            llm=self._llm,
            blobs=self._blobs,
            pages=self._pages,
            knowledge=knowledge,
            prompt=self._prompt,
            timeout_s=self._timeout_s,
        )

    async def process_pending(self, limit: int = 10, *, load_id: str | None = None) -> list[Event]:
        """Read up to `limit` pending documents; return the events announced."""
        async with self._uow_factory() as uow:
            if load_id is None:
                pending = await uow.documents.list_pending(limit)
            else:
                pending = [
                    d
                    for d in await uow.documents.list_for_load(load_id)
                    if d.status is DocumentStatus.PENDING
                ][: max(0, limit)]
        announced: list[Event] = []
        for document in pending:
            try:
                event = await self._process(document)
            except Exception:  # one bad document must not stop the tick: it stays pending
                logger.exception(
                    "document %s could not be finished; it stays pending", document.document_id
                )
                continue
            if event is not None:
                announced.append(event)
        return announced

    async def _process(self, document: Document) -> Event | None:
        inputs = await self._inputs(document)
        if inputs is None:
            await self._orphan(document)
            return None
        pages: list[Page] = []
        try:
            data = await self._blobs.get(document.blob_ref)
            pages = await asyncio.to_thread(self._pages.pages, data, document.content_type)
            reading = await self._read(document, pages, inputs)
        except Exception as exc:  # the file or the model: the document is unreadable
            logger.warning(
                "document %s (%s) could not be read: %s",
                document.document_id,
                document.filename,
                f"{type(exc).__name__}: {exc}",
                exc_info=not isinstance(exc, LlmError | KeyError),
            )
            reading = Reading.failed(_why(exc))
        return await self._finish(document, reading, pages, inputs.broker)

    async def _inputs(self, document: Document) -> _Inputs | None:
        async with self._uow_factory() as uow:
            load = await uow.loads.get(document.load_id)
            if load is None:
                return None
            events = await uow.events.recent(load.load_id, SEAL_EVENTS_LIMIT)
        broker = await self._knowledge.broker(load.broker_id)
        return _Inputs(load=load, broker=broker, seal=reported_seal(events))

    async def _read(self, document: Document, pages: Sequence[Page], inputs: _Inputs) -> Reading:
        types = inputs.broker.document_types
        if not types:
            return Reading(
                status=DocumentStatus.NEEDS_REVIEW,
                reasons=("the broker has no document types to read it as",),
            )
        imaged = [p for p in pages if p.image_png is not None][:MAX_IMAGES]
        if not imaged and not any(p.text.strip() for p in pages):
            return Reading.failed("the file has no text and no page image to read")
        request = LlmRequest(
            role=ModelRole.EXTRACT,
            system=extract_system(self._prompt, types),
            user=extract_user(
                self._prompt,
                filename=document.filename,
                content_type=document.content_type,
                pages=pages,
                image_pages=[p.number for p in imaged],
            ),
            timeout_s=self._timeout_s,
            prompt_version=self._prompt.version,
            images=tuple(
                ImageInput(data=p.image_png, media_type="image/png")
                for p in imaged
                if p.image_png is not None
            ),
        )
        response = await self._llm.generate(request, Extraction)
        logger.info(
            "document %s read as %s (confidence %.2f, %d image(s), $%.5f, prompt %s)",
            document.document_id,
            response.parsed.type_id,
            response.parsed.confidence,
            len(request.images),
            response.usage.cost_usd,
            self._prompt.version,
        )
        return read_extraction(response.parsed, types, inputs.load, reported_seal=inputs.seal)

    async def _finish(
        self, document: Document, reading: Reading, pages: Sequence[Page], broker: Broker
    ) -> Event | None:
        now = self._clock.now()
        finished = document.read(reading, text=_text(pages), pages=len(pages), at=now)
        event = Event(
            event_id=f"doc:{document.document_id}",
            load_id=document.load_id,
            type=EventType.DOCUMENT_PROCESSED,
            occurred_at=now,
            channel=Channel.SYSTEM,
            body=reading.summary(),
            data={
                "document_id": document.document_id,
                "type_id": finished.type_id,
                "status": finished.status.value,
                "attachment_id": document.attachment_id,
            },
        )
        async with self._uow_factory() as uow:
            current = await uow.documents.get(document.document_id)
            if current is None or current.status is not DocumentStatus.PENDING:
                return None  # another worker finished it while this one was reading
            announced = await uow.events.append(event)
            # Saved either way, so a document never stays pending once read: an event
            # already logged means another worker announced it, and only queuing and the
            # obligation are left to the worker whose event was logged.
            await uow.documents.save(finished)
            if announced:
                await uow.queue.enqueue(event)
                if finished.clean:
                    await _satisfy_requirements(uow, finished, broker)
            await uow.commit()
        return event if announced else None

    async def _orphan(self, document: Document) -> None:
        logger.warning("document %s names unknown load %r", document.document_id, document.load_id)
        failed = document.read(
            Reading.failed("the load is unknown"), text="", pages=0, at=self._clock.now()
        )
        async with self._uow_factory() as uow:
            await uow.documents.save(failed)
            await uow.commit()


async def _satisfy_requirements(uow: UnitOfWork, document: Document, broker: Broker) -> None:
    """Close, as satisfied, the open obligation each requirement for this document's type
    opened (a POD owed at delivery), and cancel its pending follow-up timers."""
    kinds = dict.fromkeys(
        r.opens_obligation
        for r in broker.documents
        if r.type_id == document.type_id and r.opens_obligation is not None
    )
    for kind in kinds:
        obligation = await uow.obligations.get_open(document.load_id, kind)
        if obligation is None:
            continue
        for timer in await uow.timers.pending_for_obligation(obligation.obligation_id):
            await uow.timers.save(timer.mark(TimerStatus.CANCELLED))
        await uow.obligations.save(obligation.close(ObligationStatus.SATISFIED))
        logger.info("document %s satisfied the %s obligation", document.document_id, kind)


def _text(pages: Sequence[Page]) -> str:
    text = "\n\n".join(p.text.strip() for p in pages if p.text.strip())
    return text[:MAX_STORED_TEXT_CHARS]


def _why(exc: Exception) -> str:
    """Why a document could not be read, for the event and the Hero who reads it."""
    if isinstance(exc, LlmError):
        return "the extract model did not give a reading"
    if isinstance(exc, KeyError):
        return "the file of the attachment is missing"
    if isinstance(exc, TimeoutError):
        return "reading the file took too long"
    detail = str(exc).strip()
    reason = f"the file could not be read ({type(exc).__name__}{': ' + detail if detail else ''})"
    return reason[:_REASON_LIMIT]
