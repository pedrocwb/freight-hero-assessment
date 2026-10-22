"""The UI server needs no model/database and forwards only durable workspace requests."""

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from freight_agent.api.app import create_app
from freight_agent.api.routes import durable
from freight_agent.settings import Settings

pytestmark = pytest.mark.unit


def settings(tmp_path: Path) -> Settings:
    return Settings(
        **{
            "_env_file": None,
            "database_url": None,
            "openrouter_api_key": None,
            "langsmith_api_key": None,
            "models_config": tmp_path / "absent.yaml",
            "web_dist": tmp_path / "absent",
            "durable_api_url": "http://durable.internal:8001",
        }
    )


def test_ui_server_starts_without_runtime_dependencies(tmp_path: Path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        assert client.get("/api/health").json() == {"status": "ok", "mode": "workspace-proxy"}
        paths = client.get("/openapi.json").json()["paths"]
        assert set(paths) == {"/api/health", "/api/durable/health", "/api/durable/workspace/{path}"}
    assert not hasattr(app.state, "service")


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/sim/state"),
        ("POST", "/api/sim/reset"),
        ("POST", "/api/sim/advance"),
        ("POST", "/api/loads"),
        ("POST", "/api/v1/events"),
        ("POST", "/api/scenarios/a_client_a_high_risk/run"),
        ("GET", "/api/stream"),
        ("GET", "/api/durable/sop"),
        ("POST", "/api/durable/demos/aurora/aurora-test"),
    ],
)
def test_retired_routes_are_not_executable(tmp_path: Path, method: str, path: str):
    with TestClient(create_app(settings(tmp_path))) as client:
        assert client.request(method, path).status_code == 404


@pytest.mark.parametrize(
    "method,path,payload,status",
    [
        ("GET", "/health", None, 200),
        ("GET", "/workspace/brokers", None, 200),
        ("PUT", "/workspace/brokers/keystone/profile", {"revision": 2}, 409),
        (
            "POST",
            "/workspace/brokers/aurora/loads/aurora-test/start",
            {"scenario_id": "aurora_extreme", "revision": 1},
            202,
        ),
        (
            "POST",
            "/workspace/brokers/keystone/loads/keystone-test/messages",
            {
                "message_id": "retry-key",
                "sender_contact_id": "drv_20931",
                "channel": "sms",
                "body": "Chego em 20 minutos.",
            },
            202,
        ),
        ("POST", "/workspace/brokers/northline/loads/northline-test/stop", None, 202),
    ],
)
def test_bridge_preserves_broker_path_body_and_status(
    tmp_path: Path, monkeypatch, method, path, payload, status
):
    seen = []

    def handle(request: httpx.Request):
        seen.append(request)
        return httpx.Response(status, json={"result": "upstream"})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        durable.httpx,
        "AsyncClient",
        lambda **kw: original(**kw, transport=httpx.MockTransport(handle)),
    )
    with TestClient(create_app(settings(tmp_path))) as client:
        response = client.request(method, "/api/durable" + path, json=payload)
    assert response.status_code == status
    assert response.json() == {"result": "upstream"}
    assert len(seen) == 1
    assert str(seen[0].url) == "http://durable.internal:8001" + path
    assert seen[0].method == method
    if payload is not None:
        assert json.loads(seen[0].content) == payload


@pytest.mark.parametrize("failure", ["connect", "non-json"])
def test_unavailable_ingress_is_explicit_and_hides_private_details(
    tmp_path: Path, monkeypatch, failure
):
    def handle(request: httpx.Request):
        if failure == "connect":
            raise httpx.ConnectError("private connection detail", request=request)
        return httpx.Response(502, text="private connection detail")

    original = httpx.AsyncClient
    monkeypatch.setattr(
        durable.httpx,
        "AsyncClient",
        lambda **kw: original(**kw, transport=httpx.MockTransport(handle)),
    )
    with TestClient(create_app(settings(tmp_path))) as client:
        response = client.get("/api/durable/health")
        assert response.status_code == 503
        assert "private connection detail" not in response.text
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/durable/workspace/https://elsewhere").status_code == 422
