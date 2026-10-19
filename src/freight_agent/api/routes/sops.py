"""The SOP viewer: the sections a client sees, and which apply to an event."""

from fastapi import APIRouter

from freight_agent.adapters.sop.files import FileSopRepository
from freight_agent.api.deps import AppSettings
from freight_agent.api.schemas import SopView
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Stage
from freight_agent.domain.sop import ResolvedSop, effective_sections, resolve

router = APIRouter(prefix="/sops", tags=["sops"])


def _types(load_types: str) -> tuple[str, ...]:
    """`a,b` as given in the query; blanks dropped."""
    return tuple(t.strip() for t in load_types.split(",") if t.strip())


@router.get("")
async def sections(settings: AppSettings, client_id: str, load_types: str = "") -> SopView:
    """Every section the client's loads of these types follow, after the layers merge."""
    sop = FileSopRepository(settings.sops_dir)
    types = _types(load_types)
    merged = effective_sections(
        await sop.sections_for_client(client_id), client_id=client_id, load_types=types
    )
    return SopView(version=sop.version(), client_id=client_id, load_types=types, sections=merged)


@router.get("/preview")
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
