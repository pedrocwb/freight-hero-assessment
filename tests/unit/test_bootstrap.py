"""The composition root, with a fake in place of OpenRouter: no network, and a key only
where a test sets a fake one."""

import json
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr

from freight_agent import bootstrap
from freight_agent.adapters.channels.fakes import recording
from freight_agent.adapters.channels.fakes.recording import RecordingGateways
from freight_agent.adapters.clock import SystemClock, UuidIdGenerator, VirtualClock
from freight_agent.adapters.documents.fs_blobs import FsBlobStore
from freight_agent.adapters.knowledge.memory import MemorySopIndex
from freight_agent.adapters.llm.cassette import CassetteMissing, CassetteModel, model_slug
from freight_agent.adapters.llm.models_config import (
    ModelsConfig,
    load_models_config,
)
from freight_agent.adapters.llm.resilient import ResilientModel
from freight_agent.adapters.llm.router import RoleRouter
from freight_agent.adapters.llm.scripted import ScriptedModel
from freight_agent.adapters.observability.langsmith import LangSmithTracer
from freight_agent.adapters.observability.null import NullTracer
from freight_agent.adapters.persistence.memory.store import MemoryStore, MemoryUnitOfWork
from freight_agent.adapters.sop.files import FileSopRepository
from freight_agent.application.documents import DocumentPipeline, NullDocumentProcessor
from freight_agent.application.runtime import AgentRuntime, RuntimeConfig, RuntimeDeps
from freight_agent.bootstrap import (
    BootstrapError,
    LazyAdapter,
    Simulation,
    build_llm,
    build_runtime,
    build_runtime_deps,
    build_simulation,
    build_tracer,
)
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.load import Load
from freight_agent.ports.gateways import Gateways
from freight_agent.ports.llm import LanguageModel, LlmError, LlmRequest, ModelRole
from freight_agent.settings import MissingSetting, Settings

ROOT = Path(__file__).resolve().parents[2]
MODELS_YAML = ROOT / "config" / "models.yaml"
SOPS = ROOT / "sops"
KEY = "sk-or-v1-bootstrap-test-secret"
LANGSMITH_KEY = "lsv2_pt_bootstrap-test-secret"
T0 = datetime(2026, 10, 13, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
GATE = LlmRequest(role=ModelRole.GATE, system="classify", user="driver: ok", max_tool_rounds=0)
ACK = GateDecision(intent=Intent.ACKNOWLEDGEMENT, act=False, reason="just ok")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No variable from the developer's shell, and no `.env` from the repository root."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def config() -> ModelsConfig:
    return load_models_config(MODELS_YAML)


@pytest.fixture
def clock() -> VirtualClock:
    return VirtualClock(T0)


def _settings(tmp_path: Path, **values: Any) -> Settings:
    """The shipped models and SOPs, cassettes in a temporary folder, no `.env`."""
    paths = {"models_config": MODELS_YAML, "sops_dir": SOPS, "cassettes_dir": tmp_path / "tapes"}
    options: dict[str, Any] = {"_env_file": None, **paths, **values}
    return Settings(**options)


@dataclass
class FakeOpenRouter:
    """Stands in for `models_for_role`: one scripted model per entry of a role's chain.

    A model id serves several roles in the shipped config, so each id gets one shared
    model, as one OpenRouter model would be."""

    keys: list[str] = field(default_factory=list)
    models: dict[str, ScriptedModel] = field(default_factory=dict)

    def __call__(
        self, config: ModelsConfig, role: ModelRole, key: SecretStr
    ) -> Sequence[tuple[str, LanguageModel]]:
        self.keys.append(key.get_secret_value())
        return [
            (entry.model, self.models.setdefault(entry.model, ScriptedModel(entry.model)))
            for entry in config.role(role).chain()
        ]


def _router(llm: LanguageModel) -> RoleRouter:
    assert isinstance(llm, RoleRouter)
    return llm


def _fake_module(monkeypatch: pytest.MonkeyPatch, adapter: LazyAdapter, value: Any) -> None:
    module = ModuleType(adapter.module)
    setattr(module, adapter.attribute, value)
    monkeypatch.setitem(sys.modules, adapter.module, module)


def _absent(monkeypatch: pytest.MonkeyPatch, adapter: LazyAdapter) -> None:
    """The adapter's module cannot be imported, whether or not its card has merged."""
    monkeypatch.setitem(sys.modules, adapter.module, None)


# --- build_llm: replay ---------------------------------------------------------


@pytest.mark.unit
async def test_replay_serves_each_role_from_the_cassettes_of_its_primary_model(
    tmp_path: Path, config: ModelsConfig, clock: VirtualClock
) -> None:
    router = _router(build_llm(_settings(tmp_path), clock=clock))

    assert router.roles == frozenset(config.roles)
    for role in config.roles:
        model = router.model_for(role)
        assert isinstance(model, CassetteModel)
        folder = model.path_for(GATE, GateDecision).parent
        assert folder == tmp_path / "tapes" / model_slug(config.role(role).model)
    with pytest.raises(CassetteMissing):  # replays only: nothing recorded, nothing called
        await router.generate(GATE, GateDecision)


@pytest.mark.unit
def test_replay_needs_no_key_and_builds_no_live_model(tmp_path: Path, clock: VirtualClock) -> None:
    openrouter = FakeOpenRouter()

    build_llm(_settings(tmp_path, llm_mode="replay"), clock=clock, live_chain=openrouter)

    assert openrouter.keys == []


# --- build_llm: live -----------------------------------------------------------


@pytest.mark.unit
async def test_live_puts_each_roles_fallback_chain_behind_a_resilient_model(
    tmp_path: Path, config: ModelsConfig, clock: VirtualClock
) -> None:
    openrouter = FakeOpenRouter()
    settings = _settings(tmp_path, llm_mode="live", openrouter_api_key=KEY)

    router = _router(build_llm(settings, clock=clock, live_chain=openrouter))

    assert router.roles == frozenset(config.roles)
    for role in config.roles:
        model = router.model_for(role)
        assert isinstance(model, ResilientModel)
        chain = [entry.model for entry in config.role(role).chain()]
        assert [status.name for status in model.status()] == chain
    assert set(openrouter.keys) == {KEY}


@pytest.mark.unit
async def test_a_live_gate_call_reaches_the_gates_primary_model(
    tmp_path: Path, config: ModelsConfig, clock: VirtualClock
) -> None:
    openrouter = FakeOpenRouter()
    settings = _settings(tmp_path, llm_mode="live", openrouter_api_key=KEY)
    llm = build_llm(settings, clock=clock, live_chain=openrouter)
    primary = config.role(ModelRole.GATE).model
    openrouter.models[primary].push(ModelRole.GATE, ACK)

    response = await llm.generate(GATE, GateDecision)

    assert (response.parsed, response.usage.model) == (ACK, primary)


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["live", "record"])
def test_a_mode_that_calls_openrouter_fails_clearly_without_the_key(
    tmp_path: Path, clock: VirtualClock, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    openrouter = FakeOpenRouter()

    with pytest.raises(MissingSetting, match="OPENROUTER_API_KEY"):
        build_llm(_settings(tmp_path, llm_mode=mode), clock=clock, live_chain=openrouter)

    assert openrouter.keys == []
    assert capsys.readouterr() == ("", "")


# --- build_llm: record ---------------------------------------------------------


@pytest.mark.unit
async def test_record_saves_the_primary_models_answers_for_replay(
    tmp_path: Path, config: ModelsConfig, clock: VirtualClock
) -> None:
    openrouter = FakeOpenRouter()
    settings = _settings(tmp_path, llm_mode="record", openrouter_api_key=KEY)
    recorder = _router(build_llm(settings, clock=clock, live_chain=openrouter))
    primary = config.role(ModelRole.GATE).model
    openrouter.models[primary].push(ModelRole.GATE, ACK)

    recorded = await recorder.generate(GATE, GateDecision)
    replayed = await build_llm(settings, mode="replay", clock=clock).generate(GATE, GateDecision)

    assert recorded.parsed == replayed.parsed == ACK
    cassette = recorder.model_for(ModelRole.GATE)
    assert isinstance(cassette, CassetteModel)
    assert cassette.path_for(GATE, GateDecision).parent == tmp_path / "tapes" / model_slug(primary)


@pytest.mark.unit
async def test_record_never_files_a_fallbacks_answer_under_the_primary_model(
    tmp_path: Path, config: ModelsConfig, clock: VirtualClock
) -> None:
    """A cassette folder holds one model's answers; a failed primary call is re-recorded."""
    openrouter = FakeOpenRouter()
    settings = _settings(tmp_path, llm_mode="record", openrouter_api_key=KEY)
    recorder = _router(build_llm(settings, clock=clock, live_chain=openrouter))
    primary, fallback = (entry.model for entry in config.role(ModelRole.GATE).chain()[:2])
    openrouter.models[primary].push(ModelRole.GATE, LlmError("rate limited"))
    openrouter.models[fallback].push(ModelRole.GATE, ACK)

    with pytest.raises(LlmError, match="rate limited"):
        await recorder.generate(GATE, GateDecision)

    assert openrouter.models[fallback].calls == []
    cassette = recorder.model_for(ModelRole.GATE)
    assert isinstance(cassette, CassetteModel)
    assert not cassette.path_for(GATE, GateDecision).exists()


# --- build_llm: scripted and the mode ------------------------------------------


@pytest.mark.unit
async def test_scripted_mode_returns_a_model_that_tests_feed(
    tmp_path: Path, clock: VirtualClock
) -> None:
    llm = build_llm(_settings(tmp_path, llm_mode="scripted"), clock=clock)

    assert isinstance(llm, ScriptedModel)
    llm.push(ModelRole.GATE, ACK)
    assert (await llm.generate(GATE, GateDecision)).parsed == ACK


@pytest.mark.unit
def test_the_mode_argument_wins_over_the_settings(tmp_path: Path, clock: VirtualClock) -> None:
    settings = _settings(tmp_path, llm_mode="live")  # live, and no key: would fail

    llm = build_llm(settings, mode="replay", clock=clock)

    assert isinstance(_router(llm).model_for(ModelRole.AGENT), CassetteModel)


@pytest.mark.unit
def test_a_missing_models_config_names_the_file_and_the_setting(
    tmp_path: Path, clock: VirtualClock
) -> None:
    missing = tmp_path / "nope.yaml"

    with pytest.raises(BootstrapError, match="MODELS_CONFIG") as raised:
        build_llm(_settings(tmp_path, models_config=missing), clock=clock)

    assert str(missing) in str(raised.value)


# --- build_runtime -------------------------------------------------------------


@pytest.mark.unit
async def test_the_default_runtime_is_local_and_offline(tmp_path: Path) -> None:
    deps = build_runtime_deps(_settings(tmp_path))

    assert isinstance(deps.clock, SystemClock)
    assert isinstance(deps.ids, UuidIdGenerator)
    assert isinstance(deps.uow_factory(), MemoryUnitOfWork)
    assert isinstance(_router(deps.llm).model_for(ModelRole.GATE), CassetteModel)  # replay
    assert isinstance(deps.knowledge, MemorySopIndex)  # the sops/ tree
    assert await deps.knowledge.version("northline") == FileSopRepository(SOPS).version()
    assert isinstance(deps.blobs, FsBlobStore)
    assert isinstance(deps.documents, DocumentPipeline)  # W3-DOC plugged in by name
    assert isinstance(deps.gateways, Gateways)
    assert type(deps.gateways.sms).__module__ == recording.__name__
    assert isinstance(deps.tracer, NullTracer)
    assert deps.config == RuntimeConfig()


@pytest.mark.unit
def test_build_runtime_returns_the_agent_runtime(tmp_path: Path) -> None:
    assert isinstance(build_runtime(_settings(tmp_path)), AgentRuntime)


@pytest.mark.unit
async def test_every_unit_of_work_of_a_runtime_shares_one_store(tmp_path: Path, load: Load) -> None:
    deps = build_runtime_deps(_settings(tmp_path))
    async with deps.uow_factory() as uow:
        await uow.loads.save(load)
        await uow.commit()

    async with deps.uow_factory() as uow:
        assert await uow.loads.get(load.load_id) == load
    other = build_runtime_deps(_settings(tmp_path))
    async with other.uow_factory() as uow:
        assert await uow.loads.get(load.load_id) is None


@pytest.mark.unit
def test_what_the_caller_passes_replaces_the_default(tmp_path: Path, clock: VirtualClock) -> None:
    gateways = RecordingGateways().as_gateways()
    llm = ScriptedModel()
    persistence = MemoryStore().unit_of_work
    settings = _settings(tmp_path, llm_mode="live")  # no key: building the llm would fail

    deps = build_runtime_deps(
        settings, clock=clock, gateways=gateways, llm=llm, persistence=persistence
    )

    assert (deps.clock, deps.gateways, deps.llm, deps.uow_factory) == (
        clock,
        gateways,
        llm,
        persistence,
    )


# --- tracer --------------------------------------------------------------------


@pytest.mark.unit
def test_tracing_is_off_by_default(tmp_path: Path) -> None:
    assert isinstance(
        build_tracer(_settings(tmp_path, langsmith_api_key=LANGSMITH_KEY)), NullTracer
    )


@pytest.mark.unit
def test_langsmith_traces_when_tracing_is_on_and_the_key_is_set(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        langsmith_tracing=True,
        langsmith_api_key=LANGSMITH_KEY,
        langsmith_project="freight-evals",
    )

    tracer = build_runtime_deps(settings).tracer

    # Building it reaches no network: the SDK client is created on the first export.
    assert isinstance(tracer, LangSmithTracer)
    assert tracer._project == "freight-evals"  # pyright: ignore[reportPrivateUsage]


@pytest.mark.unit
def test_tracing_on_also_switches_on_langgraph_and_langchain_tracing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    settings = _settings(
        tmp_path, langsmith_tracing=True, langsmith_api_key=LANGSMITH_KEY, langsmith_project="p"
    )

    build_tracer(settings)

    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_API_KEY"] == LANGSMITH_KEY
    assert os.environ["LANGSMITH_PROJECT"] == "p"


@pytest.mark.unit
def test_tracing_off_leaves_the_environment_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)

    build_tracer(_settings(tmp_path, langsmith_api_key=LANGSMITH_KEY))

    assert "LANGSMITH_TRACING" not in os.environ


@pytest.mark.unit
def test_tracing_on_without_a_key_falls_back_to_no_tracing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        tracer = build_tracer(_settings(tmp_path, langsmith_tracing=True))

    assert isinstance(tracer, NullTracer)
    assert "LANGSMITH_API_KEY" in caplog.text


# --- lazy adapters -------------------------------------------------------------


@pytest.mark.unit
def test_a_lazy_adapter_is_none_while_its_module_or_attribute_is_missing() -> None:
    assert LazyAdapter("freight_agent.no_such_module", "Anything").load() is None
    assert LazyAdapter("json", "no_such_attribute").load() is None
    assert LazyAdapter("json", "dumps").load() is json.dumps


@pytest.mark.unit
def test_a_lazy_adapter_that_lacks_one_of_its_own_dependencies_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An installed adapter with a broken import is a real bug, not a missing adapter."""
    (tmp_path / "half_installed_adapter.py").write_text("import no_such_dependency\n", "utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(ModuleNotFoundError, match="no_such_dependency"):
        LazyAdapter("half_installed_adapter", "Thing").load()


# --- simulation ----------------------------------------------------------------


@dataclass
class RuntimeSpy:
    deps: RuntimeDeps


@pytest.mark.unit
async def test_the_simulation_runs_on_its_virtual_clock_and_records_deliveries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap, "AgentRuntime", RuntimeSpy)

    simulation = build_simulation(_settings(tmp_path), T0)

    assert isinstance(simulation, Simulation)
    assert isinstance(simulation.runtime, RuntimeSpy)
    deps = simulation.runtime.deps
    assert simulation.clock.now() == T0
    assert deps.clock is simulation.clock
    await deps.gateways.sms.send(load_id="481207", contact_id="drv", text="hi", idempotency_key="k")
    assert [d.channel for d in simulation.gateways.log.deliveries] == ["sms"]


@pytest.mark.unit
def test_the_simulation_builds_the_agent_runtime(tmp_path: Path) -> None:
    simulation = build_simulation(_settings(tmp_path), T0)

    assert isinstance(simulation.runtime, AgentRuntime)
    assert isinstance(simulation.clock, VirtualClock)
    assert isinstance(simulation.gateways, RecordingGateways)


@pytest.mark.unit
def test_the_simulation_needs_a_timezone_aware_start(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone"):
        build_simulation(_settings(tmp_path), datetime(2026, 10, 13, 7, 0))


# --- knowledge, blobs and documents (wave 3) -------------------------------------


@pytest.mark.unit
async def test_the_local_runtime_reads_the_sop_files(tmp_path: Path) -> None:
    knowledge = bootstrap.build_knowledge(_settings(tmp_path))

    assert isinstance(knowledge, MemorySopIndex)
    assert await knowledge.version("northline") == FileSopRepository(SOPS).version()


@pytest.mark.unit
def test_blobs_go_under_blobs_dir_and_nothing_is_written_up_front(tmp_path: Path) -> None:
    blobs = bootstrap.build_blobs(_settings(tmp_path, blobs_dir=tmp_path / "files"))

    assert isinstance(blobs, FsBlobStore)
    assert not (tmp_path / "files").exists()


@pytest.mark.unit
def test_the_document_pipeline_is_built_once_its_card_provides_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[dict[str, Any]] = []

    class Pipeline:
        def __init__(self, **ports: Any) -> None:
            built.append(ports)

        async def process_pending(self, limit: int = 10) -> list[Any]:
            return []

    class Pages:
        pass

    _fake_module(monkeypatch, bootstrap.DOCUMENT_PIPELINE, Pipeline)
    _fake_module(monkeypatch, bootstrap.PAGE_READER, Pages)

    deps = build_runtime_deps(_settings(tmp_path))

    assert isinstance(deps.documents, Pipeline)
    (ports,) = built
    assert set(ports) == {"uow_factory", "clock", "ids", "llm", "blobs", "pages", "timeout_s"}
    assert (ports["blobs"], ports["timeout_s"]) == (deps.blobs, deps.config.llm_timeout_s)
    assert isinstance(ports["pages"], Pages)


@pytest.mark.unit
@pytest.mark.parametrize("missing", ["pipeline", "pages"])
def test_no_document_is_read_until_both_document_adapters_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    present, absent = (
        (bootstrap.PAGE_READER, bootstrap.DOCUMENT_PIPELINE)
        if missing == "pipeline"
        else (bootstrap.DOCUMENT_PIPELINE, bootstrap.PAGE_READER)
    )
    _fake_module(monkeypatch, present, object)
    _absent(monkeypatch, absent)

    assert isinstance(build_runtime_deps(_settings(tmp_path)).documents, NullDocumentProcessor)
