"""The health endpoint: one verdict from our own data, and the settings without secrets."""

from pathlib import Path

import pytest
from pydantic import SecretStr

from tests.integration.test_api_harness import LOAD_ID, local, open_api

pytestmark = pytest.mark.unit

SECRET = "sk-or-v1-api-health-test-secret"


async def test_a_quiet_simulation_is_healthy(tmp_path: Path) -> None:
    async with open_api(tmp_path) as api:
        health = await api.get("/api/health")

    assert health["status"] == "ok"
    assert health["reasons"] == []
    assert health["queue"]["queued"] == 0
    assert health["runs"]["total"] == 0
    assert health["cost"]["total_cost_usd"] == 0.0
    assert health["outbox"]["pending"] == 0
    assert health["settings"]["LLM_MODE"] == "scripted"


async def test_health_never_shows_a_secret(tmp_path: Path) -> None:
    async with open_api(tmp_path, openrouter_api_key=SecretStr(SECRET)) as api:
        response = await api.client.get("/api/health")

    assert SECRET not in response.text
    assert response.json()["settings"]["OPENROUTER_API_KEY"] == "set"


async def test_many_escalations_turn_health_to_warn(tmp_path: Path) -> None:
    async with open_api(tmp_path) as api:
        await api.create_load()
        await api.message("who is this?", local(7, 0), sender="stranger")  # escalated

        health = await api.get("/api/health")

    assert health["status"] == "warn"
    assert [r["check"] for r in health["reasons"]] == ["escalation_rate"]
    assert health["runs"]["status_counts"]["escalated"] == 1
    assert health["cost"]["loads"][0]["load_id"] == LOAD_ID
