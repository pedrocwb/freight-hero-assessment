"""Facility memory: the facts a run reads about its stops, and the extraction that writes them
after a load is delivered.

Read path. A run reads the facts of its load's current and next stop
(`facts_in_view`) in the graph's knowledge node, outside any unit of work, as it reads
the SOP; the agent's prompt shows them as notes from earlier loads, untrusted, and the
`recall_facility` tool searches the client's facts by meaning
(`application/agent_tools.py`). A read that fails is logged and the run goes on without
notes: the memory is help, never a reason to stop a run.

Write path. When a run sets a load to `delivered`, the runtime queues an
`ExtractionJob` for it after its commit, and the runtime's tick works through the queue
(`MemoryExtractor.process_pending`), after the outbox, like the document pipeline:

1. one short unit of work reads the load and its event log; the facts of its stops come
   from the memory, by client;
2. outside any transaction, one `memory` call, on the memory template
   (`prompts/memory.yaml`), reads the load's inbound messages and Hero updates (each with
   its id and the stops it may be about) and the facilities' current facts, and answers
   add, update and delete operations, each citing the event it comes from
   (`domain.memory.MemoryExtraction`);
3. code checks every operation (`domain.memory.check_operation`: the cited event is an
   inbound event of this load about that stop, no phone, email, money or instruction,
   a short text, an existing fact for an update or a delete, the facility's cap) and
   writes the ones that pass, all in the load's client namespace. The job records what
   was added, updated, deleted and dropped, with why, and the call's cost.

A model failure leaves the job pending for the next tick, up to `MAX_EXTRACTION_ATTEMPTS`
tries; then it is `failed`. A load without any inbound text is done with no model call.

LangMem's memory manager was considered for step 2 and not used: it calls a LangChain
chat model directly, so the call would skip this project's model interface (the fallback
chain, the retries, the cost in `Usage`), its instructions are its own rather than a
template versioned in the Prompt Hub, and its operations do not cite a source event, which
the guardrails check. The extraction is one structured call; it is written here.
"""

import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC
from zoneinfo import ZoneInfo

from freight_agent.application.prompts import check_variables, learned_on
from freight_agent.domain.events import Event, EventType
from freight_agent.domain.load import Load
from freight_agent.domain.memory import (
    FACTS_PER_STOP,
    MAX_EXTRACTION_ATTEMPTS,
    MAX_FACT_CHARS,
    MAX_FACTS_PER_FACILITY,
    MAX_OPERATIONS,
    ExtractionJob,
    ExtractionStatus,
    FacilityFact,
    FactDelete,
    FactKind,
    MemoryExtraction,
    ProposedOperation,
    check_operation,
    facility_key,
    source_events,
    stop_by_key,
    stops_in_view,
)
from freight_agent.interfaces.clock import Clock, IdGenerator
from freight_agent.interfaces.llm import LanguageModel, LlmError, LlmRequest, ModelRole
from freight_agent.interfaces.memory import ExtractionQueue, FacilityMemory
from freight_agent.interfaces.prompts import ChatPrompt
from freight_agent.interfaces.repositories import UnitOfWorkFactory

logger = logging.getLogger(__name__)

# The events of a load the extraction reads at most (a load has tens).
EVENTS_LIMIT = 500
# The characters of one message the memory model reads at most.
MESSAGE_TEXT_LIMIT = 600

MEMORY_SYSTEM_VARIABLES = frozenset({"kinds", "max_chars", "max_operations"})
MEMORY_USER_VARIABLES = frozenset({"load_id", "facilities", "messages"})

_KIND_MEANINGS: Mapping[FactKind, str] = {
    FactKind.CHECK_IN: "where and how drivers check in (a gate, a window, a door)",
    FactKind.HOURS: "when the facility receives, ships or closes",
    FactKind.REQUIREMENT: "what the facility asks for (a PO at the gate, a seal, a pallet type)",
    FactKind.ACCESS: "how to get in or around (an entrance, parking, a dock door)",
    FactKind.CONTACT: "whom to ask for there (a role or a desk, never a phone number)",
    FactKind.OTHER: "anything else that will help the next driver at this facility",
}


def check_memory_prompt(prompt: ChatPrompt) -> ChatPrompt:
    """`prompt`, or `PromptError` when it names a variable the extraction does not compute."""
    return check_variables(prompt, system=MEMORY_SYSTEM_VARIABLES, user=MEMORY_USER_VARIABLES)


# --- the read path ------------------------------------------------------------------------


async def facts_in_view(memory: FacilityMemory | None, load: Load) -> tuple[FacilityFact, ...]:
    """The facts of the load's current and next stop, for its client, the most recent
    first per stop. Fail-open: a memory that cannot be read gives none."""
    if memory is None:
        return ()
    found: list[FacilityFact] = []
    try:
        for stop in stops_in_view(load):
            found += await memory.recall(load.broker_id, facility_key(stop), limit=FACTS_PER_STOP)
    except Exception:
        logger.warning(
            "the facility memory of load %s could not be read", load.load_id, exc_info=True
        )
        return ()
    return tuple(found)


# --- the write path -----------------------------------------------------------------------


class NullMemoryProcessor:
    """Implements `interfaces.memory.MemoryProcessor` by extracting nothing (a runtime
    without facility memory)."""

    async def process_pending(self, limit: int = 5) -> list[ExtractionJob]:
        return []


class MemoryExtractor:
    """Implements `interfaces.memory.MemoryProcessor`; see the module docstring."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdGenerator,
        llm: LanguageModel,
        memory: FacilityMemory,
        queue: ExtractionQueue,
        prompt: ChatPrompt,
        timeout_s: float = 60.0,
    ) -> None:
        """`PromptError` when the template does not fit (`check_memory_prompt`)."""
        self._prompt = check_memory_prompt(prompt)
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._llm = llm
        self._memory = memory
        self._queue = queue
        self._timeout_s = timeout_s

    @property
    def prompt(self) -> ChatPrompt:
        return self._prompt

    async def process_pending(self, limit: int = 5) -> list[ExtractionJob]:
        """Extract up to `limit` queued loads; return the jobs that finished (done or
        failed for good). One job that breaks never stops the others."""
        finished: list[ExtractionJob] = []
        for job in await self._queue.pending(limit):
            try:
                result = await self.extract(job)
            except Exception:
                logger.exception(
                    "the facility memory of load %s could not be extracted", job.load_id
                )
                continue
            if result.status is not ExtractionStatus.PENDING:
                finished.append(result)
        return finished

    async def extract_load(self, client_id: str, load_id: str) -> ExtractionJob:
        """Extract one load now, queued or not, done before or not (`agent memory extract`)."""
        job = await self._queue.get(client_id, load_id) or ExtractionJob(
            load_id=load_id, client_id=client_id, queued_at=self._clock.now()
        )
        fresh = job.model_copy(
            update={"status": ExtractionStatus.PENDING, "attempts": 0, "error": None}
        )
        return await self.extract(fresh)

    async def extract(self, job: ExtractionJob) -> ExtractionJob:
        """One try at one job; the job as saved after it."""
        async with self._uow_factory() as uow:
            load = await uow.loads.get(job.load_id)
            events = await uow.events.recent(job.load_id, EVENTS_LIMIT) if load else []
        if load is None or load.broker_id != job.client_id:
            return await self._save(
                job, failed=f"load {job.load_id!r} of {job.client_id!r} is unknown"
            )
        sources = source_events(load, events)
        if not sources:
            return await self._save(job, done=True)
        facts = {
            key: await self._memory.recall(load.broker_id, key, limit=MAX_FACTS_PER_FACILITY)
            for key in stop_by_key(load)
        }
        request = LlmRequest(
            role=ModelRole.MEMORY,
            system=memory_system(self._prompt),
            user=memory_user(self._prompt, load, sources, facts),
            timeout_s=self._timeout_s,
            prompt_version=self._prompt.version,
        )
        try:
            response = await self._llm.generate(request, MemoryExtraction)
        except LlmError as exc:
            attempts = job.attempts + 1
            error = f"the memory model failed: {exc}"[:300]
            logger.warning("facility memory of load %s: %s (try %d)", load.load_id, error, attempts)
            if attempts >= MAX_EXTRACTION_ATTEMPTS:
                return await self._save(job.model_copy(update={"attempts": attempts}), failed=error)
            update = {"attempts": attempts, "error": error}
            return await self._save(job.model_copy(update=update))
        applied = await self._apply(load, sources, facts, response.parsed.operations)
        cost = job.cost_usd + response.usage.cost_usd
        done = job.model_copy(update={**applied, "cost_usd": cost})
        logger.info(
            "facility memory of load %s: %d added, %d updated, %d deleted, %d dropped ($%.5f)",
            load.load_id,
            done.added,
            done.updated,
            done.deleted,
            len(done.dropped),
            response.usage.cost_usd,
        )
        return await self._save(done, done=True)

    async def _apply(
        self,
        load: Load,
        sources: Mapping[str, tuple[Event, frozenset[str]]],
        facts: Mapping[str, Sequence[FacilityFact]],
        operations: Sequence[ProposedOperation],
    ) -> dict[str, object]:
        """Check each operation against the facts as the earlier ones left them, and write
        those that pass, in the load's client namespace."""
        now = self._clock.now()
        stops = stop_by_key(load)
        current = {key: list(found) for key, found in facts.items()}
        upserts: dict[str, FacilityFact] = {}
        deletes: list[FactDelete] = []
        dropped: list[str] = []
        counts = {"added": 0, "updated": 0, "deleted": 0}
        for index, op in enumerate(operations):
            if index >= MAX_OPERATIONS:
                dropped.append(f"{op.op} {op.facility_key}: over {MAX_OPERATIONS} operations")
                continue
            reason = check_operation(op, load=load, sources=sources, facts=current)
            if reason is not None:
                dropped.append(f"{op.op} {op.facility_key}: {reason}")
                continue
            known = current[op.facility_key]
            if op.op == "delete":
                known[:] = [f for f in known if f.fact_id != op.fact_id]
                upserts.pop(op.fact_id or "", None)
                deletes.append(
                    FactDelete(
                        client_id=load.broker_id,
                        facility_key=op.facility_key,
                        fact_id=op.fact_id or "",
                    )
                )
                counts["deleted"] += 1
                continue
            event = sources[op.event_id][0]
            previous = next((f for f in known if f.fact_id == op.fact_id), None)
            fact = FacilityFact(
                fact_id=previous.fact_id if previous else self._ids.new("fact"),
                client_id=load.broker_id,
                facility_key=op.facility_key,
                facility_name=stops[op.facility_key].name,
                text=(op.text or "").strip(),
                kind=op.kind,
                source_load_id=load.load_id,
                source_event_id=event.event_id,
                source_at=event.occurred_at,
                created_at=previous.created_at if previous else now,
                updated_at=now,
            )
            known[:] = [*(f for f in known if f.fact_id != fact.fact_id), fact]
            upserts[fact.fact_id] = fact
            counts["updated" if previous else "added"] += 1
        await self._memory.apply(load.broker_id, upserts=tuple(upserts.values()), deletes=deletes)
        return {**counts, "dropped": tuple(dropped)}

    async def _save(
        self, job: ExtractionJob, *, done: bool = False, failed: str | None = None
    ) -> ExtractionJob:
        if failed is not None:
            job = job.model_copy(
                update={
                    "status": ExtractionStatus.FAILED,
                    "error": failed,
                    "finished_at": self._clock.now(),
                }
            )
        elif done:
            job = job.model_copy(
                update={
                    "status": ExtractionStatus.DONE,
                    "error": None,
                    "finished_at": self._clock.now(),
                }
            )
        await self._queue.save(job)
        return job


# --- the memory prompt --------------------------------------------------------------------


def memory_system(prompt: ChatPrompt) -> str:
    return prompt.system.format(
        {
            "kinds": "\n".join(f"- {k.value}: {_KIND_MEANINGS[k]}" for k in FactKind),
            "max_chars": str(MAX_FACT_CHARS),
            "max_operations": str(MAX_OPERATIONS),
        }
    )


def memory_user(
    prompt: ChatPrompt,
    load: Load,
    sources: Mapping[str, tuple[Event, frozenset[str]]],
    facts: Mapping[str, Sequence[FacilityFact]],
) -> str:
    """The load's facilities with their facts, and its messages. Every text that came from
    outside (a message, a fact, a stop name) is a JSON string."""
    return prompt.user.format(
        {
            "load_id": load.load_id,
            "facilities": "\n".join(_facility_lines(load, facts)),
            "messages": "\n".join(_message_lines(load, sources)),
        }
    )


def _facility_lines(load: Load, facts: Mapping[str, Sequence[FacilityFact]]) -> list[str]:
    lines: list[str] = []
    for stop in sorted(load.stops, key=lambda s: s.sequence):
        key = facility_key(stop)
        lines.append(
            f"- facility_key {key}: stop {stop.sequence}, {stop.type.value}, "
            f"{_quote(stop.name)}, {_quote(stop.address)}"
        )
        known = facts.get(key, ())
        if not known:
            lines.append("  facts: none")
            continue
        lines.append("  facts:")
        lines.extend(
            f"  - fact_id {f.fact_id} ({f.kind.value}, told {learned_on(f, stop)}): "
            f"{_quote(f.text)}"
            for f in known
        )
    return lines


def _message_lines(load: Load, sources: Mapping[str, tuple[Event, frozenset[str]]]) -> list[str]:
    """Times are local to the first stop, as the load's day starts there."""
    zone = ZoneInfo(min(load.stops, key=lambda s: s.sequence).timezone)
    lines: list[str] = []
    for event_id, (event, keys) in sources.items():
        about = ", ".join(sorted(keys))
        sender = _sender(load, event)
        moment = event.occurred_at
        when = (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(zone)
        text = event.body.strip()
        if len(text) > MESSAGE_TEXT_LIMIT:
            text = text[: MESSAGE_TEXT_LIMIT - 1].rstrip() + "…"
        lines.append(
            f"- event_id {event_id} ({when:%Y-%m-%d %H:%M}, {event.type.value} from {sender}; "
            f"about: {about}): {_quote(text)}"
        )
    return lines


def _sender(load: Load, event: Event) -> str:
    contact = load.contact(event.sender_contact_id) if event.sender_contact_id else None
    if contact is not None:
        return contact.role.value
    return "a Hero" if event.type is EventType.HERO_UPDATE else "an unknown sender"


def _quote(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)
