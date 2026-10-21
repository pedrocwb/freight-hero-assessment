"""Contract suite for `DocumentRepository` (`interfaces/documents.py`).

Owner: card W3-DOC. Runs on the memory store through the shared `uow_factory` fixture,
each test on a fresh, empty store. A document's load exists, so an implementation may
enforce the foreign key.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from freight_agent.adapters.clock import SequentialIdGenerator, VirtualClock
from freight_agent.adapters.documents.fake_pages import FakePageReader
from freight_agent.adapters.documents.memory_blobs import MemoryBlobStore
from freight_agent.adapters.knowledge.memory import MemorySopIndex
from freight_agent.application.documents import DocumentPipeline
from freight_agent.domain.broker import (
    CatalogOverrides,
    DocumentRequirement,
    IntentDef,
    ObligationDef,
    client_broker,
)
from freight_agent.domain.documents import (
    Document,
    DocumentStatus,
    ExtractedValue,
    Extraction,
    FieldCheck,
)
from freight_agent.domain.load import Load, Stage
from freight_agent.domain.obligations import Obligation, ObligationStatus
from freight_agent.domain.timers import Timer, TimerStatus
from freight_agent.interfaces.llm import LlmError, ModelRole
from freight_agent.interfaces.repositories import UnitOfWorkFactory
from tests.fakes import FakeModel
from tests.prompts import PROMPTS

pytestmark = pytest.mark.contract

LA = ZoneInfo("America/Los_Angeles")
A = "481207"  # the fixture load
B = "550031"  # a second load: the fixture under another id


@pytest.fixture
async def loads(uow_factory: UnitOfWorkFactory, load: Load) -> tuple[Load, Load]:
    other = load.model_copy(update={"load_id": B})
    async with uow_factory() as uow:
        await uow.loads.save(load)
        await uow.loads.save(other)
        await uow.commit()
    return load, other


def at(minute: int) -> datetime:
    return datetime(2026, 10, 14, 9, 40, tzinfo=LA) + timedelta(minutes=minute)


def pending(
    document_id: str, *, load_id: str = A, minute: int = 0, attachment: str = "a1"
) -> Document:
    return Document.pending(
        document_id=document_id,
        load_id=load_id,
        attachment_id=attachment,
        broker_id="northline",
        blob_ref="sha256:" + "a" * 64,
        filename="pod.pdf",
        content_type="application/pdf",
        received_at=at(minute),
    )


def processed(document: Document, *, minute: int = 1) -> Document:
    return document.model_copy(
        update={
            "status": DocumentStatus.NEEDS_REVIEW,
            "type_id": "pod",
            "confidence": 0.93,
            "extracted": {
                "receiver": "Valley DC",
                "po_numbers": "55812",
                "signed": True,
                "weight": 38000,
                "rate": 1250.5,
                "exceptions": "",
                "missing": None,
            },
            "checks": (
                FieldCheck(
                    field="receiver",
                    expected="Valley DC",
                    found="Valley DC",
                    ok=True,
                    check="stop_name",
                ),
                FieldCheck(
                    field="po_numbers", expected="55821", found="55812", ok=False, check="po_number"
                ),
            ),
            "text": "PROOF OF DELIVERY\nPO NO.\n55812 — ünïcode",
            "pages": 2,
            "processed_at": at(minute),
        }
    )


async def save(uow_factory: UnitOfWorkFactory, *documents: Document) -> None:
    async with uow_factory() as uow:
        for document in documents:
            await uow.documents.save(document)
        await uow.commit()


async def test_a_saved_document_comes_back_whole(
    uow_factory: UnitOfWorkFactory, loads: tuple[Load, Load]
) -> None:
    document = processed(pending("doc_1"))
    await save(uow_factory, document)

    async with uow_factory() as uow:
        found = await uow.documents.get("doc_1")

    assert found == document
    assert found is not None and found.received_at == at(0)  # the same instant
    assert found.extracted["weight"] == 38000 and found.extracted["signed"] is True


async def test_an_unknown_document_is_none(
    uow_factory: UnitOfWorkFactory, loads: tuple[Load, Load]
) -> None:
    async with uow_factory() as uow:
        assert await uow.documents.get("doc_404") is None
        assert await uow.documents.for_attachment(A, "a404") is None
        assert await uow.documents.list_for_load(A) == []
        assert await uow.documents.list_pending() == []


async def test_saving_again_replaces_the_document(
    uow_factory: UnitOfWorkFactory, loads: tuple[Load, Load]
) -> None:
    first = pending("doc_1")
    await save(uow_factory, first)
    read = processed(first)

    await save(uow_factory, read)

    async with uow_factory() as uow:
        assert await uow.documents.get("doc_1") == read
        assert await uow.documents.list_pending() == []
        assert await uow.documents.list_for_load(A) == [read]


async def test_the_document_of_an_attachment_is_found_by_load_and_attachment(
    uow_factory: UnitOfWorkFactory, loads: tuple[Load, Load]
) -> None:
    mine = pending("doc_1", attachment="a1")
    other_attachment = pending("doc_2", attachment="a2")
    other_load = pending("doc_3", load_id=B, attachment="a1")
    await save(uow_factory, other_load, other_attachment, mine)

    async with uow_factory() as uow:
        assert await uow.documents.for_attachment(A, "a1") == mine
        assert await uow.documents.for_attachment(B, "a1") == other_load
        assert await uow.documents.for_attachment(B, "a2") is None


async def test_a_redelivered_attachment_gives_its_oldest_document(
    uow_factory: UnitOfWorkFactory, loads: tuple[Load, Load]
) -> None:
    await save(uow_factory, pending("doc_2", minute=5), pending("doc_1", minute=0))

    async with uow_factory() as uow:
        found = await uow.documents.for_attachment(A, "a1")

    assert found is not None and found.document_id == "doc_1"


async def test_a_loads_documents_are_oldest_first_by_natural_id_on_ties(
    uow_factory: UnitOfWorkFactory, loads: tuple[Load, Load]
) -> None:
    await save(
        uow_factory,
        pending("doc_10", minute=1),
        pending("doc_9", minute=1),
        pending("doc_11", minute=0),
        pending("doc_1", load_id=B, minute=0),
    )

    async with uow_factory() as uow:
        ids = [d.document_id for d in await uow.documents.list_for_load(A)]

    assert ids == ["doc_11", "doc_9", "doc_10"]


async def test_pending_documents_of_every_load_are_oldest_first_up_to_the_limit(
    uow_factory: UnitOfWorkFactory, loads: tuple[Load, Load]
) -> None:
    await save(
        uow_factory,
        pending("doc_1", minute=3),
        pending("doc_2", load_id=B, minute=1),
        processed(pending("doc_3", minute=0)),
        pending("doc_4", minute=2),
    )

    async with uow_factory() as uow:
        assert [d.document_id for d in await uow.documents.list_pending()] == [
            "doc_2",
            "doc_4",
            "doc_1",
        ]
        assert [d.document_id for d in await uow.documents.list_pending(2)] == ["doc_2", "doc_4"]
        assert await uow.documents.list_pending(0) == []


async def test_an_offset_is_kept_as_the_same_instant(
    uow_factory: UnitOfWorkFactory, loads: tuple[Load, Load]
) -> None:
    utc = pending("doc_1").model_copy(update={"received_at": at(0).astimezone(UTC)})
    await save(uow_factory, utc)

    async with uow_factory() as uow:
        found = await uow.documents.get("doc_1")

    assert found is not None and found.received_at == at(0)


async def test_a_document_not_committed_is_not_saved(
    uow_factory: UnitOfWorkFactory, loads: tuple[Load, Load]
) -> None:
    async with uow_factory() as uow:
        await uow.documents.save(pending("doc_1"))

    async with uow_factory() as uow:
        assert await uow.documents.get("doc_1") is None


async def test_a_unit_of_work_that_fails_saves_no_document(
    uow_factory: UnitOfWorkFactory, loads: tuple[Load, Load]
) -> None:
    with pytest.raises(RuntimeError):
        async with uow_factory() as uow:
            await uow.documents.save(pending("doc_1"))
            await uow.commit()
            raise RuntimeError("after commit, before the end of the block")

    async with uow_factory() as uow:
        assert await uow.documents.get("doc_1") is None


# --- the pipeline on this store ---------------------------------------------------------------

CLEAN_POD = Extraction(
    type_id="pod",
    confidence=0.95,
    fields=(
        ExtractedValue(name="receiver", value="Valley DC"),
        ExtractedValue(name="po_numbers", value="55821"),
        ExtractedValue(name="signed", value="true"),
        ExtractedValue(name="delivered_at", value="2026-10-14T09:42"),
        ExtractedValue(name="exceptions", value=""),
    ),
)


async def test_the_pipeline_announces_a_document_once_on_this_store(
    uow_factory: UnitOfWorkFactory, loads: tuple[Load, Load]
) -> None:
    """A broker without a catalog reads with the standard types; the document, its event
    and the queue are written together, and a second pass finds nothing pending."""
    blobs = MemoryBlobStore()
    ref = await blobs.put(b"PROOF OF DELIVERY\nPO 55821\nValley DC", "text/plain")
    document = pending("doc_1").model_copy(update={"blob_ref": ref, "content_type": "text/plain"})
    await save(uow_factory, document, pending("doc_2", load_id=B, minute=1))
    model = FakeModel()
    model.push(ModelRole.EXTRACT, CLEAN_POD, LlmError("down"))
    pipeline = DocumentPipeline(
        uow_factory=uow_factory,
        clock=VirtualClock(at(2)),
        ids=SequentialIdGenerator(),
        llm=model,
        blobs=blobs,
        pages=FakePageReader(),
        knowledge=MemorySopIndex([]),
        prompt=PROMPTS.extract,
    )

    first, second = await pipeline.process_pending()

    assert (first.data["status"], second.data["status"]) == ("processed", "failed")
    async with uow_factory() as uow:
        assert [d.status for d in await uow.documents.list_for_load(A)] == ["processed"]
        assert [d.status for d in await uow.documents.list_for_load(B)] == ["failed"]
        assert await uow.events.get("doc:doc_1") == first
        assert await uow.events.get("doc:doc_2") == second
        assert await uow.queue.size() == 2
    assert await pipeline.process_pending() == []


async def test_the_pipeline_announces_a_clean_pod_and_satisfies_the_pod_obligation(
    uow_factory: UnitOfWorkFactory, loads: tuple[Load, Load]
) -> None:
    """The pipeline's write unit of work on this store: the event, the document, the
    queue, the obligation and its timer, together."""
    blobs = MemoryBlobStore()
    ref = await blobs.put(b"PROOF OF DELIVERY\nPO 55821\nValley DC", "text/plain")
    pod = ObligationDef(kind="pod", title="POD", description="The signed POD.")
    owes_pod = DocumentRequirement(
        type_id="pod", at_stage=Stage.AT_DELIVERY, opens_obligation="pod"
    )
    pod_due = IntentDef(
        intent_id="pod_follow_up_due",
        title="POD follow-up due",
        description="The POD wait is over.",
        system=True,
    )
    broker = client_broker(
        "northline",
        CatalogOverrides(obligations=(pod,), intents=(pod_due,), documents=(owes_pod,)),
    )
    obligation = Obligation.open(
        obligation_id="obl_1", load_id=A, kind="pod", at=at(0), first_attempt=False
    )
    timer = Timer(
        timer_id="timer_1", load_id=A, obligation_id="obl_1", due_at=at(120), created_at=at(0)
    )
    document = pending("doc_1").model_copy(update={"blob_ref": ref, "content_type": "text/plain"})
    async with uow_factory() as uow:
        await uow.obligations.save(obligation)
        await uow.timers.save(timer)
        await uow.documents.save(document)
        await uow.commit()
    model = FakeModel()
    model.push(
        ModelRole.EXTRACT,
        Extraction(
            type_id="pod",
            confidence=0.95,
            fields=(
                ExtractedValue(name="receiver", value="Valley DC"),
                ExtractedValue(name="po_numbers", value="55821"),
                ExtractedValue(name="signed", value="true"),
                ExtractedValue(name="delivered_at", value="2026-10-14T09:42"),
                ExtractedValue(name="exceptions", value=""),
            ),
        ),
    )
    pipeline = DocumentPipeline(
        uow_factory=uow_factory,
        clock=VirtualClock(at(2)),
        ids=SequentialIdGenerator(),
        llm=model,
        blobs=blobs,
        pages=FakePageReader(),
        knowledge=MemorySopIndex([], brokers=[broker]),
        prompt=PROMPTS.extract,
    )

    (event,) = await pipeline.process_pending()

    async with uow_factory() as uow:
        found = await uow.documents.get("doc_1")
        assert found is not None and found.status is DocumentStatus.PROCESSED
        assert await uow.events.get("doc:doc_1") == event
        assert await uow.obligations.get("obl_1") == obligation.close(ObligationStatus.SATISFIED)
        assert await uow.timers.get("timer_1") == timer.mark(TimerStatus.CANCELLED)
        claimed = await uow.queue.claim_next(at(2))
        assert claimed is not None and claimed.event_id == "doc:doc_1"
    assert await pipeline.process_pending() == []
