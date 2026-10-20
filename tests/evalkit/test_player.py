"""The scenario player: plays a scenario's steps on a runtime and observes each step.

Two halves. The timing rules of the `evalkit.spec` docstring, the derived ids, the
attachments and the judge are pinned on `ScenarioPlayer` with a small fake runtime (the
facade subset the player uses), so each test sees every tick. `play_scenario` is then
played on the real trial runtime with the real scenario files, a `FakeModel` giving the
model's rounds: setup errors, a runtime error halfway, the model calls of a step (an agent
loop counts once) and the judge's usage kept apart.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import pytest

from freight_agent.adapters.clock import SequentialIdGenerator, VirtualClock
from freight_agent.adapters.documents.memory_blobs import MemoryBlobStore, blob_ref
from freight_agent.adapters.knowledge.memory import MemorySopIndex
from freight_agent.adapters.observability.null import NullTracer
from freight_agent.application.contracts import (
    InboundMessage,
    IngestResult,
    LoadSnapshot,
    LocationPing,
)
from freight_agent.application.documents import DocumentPipeline
from freight_agent.application.runtime import AgentRuntime
from freight_agent.domain.actions import (
    AgentOutcome,
    CloseObligation,
    CloseStatus,
    CreateHeroTask,
    Plan,
    Purpose,
    ScheduleTimer,
    SendEmail,
    SendSms,
)
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.load import Channel, DelayRisk, Load
from freight_agent.domain.outbox import OutboxItem, OutboxKind
from freight_agent.domain.runs import RunRecord, RunStatus
from freight_agent.domain.timers import Timer, TimerStatus
from freight_agent.evalkit.counting import CountingModel
from freight_agent.evalkit.graders import grade
from freight_agent.evalkit.judge import Judge, JudgeSpec, JudgeVerdict
from freight_agent.evalkit.results import StepObservation
from freight_agent.evalkit.runner import (
    Played,
    RunnerError,
    ScenarioPlayer,
    build_trial_runtime,
    local_time,
    play_scenario,
    read_attachments,
    read_load,
    scenario_start,
)
from freight_agent.evalkit.spec import Scenario, load_scenario
from freight_agent.ports.llm import ChatRequest, LlmError, LlmRequest, ModelRole
from tests.fakes import FakeModel, Round, act, call
from tests.prompts import PROMPTS

ROOT = Path(__file__).resolve().parents[2]
EVALS = ROOT / "evals"
SOPS = MemorySopIndex.from_files(ROOT / "sops")
LA = ZoneInfo("America/Los_Angeles")
DAY = date(2026, 10, 13)
LOAD_ID = "481207"
DRIVER, DISPATCHER = "drv_20931", "dsp_4410"
POD = "fixtures/docs/pod_clean.pdf"

ACK = GateDecision(intent=Intent.ACKNOWLEDGEMENT, act=False, reason="just ok")
QUESTION = GateDecision(intent=Intent.QUESTION, act=True, reason="asks something")

pytestmark = pytest.mark.unit


def la(hhmm: str) -> datetime:
    return datetime.combine(DAY, time.fromisoformat(hhmm), tzinfo=LA)


# --- a fake runtime ---------------------------------------------------------------------


@dataclass(frozen=True)
class _Queued:
    event_id: str
    kind: Literal["message", "risk", "timer"]


class FakeRuntime:
    """Just enough of `AgentRuntime` for the player.

    Ingested messages and pings are queued. `tick` fires due timers into the queue, then
    runs every queued event: one run record and one SMS to the driver each and, with
    `follow_up_minutes`, a new timer that many minutes later. A message calls the gate
    and, when the gate acts, an agent loop of two rounds; a ping or a timer calls no
    model.
    """

    def __init__(
        self,
        clock: VirtualClock,
        model: CountingModel,
        *,
        follow_up_minutes: int | None,
        fires: bool,
    ) -> None:
        self.clock = clock
        self.model = model
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
        self._queue.append(_Queued(message.message_id, "message"))
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
        if item.kind == "message":
            await self._think(item)
        self.outbox.append(
            OutboxItem(
                outbox_id=self._ids.new("out"),
                idempotency_key=f"{item.event_id}:0",
                run_id=run_id,
                load_id=LOAD_ID,
                kind=OutboxKind.SMS,
                payload={"event_id": item.event_id, "contact_id": DRIVER, "text": "Hi there"},
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
            status=RunStatus.ACTED,
        )
        self.runs.append(run)
        return run

    async def _think(self, item: _Queued) -> None:
        gate = LlmRequest(role=ModelRole.GATE, system="gate", user=item.event_id)
        if not (await self.model.generate(gate, GateDecision)).parsed.act:
            return
        first = ChatRequest(role=ModelRole.AGENT, system="agent", user=item.event_id)
        reply = await self.model.chat(first, AgentOutcome)
        again = ChatRequest(
            role=ModelRole.AGENT, system="agent", user=item.event_id, history=(reply.turn,)
        )
        await self.model.chat(again, AgentOutcome)


@dataclass
class Rig:
    """A player on a fake runtime, with the fake model behind the counting one."""

    scenario: Scenario
    timers: tuple[str, ...] = ()  # local HH:MM on DAY, seeded before the play
    follow_up_minutes: int | None = None
    fires: bool = True
    judged: bool = True
    fake: FakeModel = field(default_factory=FakeModel)
    blobs: MemoryBlobStore = field(default_factory=MemoryBlobStore)

    def __post_init__(self) -> None:
        self.clock = VirtualClock(scenario_start(self.scenario))
        self.model = CountingModel(self.fake)
        self.runtime = FakeRuntime(
            self.clock, self.model, follow_up_minutes=self.follow_up_minutes, fires=self.fires
        )
        for hhmm in self.timers:
            self.runtime.seed_timer(la(hhmm))
        self.player = ScenarioPlayer(
            self.scenario,
            load=read_load(EVALS, self.scenario),
            runtime=self.runtime,
            clock=self.clock,
            model=self.model,
            blobs=self.blobs,
            files=read_attachments(EVALS, self.scenario),
            judge=Judge(self.fake, PROMPTS.judge) if self.judged else None,
        )

    async def play(self) -> list[StepObservation]:
        await self.player.start()
        return [await self.player.play(i) for i in range(len(self.scenario.steps))]


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


def message(at: str, body: str = "ok", **fields: Any) -> dict[str, Any]:
    return {"at": at, "message": {"channel": "sms", "sender": DRIVER, "body": body, **fields}}


# --- time ---------------------------------------------------------------------------


async def test_the_clock_stops_at_every_pending_timer_before_the_step_in_order() -> None:
    rig = Rig(scenario({"at": "08:00"}), timers=("07:20", "07:10", "09:00"))

    (observation,) = await rig.play()

    assert rig.runtime.ticks == [la("07:10"), la("07:20")]
    assert rig.clock.now() == la("08:00")
    assert [t.status for t in rig.runtime.timers] == [
        TimerStatus.FIRED,
        TimerStatus.FIRED,
        TimerStatus.PENDING,
    ]
    assert [o.created_at for o in observation.outbox] == [la("07:10"), la("07:20")]


async def test_timers_scheduled_on_the_way_are_followed_too() -> None:
    rig = Rig(scenario(ping("07:00"), {"at": "08:30"}), follow_up_minutes=30)

    observations = await rig.play()

    assert rig.runtime.ticks == [la("07:01"), la("07:31"), la("08:01")]
    assert [r.started_at for r in observations[1].runs] == [la("07:31"), la("08:01")]
    assert observations[1].snapshot.timers[-1].due_at == la("08:31")


async def test_a_timer_due_at_the_step_time_fires_before_the_injection() -> None:
    rig = Rig(scenario(ping("07:00")), timers=("07:00",))

    (observation,) = await rig.play()

    assert rig.runtime.ticks == [la("07:00"), la("07:01")]
    assert [r.event_id for r in observation.runs] == ["timer:timer_1", "risk:s1:0"]


async def test_an_overdue_timer_fires_at_the_current_time() -> None:
    rig = Rig(scenario({"at": "07:00"}), timers=("06:00",))

    await rig.play()

    assert rig.runtime.ticks == [la("06:55")]


async def test_an_injection_runs_after_the_latency() -> None:
    rig = Rig(scenario(ping("07:00"), run_latency_seconds=90))

    (observation,) = await rig.play()

    assert rig.runtime.ticks == [la("07:01:30")]
    assert rig.runtime.pings[0].occurred_at == la("07:00")  # ingested at the step's time
    assert [r.started_at for r in observation.runs] == [la("07:01:30")]


async def test_latency_never_runs_past_the_next_step() -> None:
    rig = Rig(scenario(ping("07:00"), ping("07:00:30"), ping("07:00:30")))

    await rig.play()

    assert rig.runtime.ticks == [la("07:00:30"), la("07:00:30"), la("07:01:30")]


async def test_a_timer_due_inside_the_latency_window_fires_on_the_tick_after() -> None:
    """The player stops at timers due by the step's time only: one due during the
    injection's latency fires late, on the injection's own tick."""
    rig = Rig(scenario(ping("07:00")), timers=("07:00:30",))

    (observation,) = await rig.play()

    assert rig.runtime.ticks == [la("07:01")]
    assert {(r.event_id, r.started_at) for r in observation.runs} == {
        ("risk:s1:0", la("07:01")),
        ("timer:timer_1", la("07:01")),
    }


async def test_a_step_without_injection_only_advances_time() -> None:
    rig = Rig(scenario({"at": "07:30"}))

    (observation,) = await rig.play()

    assert rig.runtime.ticks == []
    assert rig.runtime.messages == [] and rig.runtime.pings == []
    assert rig.clock.now() == la("07:30")
    assert (observation.outbox, observation.runs, observation.llm_calls) == ((), (), 0)


async def test_step_times_are_local_to_the_scenario_timezone() -> None:
    rig = Rig(scenario(ping("07:00:15"), timezone="America/New_York"))

    (observation,) = await rig.play()

    assert observation.at == datetime(2026, 10, 13, 11, 0, 15, tzinfo=UTC)
    assert observation.at.tzinfo is ZoneInfo("America/New_York")
    assert scenario_start(rig.scenario) == datetime(2026, 10, 13, 10, 55, tzinfo=UTC)
    assert rig.runtime.pings[0].occurred_at == observation.at


def test_local_time_reads_seconds_too() -> None:
    assert local_time(DAY, "07:00:15", LA) == la("07:00:15")


async def test_a_runtime_that_never_fires_a_due_timer_is_an_error_not_a_hang() -> None:
    rig = Rig(scenario({"at": "07:30"}), timers=("07:10",), fires=False)

    with pytest.raises(RunnerError, match="did not fire the timer due at"):
        await rig.play()


# --- injections and ids -------------------------------------------------------------


async def test_injections_carry_derived_ids_and_the_step_time() -> None:
    steps = (
        message("07:00"),
        message("07:05", message_id="sms-42"),
        {"at": "07:10", "ping": {"delay_risk": "medium", "lat": 1.5, "lon": -2.5}},
    )
    rig = Rig(scenario(*steps))
    rig.fake.push(ModelRole.GATE, ACK, ACK)

    await rig.play()

    runtime = rig.runtime
    assert [m.message_id for m in runtime.messages] == ["s1:0", "sms-42"]
    first = runtime.messages[0]
    assert (first.load_id, first.channel, first.sender_contact_id, first.body) == (
        LOAD_ID,
        Channel.SMS,
        DRIVER,
        "ok",
    )
    assert first.occurred_at == la("07:00")
    assert first.attachments == ()
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


async def test_an_unknown_sender_is_injected_without_a_contact() -> None:
    step = message("07:00")
    step["message"]["sender"] = None
    rig = Rig(scenario(step))
    rig.fake.push(ModelRole.GATE, ACK)

    await rig.play()

    assert rig.runtime.messages[0].sender_contact_id is None


async def test_attachments_go_into_the_blob_store_and_ride_on_the_message() -> None:
    pod = {"filename": "pod.pdf", "fixture": POD}
    again = {"filename": "pod-copy.pdf", "fixture": POD, "content_type": "image/png"}
    rig = Rig(scenario(message("07:00", body="POD attached", attachments=[pod, again])))
    rig.fake.push(ModelRole.GATE, ACK)

    await rig.play()

    data = (EVALS / POD).read_bytes()
    first, second = rig.runtime.messages[0].attachments
    assert (first.attachment_id, first.filename, first.content_type) == (
        "s1:0:1",
        "pod.pdf",
        "application/pdf",
    )
    assert (second.attachment_id, second.content_type) == ("s1:0:2", "image/png")
    assert first.ref == second.ref == blob_ref(data)
    assert await rig.blobs.get(first.ref) == data


def test_attachment_fixtures_are_read_once_before_the_play() -> None:
    pod = {"filename": "pod.pdf", "fixture": POD}
    steps = (message("07:00", attachments=[pod]), message("07:05", attachments=[pod]))

    files = read_attachments(EVALS, scenario(*steps))

    assert files == {POD: (EVALS / POD).read_bytes()}


async def test_observations_hold_only_what_the_step_created() -> None:
    rig = Rig(scenario(ping("07:00"), ping("07:10")))

    first, second = await rig.play()

    assert (first.step_index, second.step_index) == (0, 1)
    assert (first.at, second.at) == (la("07:00"), la("07:10"))
    assert [o.payload["event_id"] for o in first.outbox] == ["risk:s1:0"]
    assert [o.payload["event_id"] for o in second.outbox] == ["risk:s1:1"]
    assert [r.event_id for r in first.runs] == ["risk:s1:0"]
    assert [r.event_id for r in second.runs] == ["risk:s1:1"]
    assert len(second.snapshot.outbox) == 2
    assert len(second.snapshot.runs) == 2


# --- model calls and the judge ------------------------------------------------------


async def test_a_step_counts_its_model_calls_with_an_agent_loop_as_one() -> None:
    rig = Rig(scenario(message("07:00"), message("07:10", body="what's the PO?")))
    rig.fake.push(ModelRole.GATE, ACK, QUESTION)
    rig.fake.push(ModelRole.AGENT, Round(calls=(call("get_ledger"),)), AgentOutcome())

    first, second = await rig.play()

    assert (first.llm_calls, second.llm_calls) == (1, 2)  # gate; gate and one loop
    assert (rig.model.calls, rig.model.rounds) == (3, 4)


async def test_the_judge_grades_a_rubric_step_on_the_uncounted_model() -> None:
    rubric = {"rubric": "The SMS is polite.", "min_score": 0.6}
    rig = Rig(scenario({**ping("07:00"), "expect": {"judge": rubric}}))
    rig.fake.push(ModelRole.JUDGE, JudgeVerdict(reasons=("polite",), score=0.9))

    (observation,) = await rig.play()

    assert observation.llm_calls == 0  # the judge is not one of the agent's calls
    assert observation.judge is not None and observation.judge.verdict is not None
    assert (observation.judge.verdict.score, observation.judge.passed) == (0.9, True)
    assert "Hi there" in rig.fake.rounds_of(ModelRole.JUDGE)[0].request.user
    assert [u.role for u in rig.player.judge_usage] == ["judge"]
    assert rig.model.usage == []


async def test_without_a_judge_a_rubric_step_is_not_judged() -> None:
    rubric = {"rubric": "The SMS is polite."}
    rig = Rig(scenario({**ping("07:00"), "expect": {"judge": rubric}}), judged=False)

    (observation,) = await rig.play()

    assert observation.judge is None
    assert rig.fake.calls == []


async def test_a_step_without_a_rubric_never_calls_the_judge() -> None:
    rig = Rig(scenario(ping("07:00")))

    (observation,) = await rig.play()

    assert observation.judge is None
    assert rig.fake.calls == [] and rig.player.judge_usage == []


# --- the load -----------------------------------------------------------------------


def test_load_overrides_replace_top_level_fixture_keys() -> None:
    overrides = {
        "broker": {"id": "keystone", "name": "Keystone Freight"},
        "load_types": ["reefer"],
    }

    load = read_load(EVALS, scenario({"at": "07:00"}, load_overrides=overrides))

    assert (load.broker_id, load.broker_name, load.load_types) == (
        "keystone",
        "Keystone Freight",
        ("reefer",),
    )
    assert (load.load_id, load.shipper) == (LOAD_ID, "Riverbend Foods")


# --- play_scenario: setup errors ------------------------------------------------------


async def play(
    played: Scenario, model: FakeModel | None = None, *, evals: Path = EVALS
) -> tuple[Played, FakeModel]:
    model = model or FakeModel()
    return await play_scenario(
        played, evals_dir=evals, knowledge=SOPS, prompts=PROMPTS, model=model
    ), model


async def test_a_missing_fixture_is_an_error_result(tmp_path: Path) -> None:
    played, model = await play(scenario(ping("07:00")), evals=tmp_path)

    assert played.error == "load fixture not found: fixtures/load_481207.json"
    assert (played.scenario_id, played.observations, played.usage) == ("s1", (), ())
    assert model.calls == []


@pytest.mark.parametrize(
    ("content", "reason"),
    [("[1, 2]", "must be a JSON object"), ("{not json", "cannot read load fixture")],
)
async def test_an_unusable_fixture_is_an_error_result(
    tmp_path: Path, content: str, reason: str
) -> None:
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "load_481207.json").write_text(content)

    played, _ = await play(scenario(ping("07:00")), evals=tmp_path)

    assert played.error is not None
    assert reason in played.error
    assert played.observations == ()


@pytest.mark.parametrize("overrides", [{"stage": "flying"}, {"broker": "keystone"}])
async def test_invalid_load_metadata_is_an_error_result(overrides: dict[str, Any]) -> None:
    played, _ = await play(scenario(ping("07:00"), load_overrides=overrides))

    assert played.error is not None
    assert played.error.startswith("fixtures/load_481207.json: ")


async def test_an_unknown_timezone_is_an_error_result() -> None:
    played, _ = await play(scenario(ping("07:00"), timezone="Mars/Olympus"))

    assert played.error == "unknown timezone 'Mars/Olympus'"
    assert played.observations == ()


async def test_a_missing_attachment_fixture_is_a_setup_error() -> None:
    nope = {"filename": "x.pdf", "fixture": "fixtures/docs/nope.pdf"}

    played, model = await play(scenario(message("07:00", attachments=[nope])))

    assert played.error == "attachment fixture not found: fixtures/docs/nope.pdf"
    assert played.observations == ()
    assert model.calls == []


# --- play_scenario: the real scenario files -------------------------------------------


def real(scenario_id: str, suite: str = "core") -> Scenario:
    return load_scenario(EVALS / "scenarios" / suite / f"{scenario_id}.yaml")


def eta_request(text: str = "Hi, what is your ETA to the Riverbend pickup?") -> Plan:
    return Plan(
        rationale="delay_risk_high: ask the driver for the ETA, follow up in 30 minutes",
        actions=(
            SendSms(contact_id=DRIVER, text=text, purpose=Purpose.ETA_REQUEST),
            ScheduleTimer(kind="eta_request", due_in_minutes=30),
        ),
    )


def scenario_a_agent() -> FakeModel:
    """A right agent for scenario A: each step's rounds, as `Plan` shorthands (one round
    staging the actions, then one round answering)."""
    model = FakeModel()
    model.push(ModelRole.AGENT, eta_request())
    model.push(ModelRole.GATE, QUESTION)
    answer = SendSms(contact_id=DRIVER, text="The PO number is 55821.", purpose=Purpose.REPLY)
    model.push(ModelRole.AGENT, Plan(rationale="driver_questions", actions=(answer,)))
    model.push(ModelRole.AGENT, eta_request("Checking again: what is your ETA to the pickup?"))
    escalate = (
        SendEmail(
            to_contact_ids=(DISPATCHER,),
            subject="Load 481207: driver ETA needed",
            body="The driver has not answered two ETA requests. Please send the ETA.",
            purpose=Purpose.ESCALATION,
        ),
        CreateHeroTask(title="Call the driver", description="No ETA after two SMS."),
        CloseObligation(kind="eta_request", status=CloseStatus.EXHAUSTED),
    )
    model.push(ModelRole.AGENT, Plan(rationale="escalation", actions=escalate))
    return model


async def test_scenario_a_played_with_the_right_rounds_passes_every_check() -> None:
    a = real("a_client_a_high_risk")
    seen: list[int] = []

    played = await play_scenario(
        a,
        evals_dir=EVALS,
        knowledge=SOPS,
        prompts=PROMPTS,
        model=scenario_a_agent(),
        on_step=lambda o: seen.append(o.step_index),
    )

    assert played.error is None
    assert grade(a, played.observations) == []
    assert seen == [0, 1, 2, 3]  # on_step after every step
    # Two rounds per agent loop, each counted once in `llm_calls`.
    assert [o.llm_calls for o in played.observations] == [1, 2, 1, 1]
    assert [u.role for u in played.usage] == ["agent", "agent", "gate", *["agent"] * 6]
    assert played.judge_usage == ()


async def test_scenario_a_step_0_passes_with_one_counted_call_for_two_rounds() -> None:
    a = real("a_client_a_high_risk")
    step_0 = a.model_copy(update={"steps": a.steps[:1]})
    model = FakeModel()
    model.push(ModelRole.AGENT, eta_request())

    played, _ = await play(step_0, model)

    (observation,) = played.observations
    assert grade(step_0, played.observations) == []
    assert observation.llm_calls == 1
    assert len(model.rounds_of(ModelRole.AGENT)) == 2
    assert [(i.kind, i.payload["contact_id"]) for i in observation.outbox] == [
        (OutboxKind.SMS, DRIVER)
    ]
    assert model.remaining() == 0


async def test_an_ok_from_the_driver_ends_at_the_gate() -> None:
    b1 = real("b_ack_with_nothing_open")
    model = FakeModel()
    model.push(ModelRole.GATE, ACK)

    played, _ = await play(b1, model)

    assert played.error is None
    assert grade(b1, played.observations) == []
    assert [c.role for c in model.calls] == [ModelRole.GATE]


async def test_the_judge_runs_after_a_rubric_step_and_its_usage_is_kept_apart() -> None:
    a = real("a_client_a_high_risk")
    rubric = JudgeSpec(rubric="The SMS is polite and asks for the ETA.", min_score=0.7)
    step = a.steps[0].model_copy(
        update={"expect": a.steps[0].expect.model_copy(update={"judge": rubric})}
    )
    judged = a.model_copy(update={"steps": (step,)})
    model = FakeModel()
    model.push(ModelRole.AGENT, eta_request())
    model.push(ModelRole.JUDGE, JudgeVerdict(reasons=("polite", "asks"), score=0.8))

    played, _ = await play(judged, model)

    (observation,) = played.observations
    assert grade(judged, played.observations) == []
    assert observation.llm_calls == 1  # the scenario's own expect.llm_calls still holds
    assert observation.judge is not None and observation.judge.passed
    assert "Riverbend pickup" in model.rounds_of(ModelRole.JUDGE)[0].request.user
    assert [u.role for u in played.usage] == ["agent", "agent"]
    assert [u.role for u in played.judge_usage] == ["judge"]


async def test_the_cost_adds_up_the_agent_rounds() -> None:
    a = real("a_client_a_high_risk")
    step_0 = a.model_copy(update={"steps": a.steps[:1]})
    model = FakeModel()
    staging = Round(calls=tuple(act(a) for a in eta_request().actions), cost_usd=0.01)
    model.push(ModelRole.AGENT, staging, Round(answer=AgentOutcome(), cost_usd=0.02))

    played, _ = await play(step_0, model)

    assert played.error is None
    assert played.cost_usd == pytest.approx(0.03)


async def test_a_runtime_error_stops_the_play_and_keeps_the_steps_so_far() -> None:
    a = real("a_client_a_high_risk")
    model = FakeModel()
    model.push(ModelRole.AGENT, eta_request())
    model.push(ModelRole.GATE, RuntimeError("boom"))

    played, _ = await play(a, model)

    assert played.error == "step 1: RuntimeError: boom"
    assert [o.step_index for o in played.observations] == [0]
    assert [u.role for u in played.usage] == ["agent", "agent"]  # step 0's rounds


async def test_a_model_failure_escalates_and_the_play_goes_on() -> None:
    b1 = real("b_ack_with_nothing_open")
    model = FakeModel()
    model.push(ModelRole.GATE, LlmError("provider down"))

    played, _ = await play(b1, model)

    assert played.error is None
    (observation,) = played.observations
    assert [r.status for r in observation.runs] == [RunStatus.ESCALATED]
    assert observation.llm_calls == 1  # a failed call still counts


async def test_an_error_in_a_step_callback_ends_the_play_like_a_runtime_error() -> None:
    b1 = real("b_ack_with_nothing_open")
    model = FakeModel()
    model.push(ModelRole.GATE, ACK)

    def broken(observation: StepObservation) -> None:
        raise ValueError("ui gone")

    played = await play_scenario(
        b1, evals_dir=EVALS, knowledge=SOPS, prompts=PROMPTS, model=model, on_step=broken
    )

    assert played.error == "step 0: ValueError: ui gone"
    assert len(played.observations) == 1


# --- the counting wrapper and the trial runtime ---------------------------------------


async def test_counting_model_counts_forced_answers_and_agent_loops_once() -> None:
    inner = FakeModel()
    inner.push(ModelRole.GATE, ACK, LlmError("down"))
    inner.push(
        ModelRole.AGENT,
        Round(calls=(call("get_ledger"),), cost_usd=0.25),
        Round(answer=AgentOutcome(), cost_usd=0.5),
    )
    model = CountingModel(inner)
    gate = LlmRequest(role=ModelRole.GATE, system="s", user="u")
    first = ChatRequest(role=ModelRole.AGENT, system="s", user="u")

    assert (await model.generate(gate, GateDecision)).parsed == ACK
    reply = await model.chat(first, AgentOutcome)
    await model.chat(
        ChatRequest(role=ModelRole.AGENT, system="s", user="u", history=(reply.turn,)),
        AgentOutcome,
    )
    with pytest.raises(LlmError):
        await model.generate(gate, GateDecision)

    assert (model.calls, model.rounds) == (3, 4)  # the failed call counts too
    assert [u.role for u in model.usage] == ["gate", "agent", "agent"]
    assert model.cost_usd == pytest.approx(0.75)


def test_the_trial_runtime_is_the_product_runtime_on_the_counted_model() -> None:
    start = la("06:55")
    model = FakeModel()

    trial = build_trial_runtime(model, SOPS, start, PROMPTS)

    assert isinstance(trial.runtime, AgentRuntime)
    assert isinstance(trial.model, CountingModel)
    assert trial.clock.now() == start
    deps = trial.runtime._deps  # pyright: ignore[reportPrivateUsage]
    assert (deps.clock, deps.llm, deps.knowledge, deps.blobs) == (
        trial.clock,
        trial.model,
        SOPS,
        trial.blobs,
    )
    assert isinstance(deps.documents, DocumentPipeline)
    assert isinstance(deps.ids, SequentialIdGenerator)
    assert isinstance(deps.tracer, NullTracer)
    assert deps.config.reraise_unexpected  # a harness bug ends the play with its message
