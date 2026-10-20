"""The knowledge base: the SOP viewer (`/api/sops`, `/api/sops/preview`), search
(`GET /api/sops/search`) and a broker's SOP onboarding under
`/api/brokers/{broker_id}/sops` (upload, draft, publish, versions).

Owner: card W3-KNW. Bodies: `api.schemas.SopView`, `SopUploadResult`, `SopDraftView`,
`SopDraftIn`, `SopPublishIn`, `SopPublishResult`, `SopVersionView`, `SearchHitView`.
The paths are full (the router has no prefix) because they live under two roots.

The viewer and search read the SOP index the session's runs read
(`SimulationService.knowledge`), so they show what the agent sees. Onboarding goes
through `application.sop_ingestion.SopIngestion` on the session's store and blobs:
the `compile` and `extract` roles of the session's model mode (a model given to
`create_app`'s model when it was given one, as `SimulationService.llm`; tests may also set
`app.state.knowledge_llm`), the
embedder of that mode, and the standard layer from the index. The standard layer is
published from `sops/standard/` with `agent sop import`, not uploaded.
"""

import contextlib
from collections.abc import Iterator
from pathlib import PurePath
from typing import Annotated, Final, cast

from fastapi import APIRouter, File, Query, Request, UploadFile, status

from freight_agent import bootstrap
from freight_agent.adapters.clock import SystemClock, UuidIdGenerator
from freight_agent.adapters.knowledge.embeddings import embedder_for
from freight_agent.adapters.knowledge.pdf_pages import PdfPageReader
from freight_agent.api.deps import Service
from freight_agent.api.errors import ApiError, BadRequest, Conflict, NotFound, Unprocessable
from freight_agent.api.schemas import (
    SearchHitView,
    SopDraftIn,
    SopDraftView,
    SopPublishIn,
    SopPublishResult,
    SopUploadResult,
    SopVersionView,
    SopView,
)
from freight_agent.application.sop_ingestion import (
    DraftChanged,
    DraftInvalid,
    NoDraft,
    SopIngestion,
    SopIngestionError,
    UnknownDocument,
)
from freight_agent.bootstrap import build_llm
from freight_agent.domain.broker import standard_broker
from freight_agent.domain.load import Stage
from freight_agent.domain.sop import ResolvedSop, SopDraft, layer_owner
from freight_agent.ports.knowledge import Embedder, EmbeddingError
from freight_agent.ports.llm import LanguageModel
from freight_agent.settings import LlmMode, MissingSetting, Settings

router = APIRouter(tags=["sops"])

MAX_UPLOAD_BYTES: Final = 20 * 1024 * 1024
_TYPES_BY_SUFFIX: Final = {".pdf": "application/pdf", ".md": "text/markdown", ".txt": "text/plain"}


class UpstreamError(ApiError):
    """A model or embedding provider failed: try again later."""

    status_code = 502


class TooLarge(ApiError):
    status_code = 413


def _types(load_types: str) -> tuple[str, ...]:
    """`a,b` as given in the query; blanks dropped."""
    return tuple(t.strip() for t in load_types.split(",") if t.strip())


# --- the viewer and search ---------------------------------------------------------------


@router.get("/sops")
async def sections(service: Service, client_id: str, load_types: str = "") -> SopView:
    """Every section the client's loads of these types follow, after the layers merge."""
    types = _types(load_types)
    merged = await service.knowledge.visible(client_id, types)
    version = await service.knowledge.version(client_id) or ""
    return SopView(version=version, client_id=client_id, load_types=types, sections=merged)


@router.get("/sops/preview")
async def preview(
    service: Service, client_id: str, intent: str, stage: Stage, load_types: str = ""
) -> ResolvedSop:
    """What the resolver gives the agent for this event: the applying sections and the
    catalog of every section the client sees. `intent` is an id of the client's catalog."""
    async with service.uow_factory() as uow:
        broker = await uow.brokers.get(client_id) or standard_broker()
    if broker.intent(intent) is None:
        known = ", ".join(i.intent_id for i in broker.intents)
        raise Unprocessable(f"unknown intent {intent!r} for {client_id}: use one of {known}")
    return await service.knowledge.by_applies(client_id, _types(load_types), intent, stage)


@router.get("/sops/search")
async def search(
    service: Service,
    q: str,
    broker_id: str | None = None,
    client_id: Annotated[str | None, Query(description="The same as broker_id")] = None,
    load_types: str = "",
    k: Annotated[int, Query(ge=1, le=20)] = 5,
) -> list[SearchHitView]:
    """The `k` sections the broker's loads of these types see that best match `q`."""
    owner = broker_id or client_id
    if not owner:
        raise BadRequest("give broker_id: whose sections to search")
    with _errors():
        hits = await service.knowledge.search(owner, _types(load_types), q, k)
    return [SearchHitView.model_validate(hit.model_dump()) for hit in hits]


# --- a broker's onboarding ---------------------------------------------------------------


def _embedder(settings: Settings, mode: LlmMode) -> Embedder:
    """The composition root's embedder for `mode` (`bootstrap.build_embedder`, card
    W3-OPS); until it is on this branch, the same rule from `embedder_for`."""
    build = getattr(bootstrap, "build_embedder", None)
    if build is not None:
        return build(settings, mode=mode)
    return embedder_for(settings.model_copy(update={"llm_mode": mode}))


async def _model(request: Request, service: Service, mode: LlmMode) -> LanguageModel:
    """The session's model for the onboarding roles: `app.state.knowledge_llm` when set
    (tests), else one per model mode, built once."""
    state = request.app.state
    given: LanguageModel | None = getattr(state, "knowledge_llm", None) or service.llm
    if given is not None:
        return given
    if not hasattr(state, "knowledge_models"):
        state.knowledge_models = {}
    models: dict[str, LanguageModel] = state.knowledge_models
    if mode not in models:
        with _errors():
            models[mode] = build_llm(service.settings, mode=mode, clock=SystemClock())
    return models[mode]


async def _ingestion(request: Request, service: Service) -> SopIngestion:
    mode = cast(LlmMode, (await service.state()).llm_mode)
    with _errors():
        embedder = _embedder(service.settings, mode)
    return SopIngestion(
        uow_factory=service.uow_factory,
        clock=SystemClock(),
        ids=UuidIdGenerator(),
        llm=await _model(request, service, mode),
        blobs=service.blobs,
        pages=PdfPageReader(),
        embedder=embedder,
        knowledge=service.knowledge,
    )


@contextlib.contextmanager
def _errors() -> Iterator[None]:
    """The onboarding's errors as the API's (`api.errors`)."""
    try:
        yield
    except (NoDraft, UnknownDocument) as exc:
        raise NotFound(str(exc)) from exc
    except DraftChanged as exc:
        raise Conflict(str(exc)) from exc
    except DraftInvalid as exc:
        raise Unprocessable(str(exc)) from exc
    except (SopIngestionError, MissingSetting) as exc:
        raise BadRequest(str(exc)) from exc
    except EmbeddingError as exc:
        raise UpstreamError(str(exc)) from exc


async def _view(ingestion: SopIngestion, draft: SopDraft) -> SopDraftView:
    lint = await ingestion.lint_draft(draft)
    return SopDraftView.model_validate({**draft.model_dump(), "lint": lint.problems()})


def _content_type(file: UploadFile) -> str:
    given = (file.content_type or "").split(";", 1)[0].strip().lower()
    if given and given != "application/octet-stream":
        return given
    return _TYPES_BY_SUFFIX.get(PurePath(file.filename or "").suffix.lower(), given)


@router.post("/brokers/{broker_id}/sops", status_code=status.HTTP_201_CREATED)
async def upload(
    request: Request,
    service: Service,
    broker_id: str,
    file: Annotated[UploadFile, File(description="The SOP document: a PDF, or Markdown")],
) -> SopUploadResult:
    """Store the document and compile it into the broker's draft. `compiled`: the draft
    has its sections; `stored`: the compile failed (the document is kept); `failed`:
    the file could not be read."""
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise TooLarge(f"the file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    ingestion = await _ingestion(request, service)
    with _errors():
        result = await ingestion.upload(
            broker_id, file.filename or "upload", _content_type(file), data
        )
    return SopUploadResult(
        document_id=result.document.document_id, pages=result.document.pages, status=result.status
    )


@router.get("/brokers/{broker_id}/sops/draft")
async def get_draft(request: Request, service: Service, broker_id: str) -> SopDraftView:
    """The broker's draft with what a publish would report now; 404 before any upload."""
    ingestion = await _ingestion(request, service)
    with _errors():
        return await _view(ingestion, await ingestion.get_draft(broker_id))


@router.put("/brokers/{broker_id}/sops/draft")
async def put_draft(
    request: Request, service: Service, broker_id: str, body: SopDraftIn
) -> SopDraftView:
    """Replace the broker's draft with the edited one (the uploads it came from stay)."""
    ingestion = await _ingestion(request, service)
    with _errors():
        draft = await ingestion.save_draft(
            broker_id,
            sections=body.sections,
            proposed_intents=body.proposed_intents,
            proposed_requirements=body.proposed_requirements,
            proposed_obligations=body.proposed_obligations,
        )
        return await _view(ingestion, draft)


@router.post("/brokers/{broker_id}/sops/publish")
async def publish(
    request: Request, service: Service, broker_id: str, body: SopPublishIn
) -> SopPublishResult:
    """Publish the draft: a new SOP version and the broker's next config version, with
    `percent` of its loads on it. 422 lists what the draft must fix first."""
    ingestion = await _ingestion(request, service)
    with _errors():
        published = await ingestion.publish_draft(
            broker_id, percent=body.percent, published_by="api"
        )
    return SopPublishResult(
        version=published.version,
        config_version=published.config_version,
        lint=published.warnings,
    )


@router.get("/brokers/{broker_id}/sops/versions")
async def versions(service: Service, broker_id: str) -> list[SopVersionView]:
    """Every published version of the broker's layer (`standard`: the standard layer),
    in the order they were first published."""
    async with service.uow_factory() as uow:
        found = await uow.sops.versions(layer_owner(broker_id))
    return [SopVersionView.of(version) for version in found]
