"""A UI retry starts one real-clock load; unavailable AI cannot masquerade as a demo."""

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from temporalio.client import Client
from tests.unit.application.test_runtime_harness import World
from tests.unit.orchestration.test_ingress import ClientStub

from freight_agent.api.routes import durable
from freight_agent.application.runtime import RuntimeConfig
from freight_agent.domain.load import Load
from freight_agent.orchestration import api
from freight_agent.orchestration.service import TemporalIngress
from freight_agent.settings import Settings

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("ready", [True, False])
def test_demo_start_uses_one_load_and_one_risk_event(load: Load, monkeypatch, ready: bool):
    world = World(load, config=RuntimeConfig(delivery_followups=True))
    stub = ClientStub()
    service = TemporalIngress(world.deps, cast(Client, stub), "test-demo")

    @asynccontextmanager
    async def wired(settings):
        yield service

    monkeypatch.setattr(api, "services", wired)
    settings = Settings(
        **{
            "_env_file": None,
            "openrouter_api_key": SecretStr("test") if ready else None,
            "evals_dir": ROOT / "evals",
        }
    )
    with TestClient(api.create_app(settings)) as client:
        assert client.get("/health").json()["model_ready"] is ready
        path = "/demos/aurora/aurora-test"
        response = client.post(path)
        if not ready:
            assert response.status_code == 503
            assert not stub.calls
            return
        assert response.status_code == 202
        assert client.post(path).json() == response.json()
        state = client.get("/brokers/aurora/loads/aurora-test").json()
        assert len(state["events"]) == 1
        assert state["load"]["broker_id"] == "aurora"
        assert state["load"]["delay_risk"] == "high"
        assert state["load"]["stage"] == "to_delivery"
        assert client.post("/demos/aurora/not-a-demo").status_code == 422


@pytest.mark.parametrize("unavailable", [True, False])
def test_platform_bridge_uses_only_fixed_durable_upstream(monkeypatch, unavailable: bool):
    seen = []

    def handle(request: httpx.Request):
        seen.append(request)
        if unavailable:
            raise httpx.ConnectError("private connection detail", request=request)
        return httpx.Response(202, json={"load_id": "aurora-test"})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        durable.httpx,
        "AsyncClient",
        lambda **kw: original(**kw, transport=httpx.MockTransport(handle)),
    )
    app = FastAPI()
    app.state.durable_api_url = "http://durable.internal:8001"
    app.include_router(durable.router, prefix="/api")
    with TestClient(app) as client:
        response = client.post("/api/durable/demos/aurora/aurora-test")
        assert response.status_code == (503 if unavailable else 202)
        assert str(seen[0].url) == "http://durable.internal:8001/demos/aurora/aurora-test"
        assert seen[0].method == "POST"
        assert "private connection detail" not in response.text
        assert client.get("/api/durable/loads/other-broker").status_code == 422
        assert len(seen) == 1


def test_driver_message_is_scoped_and_preserves_the_retry_key(monkeypatch):
    seen = []

    def handle(request: httpx.Request):
        seen.append(request)
        return httpx.Response(202, json={"accepted": True, "duplicate": False})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        durable.httpx,
        "AsyncClient",
        lambda **kw: original(**kw, transport=httpx.MockTransport(handle)),
    )
    app = FastAPI()
    app.state.durable_api_url = "http://durable.internal:8001"
    app.include_router(durable.router, prefix="/api")
    with TestClient(app) as client:
        path = "/api/durable/loads/aurora-test/messages"
        message = {"message_id": "retry-key-1", "body": "  Chego em 20 minutos.  "}
        assert client.post(path, json=message).status_code == 202
        assert client.post(path, json=message).status_code == 202
        payloads = [json.loads(r.content) for r in seen]
        assert all(p["message_id"] == "retry-key-1" for p in payloads)
        assert payloads[0]["sender_contact_id"] == "drv_aurora"
        assert payloads[0]["channel"] == "sms"
        assert payloads[0]["load_id"] == "aurora-test"
        assert payloads[0]["body"] == "Chego em 20 minutos."
        assert str(seen[0].url).endswith("/brokers/aurora/loads/aurora-test/messages")
        assert (
            client.post(path, json={**message, "sender_contact_id": "someone-else"}).status_code
            == 422
        )
        assert client.post(path, json={**message, "body": "  "}).status_code == 422
        assert len(seen) == 2
