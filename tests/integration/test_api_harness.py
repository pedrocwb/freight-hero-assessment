"""Shared setup for the API tests (no tests here).

Each test gets its own app on an in-memory simulation, reached through
`httpx.AsyncClient` over `ASGITransport`: no server, no network, no database. The model
is a `ScriptedModel` injected into `create_app`, so a test pushes the gate decisions and
plans the agent will return before it drives the endpoint that makes the agent run.
The real `sops/`, `evals/` and `config/` trees of the repository are used.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI

from freight_agent.adapters.llm.scripted import ScriptedModel
from freight_agent.api.app import create_app
from freight_agent.domain.actions import Action, Plan, Purpose, ScheduleTimer, SendSms
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.obligations import ObligationKind
from freight_agent.ports.llm import ModelRole
from freight_agent.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
LA = ZoneInfo("America/Los_Angeles")
LOAD_ID = "481207"
DRIVER = "drv_20931"
DISPATCHER = "dsp_4410"
BROKER = "brk_0087"
GATE = ModelRole.GATE
DECIDE = ModelRole.DECIDE
START = "2026-10-13T06:55:00-07:00"

ASK_ETA = SendSms(
    contact_id=DRIVER,
    text="Hi, this is Northline Logistics. What time will you arrive at the pickup?",
    purpose=Purpose.ETA_REQUEST,
)
ASK_AGAIN = SendSms(
    contact_id=DRIVER,
    text="Following up: what time will you arrive at the pickup?",
    purpose=Purpose.ETA_REQUEST,
)
PO_REPLY = SendSms(
    contact_id=DRIVER,
    text="The PO number is 55821. You need it at the gate.",
    purpose=Purpose.REPLY,
)
FOLLOW_UP_IN_30 = ScheduleTimer(kind=ObligationKind.ETA_REQUEST, due_in_minutes=30)
QUESTION = GateDecision(intent=Intent.QUESTION, act=True, reason="The driver asks for the PO.")


def local(hour: int, minute: int, second: int = 0) -> str:
    """An ISO time on the scenario day, 2026-10-13, as the API writes it (PDT)."""
    return f"2026-10-13T{hour:02d}:{minute:02d}:{second:02d}-07:00"


def plan(*actions: Action) -> Plan:
    return Plan(actions=actions, rationale="scripted for the API tests")


def api_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """The repository's SOPs, evals and models; no `.env`, no key, no tracing."""
    values: dict[str, Any] = {
        "llm_mode": "scripted",
        "persistence": "memory",
        "langsmith_tracing": False,
        "openrouter_api_key": None,
        "langsmith_api_key": None,
        "database_url": None,
        "sops_dir": ROOT / "sops",
        "evals_dir": ROOT / "evals",
        "models_config": ROOT / "config" / "models.yaml",
        "cassettes_dir": tmp_path / "cassettes",
        **overrides,
    }
    return Settings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


@dataclass
class Api:
    """One app, its client and the scripted model behind it."""

    app: FastAPI
    client: httpx.AsyncClient
    model: ScriptedModel

    async def fixture(self, name: str = "load_481207") -> dict[str, Any]:
        fixtures = (await self.client.get("/api/fixtures")).json()
        return next(f["metadata"] for f in fixtures if f["name"] == name)

    async def create_load(self, name: str = "load_481207") -> dict[str, Any]:
        response = await self.client.post("/api/loads", json=await self.fixture(name))
        assert response.status_code == 201, response.text
        return response.json()

    async def ping(self, risk: str, at: str, *, load_id: str = LOAD_ID) -> dict[str, Any]:
        body = {
            "load_id": load_id,
            "lat": 37.9577,
            "lon": -121.2908,
            "occurred_at": at,
            "delay_risk": risk,
        }
        response = await self.client.post("/api/v1/pings", json=body)
        assert response.status_code == 200, response.text
        return response.json()

    async def message(
        self, body: str, at: str, *, sender: str | None = DRIVER, channel: str = "sms"
    ) -> dict[str, Any]:
        payload = {
            "load_id": LOAD_ID,
            "channel": channel,
            "sender_contact_id": sender,
            "body": body,
            "occurred_at": at,
        }
        response = await self.client.post("/api/v1/events", json=payload)
        assert response.status_code == 200, response.text
        return response.json()

    async def advance(self, **body: Any) -> dict[str, Any]:
        response = await self.client.post("/api/sim/advance", json=body)
        assert response.status_code == 200, response.text
        return response.json()

    async def get(self, path: str, **params: Any) -> Any:
        response = await self.client.get(path, params=params)
        assert response.status_code == 200, response.text
        return response.json()

    async def high_risk_at_seven(self) -> None:
        """A1 of the assignment: the load, then the 07:00 ping; the ETA SMS leaves at 07:01."""
        await self.create_load()
        self.model.push(DECIDE, plan(ASK_ETA, FOLLOW_UP_IN_30))
        await self.ping("high", local(7, 0))


@asynccontextmanager
async def open_api(tmp_path: Path, **overrides: Any) -> AsyncIterator[Api]:
    model = ScriptedModel()
    app = create_app(api_settings(tmp_path, **overrides), llm=model)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield Api(app=app, client=client, model=model)
