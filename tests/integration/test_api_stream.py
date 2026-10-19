"""The event stream, read through the ASGI interface directly.

httpx's `ASGITransport` returns a response only once the app has sent the whole body,
which an event stream never does, so these tests play the server's side of ASGI: they
call the app, collect the body chunks as they come and read lines with a timeout.
"""

import asyncio
import json
from collections.abc import AsyncIterator, MutableMapping
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import pytest
from fastapi import FastAPI

from freight_agent.adapters.llm.scripted import ScriptedModel
from freight_agent.api.app import create_app
from freight_agent.api.simulation import SimulationService
from tests.integration.test_api_harness import (
    ASK_ETA,
    DECIDE,
    FOLLOW_UP_IN_30,
    LOAD_ID,
    START,
    Api,
    api_settings,
    local,
    open_api,
    plan,
)

pytestmark = pytest.mark.unit

TIMEOUT_S = 2.0


class StreamReader:
    """One GET on the app, served over raw ASGI, read line by line."""

    def __init__(self, app: FastAPI, path: str = "/api/stream") -> None:
        self._app = app
        self._path = path
        self._chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self._disconnect = asyncio.Event()
        self._request_sent = False
        self._buffer = ""
        self._task: asyncio.Task[None] | None = None
        self.status: int | None = None
        self.headers: dict[str, str] = {}

    async def __aenter__(self) -> Self:
        scope: dict[str, Any] = {
            "type": "http",
            # Below 2.4, Starlette watches `receive` for the disconnect and stops the stream.
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": self._path,
            "raw_path": self._path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"test"), (b"accept", b"text/event-stream")],
            "client": ("test", 1234),
            "server": ("test", 80),
        }
        self._task = asyncio.create_task(self._app(scope, self._receive, self._send))
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._disconnect.set()
        assert self._task is not None
        await asyncio.wait_for(self._task, TIMEOUT_S)

    async def _receive(self) -> dict[str, Any]:
        if not self._request_sent:
            self._request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {k.decode(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body":
            self._chunks.put_nowait(message.get("body", b""))

    async def line(self) -> str:
        while "\n" not in self._buffer:
            chunk = await asyncio.wait_for(self._chunks.get(), TIMEOUT_S)
            self._buffer += chunk.decode()
        line, self._buffer = self._buffer.split("\n", 1)
        return line

    async def event(self, name: str) -> dict[str, Any]:
        """Skip to the next event called `name` and return its data."""
        while await self.line() != f"event: {name}":
            pass
        data = await self.line()
        assert data.startswith("data: "), data
        return json.loads(data.removeprefix("data: "))


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[Api]:
    async with open_api(tmp_path) as api:
        yield api


async def test_the_stream_starts_with_the_clock(api: Api) -> None:
    async with StreamReader(api.app) as stream:
        state = await stream.event("clock")

    assert stream.status == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert (state["now"], state["loads"]) == (START, [])


async def test_a_run_is_published_with_its_outbox_and_the_load(api: Api) -> None:
    async with StreamReader(api.app) as stream:
        await stream.event("clock")
        await api.high_risk_at_seven()

        run = await stream.event("run")
        outbox = await stream.event("outbox")
        load = await stream.event("load")
        clock = await stream.event("clock")

    assert (run["status"], run["load_id"], run["cost_usd"]) == ("acted", LOAD_ID, 0.0)
    assert (outbox["kind"], outbox["status"], outbox["payload"]["text"]) == (
        "sms",
        "sent",
        ASK_ETA.text,
    )
    assert (load["load_id"], load["delay_risk"], load["pending_timers"]) == (LOAD_ID, "high", 1)
    assert clock["now"] == local(7, 1)


async def test_an_idle_stream_sends_heartbeats(tmp_path: Path) -> None:
    app = create_app(api_settings(tmp_path), llm=ScriptedModel(), heartbeat_s=0.01)

    async with StreamReader(app) as stream:
        await stream.event("clock")
        lines = [await stream.line() for _ in range(4)]

    assert ": heartbeat" in lines


async def test_a_closed_stream_unsubscribes(api: Api) -> None:
    service: SimulationService = api.app.state.service
    async with StreamReader(api.app) as stream:
        await stream.event("clock")
        assert service.bus.subscribers == 1

    assert service.bus.subscribers == 0


async def test_events_still_flow_after_a_reset(api: Api) -> None:
    async with StreamReader(api.app) as stream:
        await stream.event("clock")
        await api.client.post("/api/sim/reset", json={})
        assert (await stream.event("clock"))["now"] == START

        api.model.push(DECIDE, plan(ASK_ETA, FOLLOW_UP_IN_30))
        await api.create_load()
        await api.client.post(
            "/api/v1/pings",
            json={"load_id": LOAD_ID, "lat": 1.0, "lon": 2.0, "delay_risk": "high"},
        )
        run = await stream.event("run")

    assert run["status"] == "acted"
