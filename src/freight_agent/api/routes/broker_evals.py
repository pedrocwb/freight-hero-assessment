"""A broker's eval runs: `POST /api/brokers/{broker_id}/evals/run` runs a suite on the
broker's current version (`api.schemas.BrokerEvalRunRequest`).

The broker and its published sections are read from the session's store, then the
suite runs in a worker thread on that configuration, exactly as `agent eval run
--broker-file` runs it (`api.evals.run_suite`). Scripted, replay and live modes; a live
run stops at `cost_cap_usd` and carries a LangSmith link when LangSmith is configured.
Record mode writes cassettes and belongs to the CLI.
"""

from functools import partial

import anyio.to_thread
from fastapi import APIRouter

from freight_agent.api import evals
from freight_agent.api.deps import AppSettings, Service
from freight_agent.api.errors import BadRequest
from freight_agent.api.evals import SuiteSummary
from freight_agent.api.schemas import BrokerEvalRunRequest

router = APIRouter(prefix="/brokers/{broker_id}/evals", tags=["evals"])


@router.post("/run")
async def run(
    broker_id: str, service: Service, settings: AppSettings, request: BrokerEvalRunRequest
) -> SuiteSummary:
    """Runs in a worker thread with its own event loop: it may take seconds (minutes live)."""
    if request.mode == "record":
        raise BadRequest("record mode writes cassettes: run it from the CLI")
    broker = await evals.session_broker(service.uow_factory, broker_id)
    chosen = evals.select(evals.scenarios(settings.evals_dir), request.suite, None)
    job = partial(
        evals.run_suite_blocking,
        chosen,
        settings=settings,
        suite=request.suite,
        mode=request.mode,
        k=request.k,
        broker=broker,
        cost_cap_usd=request.cost_cap_usd,
    )
    return await anyio.to_thread.run_sync(job)
