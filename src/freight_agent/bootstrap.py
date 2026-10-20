"""The composition root: the only place that wires adapters to ports.

The API, the worker, the CLI and the eval harness all build the agent here, from one
`Settings`, so they run the same `AgentRuntime` and differ only in the adapters chosen.
Nothing else imports adapters into the core, and `application`, `domain` and `ports`
never import this module (the import-linter contracts forbid it).

Local mode is the default and needs no secret and no network: in-memory storage,
recording gateways (there are no real integrations), recorded model answers (`replay`)
and no tracing. Each outside system is switched on by its setting. Tracing that is
switched on without its key falls back to no tracing, with a warning that names no
secret.

Wave 3 adapters are found by name, through `LazyAdapter`, so a card plugs in without
editing this file:

| Adapter (card) | Name | Chosen when |
|---|---|---|
| Document pipeline (DOC) | `application.documents.DocumentPipeline` | always |
| Page reader (DOC) | `adapters.documents.pdfium.PdfiumPageReader` | with the pipeline |

Until one exists, the runtime behaves as before wave 3: it reads no document. The runs
read the SOP files of `SOPS_DIR`.
"""

import importlib
import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from freight_agent.adapters.channels.fakes.recording import RecordingGateways
from freight_agent.adapters.clock import SystemClock, UuidIdGenerator, VirtualClock
from freight_agent.adapters.documents.fs_blobs import FsBlobStore
from freight_agent.adapters.knowledge.memory import MemorySopIndex
from freight_agent.adapters.llm.cassette import CassetteModel
from freight_agent.adapters.llm.models_config import ModelsConfig, load_models_config
from freight_agent.adapters.llm.openrouter import models_for_role
from freight_agent.adapters.llm.resilient import ResilientModel
from freight_agent.adapters.llm.router import RoleRouter
from freight_agent.adapters.llm.scripted import ScriptedModel
from freight_agent.adapters.observability.langsmith import LangSmithTracer, langsmith_client_factory
from freight_agent.adapters.observability.null import NullTracer
from freight_agent.adapters.persistence.memory.store import MemoryStore
from freight_agent.adapters.sop.files import FileSopRepository
from freight_agent.application.documents import NullDocumentProcessor
from freight_agent.application.runtime import AgentRuntime, RuntimeConfig, RuntimeDeps
from freight_agent.ports.clock import Clock
from freight_agent.ports.documents import BlobStore, DocumentProcessor
from freight_agent.ports.gateways import Gateways
from freight_agent.ports.knowledge import SopIndex
from freight_agent.ports.llm import LanguageModel, ModelRole
from freight_agent.ports.repositories import UnitOfWorkFactory
from freight_agent.ports.tracing import Tracer
from freight_agent.settings import LlmMode, Settings, is_set

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
# timeout_s=)` and `PdfiumPageReader()` (card W3-DOC).
DOCUMENT_PIPELINE = LazyAdapter("freight_agent.application.documents", "DocumentPipeline")
PAGE_READER = LazyAdapter("freight_agent.adapters.documents.pdfium", "PdfiumPageReader")
LIVE_MODES: tuple[LlmMode, ...] = ("live", "record")


# --- language model ------------------------------------------------------------


def openrouter_chain(
    config: ModelsConfig, role: ModelRole, key: SecretStr
) -> tuple[tuple[str, LanguageModel], ...]:
    """The production chain: one OpenRouter model per entry of the role's fallback list."""
    return tuple((model.model_id, model) for model in models_for_role(config, role, key))


def build_llm(
    settings: Settings,
    *,
    mode: LlmMode | None = None,
    clock: Clock,
    live_chain: LiveChain = openrouter_chain,
) -> LanguageModel:
    """The model the runtime calls, for `mode` (default: `settings.llm_mode`).

    - `scripted`: a `ScriptedModel`; tests and the scripted eval path push its outputs.
    - `replay`: per role, the recorded answers of the role's primary model. No key.
    - `live`: per role, the fallback chain behind circuit breakers on `clock`.
    - `record`: per role, the primary model, its answers saved for `replay`.

    Every role in `config/models.yaml` gets a model, so the eval judge, the simulated
    driver and the document extractor are served by the same router as the gate and the
    agent.
    """
    mode = mode or settings.llm_mode
    if mode == "scripted":
        return ScriptedModel()
    config = _models_config(settings.models_config)
    roles = tuple(config.roles)
    if mode == "replay":
        return RoleRouter(
            {
                role: CassetteModel(settings.cassettes_dir, config.role(role).model, "replay")
                for role in roles
            }
        )
    if mode not in LIVE_MODES:
        raise ValueError(f"unknown llm mode {mode!r}: use scripted, replay, live or record")
    key = settings.require_openrouter_key()
    if mode == "live":
        return RoleRouter(
            {role: ResilientModel(live_chain(config, role, key), clock) for role in roles}
        )
    return RoleRouter(
        {
            role: CassetteModel(
                settings.cassettes_dir,
                config.role(role).model,
                "record",
                inner=_primary(live_chain(config, role, key)),
            )
            for role in roles
        }
    )


def _primary(chain: Sequence[tuple[str, LanguageModel]]) -> LanguageModel:
    """Record from the primary alone. Cassettes are filed under the primary's id and
    replayed as its answers, so a fallback's answer must never be recorded there: a
    failed primary call fails the recording run, and the next run records it again."""
    (_, model), *_ = chain
    return model


def _models_config(path: Path) -> ModelsConfig:
    try:
        return load_models_config(path)
    except FileNotFoundError:
        raise BootstrapError(
            f"the models config {path} does not exist: set MODELS_CONFIG, or run from the "
            "repository root"
        ) from None


# --- persistence ---------------------------------------------------------------


def build_uow_factory(settings: Settings) -> UnitOfWorkFactory:
    """One in-memory store per runtime, shared by every unit of work it opens."""
    return MemoryStore().unit_of_work


# --- tracing -------------------------------------------------------------------


def build_tracer(settings: Settings, *, sop_version: Callable[[], str] | None = None) -> Tracer:
    """LangSmith when `LANGSMITH_TRACING` is on and its key is set, otherwise no tracing.

    Tracing is optional and must never stop the agent, so a missing key or a missing
    adapter falls back to `NullTracer` with a warning (which names no secret).
    """
    if not settings.langsmith_tracing:
        return NullTracer()
    key = settings.langsmith_api_key
    if not is_set(key):
        logger.warning("LANGSMITH_TRACING is on but LANGSMITH_API_KEY is not set: no tracing")
        return NullTracer()
    enable_native_tracing(settings)
    return LangSmithTracer(
        langsmith_client_factory(key.get_secret_value()),
        project_name=settings.langsmith_project,
        sop_version=sop_version,
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


# --- knowledge, blobs and documents ----------------------------------------------


def build_knowledge(settings: Settings) -> SopIndex:
    """The SOP index the runs read: the `sops/` tree (`MemorySopIndex.from_files`), read on
    every call, so an edited file applies to the next run."""
    return MemorySopIndex.from_files(settings.sops_dir)


def build_blobs(settings: Settings) -> BlobStore:
    """Attachment bytes, under `BLOBS_DIR` (created on the first upload)."""
    return FsBlobStore(settings.blobs_dir)


def build_documents(deps: RuntimeDeps) -> DocumentProcessor:
    """The document pipeline over the runtime's own ports, once card W3-DOC provides it;
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
) -> RuntimeDeps:
    """Every port of the runtime, from the settings; what the caller passes wins.

    `gateways` defaults to recording fakes: this build has no real integrations, so
    deliveries are recorded, not sent.
    """
    clock = SystemClock() if clock is None else clock
    uow_factory = build_uow_factory(settings) if persistence is None else persistence
    if knowledge is None:
        knowledge = build_knowledge(settings)
    deps = RuntimeDeps(
        uow_factory=uow_factory,
        clock=clock,
        ids=UuidIdGenerator(),
        llm=build_llm(settings, clock=clock) if llm is None else llm,
        knowledge=knowledge,
        gateways=RecordingGateways().as_gateways() if gateways is None else gateways,
        tracer=build_tracer(settings, sop_version=FileSopRepository(settings.sops_dir).version),
        blobs=build_blobs(settings) if blobs is None else blobs,
        config=RuntimeConfig(),
    )
    return replace(deps, documents=build_documents(deps))


def build_runtime(
    settings: Settings,
    *,
    clock: Clock | None = None,
    gateways: Gateways | None = None,
    llm: LanguageModel | None = None,
    persistence: UnitOfWorkFactory | None = None,
    knowledge: SopIndex | None = None,
    blobs: BlobStore | None = None,
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
        )
    )


@dataclass(frozen=True)
class Simulation:
    """A runtime on a hand-moved clock whose deliveries are recorded, for the platform
    and the CLI: they move `clock` and read what the agent sent from `gateways.log`."""

    runtime: AgentRuntime
    clock: VirtualClock
    gateways: RecordingGateways


def build_simulation(
    settings: Settings, start: datetime, *, llm: LanguageModel | None = None
) -> Simulation:
    """A simulation starting at `start` (timezone-aware). Storage and model come from the
    settings, as for the product; `llm` replaces the model (a scripted one, in tests)."""
    clock = VirtualClock(start)
    gateways = RecordingGateways()
    runtime = build_runtime(settings, clock=clock, gateways=gateways.as_gateways(), llm=llm)
    return Simulation(runtime=runtime, clock=clock, gateways=gateways)
