"""The Hero console: the agent's tasks for our operations team, across loads."""

from typing import Annotated, Literal

from fastapi import APIRouter, Body

from freight_agent.api.deps import Service
from freight_agent.api.schemas import CompleteTaskRequest, HeroTask

router = APIRouter(prefix="/hero", tags=["hero"])


@router.get("/tasks", response_model_exclude_none=True)
async def tasks(service: Service, status: Literal["open", "done", "all"] = "all") -> list[HeroTask]:
    return await service.hero_tasks(None if status == "all" else status)


@router.post("/tasks/{task_id}/complete", response_model_exclude_none=True)
async def complete(
    service: Service,
    task_id: str,
    request: Annotated[CompleteTaskRequest | None, Body()] = None,
) -> HeroTask:
    """Platform state only: the agent does not hear about it (yet)."""
    return await service.complete_task(task_id, (request or CompleteTaskRequest()).note)
