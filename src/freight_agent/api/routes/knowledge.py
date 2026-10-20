"""The knowledge base: the SOP viewer (`/api/sops`, moved here unchanged from the
pre-wave-3 `routes/sops.py`) and, with card W3-KNW, a broker's SOP onboarding under
`/api/brokers/{broker_id}/sops` (upload, draft, publish, versions) and
`GET /api/sops/search?q=`.

Owner: card W3-KNW. Bodies: `api.schemas.SopView`, `SopUploadResult`, `SopDraftView`,
`SopDraftIn`, `SopPublishIn`, `SopPublishResult`, `SopVersionView`, `SearchHitView`.
The paths are full (the router has no prefix) because they live under two roots.
"""

from fastapi import APIRouter

from freight_agent.adapters.sop.files import FileSopRepository
from freight_agent.api.deps import AppSettings
from freight_agent.api.schemas import SopView
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Stage
from freight_agent.domain.sop import ResolvedSop, effective_sections, resolve

router = APIRouter(tags=["sops"])


def _types(load_types: str) -> tuple[str, ...]:
    """`a,b` as given in the query; blanks dropped."""
    return tuple(t.strip() for t in load_types.split(",") if t.strip())


@router.get("/sops")
async def sections(settings: AppSettings, client_id: str, load_types: str = "") -> SopView:
    """Every section the client's loads of these types follow, after the layers merge."""
    sop = FileSopRepository(settings.sops_dir)
    types = _types(load_types)
    merged = effective_sections(
        await sop.sections_for_client(client_id), client_id=client_id, load_types=types
    )
    return SopView(version=sop.version(), client_id=client_id, load_types=types, sections=merged)


@router.get("/sops/preview")
async def preview(
    settings: AppSettings, client_id: str, intent: Intent, stage: Stage, load_types: str = ""
) -> ResolvedSop:
    """What the resolver gives the agent for this event: the applying sections and the
    catalog of every section the client sees."""
    sop = FileSopRepository(settings.sops_dir)
    return resolve(
        await sop.sections_for_client(client_id),
        client_id=client_id,
        load_types=_types(load_types),
        intent=intent,
        stage=stage,
    )
