"""The SOP viewer: the merged sections (`GET /api/sops`), the resolver preview
(`GET /api/sops/preview`) and search (`GET /api/sops/search`).

Bodies: `api.schemas.SopView`, `SearchHitView`. All three read the SOP index the
session's runs read (`SimulationService.knowledge`), so they show what the agent sees.
"""

from typing import Annotated

from fastapi import APIRouter, Query

from freight_agent.api.deps import Service
from freight_agent.api.errors import BadRequest, Unprocessable
from freight_agent.api.schemas import SearchHitView, SopView
from freight_agent.domain.broker import standard_broker
from freight_agent.domain.load import Stage
from freight_agent.domain.sop import ResolvedSop

router = APIRouter(prefix="/sops", tags=["sops"])


def _types(load_types: str) -> tuple[str, ...]:
    """`a,b` as given in the query; blanks dropped."""
    return tuple(t.strip() for t in load_types.split(",") if t.strip())


@router.get("")
async def sections(service: Service, client_id: str, load_types: str = "") -> SopView:
    """Every section the client's loads of these types follow, after the layers merge."""
    types = _types(load_types)
    merged = await service.knowledge.visible(client_id, types)
    version = await service.knowledge.version(client_id) or ""
    return SopView(version=version, client_id=client_id, load_types=types, sections=merged)


@router.get("/preview")
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


@router.get("/search")
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
    hits = await service.knowledge.search(owner, _types(load_types), q, k)
    return [SearchHitView.model_validate(hit.model_dump()) for hit in hits]
