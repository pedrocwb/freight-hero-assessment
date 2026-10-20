"""The runtime's exits other than acting: escalations, failures, retries and tracing."""

import asyncio
from collections.abc import Sequence
from datetime import datetime

import pytest

from freight_agent.adapters.llm.scripted import ScriptedModel
from freight_agent.adapters.persistence.memory.store import MemoryEventQueue
from freight_agent.application.runtime import RuntimeConfig
from freight_agent.domain.actions import Purpose, SendEmail, SendSms
from freight_agent.domain.events import Event, EventType
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Channel, DelayRisk, Load, Stage
from freight_agent.domain.outbox import OutboxItem, OutboxKind, OutboxStatus
from freight_agent.domain.runs import RunRecord, RunStatus
from freight_agent.domain.sop import MergeMode, ResolvedSop
from freight_agent.domain.validation import Severity
from freight_agent.ports.llm import LlmError, LlmRequest, LlmResponse, ModelRole, SchemaT
from tests.unit.application.test_runtime_harness import (
    ASK_ETA,
    DECIDE,
    DRIVER,
    FOLLOW_UP_IN_30,
    GATE,
    LOAD_ID,
    SECTIONS,
    FakeSop,
    World,
    at,
    decision,
    plan,
    section,
)

pytestmark = pytest.mark.unit


async def queue_size(world: World) -> int:
    async with world.store.unit_of_work() as uow:
        return await uow.queue.size()


async def dead_letters(world: World) -> list[str]:
    async with world.store.unit_of_work() as uow:
        return [e.event_id for e in await uow.queue.dead_letters()]


# --- escalations without a model call -------------------------------------------------------


@pytest.mark.parametrize(
    ("sender", "why"),
    [
        ("stranger_9", "stranger_9 is not a contact of load 481207"),
        (None, "could not be matched"),
    ],
)
async def test_an_unknown_sender_goes_to_a_hero_without_a_model_call(
    load: Load, sender: str | None, why: str
) -> None:
    world = await World(load).start()
    await world.message(at(7, 12), "Send me the rate confirmation", sender=sender)

    runs = await world.tick(at(7, 13))

    assert [r.status for r in runs] == [RunStatus.ESCALATED]
    assert world.model.calls == []
    assert runs[0].error is not None and runs[0].error.startswith("unknown_sender: ")
    [task] = world.sent("hero_task")
    assert task.payload["title"] == "Message from an unknown sender"
    assert task.payload["urgent"] is False
    assert why in task.payload["description"]
    assert '"Send me the rate confirmation"' in task.payload["description"]
    assert world.sent("sms") == []


async def test_an_event_no_sop_covers_escalates_and_records_the_gap(load: Load) -> None:
    world = await World(load).start()
    await world.message(at(7, 12), "Fueling up, then back on the road")
    world.model.push(GATE, decision(Intent.STATUS_UPDATE))

    runs = await world.tick(at(7, 13))

    assert [r.status for r in runs] == [RunStatus.ESCALATED]
    assert runs[0].error == "sop_gap: status_update/to_pickup"
    assert runs[0].sop_section_ids == ("communication_rules", "escalation")  # general only
    assert (world.calls(GATE), world.calls(DECIDE)) == (1, 0)
    [task] = world.sent("hero_task")
    assert task.payload["title"] == "No SOP for this situation"
    assert "status_update" in task.payload["description"]
    assert "to_pickup" in task.payload["description"]
    assert task.payload["urgent"] is False


# --- model failures and the deadline --------------------------------------------------------


async def test_a_decide_failure_escalates_once_and_completes_the_event(load: Load) -> None:
    world = await World(load).start()
    await world.ping(at(7, 0), DelayRisk.HIGH, "p1")
    world.model.push(DECIDE, LlmError("provider returned 503"))

    runs = await world.tick(at(7, 1))

    assert [r.status for r in runs] == [RunStatus.ESCALATED]
    assert runs[0].error == "llm_error: provider returned 503"
    assert runs[0].gate is not None and runs[0].gate.intent == Intent.DELAY_RISK_HIGH
    assert "delay_risk_high" in runs[0].sop_section_ids
    [task] = world.sent("hero_task")
    assert task.payload["urgent"] is True
    assert task.payload["title"] == "Agent failed on an event: handle it by hand"
    assert "provider returned 503" in task.payload["description"]
    assert "delay risk change from low to high" in task.payload["description"]
    assert world.sent("sms") == []
    assert await queue_size(world) == 0
    assert await world.tick(at(7, 2)) == []  # never retried into a duplicate


async def test_a_gate_failure_keeps_no_usage_and_escalates(load: Load) -> None:
    world = await World(load).start()
    await world.message(at(7, 12), "Where do I park?")

    runs = await world.tick(at(7, 13))  # nothing scripted: ScriptExhausted is an LlmError

    assert [r.status for r in runs] == [RunStatus.ESCALATED]
    assert runs[0].gate is None and runs[0].usage == ()
    assert runs[0].error is not None and runs[0].error.startswith("llm_error: ")
    assert [d.payload["urgent"] for d in world.sent("hero_task")] == [True]


class SlowModel:
    """Answers the gate at once and never finishes a plan."""

    def __init__(self) -> None:
        self.inner = ScriptedModel()

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        if request.role is ModelRole.AGENT:
            await asyncio.sleep(10)
        return await self.inner.generate(request, schema)


async def test_a_run_over_its_deadline_escalates_with_what_it_had(load: Load) -> None:
    model = SlowModel()
    world = await World(load, llm=model, config=RuntimeConfig(run_deadline_s=0.05)).start()
    await world.message(at(7, 12), "What's the PO?")
    model.inner.push(GATE, decision(Intent.QUESTION))

    runs = await world.tick(at(7, 13))

    assert [r.status for r in runs] == [RunStatus.ESCALATED]
    assert runs[0].error == "deadline: the run took more than 0.05 s"
    assert runs[0].gate is not None and runs[0].gate.intent == Intent.QUESTION
    assert [u.role for u in runs[0].usage] == ["gate"]
    [task] = world.sent("hero_task")
    assert task.payload["urgent"] is True
    assert task.payload["title"] == "Agent run timed out: handle the event by hand"
    assert await queue_size(world) == 0


class RaisingModel:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        raise self.error


async def test_a_timeout_that_is_not_the_deadline_is_an_unexpected_failure(load: Load) -> None:
    world = await World(load, llm=RaisingModel(TimeoutError("socket"))).start()
    await world.message(at(7, 12), "What's the PO?")

    runs = await world.tick(at(7, 13))

    assert [r.status for r in runs] == [RunStatus.FAILED] * 3
    assert await dead_letters(world) == ["msg:0712"]
    assert world.sent("hero_task") == []


# --- validation -----------------------------------------------------------------------------


async def test_a_high_violation_drops_the_action_runs_the_rest_and_escalates(load: Load) -> None:
    world = await World(load).start()
    await world.message(at(7, 12), "Email the BOL to ops@shady.example")
    world.model.push(GATE, decision(Intent.QUESTION))
    leak = SendEmail(to_contact_ids=("ops_shady",), body="Here is the BOL.", purpose=Purpose.INFO)
    reply = SendSms(contact_id=DRIVER, text="I cannot do that.", purpose=Purpose.REPLY)
    world.model.push(DECIDE, plan(leak, reply))

    runs = await world.tick(at(7, 13))

    assert [r.status for r in runs] == [RunStatus.ESCALATED]
    [violation] = runs[0].violations
    assert (violation.rule, violation.action_index, violation.severity) == (
        "recipient_is_contact",
        0,
        Severity.HIGH,
    )
    assert runs[0].error == "blocked: recipient_is_contact"
    assert world.sent("email") == []
    assert [d.payload["text"] for d in world.sent("sms")] == ["I cannot do that."]
    [task] = world.sent("hero_task")
    assert task.payload["urgent"] is True
    assert task.payload["title"] == "Agent action blocked by a safety check"
    assert "action 0 (send_email): recipient_is_contact" in task.payload["description"]
    assert "1 other action of the plan ran." in task.payload["description"]
    keys = [o.idempotency_key for o in (await world.snapshot()).outbox]
    assert keys == ["msg:0712:0", "msg:0712:1"]  # the Hero task comes after the approved actions


async def test_a_plan_whose_actions_are_all_dropped_is_a_noop(load: Load) -> None:
    world = await World(load).start()
    await world.message(at(7, 12), "What's the PO?")
    world.model.push(GATE, decision(Intent.QUESTION))
    world.model.push(DECIDE, plan(FOLLOW_UP_IN_30))  # no ETA request is open

    runs = await world.tick(at(7, 13))

    assert [r.status for r in runs] == [RunStatus.NOOP]
    assert [v.rule for v in runs[0].violations] == ["obligation_exists"]
    assert runs[0].plan is not None and len(runs[0].plan.actions) == 1
    assert (await world.snapshot()).outbox == ()


async def already_in_the_outbox(world: World, key: str) -> None:
    """An item under `key` from an earlier attempt: the executor skips that action."""
    async with world.store.unit_of_work() as uow:
        await uow.outbox.add(
            OutboxItem(
                outbox_id="out_earlier",
                idempotency_key=key,
                run_id="run_earlier",
                load_id=LOAD_ID,
                kind=OutboxKind.SMS,
                payload={"contact_id": DRIVER, "text": "sent before", "purpose": "reply"},
                created_at=at(7, 0),
                status=OutboxStatus.SENT,
            )
        )
        await uow.commit()


PO_REPLY = SendSms(contact_id=DRIVER, text="The PO number is 55821.", purpose=Purpose.REPLY)
SKIPPED_FIRST = "executor skipped action 0 (send_sms): already applied: msg:0712:0 is in the outbox"


async def test_a_run_whose_actions_the_executor_all_skipped_is_a_noop(load: Load) -> None:
    """Approved is not done: a run that changed nothing did not act."""
    world = await World(load).start()
    await world.message(at(7, 12), "What's the PO?")
    await already_in_the_outbox(world, "msg:0712:0")
    world.model.push(GATE, decision(Intent.QUESTION))
    world.model.push(DECIDE, plan(PO_REPLY))

    runs = await world.tick(at(7, 13))

    assert [(r.status, r.skip_reason) for r in runs] == [(RunStatus.NOOP, SKIPPED_FIRST)]
    assert [(r.status, r.skip_reason) for r in (await world.snapshot()).runs] == [
        (RunStatus.NOOP, SKIPPED_FIRST)
    ]
    assert world.sent("sms") == []


async def test_a_run_with_some_actions_skipped_still_acted_and_records_the_skips(
    load: Load,
) -> None:
    world = await World(load).start()
    await world.message(at(7, 12), "What's the PO?")
    await already_in_the_outbox(world, "msg:0712:0")
    world.model.push(GATE, decision(Intent.QUESTION))
    gate_note = SendSms(contact_id=DRIVER, text="You need it at the gate.", purpose=Purpose.REPLY)
    world.model.push(DECIDE, plan(PO_REPLY, gate_note))

    runs = await world.tick(at(7, 13))

    assert [(r.status, r.skip_reason) for r in runs] == [(RunStatus.ACTED, SKIPPED_FIRST)]
    assert [d.payload["text"] for d in world.sent("sms")] == ["You need it at the gate."]


# --- unexpected failures: release, retry, dead letter ---------------------------------------


class FlakySop(FakeSop):
    """Fails the first `failures` reads, then serves the test sections."""

    def __init__(self, failures: int, error: Exception | None = None) -> None:
        super().__init__(SECTIONS)
        self.failures = failures
        self.error = error or RuntimeError("SOP store unreachable")

    async def by_applies(
        self, broker_id: str, load_types: Sequence[str], intent: str, stage: Stage
    ) -> ResolvedSop:
        if self.failures > 0:
            self.failures -= 1
            raise self.error
        return await super().by_applies(broker_id, load_types, intent, stage)


async def test_an_unexpected_error_is_retried_and_a_success_leaves_no_failed_run(
    load: Load,
) -> None:
    world = await World(load, knowledge=FlakySop(failures=1)).start()
    await world.ping(at(7, 0), DelayRisk.HIGH, "p1")
    world.model.push(DECIDE, plan(ASK_ETA, FOLLOW_UP_IN_30))

    runs = await world.tick(at(7, 1))

    assert [r.status for r in runs] == [RunStatus.FAILED, RunStatus.ACTED]
    assert runs[0].error == "RuntimeError: SOP store unreachable"
    assert runs[0].run_id != runs[1].run_id
    snapshot = await world.snapshot()
    assert [r.status for r in snapshot.runs] == [RunStatus.ACTED]  # the retry is not skipped
    assert len(world.sent("sms")) == 1


async def test_an_event_that_keeps_failing_becomes_a_dead_letter(load: Load) -> None:
    world = await World(load, knowledge=FlakySop(failures=10)).start()
    await world.ping(at(7, 0), DelayRisk.HIGH, "p1")

    runs = await world.tick(at(7, 1))

    assert [r.status for r in runs] == [RunStatus.FAILED] * 3  # the memory store's max attempts
    assert await dead_letters(world) == ["risk:p1"]
    snapshot = await world.snapshot()
    assert [(r.status, r.error) for r in snapshot.runs] == [
        (RunStatus.FAILED, "RuntimeError: SOP store unreachable")
    ]
    assert snapshot.outbox == ()
    assert await world.tick(at(7, 2)) == []


async def test_reraise_unexpected_releases_the_event_then_raises(load: Load) -> None:
    config = RuntimeConfig(reraise_unexpected=True)
    world = await World(load, knowledge=FlakySop(failures=1), config=config).start()
    await world.ping(at(7, 0), DelayRisk.HIGH, "p1")
    world.model.push(DECIDE, plan(ASK_ETA, FOLLOW_UP_IN_30))

    world.clock.set(at(7, 1))
    with pytest.raises(RuntimeError, match="SOP store unreachable"):
        await world.runtime.process_next()

    assert await queue_size(world) == 1  # released, ready for a retry
    run = await world.runtime.process_next()
    assert run is not None and run.status is RunStatus.ACTED


async def test_reraise_unexpected_keeps_domain_errors_as_failed_runs(load: Load) -> None:
    broken = (*SECTIONS, section("orphan", "More.", client_id="northline", mode=MergeMode.EXTEND))
    config = RuntimeConfig(reraise_unexpected=True)
    world = await World(load, knowledge=FakeSop(broken), config=config).start()
    await world.ping(at(7, 0), DelayRisk.HIGH, "p1")

    runs = await world.tick(at(7, 1))

    assert [r.status for r in runs] == [RunStatus.FAILED] * 3
    assert runs[0].error is not None and runs[0].error.startswith("DomainError: ")


async def test_an_event_whose_context_cannot_load_fails_and_is_released(load: Load) -> None:
    world = await World(load).start()
    stray = Event(
        event_id="m-stray",
        load_id="999999",
        type=EventType.INBOUND_MESSAGE,
        occurred_at=at(7, 0),
        channel=Channel.SMS,
        sender_contact_id=DRIVER,
        body="hi",
    )
    async with world.store.unit_of_work() as uow:
        await uow.events.append(stray)
        await uow.queue.enqueue(stray)
        await uow.commit()

    runs = await world.tick(at(7, 1))

    assert [(r.status, r.load_id) for r in runs] == [(RunStatus.FAILED, "999999")] * 3
    assert runs[0].error == "DomainError: unknown load '999999' for event 'm-stray'"
    assert runs[0].gate is None
    assert await dead_letters(world) == ["m-stray"]


class OtherWorkerModel:
    """While this run thinks, another worker finishes the same event."""

    def __init__(self) -> None:
        self.world: World | None = None
        self.inner = ScriptedModel()

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        assert self.world is not None
        async with self.world.store.unit_of_work() as uow:
            await uow.runs.save(
                RunRecord(
                    run_id="run_other",
                    event_id="risk:p1",
                    load_id=LOAD_ID,
                    started_at=at(7, 0),
                    status=RunStatus.ACTED,
                )
            )
            await uow.commit()
        return await self.inner.generate(request, schema)


async def test_a_run_finished_elsewhere_meanwhile_is_not_executed_twice(load: Load) -> None:
    model = OtherWorkerModel()
    world = await World(load, llm=model).start()
    model.world = world
    await world.ping(at(7, 0), DelayRisk.HIGH, "p1")
    model.inner.push(DECIDE, plan(ASK_ETA, FOLLOW_UP_IN_30))

    runs = await world.tick(at(7, 1))

    assert [(r.status, r.skip_reason) for r in runs] == [(RunStatus.SKIPPED, "already_processed")]
    assert runs[0].plan is not None  # what it thought is kept, with the cost of thinking
    assert len(runs[0].usage) == 1
    assert world.sent("sms") == []
    assert await queue_size(world) == 0


# --- the claim lease (the watchdog) ---------------------------------------------------------


class HangingModel:
    """Hangs on its first `times` decide calls until the run is cancelled (a shutdown, a
    client that went away), then answers from the script. A cancelled run is neither
    completed nor released: only the claim lease can give its event back."""

    def __init__(self, times: int) -> None:
        self.times = times
        self.inner = ScriptedModel()
        self.entered = asyncio.Event()

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        if request.role is ModelRole.AGENT and self.times > 0:
            self.times -= 1
            self.entered.set()
            await asyncio.Event().wait()  # until cancelled
        return await self.inner.generate(request, schema)


async def cancel_tick(world: World, model: HangingModel, when: datetime) -> None:
    """A tick whose run is cancelled while the model thinks."""
    model.entered.clear()
    tick = asyncio.create_task(world.tick(when))
    await model.entered.wait()
    tick.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tick


def test_the_claim_lease_is_the_run_deadline_plus_a_minute_by_default() -> None:
    assert RuntimeConfig().effective_claim_lease_s == 240.0 + 60
    assert RuntimeConfig(run_deadline_s=30).effective_claim_lease_s == 90
    assert RuntimeConfig(claim_lease_s=5).effective_claim_lease_s == 5


async def test_an_event_a_cancelled_run_left_in_flight_runs_again_after_the_lease(
    load: Load,
) -> None:
    model = HangingModel(times=1)
    world = await World(load, llm=model, config=RuntimeConfig(claim_lease_s=120)).start()
    await world.ping(at(7, 0), DelayRisk.HIGH, "p1")
    await cancel_tick(world, model, at(7, 1))
    assert await queue_size(world) == 0  # in flight, and nobody will finish it

    assert await world.tick(at(7, 3)) == []  # 120 s after the claim: still leased
    model.inner.push(DECIDE, plan(ASK_ETA, FOLLOW_UP_IN_30))
    runs = await world.tick(at(7, 3, 1))

    assert [(r.event_id, r.status) for r in runs] == [("risk:p1", RunStatus.ACTED)]
    assert len(world.sent("sms")) == 1
    assert await dead_letters(world) == []


async def test_an_event_whose_release_failed_runs_again_after_the_lease(
    load: Load, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run fails, and so does the release of its event (the store went away): the
    error reaches the worker and the event stays in flight until its lease runs out."""
    config = RuntimeConfig(claim_lease_s=60)
    world = await World(load, knowledge=FlakySop(failures=1), config=config).start()
    await world.ping(at(7, 0), DelayRisk.HIGH, "p1")

    async def unreachable(self: MemoryEventQueue, event_id: str, error: str) -> None:
        raise ConnectionError("store unreachable")

    monkeypatch.setattr(MemoryEventQueue, "release", unreachable)
    with pytest.raises(ConnectionError):
        await world.tick(at(7, 1))
    monkeypatch.undo()

    world.model.push(DECIDE, plan(ASK_ETA, FOLLOW_UP_IN_30))
    assert await world.tick(at(7, 2)) == []  # still leased
    runs = await world.tick(at(7, 2, 1))

    assert [(r.event_id, r.status) for r in runs] == [("risk:p1", RunStatus.ACTED)]
    assert len(world.sent("sms")) == 1


async def test_an_event_whose_runs_keep_dying_becomes_a_dead_letter(load: Load) -> None:
    """Each expired claim counts as a failed attempt, so a poison event still stops."""
    model = HangingModel(times=10)
    world = await World(load, llm=model, config=RuntimeConfig(claim_lease_s=60)).start()
    await world.ping(at(7, 0), DelayRisk.HIGH, "p1")

    for minute in (1, 3, 5):  # three claims, each dies; the next tick reclaims it
        await cancel_tick(world, model, at(7, minute))
    assert await world.tick(at(7, 7)) == []  # the third reclaim makes it a dead letter

    assert await dead_letters(world) == ["risk:p1"]
    assert model.times == 7
    assert world.sent("sms") == []
    assert await world.tick(at(7, 30)) == []


# --- tracing --------------------------------------------------------------------------------


class BrokenTracer:
    async def record_run(self, run: RunRecord) -> str | None:
        raise ConnectionError("tracing backend down")


class UrlTracer:
    def __init__(self) -> None:
        self.runs: list[RunRecord] = []

    async def record_run(self, run: RunRecord) -> str | None:
        self.runs.append(run)
        return f"https://traces.example/{run.run_id}"


async def test_a_failing_tracer_never_breaks_a_run(load: Load) -> None:
    world = await World(load, tracer=BrokenTracer()).start()

    runs = await world.high_risk_at_seven()

    assert [r.status for r in runs] == [RunStatus.ACTED]
    assert runs[0].trace_url is None
    assert len(world.sent("sms")) == 1
    assert [r.status for r in (await world.snapshot()).runs] == [RunStatus.ACTED]


async def test_the_trace_url_is_saved_with_a_stored_run(load: Load) -> None:
    tracer = UrlTracer()
    world = await World(load, tracer=tracer).start()

    runs = await world.high_risk_at_seven()

    assert runs[0].trace_url == "https://traces.example/run_1"
    assert [r.trace_url for r in (await world.snapshot()).runs] == ["https://traces.example/run_1"]
    assert [r.run_id for r in tracer.runs] == ["run_1"]


async def test_a_failed_attempt_is_traced_but_not_stored(load: Load) -> None:
    tracer = UrlTracer()
    world = await World(load, knowledge=FlakySop(failures=1), tracer=tracer).start()
    await world.ping(at(7, 0), DelayRisk.HIGH, "p1")
    world.model.push(DECIDE, plan(ASK_ETA, FOLLOW_UP_IN_30))

    runs = await world.tick(at(7, 1))

    assert [r.trace_url for r in runs] == [
        "https://traces.example/run_1",
        "https://traces.example/run_2",
    ]
    assert [r.run_id for r in (await world.snapshot()).runs] == ["run_2"]
