"""Wave 3 runner features: scripted transcripts, extraction scripts, attachments, the judge
and broker exports.

A probe runtime stands in for `AgentRuntime` (as `FakeRuntime` does in `test_runner.py`),
so these pin what the runner gives the runtime and the model before the agent loop and
the document pipeline exist: what the agent model returns (its transcript), what the
extraction model is asked, which bytes an attachment carries, which model the judge uses.
"""

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from freight_agent.adapters.documents.memory_blobs import MemoryBlobStore, blob_ref
from freight_agent.adapters.knowledge.memory import MemorySopIndex
from freight_agent.adapters.llm.scripted import ScriptedModel, ScriptedReply
from freight_agent.application.contracts import (
    InboundMessage,
    IngestResult,
    LoadSnapshot,
    LocationPing,
)
from freight_agent.application.documents import NullDocumentProcessor
from freight_agent.application.runtime import AgentRuntime
from freight_agent.domain.actions import AgentOutcome, SendSms
from freight_agent.domain.broker import new_broker
from freight_agent.domain.intents import GateDecision
from freight_agent.domain.load import Load
from freight_agent.domain.outbox import OutboxItem, OutboxKind
from freight_agent.domain.runs import RunRecord, RunStatus, Usage
from freight_agent.domain.sop import SopLayer, SopSection
from freight_agent.evalkit import runner as runner_module
from freight_agent.evalkit.brokers import BrokerExport, BrokerSopIndex
from freight_agent.evalkit.runner import (
    SCRIPTED_TOOL_RESULT,
    BrokerSeededRuntime,
    ScenarioRunner,
    TrialDeps,
    build_trial_runtime,
    document_processor,
    scripted_reply,
    scripted_transcript,
)
from freight_agent.evalkit.spec import AgentTurn, Scenario, ScriptedToolCall
from freight_agent.ports.clock import Clock
from freight_agent.ports.gateways import Gateways
from freight_agent.ports.knowledge import SopIndex
from freight_agent.ports.llm import LanguageModel, LlmRequest, LlmResponse, ModelRole, SchemaT

ROOT = Path(__file__).resolve().parents[2]
EVALS = ROOT / "evals"
LOAD_ID = "481207"
NO_SOP = MemorySopIndex((), version="test")
POD = "fixtures/docs/pod_clean.pdf"


class ExtractedValue(BaseModel):
    name: str
    value: str | None


class Extraction(BaseModel):
    """Stands in for the pipeline's schema, `domain.documents.Extraction` (card W3-DOC)."""

    type_id: str | None
    confidence: float
    fields: tuple[ExtractedValue, ...] = ()
    notes: str = ""


@dataclass
class ProbeRuntime:
    """Each message: an extract call per attachment, a gate call, and an agent call when
    the gate acts; every SMS of the agent's answer becomes an outbox item."""

    deps: TrialDeps
    load: Load | None = None
    messages: list[InboundMessage] = field(default_factory=list[InboundMessage])
    queue: list[InboundMessage] = field(default_factory=list[InboundMessage])
    replies: list[LlmResponse[AgentOutcome]] = field(
        default_factory=list[LlmResponse[AgentOutcome]]
    )
    extractions: list[Extraction] = field(default_factory=list[Extraction])
    outbox: list[OutboxItem] = field(default_factory=list[OutboxItem])
    runs: list[RunRecord] = field(default_factory=list[RunRecord])

    async def register_load(self, load: Load) -> None:
        self.load = load

    async def ingest_message(self, message: InboundMessage) -> IngestResult:
        self.messages.append(message)
        self.queue.append(message)
        return IngestResult(accepted=True, event_id=message.message_id)

    async def ingest_ping(self, ping: LocationPing) -> IngestResult:
        return IngestResult(accepted=True)

    async def tick(self) -> list[RunRecord]:
        model, now = self.deps.model, self.deps.clock.now()
        for message in self.queue:
            for _ in message.attachments:
                request = LlmRequest(role=ModelRole.EXTRACT, system="x", user="pod")
                self.extractions.append((await model.generate(request, Extraction)).parsed)
            gate = LlmRequest(role=ModelRole.GATE, system="gate", user=message.body)
            if (await model.generate(gate, GateDecision)).parsed.act:
                agent = LlmRequest(role=ModelRole.AGENT, system="agent", user=message.body)
                reply = await model.generate(agent, AgentOutcome)
                self.replies.append(reply)
                for action in reply.parsed.actions:
                    if isinstance(action, SendSms):
                        self.outbox.append(
                            OutboxItem(
                                outbox_id=f"out_{len(self.outbox) + 1}",
                                idempotency_key=f"{message.message_id}:{len(self.outbox)}",
                                run_id="run_1",
                                load_id=LOAD_ID,
                                kind=OutboxKind.SMS,
                                payload={"contact_id": action.contact_id, "text": action.text},
                                created_at=now,
                            )
                        )
            self.runs.append(
                RunRecord(
                    run_id=f"run_{len(self.runs) + 1}",
                    event_id=message.message_id,
                    load_id=LOAD_ID,
                    started_at=now,
                    status=RunStatus.ACTED,
                )
            )
        self.queue.clear()
        return []

    async def next_timer_due_at(self) -> datetime | None:
        return None

    async def snapshot(self, load_id: str) -> LoadSnapshot:
        assert self.load is not None
        return LoadSnapshot(
            load=self.load,
            obligations=(),
            timers=(),
            events=(),
            outbox=tuple(self.outbox),
            runs=tuple(self.runs),
        )


@dataclass
class Probes:
    created: list[ProbeRuntime] = field(default_factory=list[ProbeRuntime])

    def __call__(self, deps: TrialDeps) -> ProbeRuntime:
        self.created.append(ProbeRuntime(deps))
        return self.created[-1]

    @property
    def runtime(self) -> ProbeRuntime:
        return self.created[-1]


def scenario(*steps: dict[str, Any], **fields: Any) -> Scenario:
    return Scenario.model_validate(
        {
            "id": "s1",
            "title": "t",
            "suite": "core",
            "load": "fixtures/load_481207.json",
            "date": "2026-10-13",
            "start": "06:55",
            "steps": list(steps),
            **fields,
        }
    )


GATE_ACT = {"intent": "question", "act": True, "reason": "asks"}
POLITE = {"type": "send_sms", "contact_id": "drv_20931", "text": "Hi, what is your ETA, please?"}


def message(body: str = "what's the PO?", **extra: Any) -> dict[str, Any]:
    step: dict[str, Any] = {
        "at": "07:00",
        "message": {"channel": "sms", "sender": "drv_20931", "body": body},
    }
    attachments = extra.pop("attachments", None)
    if attachments is not None:
        step["message"]["attachments"] = attachments
    return {**step, **extra}


def scripted(probes: Probes, evals: Path = EVALS, **kwargs: Any) -> ScenarioRunner:
    return ScenarioRunner(evals, NO_SOP, "scripted", trial_factory=probes, **kwargs)


# --- scripted transcripts ---------------------------------------------------------------


@pytest.mark.unit
def test_tool_calls_become_the_openrouter_transcript_one_round_per_call() -> None:
    calls = (
        ScriptedToolCall(name="search_sop", args={"query": "lumper"}, result="1. escalation"),
        ScriptedToolCall(name="send_email", args={"to_contact_ids": ["x@y.example"]}),
    )

    transcript = scripted_transcript(calls)

    assert transcript == (
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "name": "search_sop", "args": {"query": "lumper"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "search_sop",
            "content": "1. escalation",
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_2", "name": "send_email", "args": {"to_contact_ids": ["x@y.example"]}}
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_2",
            "name": "send_email",
            "content": SCRIPTED_TOOL_RESULT,
        },
    )


@pytest.mark.unit
def test_a_turn_without_tool_calls_is_the_bare_outcome() -> None:
    turn = AgentTurn.model_validate({"rationale": "r", "intent": "other", "no_procedure": True})

    reply = scripted_reply(turn)

    assert reply == AgentOutcome(rationale="r", intent="other", no_procedure=True)
    assert type(reply) is AgentOutcome


@pytest.mark.unit
def test_a_turn_with_tool_calls_is_a_scripted_reply() -> None:
    turn = AgentTurn.model_validate({"tool_calls": [{"name": "get_ledger"}], "actions": [POLITE]})

    reply = scripted_reply(turn)

    assert isinstance(reply, ScriptedReply)
    assert type(reply.parsed) is AgentOutcome
    assert len(reply.parsed.actions) == 1
    assert reply.transcript[0]["tool_calls"] == [{"id": "call_1", "name": "get_ledger", "args": {}}]


@pytest.mark.unit
async def test_the_agent_model_returns_the_scripted_transcript_to_the_runtime() -> None:
    probes = Probes()
    turn = {"tool_calls": [{"name": "search_sop", "args": {"query": "po"}}], "actions": [POLITE]}
    step = message(script={"gate": [GATE_ACT], "agent": [turn]})

    outcome = await scripted(probes).run(scenario(step))

    assert outcome.failures == () and outcome.error is None
    (reply,) = probes.runtime.replies
    assert [e["role"] for e in reply.transcript] == ["assistant", "tool"]
    assert reply.parsed.actions == (SendSms.model_validate(POLITE),)


# --- extraction scripts and attachments --------------------------------------------------

EXTRACTED = {
    "type_id": "pod",
    "confidence": 0.97,
    "fields": [
        {"name": "receiver", "value": "Valley DC"},
        {"name": "po_numbers", "value": "55821"},
    ],
    "notes": "",
}


@pytest.mark.unit
async def test_an_attachment_carries_the_fixture_bytes_in_the_trials_blob_store() -> None:
    probes = Probes()
    attachments = [{"filename": "pod.pdf", "fixture": POD}]
    step = message(
        "POD attached",
        attachments=attachments,
        script={"gate": [GATE_ACT], "agent": [{}], "extract": [EXTRACTED]},
    )

    outcome = await scripted(probes).run(scenario(step))

    assert outcome.failures == () and outcome.error is None
    (sent,) = probes.runtime.messages
    (attachment,) = sent.attachments
    data = (EVALS / POD).read_bytes()
    assert (attachment.attachment_id, attachment.filename) == ("s1:0:1", "pod.pdf")
    assert (attachment.content_type, attachment.ref) == ("application/pdf", blob_ref(data))
    assert await probes.runtime.deps.blobs.get(attachment.ref) == data
    assert probes.runtime.extractions == [Extraction.model_validate(EXTRACTED)]
    assert outcome.observations[0].llm_calls == 3  # extract, gate, agent


@pytest.mark.unit
async def test_a_missing_attachment_fixture_is_a_setup_error() -> None:
    probes = Probes()
    step = message(attachments=[{"filename": "x.pdf", "fixture": "fixtures/docs/nope.pdf"}])

    outcome = await scripted(probes).run(scenario(step))

    assert outcome.error == "attachment fixture not found: fixtures/docs/nope.pdf"
    assert probes.created == []


@pytest.mark.unit
async def test_unused_and_missing_extraction_entries_fail_the_step() -> None:
    unused = message(script={"gate": [GATE_ACT], "agent": [{}], "extract": [EXTRACTED]})
    missing = message(
        attachments=[{"filename": "pod.pdf", "fixture": POD}],
        script={"gate": [GATE_ACT], "agent": [{}]},
    )

    left = await scripted(Probes()).run(scenario(unused))
    called = await scripted(Probes()).run(scenario(missing))

    assert [f.message for f in left.failures] == ["1 scripted extract output(s) were not used"]
    assert called.error is not None and "ScriptExhausted" in called.error


# --- the judge --------------------------------------------------------------------------

RUBRIC = {"rubric": "The SMS is polite.", "min_score": 0.6}
NICE = {"reasons": ["polite"], "score": 0.9}


@pytest.mark.unit
async def test_the_judge_grades_the_step_on_the_inner_model_outside_llm_calls() -> None:
    step = message(
        script={"gate": [GATE_ACT], "agent": [{"actions": [POLITE]}], "judge": [NICE]},
        expect={"judge": RUBRIC},
    )

    outcome = await scripted(Probes()).run(scenario(step))

    assert outcome.failures == () and outcome.error is None
    observation = outcome.observations[0]
    assert observation.llm_calls == 2  # gate and agent; the judge is not counted
    assert [u.role for u in outcome.usage] == ["gate", "agent"]
    assert [u.role for u in outcome.judge_usage] == ["judge"]
    assert observation.judge is not None and observation.judge.verdict is not None
    assert observation.judge.verdict.score == 0.9
    assert outcome.warnings == ()


@pytest.mark.unit
async def test_a_rubric_without_a_scripted_verdict_is_skipped_with_a_warning() -> None:
    step = message(
        script={"gate": [GATE_ACT], "agent": [{"actions": [POLITE]}]}, expect={"judge": RUBRIC}
    )

    outcome = await scripted(Probes()).run(scenario(step))

    assert outcome.failures == ()
    judge = outcome.observations[0].judge
    assert judge is not None and judge.skipped is not None
    assert outcome.warnings == (
        "step 0: the rubric was not judged (scripted mode, no script.judge)",
    )
    assert outcome.judge_usage == ()


@pytest.mark.unit
async def test_a_scripted_verdict_without_a_rubric_is_an_unused_entry() -> None:
    step = message(script={"gate": [GATE_ACT], "agent": [{}], "judge": [NICE]})

    outcome = await scripted(Probes()).run(scenario(step))

    assert [f.message for f in outcome.failures] == ["1 scripted judge output(s) were not used"]


class ReplayModel:
    """A stand-in replay model: answers every role from its queue, counts nothing."""

    def __init__(self) -> None:
        self.model = ScriptedModel(model_name="replay/model")
        self.model.push(ModelRole.GATE, GateDecision.model_validate(GATE_ACT))
        self.model.push(ModelRole.AGENT, AgentOutcome.model_validate({"actions": [POLITE]}))
        self.model.push(ModelRole.JUDGE, BaseModelVerdict(score=0.1, reasons=("rude",)))

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        response = await self.model.generate(request, schema)
        usage = Usage(role=request.role.value, model="replay/model", cost_usd=0.01)
        return LlmResponse(parsed=response.parsed, usage=usage)


class BaseModelVerdict(BaseModel):
    score: float
    reasons: tuple[str, ...]


@pytest.mark.unit
async def test_in_replay_the_given_model_judges_and_its_cost_is_reported_apart() -> None:
    replay = ReplayModel()
    runner = ScenarioRunner(EVALS, NO_SOP, "replay", model=replay, trial_factory=Probes())
    step = message(expect={"judge": RUBRIC}, script={"judge": [NICE]})  # the script is ignored

    outcome = await runner.run(scenario(step))

    judge = outcome.observations[0].judge
    assert judge is not None and judge.verdict is not None
    assert judge.verdict.score == 0.1
    assert outcome.observations[0].llm_calls == 2
    assert sum(u.cost_usd for u in outcome.judge_usage) == 0.01
    assert replay.model.remaining() == 0


# --- broker exports ---------------------------------------------------------------------


def acme_export() -> BrokerExport:
    section = SopSection(
        section_id="detention",
        layer=SopLayer.CLIENT,
        client_id="acme",
        title="Detention",
        description="Truck held over 2 hours.",
        intents=("detention_request",),
        body="Write a TMS note.",
        source="acme",
    )
    return BrokerExport(broker=new_broker("acme", "Acme Freight"), sections=(section,))


def evals_with_export(tmp_path: Path) -> Path:
    evals = tmp_path / "evals"
    (evals / "fixtures").mkdir(parents=True)
    (evals / "brokers").mkdir()
    shutil.copy(EVALS / "fixtures" / "load_481207.json", evals / "fixtures")
    (evals / "brokers" / "acme.json").write_text(acme_export().to_json(), encoding="utf-8")
    return evals


@pytest.mark.unit
async def test_a_scenarios_broker_file_puts_the_load_on_the_broker_with_its_index(
    tmp_path: Path,
) -> None:
    probes = Probes()
    evals = evals_with_export(tmp_path)

    outcome = await scripted(probes, evals).run(
        scenario({"at": "07:00"}, broker_file="brokers/acme.json")
    )

    assert outcome.error is None
    runtime = probes.runtime
    assert runtime.load is not None
    assert (runtime.load.broker_id, runtime.load.broker_name) == ("acme", "Acme Freight")
    assert runtime.deps.broker == acme_export().broker
    assert isinstance(runtime.deps.knowledge, BrokerSopIndex)
    visible = await runtime.deps.knowledge.visible("acme", ())
    assert [s.section_id for s in visible] == ["detention"]


@pytest.mark.unit
async def test_the_runners_broker_wins_over_the_scenarios(tmp_path: Path) -> None:
    probes = Probes()
    other = BrokerExport(broker=new_broker("beta", "Beta"))
    runner = scripted(probes, evals_with_export(tmp_path), broker=other)

    await runner.run(scenario({"at": "07:00"}, broker_file="brokers/acme.json"))

    assert probes.runtime.load is not None and probes.runtime.load.broker_id == "beta"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("content", "error"),
    [(None, "broker file not found: brokers/acme.json"), ("{}", "is not a broker export")],
)
async def test_a_bad_broker_file_is_a_setup_error(
    tmp_path: Path, content: str | None, error: str
) -> None:
    evals = evals_with_export(tmp_path)
    path = evals / "brokers" / "acme.json"
    if content is None:
        path.unlink()
    else:
        path.write_text(content)

    outcome = await scripted(Probes(), evals).run(
        scenario({"at": "07:00"}, broker_file="brokers/acme.json")
    )

    assert outcome.error is not None and error in outcome.error


@pytest.mark.unit
async def test_a_pre_wave_3_runtime_factory_cannot_seed_a_broker(tmp_path: Path) -> None:
    def legacy(
        clock: Clock, model: LanguageModel, gateways: Gateways, knowledge: SopIndex
    ) -> ProbeRuntime:
        raise AssertionError("never built")

    runner = ScenarioRunner(evals_with_export(tmp_path), NO_SOP, "scripted", runtime_factory=legacy)

    outcome = await runner.run(scenario({"at": "07:00"}, broker_file="brokers/acme.json"))

    assert outcome.error is not None and "cannot be seeded" in outcome.error


@pytest.mark.unit
def test_runtime_and_trial_factories_are_exclusive() -> None:
    with pytest.raises(ValueError, match="not both"):
        ScenarioRunner(
            EVALS,
            NO_SOP,
            "scripted",
            runtime_factory=lambda *a: Probes()(a[0]),
            trial_factory=Probes(),
        )


# --- the default trial runtime ------------------------------------------------------------


def trial_deps(**fields: Any) -> TrialDeps:
    from freight_agent.adapters.channels.fakes.recording import RecordingGateways
    from freight_agent.adapters.clock import VirtualClock
    from freight_agent.evalkit.counting import CountingModel

    defaults: dict[str, Any] = {
        "clock": VirtualClock(datetime.fromisoformat("2026-10-13T07:00:00-07:00")),
        "model": CountingModel(ScriptedModel()),
        "gateways": RecordingGateways().as_gateways(),
        "knowledge": NO_SOP,
        "blobs": MemoryBlobStore(),
    }
    return TrialDeps(**{**defaults, **fields})


def fixture_load() -> Load:
    return Load.from_metadata(json.loads((EVALS / "fixtures" / "load_481207.json").read_text()))


@pytest.mark.unit
def test_the_default_trial_runtime_is_the_product_runtime_with_the_trials_blobs() -> None:
    deps = trial_deps()

    runtime = build_trial_runtime(deps)

    assert isinstance(runtime, AgentRuntime)
    inner = runtime._deps  # pyright: ignore[reportPrivateUsage]
    assert inner.blobs is deps.blobs
    assert isinstance(inner.documents, NullDocumentProcessor)  # until card W3-DOC
    assert inner.config.reraise_unexpected


@pytest.mark.unit
async def test_a_broker_is_saved_in_the_trials_store_before_the_load() -> None:
    broker = new_broker("acme", "Acme Freight")

    runtime = build_trial_runtime(trial_deps(broker=broker))
    assert isinstance(runtime, BrokerSeededRuntime)
    await runtime.register_load(fixture_load())

    agent = runtime.inner
    assert isinstance(agent, AgentRuntime)
    async with agent._deps.uow_factory() as uow:  # pyright: ignore[reportPrivateUsage]
        assert await uow.brokers.get("acme") == broker
    snapshot = await runtime.snapshot(LOAD_ID)
    assert snapshot.load.load_id == LOAD_ID
    assert await runtime.next_timer_due_at() is None
    assert await runtime.tick() == []


@pytest.mark.unit
async def test_the_trial_runtime_may_run_on_another_store() -> None:
    from freight_agent.adapters.persistence.memory.store import MemoryStore

    store = MemoryStore()
    broker = new_broker("acme", "Acme Freight")

    runtime = build_trial_runtime(trial_deps(broker=broker), uow_factory=store.unit_of_work)
    await runtime.register_load(fixture_load())

    async with store.unit_of_work() as uow:
        assert await uow.brokers.get("acme") == broker
        assert await uow.loads.get(LOAD_ID) is not None


@pytest.mark.unit
def test_the_document_pipeline_is_used_once_its_card_provides_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: dict[str, Any] = {}

    class Pipeline:
        def __init__(self, **kwargs: Any) -> None:
            built.update(kwargs)

        async def process_pending(self, limit: int = 10) -> list[Any]:
            return []

    class Pages:
        pass

    found = {"DocumentPipeline": Pipeline, "PdfiumPageReader": Pages}
    monkeypatch.setattr(runner_module, "_adapter", lambda module, attribute: found[attribute])
    deps = trial_deps()

    runtime = build_trial_runtime(deps)

    assert isinstance(runtime, AgentRuntime)
    assert isinstance(runtime._deps.documents, Pipeline)  # pyright: ignore[reportPrivateUsage]
    assert built["blobs"] is deps.blobs
    assert built["llm"] is deps.model
    assert isinstance(built["pages"], Pages)
    assert set(built) == {"uow_factory", "clock", "ids", "llm", "blobs", "pages", "timeout_s"}


@pytest.mark.unit
def test_an_adapter_whose_module_is_missing_is_none_and_a_broken_one_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = runner_module._adapter  # pyright: ignore[reportPrivateUsage]
    (tmp_path / "broken_card_module.py").write_text("import not_a_real_dependency\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    assert adapter("freight_agent.adapters.documents.not_yet", "X") is None
    assert adapter("freight_agent.adapters.documents.memory_blobs", "Nope") is None
    with pytest.raises(ModuleNotFoundError):
        adapter("broken_card_module", "X")
    assert document_processor is runner_module.document_processor
