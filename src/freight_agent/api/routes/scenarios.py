"""Scenarios on the platform: list them, play one live inside the simulation, follow it.

`GET /api/scenarios/run` and the `scenario` event of `GET /api/stream` carry the same
body (`api.scenarios.ScenarioRun`); the load endpoints show the scenario's load.
"""

from fastapi import APIRouter, Request

from freight_agent.api.scenarios import ScenarioInfo, ScenarioRun, ScenarioRunner, ScenarioStarted

router = APIRouter(prefix="/scenarios", tags=["scenarios"])


def _runner(request: Request) -> ScenarioRunner:
    return request.app.state.scenarios


@router.get("")
async def scenarios(request: Request) -> list[ScenarioInfo]:
    return _runner(request).scenarios()


@router.post("/{scenario_id}/run", status_code=202)
async def run(request: Request, scenario_id: str) -> ScenarioStarted:
    """Reset the simulation and play the scenario in the background. 409 while one is
    running; 503 without the model's key."""
    return await _runner(request).start(scenario_id)


@router.get("/run")
async def current(request: Request) -> ScenarioRun:
    """The scenario run in progress, or the last one."""
    return _runner(request).current
