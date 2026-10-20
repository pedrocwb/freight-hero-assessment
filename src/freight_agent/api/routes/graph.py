"""The run graph, as LangGraph draws it: what `RunRecord.nodes` walks through."""

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from freight_agent.api.deps import Service

router = APIRouter(tags=["graph"])


class GraphView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mermaid: str
    nodes: tuple[str, ...]


@router.get("/graph")
async def graph(service: Service) -> GraphView:
    run_graph = service.session.simulation.runtime.graph
    return GraphView(mermaid=run_graph.mermaid(), nodes=run_graph.nodes())
