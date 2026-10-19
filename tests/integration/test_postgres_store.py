"""Postgres-specific behavior of the persistence adapter, beyond the shared contract
(`tests/contract/test_unit_of_work.py`).

Several connection pools on one database stand for several worker processes: what one
pool's open transaction holds back from another, and what the database itself refuses.
Each test runs in a schema of its own, dropped afterwards.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg import sql

from freight_agent.adapters.persistence.memory.store import MemoryStore
from freight_agent.adapters.persistence.postgres.migrate import scratch_schema
from freight_agent.adapters.persistence.postgres.store import PostgresStore, open_store
from freight_agent.domain.actions import CreateHeroTask, Plan, Purpose, ScheduleTimer, SendSms
from freight_agent.domain.events import Event, EventType
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.load import Channel, Load
from freight_agent.domain.obligations import Obligation, ObligationKind
from freight_agent.domain.outbox import OutboxItem, OutboxKind
from freight_agent.domain.runs import RunRecord, RunStatus, Usage
from freight_agent.domain.timers import Timer
from freight_agent.domain.validation import Severity, Violation
from freight_agent.ports.repositories import UnitOfWork, UnitOfWorkFactory

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="set DATABASE_URL to run the Postgres tests"),
]

LA = ZoneInfo("America/Los_Angeles")
A, B, C = "481207", "550031", "660042"
ETA = ObligationKind.ETA_REQUEST


def at(minute: int) -> datetime:
    return datetime(2026, 10, 13, 7, 0, tzinfo=LA) + timedelta(minutes=minute)


def _message(event_id: str, load_id: str = A, minute: int = 0) -> Event:
    return Event(
        event_id=event_id,
        load_id=load_id,
        type=EventType.INBOUND_MESSAGE,
        occurred_at=at(minute),
        channel=Channel.SMS,
        sender_contact_id="drv_20931",
        body="ok",
    )


def _run(run_id: str, event_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id, event_id=event_id, load_id=A, started_at=at(0), status=RunStatus.ACTED
    )


def _item(outbox_id: str, key: str, minute: int = 0) -> OutboxItem:
    return OutboxItem(
        outbox_id=outbox_id,
        idempotency_key=key,
        run_id="run_1",
        load_id=A,
        kind=OutboxKind.SMS,
        payload={"contact_id": "drv_20931", "text": "ETA?", "purpose": "eta_request"},
        created_at=at(minute),
    )


@pytest.fixture
async def schema() -> AsyncIterator[str]:
    async with scratch_schema(DATABASE_URL, prefix="itest") as name:
        yield name


@pytest.fixture
async def store(schema: str) -> AsyncIterator[PostgresStore]:
    async with open_store(DATABASE_URL, schema=schema) as opened:
        yield opened


@pytest.fixture
async def other_store(schema: str) -> AsyncIterator[PostgresStore]:
    """A second pool on the same schema: another worker process."""
    async with open_store(DATABASE_URL, schema=schema) as opened:
        yield opened


@pytest.fixture
async def loads(store: PostgresStore, load: Load) -> None:
    async with store.unit_of_work() as uow:
        for load_id in (A, B, C):
            await uow.loads.save(load.model_copy(update={"load_id": load_id}))
        await uow.commit()


async def _ingest(store: PostgresStore, *events: Event) -> None:
    async with store.unit_of_work() as uow:
        for event in events:
            assert await uow.events.append(event)
            await uow.queue.enqueue(event)
        await uow.commit()


async def _claim(store: PostgresStore) -> str | None:
    async with store.unit_of_work() as uow:
        event = await uow.queue.claim_next()
        await uow.commit()
    return None if event is None else event.event_id


async def _complete(store: PostgresStore, event_id: str) -> None:
    async with store.unit_of_work() as uow:
        await uow.queue.complete(event_id)
        await uow.commit()


async def _execute(schema: str, statement: sql.SQL | sql.Composed) -> None:
    """Run one statement on a connection of its own, outside the adapter."""
    async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as conn:
        await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        await conn.execute(statement)


# --- The queue across workers ------------------------------------------------------


@pytest.mark.usefixtures("loads")
async def test_a_claim_not_yet_committed_holds_its_load_back_from_another_worker(
    store: PostgresStore, other_store: PostgresStore
) -> None:
    await _ingest(store, _message("a1"), _message("a2", minute=1), _message("b1", B, minute=2))

    async with store.unit_of_work() as first:
        claimed = await first.queue.claim_next()
        assert claimed is not None and claimed.event_id == "a1"
        # a1 is claimed in a transaction the other worker cannot see yet: it skips the
        # locked row, and a2 still waits behind a1 rather than jumping ahead of it.
        assert await _claim(other_store) == "b1"
        assert await _claim(other_store) is None
        await first.commit()

    assert await _claim(other_store) is None
    await _complete(store, "a1")
    assert await _claim(other_store) == "a2"


@pytest.mark.usefixtures("loads")
async def test_a_claim_that_loses_the_race_for_a_load_moves_on_to_another(
    store: PostgresStore, other_store: PostgresStore
) -> None:
    """Arrival order is taken at insert, visibility at commit, so an event can show up
    ahead of one that is already in flight. The second worker then picks it, and the
    partial unique index on in-flight loads makes it wait for the first claim's outcome
    and fail; the claim rolls back to its savepoint and takes the next load instead."""
    slow = store.unit_of_work()
    await slow.__aenter__()
    await slow.events.append(_message("a1"))
    await slow.queue.enqueue(_message("a1"))  # the earlier arrival, committed last
    await _ingest(other_store, _message("a2", minute=1), _message("b1", B, minute=2))

    first = store.unit_of_work()
    await first.__aenter__()
    claimed = await first.queue.claim_next()
    assert claimed is not None and claimed.event_id == "a2"  # a1 is not visible yet
    await slow.commit()
    await slow.__aexit__(None, None, None)

    second = asyncio.create_task(_claim(other_store))
    await asyncio.sleep(0.3)  # the second claim now waits on the first one's index entry
    assert not second.done()
    await first.commit()
    await first.__aexit__(None, None, None)

    assert await second == "b1"
    async with store.unit_of_work() as uow:
        assert await uow.queue.size() == 1  # a1 still waits behind a2


@pytest.mark.usefixtures("loads")
async def test_workers_on_two_pools_never_run_two_events_of_one_load_at_once(
    store: PostgresStore, other_store: PostgresStore
) -> None:
    per_load = 20
    events = [
        _message(f"{load_id}:{n}", load_id, minute=n)
        for n in range(per_load)
        for load_id in (A, B, C)
    ]
    await _ingest(store, *events)
    in_flight: set[str] = set()
    processed: dict[str, list[str]] = {A: [], B: [], C: []}

    async def worker(pool: PostgresStore) -> None:
        while sum(len(done) for done in processed.values()) < len(events):
            async with pool.unit_of_work() as uow:
                event = await uow.queue.claim_next()
                await uow.commit()
            if event is None:
                await asyncio.sleep(0.005)
                continue
            assert event.load_id not in in_flight, "two events of one load in flight"
            in_flight.add(event.load_id)
            processed[event.load_id].append(event.event_id)
            await asyncio.sleep(0.001)
            in_flight.discard(event.load_id)  # before the completion commits
            await _complete(pool, event.event_id)

    workers = [worker(pool) for pool in (store, other_store) for _ in range(4)]
    await asyncio.wait_for(asyncio.gather(*workers), timeout=30)

    for load_id, done in processed.items():
        assert done == [f"{load_id}:{n}" for n in range(per_load)]


@pytest.mark.usefixtures("loads")
async def test_the_scheduler_skips_a_timer_whose_obligation_a_run_holds(
    store: PostgresStore, other_store: PostgresStore
) -> None:
    """A run locks the obligation, then its timers; the scheduler locks a timer, then
    reads its obligation. Were the scheduler to wait, the two could deadlock, so it
    leaves the timer for its next tick."""
    obligation = Obligation.open(
        obligation_id="obl_1", load_id=A, kind=ETA, at=at(0), first_attempt=True
    )
    timer = Timer(
        timer_id="timer_1", load_id=A, obligation_id="obl_1", due_at=at(30), created_at=at(0)
    )
    async with store.unit_of_work() as uow:
        await uow.obligations.save(obligation.attach_timer("timer_1"))
        await uow.timers.save(timer)
        await uow.commit()

    async with store.unit_of_work() as run:
        assert await run.obligations.get_open(A, ETA) is not None
        async with other_store.unit_of_work() as scheduler:
            assert await asyncio.wait_for(scheduler.timers.due(at(30)), timeout=2) == []

    async with other_store.unit_of_work() as scheduler:
        assert await scheduler.timers.due(at(30)) == [timer]


@pytest.mark.usefixtures("loads")
async def test_the_database_refuses_a_second_event_of_a_load_in_flight(
    store: PostgresStore, schema: str
) -> None:
    await _ingest(store, _message("a1"), _message("a2", minute=1))
    assert await _claim(store) == "a1"

    with pytest.raises(psycopg.errors.UniqueViolation):
        await _execute(schema, sql.SQL("UPDATE event_queue SET state = 'in_flight'"))


# --- Duplicates and the open transaction ---------------------------------------------


@pytest.mark.usefixtures("loads")
async def test_a_rejected_duplicate_leaves_the_transaction_usable_until_commit(
    store: PostgresStore,
) -> None:
    async with store.unit_of_work() as uow:
        await uow.events.append(_message("m1"))
        await uow.runs.save(_run("run_1", "m1"))
        await uow.outbox.add(_item("out_1", "m1:0"))
        await uow.commit()

    async with store.unit_of_work() as uow:
        # A failed INSERT would abort the transaction: every later statement would be
        # refused and the commit would roll everything back.
        assert await uow.events.append(_message("m1", minute=5)) is False
        assert await uow.outbox.add(_item("out_2", "m1:0")) is False
        assert await uow.events.append(_message("m2", minute=1)) is True
        assert await uow.outbox.add(_item("out_3", "m1:1")) is True
        await uow.commit()

    async with store.unit_of_work() as uow:
        assert [e.event_id for e in await uow.events.recent(A, 10)] == ["m1", "m2"]
        assert [i.outbox_id for i in await uow.outbox.list_for_load(A)] == ["out_1", "out_3"]


@pytest.mark.usefixtures("loads")
async def test_the_same_event_appended_by_two_workers_at_once_is_stored_once(
    store: PostgresStore, other_store: PostgresStore
) -> None:
    async def append(pool: PostgresStore) -> bool:
        async with pool.unit_of_work() as uow:
            added = await uow.events.append(_message("m1"))
            await asyncio.sleep(0.05)  # both inserts are in flight together
            await uow.commit()
        return added

    results = await asyncio.gather(append(store), append(other_store))
    assert sorted(results) == [False, True]


@pytest.mark.usefixtures("loads")
async def test_the_event_log_is_append_only(store: PostgresStore, schema: str) -> None:
    await _ingest(store, _message("m1"))
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        await _execute(schema, sql.SQL("UPDATE events SET body = 'changed'"))
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        await _execute(schema, sql.SQL("DELETE FROM events"))


async def test_a_unit_of_work_that_breaks_a_foreign_key_fails_at_commit(
    store: PostgresStore,
) -> None:
    """Foreign keys are checked when the unit of work commits, so the order of the
    writes inside it is free, and an orphan row never lands."""
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        async with store.unit_of_work() as uow:
            await uow.events.append(_message("m1", load_id="no_such_load"))
            await uow.commit()

    async with store.unit_of_work() as uow:
        assert await uow.events.get("m1") is None


@pytest.mark.usefixtures("loads")
async def test_a_unit_of_work_whose_statement_failed_refuses_to_commit(
    store: PostgresStore,
) -> None:
    """Postgres answers COMMIT on a failed transaction with a silent ROLLBACK. A caller
    that caught the error and carried on must learn that nothing was saved."""
    async with store.unit_of_work() as uow:
        await uow.events.append(_message("m1"))
        await uow.runs.save(_run("run_1", "m1"))
        await uow.outbox.add(_item("out_1", "m1:0"))
        await uow.commit()

    with pytest.raises(psycopg.errors.InFailedSqlTransaction):
        async with store.unit_of_work() as uow:
            await uow.events.append(_message("m2", minute=1))
            with pytest.raises(psycopg.errors.UniqueViolation):
                await uow.outbox.save(_item("out_2", "m1:0"))  # another item's key
            await uow.commit()

    async with store.unit_of_work() as uow:
        assert await uow.events.get("m2") is None


# --- Mapping and order -----------------------------------------------------------------


@pytest.mark.usefixtures("loads")
async def test_a_timestamp_comes_back_as_the_same_instant_in_utc(store: PostgresStore) -> None:
    """Not in the server's TimeZone setting, so that text made from it (an ISO string in
    an event's data) is the same on every server."""
    async with store.unit_of_work() as uow:
        await uow.events.append(_message("m1", minute=31))
        await uow.commit()

    async with store.unit_of_work() as uow:
        event = await uow.events.get("m1")
    assert event is not None and event.occurred_at == at(31)
    assert event.occurred_at.isoformat() == "2026-10-13T14:31:00+00:00"


@pytest.mark.usefixtures("loads")
async def test_a_run_record_round_trips_whole(store: PostgresStore) -> None:
    run = RunRecord(
        run_id="run_1",
        event_id="m1",
        load_id=A,
        started_at=at(1),
        finished_at=at(2),
        status=RunStatus.ESCALATED,
        gate=GateDecision(intent=Intent.QUESTION, act=True, reason="asks for the PO"),
        sop_section_ids=("driver_questions", "escalation"),
        plan=Plan(
            actions=(
                SendSms(contact_id="drv_20931", text="PO 55821", purpose=Purpose.REPLY),
                ScheduleTimer(kind=ObligationKind.ETA_REQUEST, due_in_minutes=30),
                CreateHeroTask(title="Call the driver", description="", urgent=True),
            ),
            rationale="answer, then follow up",
        ),
        violations=(
            Violation(rule="no_phone", action_index=0, message="m", severity=Severity.HIGH),
        ),
        usage=(Usage(role="gate", model="m", input_tokens=12, cost_usd=0.000123),),
        trace_url="https://trace/1",
    )
    async with store.unit_of_work() as uow:
        await uow.events.append(_message("m1"))
        await uow.runs.save(run)
        await uow.commit()

    async with store.unit_of_work() as uow:
        assert await uow.runs.get("run_1") == run


async def _outbox_order(
    uow_factory: UnitOfWorkFactory, load: Load, ids: Sequence[str]
) -> list[str]:
    async with uow_factory() as uow:
        await _seed_one_run(uow, load)
        for n, outbox_id in enumerate(ids):
            await uow.outbox.add(_item(outbox_id, f"m1:{n}"))
        await uow.commit()
    async with uow_factory() as uow:
        return [i.outbox_id for i in await uow.outbox.list_for_load(A)]


async def _seed_one_run(uow: UnitOfWork, load: Load) -> None:
    await uow.loads.save(load)
    await uow.events.append(_message("m1"))
    await uow.runs.save(_run("run_1", "m1"))


async def test_ties_sort_by_natural_id_and_code_point_as_in_the_memory_store(
    store: PostgresStore, load: Load
) -> None:
    """The database collation would put `out_a` before `out_B`, and plain text order
    would put `out_10` before `out_9`; the memory store does neither."""
    ids = ["out_a", "out_10", "out_B", "out_9", "out_2_1", "out_", "7"]
    memory = await _outbox_order(MemoryStore().unit_of_work, load, ids)
    assert memory == ["7", "out_9", "out_10", "out_", "out_2_1", "out_B", "out_a"]
    assert await _outbox_order(store.unit_of_work, load, ids) == memory
