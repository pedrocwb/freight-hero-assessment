"""The composition root: the only place that wires adapters to interfaces.

The API, the worker, the CLI and the eval harness all build the agent here, from one
`Settings`, so they run the same `AgentRuntime` and differ only in the adapters chosen.
Nothing else imports adapters into the core, and `application`, `domain` and `interfaces`
never import this module (the import-linter contracts forbid it).

The model is always the live one on OpenRouter, per role (`config/models.yaml`), behind
the fallback chain, its retries and its circuit breakers. The gateways record what they
would send (there are no real integrations). Storage is `PERSISTENCE`: in memory by
default, or Postgres on `DATABASE_URL` (`build_uow_factory`), where the platform's loads,
runs and ledger survive a restart (`build_platform_store`). The evals and Studio build
a fresh in-memory world per play (`memory_uow_factory`) whatever `PERSISTENCE` says: a
play is a throwaway world, not the platform's data. Tracing is
LangGraph's own, on LangSmith, when `LANGSMITH_TRACING` is on and its key is set;
switched on without its key it falls back to no tracing, with a warning that names no
secret. Without `OPENROUTER_API_KEY` the platform still starts (`platform_llm`): each run
that needs the model escalates to a Hero, saying the key is missing.

The gate's engine is `GATE_ENGINE` (`build_runtime_config`). On `jev`, the default, the
gate runs on the decision model of `config/models.yaml` (`decisions.gate`), through
OpenRouter's Decisions API (`build_decisions`); without the key it is a decision model
whose every call fails saying so, as for the chat models. On `llm` it runs on the gate's
chat model.

The prompt templates come from the LangSmith Prompt Hub, at the references
`config/prompts.yaml` pins (`build_prompts`), resolved once per process and checked
against the code that renders them before anything runs. Without `LANGSMITH_API_KEY`, or
with `PROMPTS_SOURCE=local`, they are the repository copy in `prompts/`, with a warning
for the missing key; a pin the Hub cannot serve stops the start, naming the fix.

Wave 3 adapters are found by name, through `LazyAdapter`, so a card plugs in without
editing this file:

| Adapter (card) | Name | Chosen when |
|---|---|---|
| Document pipeline (DOC) | `application.documents.DocumentPipeline` | always |
| Page reader (DOC) | `adapters.documents.pdfium.PdfiumPageReader` | with the pipeline |

Until one exists, the runtime behaves as before wave 3: it reads no document. The runs
read the SOP files of `SOPS_DIR`, and each client's catalog there.

The facility memory follows `PERSISTENCE` too (`build_platform_memory`): a LangGraph
`InMemoryStore` with an embeddings index, or LangGraph's `AsyncPostgresStore` with a
pgvector index in the same database as the load memory, whose tables `agent db migrate`
creates. The evals and Studio get a fresh in-memory one per play (`build_memory`). It
embeds with the `embeddings.memory` model of `config/models.yaml` on OpenRouter, or a
deterministic offline hash without the key or with `MEMORY_EMBEDDINGS=fake`. Its
extraction runs on the `memory` role's model and template.
"""

import importlib
import logging
import os
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from langchain_core.embeddings import Embeddings
from pydantic import SecretStr

from freight_agent.adapters.channels.fakes.recording import RecordingGateways
from freight_agent.adapters.clock import SystemClock, UuidIdGenerator, VirtualClock
from freight_agent.adapters.decisions.openrouter import OpenRouterDecisions
from freight_agent.adapters.decisions.unavailable import UnavailableDecisions
from freight_agent.adapters.documents.fs_blobs import FsBlobStore
from freight_agent.adapters.knowledge.memory import MemorySopIndex
from freight_agent.adapters.llm.models_config import ModelsConfig, load_models_config
from freight_agent.adapters.llm.openrouter import models_for_role
from freight_agent.adapters.llm.resilient import ResilientModel
from freight_agent.adapters.llm.router import RoleRouter
from freight_agent.adapters.llm.unavailable import UnavailableModel
from freight_agent.adapters.memory.embeddings import (
    HashEmbeddings,
    OpenRouterEmbeddings,
    embeddings_dims,
    memory_store,
)
from freight_agent.adapters.memory.postgres import LazyPostgresStore, postgres_index
from freight_agent.adapters.memory.store import StoreFacilityMemory
from freight_agent.adapters.observability.langsmith import LangSmithLinks, langsmith_client_factory
from freight_agent.adapters.observability.null import NullTracer
from freight_agent.adapters.persistence.memory.store import MemoryStore
from freight_agent.adapters.persistence.postgres.store import PostgresUnitOfWorkFactory
from freight_agent.adapters.prompts.hub import LangSmithPromptRegistry, langsmith_hub
from freight_agent.adapters.prompts.local import LocalPromptRegistry
from freight_agent.adapters.prompts.pins import load_pins
from freight_agent.application.documents import NullDocumentProcessor
from freight_agent.application.gate import GateEngine
from freight_agent.application.memory import MemoryExtractor
from freight_agent.application.runtime import AgentRuntime, RuntimeConfig, RuntimeDeps
from freight_agent.interfaces.clock import Clock
from freight_agent.interfaces.decisions import DecisionModel
from freight_agent.interfaces.documents import BlobStore, DocumentProcessor
from freight_agent.interfaces.gateways import Gateways
from freight_agent.interfaces.knowledge import SopIndex
from freight_agent.interfaces.llm import LanguageModel, ModelRole
from freight_agent.interfaces.memory import MemoryProcessor
from freight_agent.interfaces.prompts import PromptError, PromptRegistry, Prompts
from freight_agent.interfaces.repositories import UnitOfWorkFactory
from freight_agent.interfaces.tracing import Tracer
from freight_agent.settings import MissingSetting, Settings, is_set

logger = logging.getLogger(__name__)

# One role's fallback chain as (model id, model) pairs, primary first: the seam where
# tests replace OpenRouter.
LiveChain = Callable[[ModelsConfig, ModelRole, SecretStr], Sequence[tuple[str, LanguageModel]]]


class BootstrapError(Exception):
    """The settings ask for something that cannot be built. Says what to change."""


@dataclass(frozen=True)
class LazyAdapter:
    """An adapter found by module and attribute name, imported only when chosen.

    So the root can register an adapter whose card has not merged yet, and a heavy
    dependency loads only for the runs that use it.
    """

    module: str
    attribute: str

    def load(self) -> Any | None:
        """The attribute, or None while the module or the attribute does not exist.

        A module that exists but fails on one of its own imports raises: that adapter
        is installed and broken, which a fallback would hide.
        """
        try:
            module = importlib.import_module(self.module)
        except ModuleNotFoundError as exc:
            missing = exc.name or ""
            if self.module != missing and not self.module.startswith(f"{missing}."):
                raise
            return None
        return getattr(module, self.attribute, None)


# `DocumentPipeline(uow_factory=, clock=, ids=, llm=, blobs=, pages=, knowledge=,
# prompt=, timeout_s=)` and `PdfiumPageReader()` (card W3-DOC).
DOCUMENT_PIPELINE = LazyAdapter("freight_agent.application.documents", "DocumentPipeline")
PAGE_READER = LazyAdapter("freight_agent.adapters.documents.pdfium", "PdfiumPageReader")


# --- language model ------------------------------------------------------------


def openrouter_chain(
    config: ModelsConfig, role: ModelRole, key: SecretStr
) -> tuple[tuple[str, LanguageModel], ...]:
    """The production chain: one OpenRouter model per entry of the role's fallback list."""
    return tuple((model.model_id, model) for model in models_for_role(config, role, key))


def build_llm(
    settings: Settings, *, clock: Clock, live_chain: LiveChain = openrouter_chain
) -> LanguageModel:
    """The model the runtime calls: per role, the fallback chain behind circuit breakers
    on `clock`. Raises `MissingSetting` without `OPENROUTER_API_KEY`.

    Every role in `config/models.yaml` gets a model, so the eval judge, the simulated
    driver and the document extractor are served by the same router as the gate and the
    agent.
    """
    config = _models_config(settings.models_config)
    key = settings.require_openrouter_key()
    return RoleRouter(
        {role: ResilientModel(live_chain(config, role, key), clock) for role in config.roles}
    )


def platform_llm(
    settings: Settings, *, clock: Clock, live_chain: LiveChain = openrouter_chain
) -> LanguageModel:
    """`build_llm`, or without the key a model whose every call fails saying so: the
    platform starts anyway, and each run that needs the model escalates to a Hero."""
    try:
        return build_llm(settings, clock=clock, live_chain=live_chain)
    except MissingSetting as exc:
        return UnavailableModel(str(exc))


def _models_config(path: Path) -> ModelsConfig:
    try:
        return load_models_config(path)
    except FileNotFoundError:
        raise BootstrapError(
            f"the models config {path} does not exist: set MODELS_CONFIG, or run from the "
            "repository root"
        ) from None


def build_decisions(settings: Settings) -> DecisionModel:
    """The gate's decision model on OpenRouter (`decisions.gate` of `config/models.yaml`),
    or without `OPENROUTER_API_KEY` one whose every call fails saying so. A models file
    without a `decisions.gate` entry is a `BootstrapError`."""
    config = _models_config(settings.models_config)
    entry = config.decisions.get(ModelRole.GATE)
    if entry is None:
        raise BootstrapError(
            f"GATE_ENGINE=jev, and {settings.models_config} has no decisions.gate entry: add "
            "one (the decision model), or set GATE_ENGINE=llm"
        )
    try:
        key = settings.require_openrouter_key()
    except MissingSetting as exc:
        return UnavailableDecisions(str(exc))
    return OpenRouterDecisions(entry, config.pricing_for(entry.model), key)


def gate_decisions(settings: Settings) -> DecisionModel | None:
    """`build_decisions` when the gate runs on it (`GATE_ENGINE=jev`), else None."""
    return build_decisions(settings) if settings.gate_engine == GateEngine.JEV else None


def build_runtime_config(settings: Settings) -> RuntimeConfig:
    """The process's runtime settings: the gate's engine and its act threshold."""
    return RuntimeConfig(
        gate_engine=GateEngine(settings.gate_engine),
        gate_act_threshold=settings.gate_act_threshold,
    )


# --- persistence ---------------------------------------------------------------


def memory_uow_factory() -> UnitOfWorkFactory:
    """A fresh in-memory store: the world of one eval or Studio play."""
    return MemoryStore().unit_of_work


def build_uow_factory(settings: Settings) -> UnitOfWorkFactory:
    """The store `PERSISTENCE` names: a fresh in-memory one, or Postgres on `DATABASE_URL`
    (`MissingSetting` without it), whose pool opens on the first unit of work. Each call
    builds a new store (for Postgres, a new pool on the same database)."""
    return build_platform_store(settings).uow_factory


async def _nothing() -> None:
    return None


@dataclass(frozen=True)
class PlatformStore:
    """The platform's store, with what its lifecycle needs.

    `clear` empties it (a reset of the simulation). `durable` says whether it outlives
    the process, so the platform resumes what it holds after a restart. `close` belongs
    in the shutdown (the Postgres pool).
    """

    uow_factory: UnitOfWorkFactory
    durable: bool
    clear: Callable[[], Awaitable[None]]
    close: Callable[[], Awaitable[None]] = _nothing


def build_platform_store(settings: Settings) -> PlatformStore:
    if settings.persistence == "postgres":
        url = settings.require_database_url().get_secret_value()
        postgres = PostgresUnitOfWorkFactory(url)
        return PlatformStore(postgres, durable=True, clear=postgres.clear, close=postgres.close)
    memory = MemoryStore()
    return PlatformStore(memory.unit_of_work, durable=False, clear=memory.clear)


# --- tracing -------------------------------------------------------------------


def build_tracer(settings: Settings) -> Tracer:
    """Trace links on LangSmith when `LANGSMITH_TRACING` is on and its key is set (and
    LangGraph's native tracing switched on), otherwise no tracing.

    Tracing is optional and must never stop the agent, so a missing key falls back to
    `NullTracer` with a warning (which names no secret).
    """
    if not settings.langsmith_tracing:
        return NullTracer()
    key = settings.langsmith_api_key
    if not is_set(key):
        logger.warning("LANGSMITH_TRACING is on but LANGSMITH_API_KEY is not set: no tracing")
        return NullTracer()
    enable_native_tracing(settings)
    return LangSmithLinks(
        langsmith_client_factory(key.get_secret_value(), os.environ.get("LANGSMITH_ENDPOINT")),
        project_name=settings.langsmith_project,
    )


def enable_native_tracing(settings: Settings) -> None:
    """Let LangGraph and LangChain trace too: each graph node and each model call, with
    its prompt, nested in LangSmith. They read the process environment, and `Settings`
    may have read the key from `.env`, so the switch is copied there. A value already in
    the environment wins."""
    key = settings.langsmith_api_key
    if not is_set(key):
        return
    os.environ.setdefault("LANGSMITH_TRACING", "true")
    os.environ.setdefault("LANGSMITH_API_KEY", key.get_secret_value())
    os.environ.setdefault("LANGSMITH_PROJECT", settings.langsmith_project)


# --- prompts -------------------------------------------------------------------


def build_prompt_registry(settings: Settings) -> PromptRegistry:
    """The Hub, at the pins of `PROMPTS_CONFIG` (every role at `PROMPTS_TAG` when it is
    set), or the repository copy in `PROMPTS_DIR`: with `PROMPTS_SOURCE=local`, or
    without `LANGSMITH_API_KEY` (a warning, which names no secret)."""
    local = LocalPromptRegistry(settings.prompts_dir)
    if settings.prompts_source == "local":
        return local
    key = settings.langsmith_api_key
    if not is_set(key):
        logger.warning(
            "LANGSMITH_API_KEY is not set: the prompts are the repository copy in %s, "
            "not the LangSmith Hub's pinned versions",
            settings.prompts_dir,
        )
        return local
    pins = load_pins(settings.prompts_config, tag=settings.prompts_tag)
    hub = langsmith_hub(key.get_secret_value(), os.environ.get("LANGSMITH_ENDPOINT"))
    return LangSmithPromptRegistry(hub, pins)


def build_prompts(settings: Settings) -> Prompts:
    """Every role's template. The Hub's are pulled once per process and kept (the
    registry's cache), so a platform reset or a new runtime reads the same versions."""
    try:
        return Prompts.resolve(build_prompt_registry(settings))
    except PromptError as exc:
        raise BootstrapError(str(exc)) from None


# --- knowledge, blobs and documents ----------------------------------------------


def build_knowledge(settings: Settings) -> SopIndex:
    """The SOP index the runs read: the `sops/` tree (`MemorySopIndex.from_files`), read on
    every call, so an edited file applies to the next run."""
    return MemorySopIndex.from_files(settings.sops_dir)


def build_blobs(settings: Settings) -> BlobStore:
    """Attachment bytes, under `BLOBS_DIR` (created on the first upload)."""
    return FsBlobStore(settings.blobs_dir)


def build_documents(deps: RuntimeDeps) -> DocumentProcessor:
    """The document pipeline over the runtime's own interfaces, once card W3-DOC provides it;
    until then a processor that reads nothing."""
    pipeline, pages = DOCUMENT_PIPELINE.load(), PAGE_READER.load()
    if pipeline is None or pages is None:
        return NullDocumentProcessor()
    return pipeline(
        uow_factory=deps.uow_factory,
        clock=deps.clock,
        ids=deps.ids,
        llm=deps.llm,
        blobs=deps.blobs,
        pages=pages(),
        knowledge=deps.knowledge,
        prompt=deps.prompts.extract,
        timeout_s=deps.config.llm_timeout_s,
    )


# --- facility memory -------------------------------------------------------------

MEMORY_EMBEDDINGS = "memory"  # the use of the facility memory in `embeddings:`


def build_embeddings(settings: Settings) -> Embeddings:
    """The facility memory's embeddings: the `embeddings.memory` model on OpenRouter, or
    the offline hash (`MEMORY_EMBEDDINGS=fake`, no key, or no such entry)."""
    if settings.memory_embeddings == "fake" or not settings.model_ready:
        return HashEmbeddings()
    config = _models_config(settings.models_config)
    entry = config.embeddings.get(MEMORY_EMBEDDINGS)
    if entry is None:
        logger.warning(
            "%s has no embeddings.memory entry: the facility memory embeds offline",
            settings.models_config,
        )
        return HashEmbeddings()
    return OpenRouterEmbeddings(
        entry, config.pricing_for(entry.model), settings.require_openrouter_key()
    )


# The width of the facility memory's vectors on Postgres when the models file names no
# `embeddings.memory` model: `openai/text-embedding-3-small`'s.
DEFAULT_MEMORY_DIMS = 1536


def build_memory(
    settings: Settings, *, embeddings: Embeddings | None = None
) -> StoreFacilityMemory:
    """The facility memory and its extraction queue, on a fresh in-memory LangGraph store
    whose index embeds each fact's text: the world of one eval or Studio play, or of a
    platform on `PERSISTENCE=memory`."""
    embeddings = build_embeddings(settings) if embeddings is None else embeddings
    return StoreFacilityMemory(memory_store(embeddings, embeddings_dims(embeddings)))


def memory_dims(settings: Settings) -> int:
    """The width of the facility memory's vectors on Postgres: the `embeddings.memory`
    model's, fixed when `agent db migrate` creates the vector table."""
    entry = _models_config(settings.models_config).embeddings.get(MEMORY_EMBEDDINGS)
    return DEFAULT_MEMORY_DIMS if entry is None else entry.dims


@dataclass(frozen=True)
class PlatformMemory:
    """The platform's facility memory, with what its lifecycle needs: `durable` says
    whether it outlives the process, `clear` forgets every note and job (a reset of the
    simulation, with the load memory), `close` belongs in the shutdown."""

    memory: StoreFacilityMemory
    durable: bool
    close: Callable[[], Awaitable[None]] = _nothing

    async def clear(self) -> None:
        await self.memory.clear()


def build_platform_memory(
    settings: Settings, *, embeddings: Embeddings | None = None
) -> PlatformMemory:
    """The facility memory `PERSISTENCE` names: in memory (`build_memory`), or LangGraph's
    Postgres store on `DATABASE_URL` (`MissingSetting` without it), whose pool opens on
    first use. On Postgres the vectors are `memory_dims` wide, so without the OpenRouter
    key the offline hash is built at that width too."""
    if settings.persistence != "postgres":
        return PlatformMemory(build_memory(settings, embeddings=embeddings), durable=False)
    url = settings.require_database_url().get_secret_value()
    dims = memory_dims(settings)
    embeddings = build_embeddings(settings) if embeddings is None else embeddings
    if embeddings_dims(embeddings) != dims:
        if not isinstance(embeddings, HashEmbeddings):
            raise BootstrapError(
                f"the facility memory's embeddings are {embeddings_dims(embeddings)} wide, and "
                f"its Postgres table {dims}: make embeddings.memory.dims match the model"
            )
        embeddings = HashEmbeddings(dims)
    store = LazyPostgresStore(url, index=postgres_index(embeddings, dims))
    return PlatformMemory(StoreFacilityMemory(store), durable=True, close=store.close)


def build_memory_extraction(deps: RuntimeDeps) -> MemoryProcessor:
    """The extraction over the runtime's own interfaces, on the `memory` role and template;
    nothing to extract without a facility memory."""
    if deps.memory is None or deps.memory_queue is None:
        return deps.memory_extraction
    return MemoryExtractor(
        uow_factory=deps.uow_factory,
        clock=deps.clock,
        ids=deps.ids,
        llm=deps.llm,
        memory=deps.memory,
        queue=deps.memory_queue,
        prompt=deps.prompts.memory,
        timeout_s=deps.config.llm_timeout_s,
    )


# --- runtime -------------------------------------------------------------------


def build_runtime_deps(
    settings: Settings,
    *,
    clock: Clock | None = None,
    gateways: Gateways | None = None,
    llm: LanguageModel | None = None,
    persistence: UnitOfWorkFactory | None = None,
    knowledge: SopIndex | None = None,
    blobs: BlobStore | None = None,
    tracer: Tracer | None = None,
    prompts: Prompts | None = None,
    decisions: DecisionModel | None = None,
    memory: StoreFacilityMemory | None = None,
) -> RuntimeDeps:
    """Every interface of the runtime, from the settings; what the caller passes wins.

    `gateways` defaults to recording fakes: this build has no real integrations, so
    deliveries are recorded, not sent. `prompts` default to `build_prompts`. The
    decision model is built only for the jev gate engine (`build_decisions`). `memory`
    defaults to the facility memory of `PERSISTENCE` (`build_platform_memory`) with the
    settings' own store, and to a fresh in-memory one with a `persistence` passed.
    """
    clock = SystemClock() if clock is None else clock
    uow_factory = build_uow_factory(settings) if persistence is None else persistence
    if knowledge is None:
        knowledge = build_knowledge(settings)
    if decisions is None:
        decisions = gate_decisions(settings)
    if memory is None:
        # The platform's own store gets the platform's facility memory; a store the
        # caller passes (a play's world) gets a fresh in-memory one.
        memory = (
            build_platform_memory(settings).memory
            if persistence is None
            else build_memory(settings)
        )
    deps = RuntimeDeps(
        uow_factory=uow_factory,
        clock=clock,
        ids=UuidIdGenerator(),
        llm=platform_llm(settings, clock=clock) if llm is None else llm,
        knowledge=knowledge,
        gateways=RecordingGateways().as_gateways() if gateways is None else gateways,
        tracer=build_tracer(settings) if tracer is None else tracer,
        blobs=build_blobs(settings) if blobs is None else blobs,
        prompts=build_prompts(settings) if prompts is None else prompts,
        config=build_runtime_config(settings),
        decisions=decisions,
        memory=memory,
        memory_queue=memory,
    )
    deps = replace(deps, documents=build_documents(deps))
    return replace(deps, memory_extraction=build_memory_extraction(deps))


def build_runtime(
    settings: Settings,
    *,
    clock: Clock | None = None,
    gateways: Gateways | None = None,
    llm: LanguageModel | None = None,
    persistence: UnitOfWorkFactory | None = None,
    knowledge: SopIndex | None = None,
    blobs: BlobStore | None = None,
    tracer: Tracer | None = None,
    prompts: Prompts | None = None,
    decisions: DecisionModel | None = None,
    memory: StoreFacilityMemory | None = None,
) -> AgentRuntime:
    """The agent, wired from the settings (see `build_runtime_deps`)."""
    return AgentRuntime(
        build_runtime_deps(
            settings,
            clock=clock,
            gateways=gateways,
            llm=llm,
            persistence=persistence,
            knowledge=knowledge,
            blobs=blobs,
            tracer=tracer,
            prompts=prompts,
            decisions=decisions,
            memory=memory,
        )
    )


@dataclass(frozen=True)
class Simulation:
    """A runtime on a hand-moved clock whose deliveries are recorded, for the platform:
    it moves `clock` and reads what the agent sent from `gateways.log`."""

    runtime: AgentRuntime
    clock: VirtualClock
    gateways: RecordingGateways
