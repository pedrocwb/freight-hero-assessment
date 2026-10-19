"""The scenario runner drives a runtime through a scenario on a virtual clock.

The real AgentRuntime is not needed here: a small fake implements the facade
subset the runner uses, so these tests pin the runner's timing, per-step
diffing and script accounting on their own.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import pytest

from freight_agent.adapters.channels.fakes.recording import RecordingGateways
from freight_agent.adapters.clock import SequentialIdGenerator, VirtualClock
from freight_agent.adapters.llm.scripted import ScriptedModel
from freight_agent.adapters.observability.null import NullTracer
from freight_agent.application.contracts import (
    InboundMessage,
    IngestResult,
    LoadSnapshot,
    LocationPing,
)
from freight_agent.application.runtime import AgentRuntime
from freight_agent.domain.actions import Plan
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.load import Channel, DelayRisk, Load
from freight_agent.domain.outbox import OutboxItem, OutboxKind
from freight_agent.domain.runs import RunRecord, RunStatus
from freight_agent.domain.sop import SopSection
from freight_agent.domain.timers import Timer, TimerStatus
from freight_agent.evalkit.counting import CountingModel
from freight_agent.evalkit.runner import ScenarioRunner, build_runtime
from freight_agent.evalkit.spec import Scenario
from freight_agent.ports.clock import Clock
from freight_agent.ports.gateways import Gateways
from freight_agent.ports.llm import LanguageModel, LlmError, LlmRequest, ModelRole
from freight_agent.ports.sop import SopRepository

EVALS = Path(__file__).resolve().parents[2] / "evals"
LA = ZoneInfo("America/Los_Angeles")
DAY = date(2026, 10, 13)
LOAD_ID = "481207"

ACK = GateDecision(intent=Intent.ACKNOWLEDGEMENT, act=False, reason="just ok")
ETA = GateDecision(intent=Intent.ETA_PROVIDED, act=True, reason="gives an ETA")


def la(hhmm: str) -> datetime:
    return datetime.combine(DAY, time.fromisoformat(hhmm), tzinfo=LA)


class NoSop:
    async def sections_for_client(self, client_id: str) -> list[SopSection]:
        return []

    def version(self) -> str:
        return "test"


@dataclass(frozen=True)
class _Queued:
    event_id: str
    kind: Literal["message", "risk", "timer"]
    body: str = ""


class FakeRuntime:
    """Just enough of AgentRuntime for the runner.

    Ingested messages and pings are queued. `tick` fires due timers into the
    queue, then runs every queued event: one run record and one outbox item
    each, and, when `follow_up_minutes` is set, a new timer that many minutes
    later. Like the real runtime, a message calls the gate model (and the
    decide model when the gate acts) and a timer calls the decide model only;
    in this fake a ping calls no model.
    """

    def __init__(
        self, clock: Clock, model: LanguageModel, *, follow_up_minutes: int | None, fires: bool
    ) -> None:
        self.clock = clock
        self.model = model
        self.started_at = clock.now()
        self.follow_up_minutes = follow_up_minutes
        self.fires = fires
        self.load: Load | None = None
        self.messages: list[InboundMessage] = []
        self.pings: list[LocationPing] = []
        self.ticks: list[datetime] = []
        self.timers: list[Timer] = []
        self.outbox: list[OutboxItem] = []
        self.runs: list[RunRecord] = []
        self._queue: list[_Queued] = []
        self._ids = SequentialIdGenerator()

    def seed_timer(self, due_at: datetime) -> None:
        self.timers.append(
            Timer(
                timer_id=self._ids.new("timer"),
                load_id=LOAD_ID,
                obligation_id="obl_1",
                due_at=due_at,
                created_at=self.clock.now(),
            )
        )

    async def register_load(self, load: Load) -> None:
        self.load = load

    async def ingest_message(self, message: InboundMessage) -> IngestResult:
        if message.body == "boom":
            raise RuntimeError("boom")
        self.messages.append(message)
        self._queue.append(_Queued(message.message_id, "message", message.body))
        return IngestResult(accepted=True, event_id=message.message_id)

    async def ingest_ping(self, ping: LocationPing) -> IngestResult:
        self.pings.append(ping)
        self._queue.append(_Queued(f"risk:{ping.ping_id}", "risk"))
        return IngestResult(accepted=True, event_id=f"risk:{ping.ping_id}")

    async def tick(self) -> list[RunRecord]:
        now = self.clock.now()
        self.ticks.append(now)
        for i, timer in enumerate(self.timers):
            if self.fires and timer.status is TimerStatus.PENDING and timer.due_at <= now:
                self.timers[i] = timer.mark(TimerStatus.FIRED)
                self._queue.append(_Queued(f"timer:{timer.timer_id}", "timer"))
        runs = [await self._run(item, now) for item in self._queue]
        self._queue.clear()
        return runs

    async def next_timer_due_at(self) -> datetime | None:
        pending = [t.due_at for t in self.timers if t.status is TimerStatus.PENDING]
        return min(pending, default=None)

    async def snapshot(self, load_id: str) -> LoadSnapshot:
        assert self.load is not None and self.load.load_id == load_id
        return LoadSnapshot(
            load=self.load,
            obligations=(),
            timers=tuple(self.timers),
            events=(),
            outbox=tuple(self.outbox),
            runs=tuple(self.runs),
        )

    async def _run(self, item: _Queued, now: datetime) -> RunRecord:
        run_id = self._ids.new("run")
        status = RunStatus.ACTED
        try:
            await self._think(item)
        except LlmError:
            status = RunStatus.ESCALATED
        self.outbox.append(
            OutboxItem(
                outbox_id=self._ids.new("out"),
                idempotency_key=f"{item.event_id}:0",
                run_id=run_id,
                load_id=LOAD_ID,
                kind=OutboxKind.SMS,
                payload={"event_id": item.event_id},
                created_at=now,
            )
        )
        if self.follow_up_minutes is not None:
            self.seed_timer(now + timedelta(minutes=self.follow_up_minutes))
        run = RunRecord(
            run_id=run_id,
            event_id=item.event_id,
            load_id=LOAD_ID,
            started_at=now,
            finished_at=now,
            status=status,
        )
        self.runs.append(run)
        return run

    async def _think(self, item: _Queued) -> None:
        decide = LlmRequest(role=ModelRole.DECIDE, system="decide", user=item.event_id)
        if item.kind == "timer":
            await self.model.generate(decide, Plan)
        elif item.kind == "message":
            gate = LlmRequest(role=ModelRole.GATE, system="gate", user=item.body)
            if (await self.model.generate(gate, GateDecision)).parsed.act:
                await self.model.generate(decide, Plan)


@dataclass
class FakeFactory:
    """A `RuntimeFactory` that builds a FakeRuntime and keeps it for assertions."""

    timers: tuple[str, ...] = ()  # local HH:MM on DAY, seeded before the run
    follow_up_minutes: int | None = None
    fires: bool = True
    created: list[FakeRuntime] = field(default_factory=list[FakeRuntime])

    def __call__(
        self, clock: Clock, model: LanguageModel, gateways: Gateways, sop: SopRepository
    ) -> FakeRuntime:
        runtime = FakeRuntime(
            clock, model, follow_up_minutes=self.follow_up_minutes, fires=self.fires
        )
        for hhmm in self.timers:
            runtime.seed_timer(la(hhmm))
        self.created.append(runtime)
        return runtime

    @property
    def runtime(self) -> FakeRuntime:
        return self.created[-1]


def scenario(*steps: dict[str, Any], **fields: Any) -> Scenario:
    return Scenario.model_validate(
        {
            "id": "s1",
            "title": "test",
            "suite": "core",
            "load": "fixtures/load_481207.json",
            "date": DAY.isoformat(),
            "start": "06:55",
            "steps": list(steps),
            **fields,
        }
    )


def ping(at: str, risk: str = "high") -> dict[str, Any]:
    return {"at": at, "ping": {"delay_risk": risk}}


def message(at: str, body: str = "ok", **extra: Any) -> dict[str, Any]:
    step: dict[str, Any] = {
        "at": at,
        "message": {"channel": "sms", "sender": "drv_20931", "body": body},
    }
    if "message_id" in extra:
        step["message"]["message_id"] = extra.pop("message_id")
    return {**step, **extra}


def scripted(factory: FakeFactory, evals_dir: Path = EVALS) -> ScenarioRunner:
    return ScenarioRunner(evals_dir, NoSop(), "scripted", runtime_factory=factory)


# --- time ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_the_clock_stops_at_every_pending_timer_before_the_step_in_order() -> None:
    factory = FakeFactory(timers=("07:20", "07:10", "09:00"))

    outcome = await scripted(factory).run(scenario({"at": "08:00"}))

    assert outcome.error is None
    assert factory.runtime.ticks == [la("07:10"), la("07:20")]
    assert factory.runtime.clock.now() == la("08:00")
    assert [t.status for t in factory.runtime.timers] == [
        TimerStatus.FIRED,
        TimerStatus.FIRED,
        TimerStatus.PENDING,
    ]
    assert [o.created_at for o in outcome.observations[0].outbox] == [la("07:10"), la("07:20")]


@pytest.mark.unit
async def test_timers_scheduled_on_the_way_are_followed_too() -> None:
    factory = FakeFactory(follow_up_minutes=30)

    outcome = await scripted(factory).run(scenario(ping("07:00"), {"at": "08:30"}))

    assert factory.runtime.ticks == [la("07:01"), la("07:31"), la("08:01")]
    assert [r.started_at for r in outcome.observations[1].runs] == [la("07:31"), la("08:01")]
    assert outcome.observations[1].snapshot.timers[-1].due_at == la("08:31")


@pytest.mark.unit
async def test_a_timer_due_at_the_step_time_fires_before_the_injection() -> None:
    factory = FakeFactory(timers=("07:00",))

    outcome = await scripted(factory).run(scenario(ping("07:00")))

    assert factory.runtime.ticks == [la("07:00"), la("07:01")]
    assert [r.event_id for r in outcome.observations[0].runs] == ["timer:timer_1", "risk:s1:0"]


@pytest.mark.unit
async def test_an_overdue_timer_fires_at_the_current_time() -> None:
    factory = FakeFactory(timers=("06:00",))

    await scripted(factory).run(scenario({"at": "07:00"}))

    assert factory.runtime.ticks == [la("06:55")]


@pytest.mark.unit
async def test_an_injection_runs_after_the_latency() -> None:
    factory = FakeFactory()

    await scripted(factory).run(scenario(ping("07:00"), run_latency_seconds=90))

    assert factory.runtime.ticks == [la("07:01:30")]


@pytest.mark.unit
async def test_latency_never_runs_past_the_next_step() -> None:
    factory = FakeFactory()

    await scripted(factory).run(scenario(ping("07:00"), ping("07:00:30"), ping("07:00:30")))

    assert factory.runtime.ticks == [la("07:00:30"), la("07:00:30"), la("07:01:30")]


@pytest.mark.unit
async def test_a_step_without_injection_only_advances_time() -> None:
    factory = FakeFactory()

    outcome = await scripted(factory).run(scenario({"at": "07:30"}))

    runtime = factory.runtime
    assert runtime.ticks == []
    assert runtime.messages == [] and runtime.pings == []
    assert runtime.clock.now() == la("07:30")
    observation = outcome.observations[0]
    assert (observation.outbox, observation.runs, observation.llm_calls) == ((), (), 0)
    assert outcome.failures == ()


@pytest.mark.unit
async def test_step_times_are_local_to_the_scenario_timezone() -> None:
    factory = FakeFactory()
    new_york = ZoneInfo("America/New_York")

    outcome = await scripted(factory).run(scenario(ping("07:00:15"), timezone="America/New_York"))

    at = outcome.observations[0].at
    assert at == datetime(2026, 10, 13, 11, 0, 15, tzinfo=UTC)
    assert at.tzinfo is new_york
    assert factory.runtime.started_at == datetime(2026, 10, 13, 10, 55, tzinfo=UTC)
    assert factory.runtime.pings[0].occurred_at == at


# --- injections and ids -------------------------------------------------------------


@pytest.mark.unit
async def test_injections_carry_derived_ids_and_the_step_time() -> None:
    factory = FakeFactory()
    steps = (
        message("07:00", script={"gate": [ACK.model_dump()]}),
        message("07:05", message_id="sms-42", script={"gate": [ACK.model_dump()]}),
        {"at": "07:10", "ping": {"delay_risk": "medium", "lat": 1.5, "lon": -2.5}},
    )

    await scripted(factory).run(scenario(*steps))

    runtime = factory.runtime
    assert [m.message_id for m in runtime.messages] == ["s1:0", "sms-42"]
    first = runtime.messages[0]
    assert (first.load_id, first.channel, first.sender_contact_id, first.body) == (
        LOAD_ID,
        Channel.SMS,
        "drv_20931",
        "ok",
    )
    assert first.occurred_at == la("07:00")
    assert runtime.pings == [
        LocationPing(
            ping_id="s1:2",
            load_id=LOAD_ID,
            lat=1.5,
            lon=-2.5,
            occurred_at=la("07:10"),
            delay_risk=DelayRisk.MEDIUM,
        )
    ]


@pytest.mark.unit
async def test_an_unknown_sender_is_injected_without_a_contact() -> None:
    factory = FakeFactory()
    step = message("07:00", script={"gate": [ACK.model_dump()]})
    step["message"]["sender"] = None

    await scripted(factory).run(scenario(step))

    assert factory.runtime.messages[0].sender_contact_id is None


@pytest.mark.unit
async def test_observations_hold_only_what_the_step_created() -> None:
    factory = FakeFactory()

    outcome = await scripted(factory).run(scenario(ping("07:00"), ping("07:10")))

    first, second = outcome.observations
    assert (first.step_index, second.step_index) == (0, 1)
    assert (first.at, second.at) == (la("07:00"), la("07:10"))
    assert [o.payload["event_id"] for o in first.outbox] == ["risk:s1:0"]
    assert [o.payload["event_id"] for o in second.outbox] == ["risk:s1:1"]
    assert [r.event_id for r in first.runs] == ["risk:s1:0"]
    assert [r.event_id for r in second.runs] == ["risk:s1:1"]
    assert len(second.snapshot.outbox) == 2
    assert len(second.snapshot.runs) == 2


# --- the load -----------------------------------------------------------------------


@pytest.mark.unit
async def test_load_overrides_replace_top_level_fixture_keys() -> None:
    factory = FakeFactory()
    overrides = {
        "broker": {"id": "keystone", "name": "Keystone Freight"},
        "load_types": ["reefer"],
    }

    outcome = await scripted(factory).run(scenario({"at": "07:00"}, load_overrides=overrides))

    load = factory.runtime.load
    assert outcome.error is None
    assert load is not None
    assert (load.broker_id, load.broker_name, load.load_types) == (
        "keystone",
        "Keystone Freight",
        ("reefer",),
    )
    assert (load.load_id, load.shipper) == (LOAD_ID, "Riverbend Foods")


@pytest.mark.unit
async def test_a_missing_fixture_is_an_error_result(tmp_path: Path) -> None:
    factory = FakeFactory()

    outcome = await scripted(factory, evals_dir=tmp_path).run(scenario(ping("07:00")), trial=2)

    assert outcome.error is not None
    assert "fixtures/load_481207.json" in outcome.error
    assert (outcome.scenario_id, outcome.trial, outcome.observations) == ("s1", 2, ())
    assert factory.created == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("content", "reason"),
    [("[1, 2]", "must be a JSON object"), ("{not json", "cannot read load fixture")],
)
async def test_an_unusable_fixture_is_an_error_result(
    tmp_path: Path, content: str, reason: str
) -> None:
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "load_481207.json").write_text(content)

    outcome = await scripted(FakeFactory(), evals_dir=tmp_path).run(scenario(ping("07:00")))

    assert outcome.error is not None
    assert reason in outcome.error


@pytest.mark.unit
@pytest.mark.parametrize("overrides", [{"stage": "flying"}, {"broker": "keystone"}])
async def test_invalid_load_metadata_is_an_error_result(overrides: dict[str, Any]) -> None:
    outcome = await scripted(FakeFactory()).run(scenario(ping("07:00"), load_overrides=overrides))

    assert outcome.error is not None
    assert outcome.error.startswith("fixtures/load_481207.json: ")


@pytest.mark.unit
async def test_an_unknown_timezone_is_an_error_result() -> None:
    outcome = await scripted(FakeFactory()).run(scenario(ping("07:00"), timezone="Mars/Olympus"))

    assert outcome.error is not None
    assert "Mars/Olympus" in outcome.error


@pytest.mark.unit
async def test_a_runtime_crash_ends_the_run_with_the_steps_so_far() -> None:
    factory = FakeFactory()

    outcome = await scripted(factory).run(scenario(ping("07:00"), message("07:05", body="boom")))

    assert outcome.error == "step 1: RuntimeError: boom"
    assert [o.step_index for o in outcome.observations] == [0]


@pytest.mark.unit
async def test_a_runtime_that_never_fires_a_due_timer_is_an_error_not_a_hang() -> None:
    factory = FakeFactory(timers=("07:10",), fires=False)

    outcome = await scripted(factory).run(scenario({"at": "07:30"}))

    assert outcome.error is not None
    assert "did not fire" in outcome.error


# --- scripted mode ------------------------------------------------------------------


@pytest.mark.unit
async def test_scripted_mode_feeds_each_step_its_script() -> None:
    factory = FakeFactory()
    step = message("07:00", body="8:30", script={"gate": [ETA.model_dump()], "decide": [{}]})

    outcome = await scripted(factory).run(scenario(step))

    observation = outcome.observations[0]
    assert outcome.failures == ()
    assert observation.llm_calls == 2
    assert [r.status for r in observation.runs] == [RunStatus.ACTED]
    assert [u.role for u in outcome.usage] == ["gate", "decide"]


@pytest.mark.unit
async def test_unused_script_entries_fail_the_step() -> None:
    steps = (
        message("07:00", script={"gate": [ACK.model_dump(), ACK.model_dump()]}),
        {**ping("07:10"), "script": {"decide": [{}]}},
    )

    outcome = await scripted(FakeFactory()).run(scenario(*steps))

    assert [(f.step_index, f.check) for f in outcome.failures] == [(0, "script"), (1, "script")]
    assert "1 scripted gate output" in outcome.failures[0].message
    assert "1 scripted decide output" in outcome.failures[1].message


@pytest.mark.unit
async def test_a_model_call_with_nothing_scripted_fails_the_step() -> None:
    outcome = await scripted(FakeFactory()).run(scenario(message("07:00")))

    observation = outcome.observations[0]
    assert [(f.step_index, f.check) for f in outcome.failures] == [(0, "script")]
    assert "gate model was called 1 time" in outcome.failures[0].message
    assert observation.llm_calls == 1
    assert [r.status for r in observation.runs] == [RunStatus.ESCALATED]


@pytest.mark.unit
async def test_timers_on_the_way_to_a_step_use_that_steps_script() -> None:
    factory = FakeFactory(timers=("07:10",))

    outcome = await scripted(factory).run(scenario({"at": "07:30", "script": {"decide": [{}]}}))

    assert outcome.failures == ()
    assert outcome.observations[0].llm_calls == 1
    assert [r.status for r in outcome.observations[0].runs] == [RunStatus.ACTED]


@pytest.mark.unit
async def test_scripts_do_not_leak_into_the_next_step() -> None:
    steps = (
        message("07:00", script={"gate": [ACK.model_dump(), ACK.model_dump()]}),
        message("07:10"),
    )

    outcome = await scripted(FakeFactory()).run(scenario(*steps))

    assert [f.step_index for f in outcome.failures] == [0, 1]
    assert "not used" in outcome.failures[0].message
    assert "nothing scripted" in outcome.failures[1].message
    assert [o.llm_calls for o in outcome.observations] == [1, 1]


@pytest.mark.unit
def test_scripted_mode_owns_its_model() -> None:
    with pytest.raises(ValueError, match="scripted"):
        ScenarioRunner(EVALS, NoSop(), "scripted", model=ScriptedModel())


# --- replay and live ----------------------------------------------------------------


@pytest.mark.unit
async def test_replay_mode_uses_the_given_model_and_ignores_scripts() -> None:
    model = ScriptedModel(model_name="cassette")
    model.push(ModelRole.GATE, ACK)
    runner = ScenarioRunner(EVALS, NoSop(), "replay", model=model, runtime_factory=FakeFactory())
    step = message("07:00", script={"gate": [ETA.model_dump()], "decide": [{}]})

    outcome = await runner.run(scenario(step))

    assert outcome.failures == ()
    assert outcome.observations[0].llm_calls == 1
    assert len(model.calls) == 1
    assert [(u.role, u.model) for u in outcome.usage] == [("gate", "cassette")]


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["replay", "live"])
def test_replay_and_live_need_a_model(mode: Any) -> None:
    with pytest.raises(ValueError, match="model"):
        ScenarioRunner(EVALS, NoSop(), mode)


# --- the counting wrapper and the default runtime -----------------------------------


@pytest.mark.unit
async def test_counting_model_counts_every_call_and_keeps_the_usage() -> None:
    inner = ScriptedModel()
    inner.push(ModelRole.GATE, ACK, ETA)
    model = CountingModel(inner)
    request = LlmRequest(role=ModelRole.GATE, system="s", user="u")

    first = await model.generate(request, GateDecision)
    await model.generate(request, GateDecision)
    with pytest.raises(LlmError):
        await model.generate(request, GateDecision)

    assert first.parsed == ACK
    assert model.calls == 3
    assert [u.role for u in model.usage] == ["gate", "gate"]


@pytest.mark.unit
def test_the_default_factory_builds_the_real_runtime() -> None:
    clock = VirtualClock(la("07:00"))
    model = CountingModel(ScriptedModel())
    gateways = RecordingGateways().as_gateways()
    sop = NoSop()

    runtime = build_runtime(clock, model, gateways, sop)

    assert isinstance(runtime, AgentRuntime)
    deps = runtime._deps  # pyright: ignore[reportPrivateUsage]
    assert (deps.clock, deps.llm, deps.gateways, deps.sop) == (clock, model, gateways, sop)
    assert isinstance(deps.ids, SequentialIdGenerator)
    assert isinstance(deps.tracer, NullTracer)
