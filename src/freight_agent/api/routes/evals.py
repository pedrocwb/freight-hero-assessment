"""The eval page: list the scenarios, run a suite or one scenario."""

from functools import partial

import anyio.to_thread
from fastapi import APIRouter

from freight_agent.api import evals
from freight_agent.api.deps import AppSettings
from freight_agent.api.evals import EvalRunRequest, ScenarioInfo, SuiteSummary

router = APIRouter(prefix="/evals", tags=["evals"])


@router.get("/scenarios")
async def scenarios(settings: AppSettings) -> list[ScenarioInfo]:
    return [
        ScenarioInfo(id=s.id, title=s.title, suite=s.suite, tags=s.tags)
        for s in evals.scenarios(settings.evals_dir)
    ]


@router.post("/run")
async def run(settings: AppSettings, request: EvalRunRequest) -> SuiteSummary:
    """Runs in a worker thread with its own event loop: it may take seconds."""
    chosen = evals.select(evals.scenarios(settings.evals_dir), request.suite, request.scenario)
    job = partial(
        evals.run_suite_blocking,
        chosen,
        settings=settings,
        suite=request.suite,
        mode=request.mode,
        k=request.k,
    )
    return await anyio.to_thread.run_sync(job)
