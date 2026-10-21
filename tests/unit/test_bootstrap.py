"""The composition root, with fakes in place of OpenRouter and LangSmith: no network, and a
key only where a test sets a fake one."""

import json
import logging
import os
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr

from freight_agent import bootstrap
from freight_agent.adapters.channels.fakes import recording
from freight_agent.adapters.channels.fakes.recording import RecordingGateways
from freight_agent.adapters.clock import SystemClock, UuidIdGenerator, VirtualClock
from freight_agent.adapters.decisions.openrouter import OpenRouterDecisions
from freight_agent.adapters.decisions.unavailable import UnavailableDecisions
from freight_agent.adapters.documents.fs_blobs import FsBlobStore
from freight_agent.adapters.knowledge.memory import MemorySopIndex
from freight_agent.adapters.llm.models_config import ModelsConfig, load_models_config
from freight_agent.adapters.llm.openrouter import OpenRouterModel
from freight_agent.adapters.llm.resilient import BreakerState, ResilientModel
from freight_agent.adapters.llm.router import RoleRouter
from freight_agent.adapters.llm.unavailable import UnavailableModel
from freight_agent.adapters.observability.langsmith import LangSmithLinks
from freight_agent.adapters.observability.null import NullTracer
from freight_agent.adapters.persistence.memory.store import MemoryStore, MemoryUnitOfWork
from freight_agent.adapters.prompts.hub import HubClient, LangSmithPromptRegistry, PromptHub
from freight_agent.adapters.prompts.local import LocalPromptRegistry
from freight_agent.adapters.sop.files import FileSopRepository
from freight_agent.application.documents import DocumentPipeline, NullDocumentProcessor
from freight_agent.application.gate import GateEngine
from freight_agent.application.runtime import AgentRuntime, RuntimeConfig, RuntimeDeps
from freight_agent.bootstrap import (
    BootstrapError,
    LazyAdapter,
    Simulation,
    build_decisions,
    build_llm,
    build_prompt_registry,
    build_prompts,
    build_runtime,
    build_runtime_config,
    build_runtime_deps,
    build_simulation,
    build_tracer,
    enable_native_tracing,
    gate_decisions,
    openrouter_chain,
    platform_llm,
)
from freight_agent.domain.actions import AgentOutcome
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.load import Load
from freight_agent.interfaces.decisions import DecisionError, DecisionRequest
from freight_agent.interfaces.gateways import Gateways
from freight_agent.interfaces.llm import ChatRequest, LanguageModel, LlmError, LlmRequest, ModelRole
from freight_agent.interfaces.prompts import PromptRole
from freight_agent.settings import MissingSetting, Settings
from tests.fakes import FakeDecisions, FakeModel
from tests.prompts import PROMPTS

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
MODELS_YAML = ROOT / "config" / "models.yaml"
SOPS = ROOT / "sops"
PROMPTS_DIR = ROOT / "prompts"
KEY = "sk-or-v1-bootstrap-test-secret"
LANGSMITH_KEY = "lsv2_pt_bootstrap-test-secret"
TRACING_VARIABLES = ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT")
T0 = datetime(2026, 10, 13, 7, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
GATE = LlmRequest(role=ModelRole.GATE, system="classify", user="driver: ok")
ROUND = ChatRequest(role=ModelRole.AGENT, system="follow the SOP", user="driver: ok")
ACK = GateDecision(intent=Intent.ACKNOWLEDGEMENT, act=False, reason="just ok")
DONE = AgentOutcome(rationale="nothing to do")


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


def _settings(**values: Any) -> Settings:
    """The shipped models, SOPs and prompts, no `.env`. The prompts are the repository copy
    unless a test asks for `prompts_source="auto"`: a fake LangSmith key must not send a
    runtime to the Hub."""
    paths = {
        "models_config": MODELS_YAML,
        "sops_dir": SOPS,
        "prompts_dir": PROMPTS_DIR,
        "prompts_config": ROOT / "config" / "prompts.yaml",
        "prompts_source": "local",
    }
    options: dict[str, Any] = {"_env_file": None, **paths, **values}
    return Settings(**options)


@dataclass
class FakeOpenRouter:
    """Stands in for `openrouter_chain`: one fake model per entry of a role's chain.

    A model id serves several roles in the shipped config, so each id gets one shared
    model, as one OpenRouter model would be."""

    keys: list[str] = field(default_factory=list[str])
    models: dict[str, FakeModel] = field(default_factory=dict[str, FakeModel])

    def __call__(
        self, config: ModelsConfig, role: ModelRole, key: SecretStr
    ) -> Sequence[tuple[str, LanguageModel]]:
        self.keys.append(key.get_secret_value())
        return [
            (entry.model, self.models.setdefault(entry.model, FakeModel(entry.model)))
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


# --- build_llm -----------------------------------------------------------------


async def test_each_roles_fallback_chain_sits_behind_a_resilient_model(
    config: ModelsConfig, clock: VirtualClock
) -> None:
    openrouter = FakeOpenRouter()

    router = _router(
        build_llm(_settings(openrouter_api_key=KEY), clock=clock, live_chain=openrouter)
    )

    assert router.roles == frozenset(config.roles) == frozenset(ModelRole)
    for role in config.roles:
        model = router.model_for(role)
        assert isinstance(model, ResilientModel)
        chain = [entry.model for entry in config.role(role).chain()]
        assert [status.name for status in model.status()] == chain
    assert set(openrouter.keys) == {KEY}


async def test_a_gate_call_reaches_the_gates_primary_model(
    config: ModelsConfig, clock: VirtualClock
) -> None:
    openrouter = FakeOpenRouter()
    llm = build_llm(_settings(openrouter_api_key=KEY), clock=clock, live_chain=openrouter)
    primary = config.role(ModelRole.GATE).model
    openrouter.models[primary].push(ModelRole.GATE, ACK)

    response = await llm.generate(GATE, GateDecision)

    assert (response.parsed, response.usage.model) == (ACK, primary)


async def test_an_agent_round_reaches_the_agents_primary_model(
    config: ModelsConfig, clock: VirtualClock
) -> None:
    openrouter = FakeOpenRouter()
    llm = build_llm(_settings(openrouter_api_key=KEY), clock=clock, live_chain=openrouter)
    primary = config.role(ModelRole.AGENT).model
    openrouter.models[primary].push(ModelRole.AGENT, DONE)

    reply = await llm.chat(ROUND, AgentOutcome)

    assert (reply.answer, reply.usage.model) == (DONE, primary)


async def test_the_breakers_run_on_the_given_clock(
    config: ModelsConfig, clock: VirtualClock
) -> None:
    """A virtual clock drives the cooldowns in the simulation and the evals."""
    openrouter = FakeOpenRouter()
    llm = _router(build_llm(_settings(openrouter_api_key=KEY), clock=clock, live_chain=openrouter))
    primary = config.role(ModelRole.GATE).model
    fallback = config.role(ModelRole.GATE).chain()[1].model
    openrouter.models[primary].push(ModelRole.GATE, *[LlmError("down")] * 3)
    openrouter.models[fallback].push(ModelRole.GATE, *[ACK] * 3)
    for _ in range(3):
        await llm.generate(GATE, GateDecision)

    gate = llm.model_for(ModelRole.GATE)
    assert isinstance(gate, ResilientModel)
    assert gate.status()[0].state is BreakerState.OPEN

    clock.advance(timedelta(seconds=60))

    assert gate.status()[0].state is BreakerState.HALF_OPEN


def test_without_the_key_it_fails_clearly_and_builds_no_model(
    clock: VirtualClock, capsys: pytest.CaptureFixture[str]
) -> None:
    openrouter = FakeOpenRouter()

    with pytest.raises(MissingSetting, match="OPENROUTER_API_KEY") as raised:
        build_llm(_settings(), clock=clock, live_chain=openrouter)

    assert raised.value.variable == "OPENROUTER_API_KEY"
    assert openrouter.keys == []
    assert capsys.readouterr() == ("", "")


def test_a_blank_key_counts_as_missing(clock: VirtualClock) -> None:
    with pytest.raises(MissingSetting, match="OPENROUTER_API_KEY"):
        build_llm(_settings(openrouter_api_key="  "), clock=clock, live_chain=FakeOpenRouter())


def test_a_missing_models_config_names_the_file_and_the_setting(
    tmp_path: Path, clock: VirtualClock
) -> None:
    missing = tmp_path / "nope.yaml"
    settings = _settings(models_config=missing, openrouter_api_key=KEY)

    with pytest.raises(BootstrapError, match="MODELS_CONFIG") as raised:
        build_llm(settings, clock=clock, live_chain=FakeOpenRouter())

    assert str(missing) in str(raised.value)


def test_the_production_chain_is_one_openrouter_model_per_entry(config: ModelsConfig) -> None:
    """Built only: an OpenRouter model sends nothing until it is called."""
    chain = openrouter_chain(config, ModelRole.AGENT, SecretStr(KEY))

    assert [name for name, _ in chain] == [e.model for e in config.role(ModelRole.AGENT).chain()]
    for name, model in chain:
        assert isinstance(model, OpenRouterModel)
        assert model.model_id == name


# --- platform_llm --------------------------------------------------------------


def test_the_platform_gets_the_live_router_when_the_key_is_set(clock: VirtualClock) -> None:
    openrouter = FakeOpenRouter()

    llm = platform_llm(_settings(openrouter_api_key=KEY), clock=clock, live_chain=openrouter)

    assert _router(llm).roles == frozenset(ModelRole)
    assert set(openrouter.keys) == {KEY}


async def test_without_the_key_the_platform_gets_a_model_that_says_so(
    clock: VirtualClock,
) -> None:
    llm = platform_llm(_settings(), clock=clock, live_chain=FakeOpenRouter())

    assert isinstance(llm, UnavailableModel)
    with pytest.raises(LlmError, match="OPENROUTER_API_KEY is not set"):
        await llm.generate(GATE, GateDecision)
    with pytest.raises(LlmError, match="OPENROUTER_API_KEY is not set"):
        await llm.chat(ROUND, AgentOutcome)


def test_a_missing_models_config_still_stops_the_platform(
    tmp_path: Path, clock: VirtualClock
) -> None:
    """A missing key is expected on the platform; a missing models file is a mistake."""
    with pytest.raises(BootstrapError, match="MODELS_CONFIG"):
        platform_llm(_settings(models_config=tmp_path / "nope.yaml"), clock=clock)


# --- build_runtime -------------------------------------------------------------


async def test_the_default_runtime_is_local_and_untraced() -> None:
    deps = build_runtime_deps(_settings())

    assert isinstance(deps.clock, SystemClock)
    assert isinstance(deps.ids, UuidIdGenerator)
    assert isinstance(deps.uow_factory(), MemoryUnitOfWork)
    assert isinstance(deps.llm, UnavailableModel)  # no key: each run escalates, saying so
    assert isinstance(deps.knowledge, MemorySopIndex)  # the sops/ tree
    assert await deps.knowledge.version("northline") == FileSopRepository(SOPS).version()
    assert isinstance(deps.blobs, FsBlobStore)
    assert isinstance(deps.documents, DocumentPipeline)  # W3-DOC plugged in by name
    assert isinstance(deps.gateways, Gateways)
    assert type(deps.gateways.sms).__module__ == recording.__name__
    assert isinstance(deps.tracer, NullTracer)
    assert deps.config == RuntimeConfig(gate_engine=GateEngine.JEV)
    assert isinstance(deps.decisions, UnavailableDecisions)  # the gate's, saying the same


def test_with_the_key_the_runtime_calls_the_live_router() -> None:
    """Built only: nothing is sent until a run calls the model."""
    deps = build_runtime_deps(_settings(openrouter_api_key=KEY))

    assert _router(deps.llm).roles == frozenset(ModelRole)


def test_build_runtime_returns_the_agent_runtime() -> None:
    assert isinstance(build_runtime(_settings(), llm=FakeModel()), AgentRuntime)


async def test_every_unit_of_work_of_a_runtime_shares_one_store(load: Load) -> None:
    deps = build_runtime_deps(_settings(), llm=FakeModel())
    async with deps.uow_factory() as uow:
        await uow.loads.save(load)
        await uow.commit()

    async with deps.uow_factory() as uow:
        assert await uow.loads.get(load.load_id) == load
    other = build_runtime_deps(_settings(), llm=FakeModel())
    async with other.uow_factory() as uow:
        assert await uow.loads.get(load.load_id) is None


def test_what_the_caller_passes_replaces_the_default(clock: VirtualClock) -> None:
    gateways = RecordingGateways().as_gateways()
    llm = FakeModel()
    persistence = MemoryStore().unit_of_work
    tracer = NullTracer()
    settings = _settings(langsmith_tracing=True, langsmith_api_key=LANGSMITH_KEY)

    deps = build_runtime_deps(
        settings, clock=clock, gateways=gateways, llm=llm, persistence=persistence, tracer=tracer
    )

    assert (deps.clock, deps.gateways, deps.llm, deps.uow_factory, deps.tracer) == (
        clock,
        gateways,
        llm,
        persistence,
        tracer,
    )


@dataclass
class RuntimeSpy:
    deps: RuntimeDeps


def test_build_runtime_passes_the_model_and_the_tracer_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap, "AgentRuntime", RuntimeSpy)
    llm, tracer = FakeModel(), NullTracer()

    runtime = build_runtime(_settings(), llm=llm, tracer=tracer)

    assert isinstance(runtime, RuntimeSpy)
    assert (runtime.deps.llm, runtime.deps.tracer) == (llm, tracer)


# --- prompts -------------------------------------------------------------------


def test_without_the_langsmith_key_the_prompts_are_the_repository_copy(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="freight_agent.bootstrap"):
        prompts = build_prompts(_settings(prompts_source="auto"))

    assert prompts.versions() == PROMPTS.versions()
    assert all(v.startswith("local:") for v in prompts.versions().values())
    assert "LANGSMITH_API_KEY is not set: the prompts are the repository copy" in caplog.text


def test_prompts_source_local_runs_the_repository_copy_even_with_the_key() -> None:
    registry = build_prompt_registry(_settings(langsmith_api_key=LANGSMITH_KEY))

    assert isinstance(registry, LocalPromptRegistry)


def test_with_the_key_the_prompts_come_from_the_hub_at_the_pins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Building the registry reaches no network: each pin is pulled on its first `get`."""
    keys: list[str] = []

    def hub(api_key: str, api_url: str | None) -> PromptHub:
        keys.append(api_key)
        return PromptHub(cast(HubClient, object()))

    monkeypatch.setattr(bootstrap, "langsmith_hub", hub)

    pinned = build_prompt_registry(
        _settings(prompts_source="auto", langsmith_api_key=LANGSMITH_KEY)
    )
    candidate = build_prompt_registry(
        _settings(prompts_source="auto", langsmith_api_key=LANGSMITH_KEY, prompts_tag="candidate")
    )

    assert isinstance(pinned, LangSmithPromptRegistry)
    assert isinstance(candidate, LangSmithPromptRegistry)
    pins = pinned._pins  # pyright: ignore[reportPrivateUsage]
    assert pins == {role: f"freight-agent-{role.value}:prod" for role in PromptRole}
    assert candidate._pins[PromptRole.AGENT] == "freight-agent-agent:candidate"  # pyright: ignore[reportPrivateUsage]
    assert keys == [LANGSMITH_KEY, LANGSMITH_KEY]


def test_a_prompt_that_cannot_be_resolved_stops_the_build(tmp_path: Path) -> None:
    with pytest.raises(BootstrapError, match=r"gate\.yaml does not exist \(PROMPTS_DIR\)"):
        build_prompts(_settings(prompts_dir=tmp_path))
    with pytest.raises(BootstrapError, match="PROMPTS_CONFIG"):
        build_prompts(
            _settings(
                prompts_source="auto",
                langsmith_api_key=LANGSMITH_KEY,
                prompts_config=tmp_path / "none.yaml",
            )
        )


def test_the_runtime_renders_the_prompts_of_the_settings_or_the_callers() -> None:
    built = build_runtime_deps(_settings(), llm=FakeModel())
    given = build_runtime_deps(_settings(), llm=FakeModel(), prompts=PROMPTS)

    assert built.prompts.versions() == PROMPTS.versions()
    assert given.prompts is PROMPTS
    assert build_runtime(_settings(), llm=FakeModel(), prompts=PROMPTS).prompts is PROMPTS


# --- tracer --------------------------------------------------------------------


@pytest.fixture
def tracing_env() -> Iterator[None]:
    """`enable_native_tracing` writes the LangSmith variables to the process environment.
    `clean_env` unset them, and `monkeypatch` does not undo a variable it never saw set,
    so they are removed here: a later test must not trace to LangSmith."""
    yield
    for name in TRACING_VARIABLES:
        os.environ.pop(name, None)


@pytest.mark.usefixtures("tracing_env")
def test_tracing_is_off_by_default() -> None:
    assert isinstance(build_tracer(_settings(langsmith_api_key=LANGSMITH_KEY)), NullTracer)
    assert "LANGSMITH_TRACING" not in os.environ


@pytest.mark.usefixtures("tracing_env")
def test_tracing_on_without_a_key_falls_back_to_no_tracing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        tracer = build_tracer(_settings(langsmith_tracing=True, langsmith_api_key=" "))

    assert isinstance(tracer, NullTracer)
    assert "LANGSMITH_API_KEY" in caplog.text
    assert "LANGSMITH_TRACING" not in os.environ


@pytest.mark.usefixtures("tracing_env")
def test_tracing_on_with_a_key_links_runs_to_langsmith() -> None:
    settings = _settings(
        langsmith_tracing=True, langsmith_api_key=LANGSMITH_KEY, langsmith_project="freight-evals"
    )

    tracer = build_runtime_deps(settings, llm=FakeModel()).tracer

    # Building it reaches no network: the SDK client is created on the first link.
    assert isinstance(tracer, LangSmithLinks)
    assert tracer.project_name == "freight-evals"


@pytest.mark.usefixtures("tracing_env")
def test_tracing_on_switches_on_langgraph_and_langchain_tracing() -> None:
    settings = _settings(
        langsmith_tracing=True, langsmith_api_key=LANGSMITH_KEY, langsmith_project="p"
    )

    build_tracer(settings)

    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_API_KEY"] == LANGSMITH_KEY
    assert os.environ["LANGSMITH_PROJECT"] == "p"


@pytest.mark.usefixtures("tracing_env")
def test_a_tracing_variable_already_in_the_environment_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_PROJECT", "from-the-shell")

    enable_native_tracing(_settings(langsmith_api_key=LANGSMITH_KEY, langsmith_project="p"))

    assert os.environ["LANGSMITH_PROJECT"] == "from-the-shell"
    assert os.environ["LANGSMITH_TRACING"] == "true"


@pytest.mark.usefixtures("tracing_env")
def test_native_tracing_without_a_key_leaves_the_environment_alone() -> None:
    enable_native_tracing(_settings())

    assert not any(name in os.environ for name in TRACING_VARIABLES)


@pytest.mark.usefixtures("tracing_env")
async def test_the_null_tracer_links_no_run() -> None:
    assert await build_tracer(_settings()).trace_url(uuid4()) is None


# --- lazy adapters -------------------------------------------------------------


def test_a_lazy_adapter_is_none_while_its_module_or_attribute_is_missing() -> None:
    assert LazyAdapter("freight_agent.no_such_module", "Anything").load() is None
    assert LazyAdapter("json", "no_such_attribute").load() is None
    assert LazyAdapter("json", "dumps").load() is json.dumps


def test_a_lazy_adapter_that_lacks_one_of_its_own_dependencies_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An installed adapter with a broken import is a real bug, not a missing adapter."""
    (tmp_path / "half_installed_adapter.py").write_text("import no_such_dependency\n", "utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(ModuleNotFoundError, match="no_such_dependency"):
        LazyAdapter("half_installed_adapter", "Thing").load()


# --- simulation ----------------------------------------------------------------


async def test_the_simulation_runs_on_its_virtual_clock_and_records_deliveries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap, "AgentRuntime", RuntimeSpy)
    llm = FakeModel()

    simulation = build_simulation(_settings(), T0, llm=llm)

    assert isinstance(simulation, Simulation)
    assert isinstance(simulation.runtime, RuntimeSpy)
    deps = simulation.runtime.deps
    assert simulation.clock.now() == T0
    assert deps.clock is simulation.clock
    assert deps.llm is llm
    await deps.gateways.sms.send(load_id="481207", contact_id="drv", text="hi", idempotency_key="k")
    assert [d.channel for d in simulation.gateways.log.deliveries] == ["sms"]


def test_the_simulation_builds_the_agent_runtime() -> None:
    simulation = build_simulation(_settings(), T0, llm=FakeModel())

    assert isinstance(simulation.runtime, AgentRuntime)
    assert isinstance(simulation.clock, VirtualClock)
    assert isinstance(simulation.gateways, RecordingGateways)


def test_without_a_model_the_simulation_takes_the_platforms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap, "AgentRuntime", RuntimeSpy)

    simulation = build_simulation(_settings(), T0)

    assert isinstance(simulation.runtime, RuntimeSpy)
    assert isinstance(simulation.runtime.deps.llm, UnavailableModel)  # no key in the settings


def test_the_simulation_needs_a_timezone_aware_start() -> None:
    with pytest.raises(ValueError, match="timezone"):
        build_simulation(_settings(), datetime(2026, 10, 13, 7, 0), llm=FakeModel())


# --- knowledge, blobs and documents (wave 3) -------------------------------------


async def test_the_local_runtime_reads_the_sop_files() -> None:
    knowledge = bootstrap.build_knowledge(_settings())

    assert isinstance(knowledge, MemorySopIndex)
    assert await knowledge.version("northline") == FileSopRepository(SOPS).version()


def test_blobs_go_under_blobs_dir_and_nothing_is_written_up_front(tmp_path: Path) -> None:
    blobs = bootstrap.build_blobs(_settings(blobs_dir=tmp_path / "files"))

    assert isinstance(blobs, FsBlobStore)
    assert not (tmp_path / "files").exists()


def test_the_document_pipeline_is_built_once_its_card_provides_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[dict[str, Any]] = []

    class Pipeline:
        def __init__(self, **given: Any) -> None:
            built.append(given)

        async def process_pending(self, limit: int = 10) -> list[Any]:
            return []

    class Pages:
        pass

    _fake_module(monkeypatch, bootstrap.DOCUMENT_PIPELINE, Pipeline)
    _fake_module(monkeypatch, bootstrap.PAGE_READER, Pages)

    deps = build_runtime_deps(_settings(), llm=FakeModel())

    assert isinstance(deps.documents, Pipeline)
    (given,) = built
    assert set(given) == {
        "uow_factory",
        "clock",
        "ids",
        "llm",
        "blobs",
        "pages",
        "knowledge",
        "prompt",
        "timeout_s",
    }
    assert (given["blobs"], given["timeout_s"]) == (deps.blobs, deps.config.llm_timeout_s)
    assert given["prompt"] is deps.prompts.extract
    assert given["knowledge"] is deps.knowledge
    assert given["llm"] is deps.llm
    assert isinstance(given["pages"], Pages)


@pytest.mark.parametrize("missing", ["pipeline", "pages"])
def test_no_document_is_read_until_both_document_adapters_exist(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    present, absent = (
        (bootstrap.PAGE_READER, bootstrap.DOCUMENT_PIPELINE)
        if missing == "pipeline"
        else (bootstrap.DOCUMENT_PIPELINE, bootstrap.PAGE_READER)
    )
    _fake_module(monkeypatch, present, object)
    _absent(monkeypatch, absent)

    deps = build_runtime_deps(_settings(), llm=FakeModel())

    assert isinstance(deps.documents, NullDocumentProcessor)


# --- the gate's engine -----------------------------------------------------------------


def test_the_gate_runs_on_the_decision_model_by_default_and_on_its_chat_model_on_llm() -> None:
    assert build_runtime_deps(_settings(), llm=FakeModel()).config.gate_engine is GateEngine.JEV

    deps = build_runtime_deps(_settings(gate_engine="llm"), llm=FakeModel())

    assert deps.config.gate_engine is GateEngine.LLM
    assert deps.decisions is None
    assert gate_decisions(_settings(gate_engine="llm")) is None


def test_on_jev_the_runtime_gets_the_decision_model_of_the_models_file() -> None:
    settings = _settings(gate_engine="jev", gate_act_threshold=0.3, openrouter_api_key=KEY)

    deps = build_runtime_deps(settings, llm=FakeModel())

    assert (deps.config.gate_engine, deps.config.gate_act_threshold) == (GateEngine.JEV, 0.3)
    assert isinstance(deps.decisions, OpenRouterDecisions)
    assert deps.decisions.model_id == "typesafe/jev-1.14"
    assert KEY not in repr(deps.decisions)
    assert build_runtime_config(settings) == deps.config


async def test_on_jev_without_the_key_every_decision_says_so() -> None:
    deps = build_runtime_deps(_settings(gate_engine="jev"), llm=FakeModel())

    assert isinstance(deps.decisions, UnavailableDecisions)
    with pytest.raises(DecisionError, match="OPENROUTER_API_KEY is not set"):
        await deps.decisions.decide(
            DecisionRequest(role=ModelRole.GATE, state=("ok",), questions={})
        )


def test_a_decision_model_the_caller_passes_wins() -> None:
    fake = FakeDecisions()

    deps = build_runtime_deps(_settings(gate_engine="jev"), llm=FakeModel(), decisions=fake)

    assert deps.decisions is fake


def test_on_jev_a_models_file_without_the_gates_decision_model_stops_the_build(
    tmp_path: Path,
) -> None:
    models = tmp_path / "models.yaml"
    text = MODELS_YAML.read_text(encoding="utf-8")
    models.write_text(text.split("\ndecisions:")[0], encoding="utf-8")

    with pytest.raises(BootstrapError, match=r"no decisions\.gate entry"):
        build_decisions(_settings(models_config=models, openrouter_api_key=KEY))


# --- facility memory ---------------------------------------------------------------------


def test_the_facility_memory_embeds_offline_without_the_key_or_when_asked() -> None:
    from freight_agent.adapters.memory.embeddings import HashEmbeddings
    from freight_agent.bootstrap import build_embeddings

    assert isinstance(build_embeddings(_settings()), HashEmbeddings)
    keyed = _settings(openrouter_api_key="sk-test", memory_embeddings="fake")
    assert isinstance(build_embeddings(keyed), HashEmbeddings)


def test_with_the_key_the_facility_memory_embeds_with_the_configured_model() -> None:
    from freight_agent.adapters.memory.embeddings import OpenRouterEmbeddings
    from freight_agent.bootstrap import build_embeddings

    embeddings = build_embeddings(_settings(openrouter_api_key="sk-test"))

    assert isinstance(embeddings, OpenRouterEmbeddings)
    assert (embeddings.config.model, embeddings.dims) == ("openai/text-embedding-3-small", 1536)


def test_a_models_file_without_memory_embeddings_embeds_offline(tmp_path: Path) -> None:
    import yaml

    from freight_agent.adapters.memory.embeddings import HashEmbeddings
    from freight_agent.bootstrap import build_embeddings

    data = yaml.safe_load(MODELS_YAML.read_text(encoding="utf-8"))
    del data["embeddings"]
    models = tmp_path / "models.yaml"
    models.write_text(yaml.safe_dump(data), encoding="utf-8")

    settings = _settings(openrouter_api_key="sk-test", models_config=models)

    assert isinstance(build_embeddings(settings), HashEmbeddings)


def test_the_runtime_gets_a_facility_memory_and_its_extraction() -> None:
    from freight_agent.adapters.memory.store import StoreFacilityMemory
    from freight_agent.application.memory import MemoryExtractor
    from freight_agent.bootstrap import build_memory, build_runtime_deps

    deps = build_runtime_deps(_settings())

    assert isinstance(deps.memory, StoreFacilityMemory)
    assert deps.memory_queue is deps.memory
    assert isinstance(deps.memory_extraction, MemoryExtractor)
    assert deps.memory_extraction.prompt.role.value == "memory"
    given = build_memory(_settings())
    assert build_runtime_deps(_settings(), memory=given).memory is given
