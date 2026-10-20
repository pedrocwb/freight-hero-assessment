from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from freight_agent.adapters.persistence.memory.store import MemoryStore
from freight_agent.domain.documents import Document, DocumentStatus
from freight_agent.domain.events import Event, EventType
from freight_agent.domain.load import Channel

LA = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 10, 13, 7, 0, tzinfo=LA)


def _event(event_id: str, load_id: str = "481207", minutes: int = 0) -> Event:
    return Event(
        event_id=event_id,
        load_id=load_id,
        type=EventType.INBOUND_MESSAGE,
        occurred_at=T0 + timedelta(minutes=minutes),
        channel=Channel.SMS,
        sender_contact_id="drv_20931",
        body="ok",
    )


@pytest.mark.unit
async def test_uncommitted_work_is_rolled_back() -> None:
    store = MemoryStore()
    async with store.unit_of_work() as uow:
        await uow.events.append(_event("m1"))

    async with store.unit_of_work() as uow:
        assert await uow.events.get("m1") is None


@pytest.mark.unit
async def test_an_exception_rolls_back_committed_looking_work() -> None:
    store = MemoryStore()
    with pytest.raises(RuntimeError):
        async with store.unit_of_work() as uow:
            await uow.events.append(_event("m1"))
            await uow.commit()
            raise RuntimeError("boom")

    async with store.unit_of_work() as uow:
        assert await uow.events.get("m1") is None


@pytest.mark.unit
async def test_duplicate_event_ids_are_rejected() -> None:
    store = MemoryStore()
    async with store.unit_of_work() as uow:
        assert await uow.events.append(_event("m1")) is True
        assert await uow.events.append(_event("m1")) is False
        await uow.commit()


@pytest.mark.unit
async def test_queue_never_hands_out_two_events_of_the_same_load() -> None:
    store = MemoryStore()
    async with store.unit_of_work() as uow:
        await uow.queue.enqueue(_event("a1", "A", 0))
        await uow.queue.enqueue(_event("a2", "A", 1))
        await uow.queue.enqueue(_event("b1", "B", 2))
        first = await uow.queue.claim_next(T0)
        second = await uow.queue.claim_next(T0)
        third = await uow.queue.claim_next(T0)
        await uow.commit()

    assert first is not None and first.event_id == "a1"
    assert second is not None and second.event_id == "b1"
    assert third is None


@pytest.mark.unit
async def test_released_event_keeps_its_place_and_dead_letters_after_max_attempts() -> None:
    store = MemoryStore(max_queue_attempts=2)
    async with store.unit_of_work() as uow:
        await uow.queue.enqueue(_event("a1", "A", 0))
        await uow.queue.enqueue(_event("a2", "A", 1))
        claimed = await uow.queue.claim_next(T0)
        assert claimed is not None
        await uow.queue.release("a1", "llm timeout")
        again = await uow.queue.claim_next(T0)
        assert again is not None and again.event_id == "a1"
        await uow.queue.release("a1", "llm timeout")
        dead = await uow.queue.dead_letters()
        nxt = await uow.queue.claim_next(T0)
        await uow.commit()

    assert [e.event_id for e in dead] == ["a1"]
    assert nxt is not None and nxt.event_id == "a2"


@pytest.mark.unit
async def test_ties_on_time_follow_natural_id_order(store: MemoryStore) -> None:
    """`out_10` is created after `out_9`; text order would deliver it first."""
    from freight_agent.domain.outbox import OutboxItem, OutboxKind

    async with store.unit_of_work() as uow:
        for n in (9, 10, 2):
            await uow.outbox.add(
                OutboxItem(
                    outbox_id=f"out_{n}",
                    idempotency_key=f"e:{n}",
                    run_id="run_1",
                    load_id="481207",
                    kind=OutboxKind.SMS,
                    created_at=T0,
                )
            )
        await uow.commit()

    async with store.unit_of_work() as uow:
        pending = await uow.outbox.pending()

    assert [i.outbox_id for i in pending] == ["out_2", "out_9", "out_10"]


@pytest.mark.unit
def test_virtual_clock_advances_real_time_across_the_fall_back() -> None:
    from datetime import UTC

    from freight_agent.adapters.clock import VirtualClock

    clock = VirtualClock(datetime(2026, 11, 1, 1, 55, tzinfo=LA))

    clock.advance(timedelta(minutes=15))

    assert clock.now().astimezone(UTC) == datetime(2026, 11, 1, 9, 10, tzinfo=UTC)


# --- the wave 3 document repository on the same transactions (C0; its card's contract
# suite pins it down) ----------------------------------------------------------------------


@pytest.mark.unit
async def test_documents_are_listed_per_load_and_pending_oldest_first() -> None:
    store = MemoryStore()

    def document(document_id: str, minutes: int, load_id: str = "481207") -> Document:
        return Document.pending(
            document_id=document_id,
            load_id=load_id,
            attachment_id=f"a_{document_id}",
            broker_id="northline",
            blob_ref="r",
            filename="pod.pdf",
            content_type="application/pdf",
            received_at=T0 + timedelta(minutes=minutes),
        )

    later, earlier, other = document("doc_2", 5), document("doc_1", 1), document("doc_3", 3, "9")
    async with store.unit_of_work() as uow:
        for item in (later, earlier, other):
            await uow.documents.save(item)
        await uow.documents.save(later.model_copy(update={"status": DocumentStatus.PROCESSED}))
        await uow.commit()

    async with store.unit_of_work() as uow:
        assert [d.document_id for d in await uow.documents.list_for_load("481207")] == [
            "doc_1",
            "doc_2",
        ]
        assert [d.document_id for d in await uow.documents.list_pending()] == ["doc_1", "doc_3"]
        assert [d.document_id for d in await uow.documents.list_pending(limit=1)] == ["doc_1"]
        assert await uow.documents.for_attachment("481207", "a_doc_1") == earlier
        assert await uow.documents.for_attachment("9", "a_doc_1") is None
        assert await uow.documents.get("doc_3") == other
