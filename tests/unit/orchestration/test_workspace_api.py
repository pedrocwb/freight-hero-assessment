"""Exercise the live broker routes with real ingress logic and isolated dependencies."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from temporalio.client import Client
from tests.unit.application.test_runtime_harness import World
from tests.unit.orchestration.test_ingress import ClientStub

from freight_agent.application.runtime import RuntimeConfig
from freight_agent.domain.load import Load
from freight_agent.orchestration import api
from freight_agent.orchestration.policy import pinned_index
from freight_agent.orchestration.profiles import default_profile, freeze_profile
from freight_agent.orchestration.service import TemporalIngress
from freight_agent.settings import Settings

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("ready", [True, False])
def test_aurora_runs_through_broker_workspace_only(load: Load, monkeypatch, ready: bool):
    world = World(load, config=RuntimeConfig(delivery_followups=True))
    stub = ClientStub()
    service = TemporalIngress(world.deps, cast(Client, stub), "workspace-api")

    @asynccontextmanager
    async def wired(settings):
        yield service

    monkeypatch.setattr(api, "services", wired)
    store = Mock(close=AsyncMock())
    profiles = Mock(get_or_create=AsyncMock(side_effect=lambda broker, default: default))
    monkeypatch.setattr(api, "PostgresUnitOfWorkFactory", Mock(return_value=store))
    monkeypatch.setattr(api, "PostgresProfiles", Mock(return_value=profiles))
    settings = Settings(
        **{
            "_env_file": None,
            "openrouter_api_key": "test" if ready else None,
            "langsmith_api_key": None,
            "database_url": "postgresql://unused/test",
            "evals_dir": ROOT / "evals",
            "sops_dir": ROOT / "sops",
        }
    )
    with TestClient(api.create_app(settings)) as client:
        assert client.get("/health").json()["model_ready"] is ready
        assert client.post("/demos/aurora/aurora-test").status_code == 404
        assert client.get("/demos/aurora-policy").status_code == 404
        broker = "/workspace/brokers/aurora"
        profile = client.get(broker + "/profile").json()
        scenarios = client.get(broker + "/scenarios").json()
        path = broker + "/loads/aurora-test"
        body = {"scenario_id": scenarios[0]["id"], "revision": profile["revision"]}
        response = client.post(path + "/start", json=body)
        if ready:
            assert response.status_code == 202
            assert client.post(path + "/start", json=body).json() == response.json()
            state = client.get(path).json()
            assert len(state["events"]) == 1
            assert state["load"]["delay_risk"] == "high"
            frozen = state["load"]["sop_snapshot"]
            assert frozen["profile_revision"] == profile["revision"]
            assert "policy" not in frozen
            assert frozen["broker"]["broker_id"] == "aurora"
            message = {
                "message_id": "reply-1",
                "body": "Chego em 20 minutos.",
                "sender_contact_id": "drv_aurora",
                "channel": "sms",
            }
            assert client.post(path + "/messages", json=message).status_code == 202
            assert client.post(path + "/messages", json=message).json()["duplicate"]
            assert client.post(path + "/messages", json={**message, "body": " "}).status_code == 422
            assert client.post(path + "/stop").json()["status"] == "stopping"
            assert client.post(path + "/messages", json=message).status_code == 409
        else:
            assert response.status_code == 503
            assert not stub.calls
    store.close.assert_awaited_once()


async def test_existing_demo_snapshots_remain_readable():
    # Old loads have the same catalog/sections/version, plus a policy instead of a revision.
    frozen = freeze_profile(await default_profile(ROOT / "sops", "aurora"))
    frozen.pop("profile_revision")
    frozen["policy"] = {"max_attempts": 10, "interval_minutes": 1, "body": "original SOP"}
    index = pinned_index(frozen)
    eta = (await index.broker("aurora")).obligation("eta_request")
    assert eta and eta.max_attempts == 10 and eta.follow_up_minutes == 1
    assert await index.read("aurora", (), "delay_risk_high")
    assert await index.version("aurora") == frozen["version"]
