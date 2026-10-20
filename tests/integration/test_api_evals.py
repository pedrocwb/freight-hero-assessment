"""The eval page: list the scenarios and run a suite, or one scenario, k times."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.integration.test_api_harness import Api, open_api

pytestmark = pytest.mark.unit

SCENARIO = "a_client_a_high_risk"


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[Api]:
    async with open_api(tmp_path) as api:
        yield api


async def test_scenarios_are_listed_with_their_suite_and_tags(api: Api) -> None:
    scenarios = {s["id"]: s for s in await api.get("/api/evals/scenarios")}

    scenario = scenarios[SCENARIO]
    assert set(scenario) == {"id", "title", "suite", "tags"}
    assert scenario["suite"] == "core"
    assert "sop:delay_risk_high" in scenario["tags"]
    assert len(scenarios) >= 20


async def test_one_scenario_runs_in_scripted_mode_and_passes(api: Api) -> None:
    response = await api.client.post(
        "/api/evals/run", json={"suite": "core", "mode": "scripted", "k": 2, "scenario": SCENARIO}
    )

    assert response.status_code == 200, response.text
    summary = response.json()
    assert (summary["suite"], summary["mode"], summary["k"]) == ("core", "scripted", 2)
    assert (summary["total"], summary["passed"], summary["failed"]) == (1, 1, 0)
    assert summary["pass_rate"] == 1.0
    assert len(summary["sop_version"]) == 12
    [scenario] = summary["scenarios"]
    assert scenario["id"] == SCENARIO
    assert (scenario["trials"], scenario["passes"]) == (2, 2)
    assert (scenario["passed"], scenario["pass_at_k"], scenario["pass_hat_k"]) == (True, True, True)
    assert scenario["failures"] == []
    assert scenario["cost_usd"] == 0.0


async def test_replay_without_cassettes_fails_with_the_reason(api: Api) -> None:
    response = await api.client.post(
        "/api/evals/run", json={"mode": "replay", "scenario": SCENARIO}
    )

    assert response.status_code == 200, response.text
    [scenario] = response.json()["scenarios"]
    assert scenario["passed"] is False
    assert any(f["check"] == "error" for f in scenario["failures"])


async def test_an_unknown_scenario_is_a_404(api: Api) -> None:
    response = await api.client.post("/api/evals/run", json={"scenario": "nope"})

    assert response.status_code == 404
    assert response.json() == {"detail": "no scenario 'nope' in suite 'core'"}


async def test_an_unknown_suite_is_a_404(api: Api) -> None:
    response = await api.client.post("/api/evals/run", json={"suite": "nope"})

    assert response.status_code == 404
    assert response.json() == {"detail": "no scenarios in suite 'nope'"}


async def test_live_mode_is_not_offered(api: Api) -> None:
    response = await api.client.post("/api/evals/run", json={"mode": "live"})

    assert response.status_code == 422
