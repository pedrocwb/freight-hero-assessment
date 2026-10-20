"""The simulation clock and the loads: reset, state, advance, tick, fixtures, errors."""

import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from freight_agent.adapters.documents.memory_blobs import MemoryBlobStore
from freight_agent.adapters.llm.scripted import ScriptedModel
from freight_agent.api.app import create_app
from freight_agent.api.simulation import SimulationService
from freight_agent.domain.actions import CloseObligation, CloseStatus
from freight_agent.domain.obligations import ObligationKind
from tests.integration.test_api_harness import (
    ASK_AGAIN,
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


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[Api]:
    async with open_api(tmp_path) as api:
        yield api


# --- simulation state ---------------------------------------------------------------------


async def test_a_new_app_starts_a_simulation_at_the_scenario_start(api: Api) -> None:
    state = await api.get("/api/sim/state")

    assert state == {
        "now": START,
        "timezone": "America/Los_Angeles",
        "next_timer_due_at": None,
        "llm_mode": "scripted",
        "queue_size": 0,
        "loads": [],
    }


async def test_reset_starts_a_fresh_simulation_at_the_given_time(api: Api) -> None:
    await api.create_load()

    response = await api.client.post(
        "/api/sim/reset", json={"start": "2026-10-14T08:00:00-07:00", "llm_mode": None}
    )

    assert response.status_code == 200
    state = response.json()
    assert (state["now"], state["loads"]) == ("2026-10-14T08:00:00-07:00", [])
    assert (await api.get("/api/sim/state"))["loads"] == []


async def test_a_reset_gives_the_session_fresh_ports(api: Api) -> None:
    """The routes the runtime facade does not cover (documents, knowledge) read the
    current session's store, SOP index and blobs; a reset replaces all three."""
    service: SimulationService = api.app.state.service
    before = (service.uow_factory, service.knowledge, service.blobs)
    ref = await service.blobs.put(b"pod", "application/pdf")
    assert isinstance(service.blobs, MemoryBlobStore)
    assert await service.knowledge.version("northline") is not None

    response = await api.client.post("/api/sim/reset", json={})

    assert response.status_code == 200
    after = (service.uow_factory, service.knowledge, service.blobs)
    assert all(old is not new for old, new in zip(before, after, strict=True))
    with pytest.raises(KeyError):
        await service.blobs.get(ref)


async def test_reset_reads_a_time_without_offset_in_the_simulation_timezone(api: Api) -> None:
    response = await api.client.post(
        "/api/sim/reset", json={"start": "2026-10-13T06:00:00", "timezone": "America/Chicago"}
    )

    state = response.json()
    assert (state["now"], state["timezone"]) == ("2026-10-13T06:00:00-05:00", "America/Chicago")


async def test_reset_with_an_empty_body_uses_the_default_start(api: Api) -> None:
    await api.advance(minutes=30)

    response = await api.client.post("/api/sim/reset", json={})

    assert response.json()["now"] == START


async def test_reset_to_a_mode_that_needs_a_missing_key_keeps_the_old_simulation(
    tmp_path: Path,
) -> None:
    # No injected model here: the mode decides which model the simulation builds.
    app = create_app(api_settings(tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/sim/reset", json={"llm_mode": "live"})
        assert response.status_code == 400
        assert "OPENROUTER_API_KEY" in response.json()["detail"]
        assert (await client.get("/api/sim/state")).json()["llm_mode"] == "scripted"

        replay = await client.post("/api/sim/reset", json={"llm_mode": "replay"})
        assert replay.json()["llm_mode"] == "replay"


async def test_reset_with_an_unknown_timezone_is_a_400(api: Api) -> None:
    response = await api.client.post("/api/sim/reset", json={"timezone": "Mars/Olympus"})

    assert response.status_code == 400
    assert "Mars/Olympus" in response.json()["detail"]


# --- loads ----------------------------------------------------------------------------------


async def test_fixtures_are_the_eval_fixture_and_the_keystone_variants(api: Api) -> None:
    fixtures = {f["name"]: f["metadata"] for f in await api.get("/api/fixtures")}

    assert set(fixtures) == {"load_481207", "load_481207_keystone", "load_481207_keystone_reefer"}
    assert fixtures["load_481207"]["broker"]["id"] == "northline"
    keystone = fixtures["load_481207_keystone"]
    assert keystone["broker"] == {"id": "keystone", "name": "Keystone Freight"}
    reefer = fixtures["load_481207_keystone_reefer"]
    assert reefer["broker"]["id"] == "keystone"
    assert (reefer["load_types"], reefer["stage"]) == (["reefer"], "at_pickup")
    # Distinct ids, so the three loads can run side by side in one simulation.
    assert len({m["load_id"] for m in fixtures.values()}) == 3


async def test_create_a_load_from_the_fixture(api: Api) -> None:
    summary = await api.create_load()

    assert summary == {
        "load_id": LOAD_ID,
        "broker_id": "northline",
        "broker_name": "Northline Logistics",
        "stage": "to_pickup",
        "delay_risk": "low",
        "open_obligations": 0,
        "pending_timers": 0,
        "cost_usd": 0.0,
    }
    assert await api.get("/api/loads") == [summary]
    assert (await api.get("/api/sim/state"))["loads"] == [summary]


async def test_every_fixture_creates_a_load(api: Api) -> None:
    for name in ("load_481207", "load_481207_keystone", "load_481207_keystone_reefer"):
        await api.create_load(name)

    loads = await api.get("/api/loads")
    assert [(s["broker_id"], s["stage"]) for s in loads] == [
        ("northline", "to_pickup"),
        ("keystone", "to_pickup"),
        ("keystone", "at_pickup"),
    ]


async def test_the_load_snapshot_holds_the_load_and_its_memory(api: Api) -> None:
    await api.create_load()

    snapshot = await api.get(f"/api/loads/{LOAD_ID}")

    assert snapshot["load"]["load_id"] == LOAD_ID
    assert snapshot["load"]["contacts"][0]["id"] == "drv_20931"
    for key in ("obligations", "timers", "events", "outbox", "runs"):
        assert snapshot[key] == []


async def test_invalid_metadata_is_a_400_with_the_reason(api: Api) -> None:
    response = await api.client.post("/api/loads", json={"load_id": "x"})

    assert response.status_code == 400
    assert "invalid load metadata" in response.json()["detail"]


async def test_a_load_without_stops_is_a_400_and_is_not_registered(api: Api) -> None:
    """Registered, it would crash every run of the load, the escalation path included."""
    metadata = {**await api.fixture(), "stops": []}

    response = await api.client.post("/api/loads", json=metadata)

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid load metadata: a load needs at least one stop"}
    assert await api.get("/api/loads") == []


async def test_a_load_id_that_exists_is_a_409(api: Api) -> None:
    await api.create_load()

    response = await api.client.post("/api/loads", json=await api.fixture())

    assert response.status_code == 409
    assert LOAD_ID in response.json()["detail"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/loads/nope",
        "/api/loads/nope/runs",
        "/api/loads/nope/tms",
        "/api/loads/nope/conversation?channel=sms",
    ],
)
async def test_an_unknown_load_is_a_404(api: Api, path: str) -> None:
    response = await api.client.get(path)

    assert response.status_code == 404
    assert response.json() == {"detail": "unknown load 'nope'"}


# --- advance and tick ----------------------------------------------------------------------


async def test_advance_by_minutes_moves_the_clock(api: Api) -> None:
    result = await api.advance(minutes=30)

    assert result["state"]["now"] == local(7, 25)
    assert result["runs"] == []


async def test_advance_stops_at_each_timer_due_on_the_way(api: Api) -> None:
    await api.high_risk_at_seven()  # the 07:01 run sets a timer for 07:31
    api.model.push(DECIDE, plan(ASK_AGAIN, FOLLOW_UP_IN_30))
    close = CloseObligation(kind=ObligationKind.ETA_REQUEST, status=CloseStatus.CANCELLED)
    api.model.push(DECIDE, plan(close))

    result = await api.advance(to=local(8, 30))

    # Each timer fires at its own due time, with zero latency, as in the eval runner.
    assert [(r["started_at"], r["status"]) for r in result["runs"]] == [
        (local(7, 31), "acted"),
        (local(8, 1), "acted"),
    ]
    assert result["runs"][0]["event_id"].startswith("timer:")
    assert result["state"]["now"] == local(8, 30)
    assert result["state"]["next_timer_due_at"] is None


async def test_the_state_shows_the_next_timer(api: Api) -> None:
    await api.high_risk_at_seven()

    state = await api.get("/api/sim/state")

    assert state["now"] == local(7, 1)
    assert state["next_timer_due_at"] == local(7, 31)
    [summary] = state["loads"]
    assert (summary["delay_risk"], summary["open_obligations"], summary["pending_timers"]) == (
        "high",
        1,
        1,
    )


async def test_advance_cannot_go_back_in_time(api: Api) -> None:
    response = await api.client.post("/api/sim/advance", json={"to": "2026-10-13T06:00:00-07:00"})

    assert response.status_code == 400
    assert "backwards" in response.json()["detail"]


async def test_advance_needs_exactly_one_of_minutes_and_to(api: Api) -> None:
    both = await api.client.post("/api/sim/advance", json={"minutes": 5, "to": local(7, 0)})
    neither = await api.client.post("/api/sim/advance", json={})

    assert (both.status_code, neither.status_code) == (422, 422)
    assert "detail" in both.json()


async def test_tick_runs_what_is_due_now(api: Api) -> None:
    response = await api.client.post("/api/sim/tick")

    assert response.status_code == 200
    assert response.json()["runs"] == []
    assert response.json()["state"]["now"] == START


# --- cross-cutting ---------------------------------------------------------------------------


async def test_an_unexpected_error_is_a_500_with_an_error_id_and_no_internals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The exception text can carry a connection string or a prompt: it stays in the
    server log, under the id the client gets to report."""
    app = create_app(api_settings(tmp_path), llm=ScriptedModel())

    async def broken() -> None:
        raise RuntimeError("cannot reach postgresql://agent:hunter2@db/freight")

    monkeypatch.setattr(app.state.service, "state", broken)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with caplog.at_level(logging.ERROR, logger="freight_agent.api.app"):
            response = await client.get("/api/sim/state")

    assert response.status_code == 500
    body = response.json()
    error_id = body["error_id"]
    assert body == {
        "detail": f"internal error {error_id}: the server log has the details",
        "error_id": error_id,
    }
    assert "hunter2" not in response.text and "RuntimeError" not in response.text
    [logged] = [r for r in caplog.records if error_id in r.getMessage()]
    assert "GET /api/sim/state" in logged.getMessage()
    assert logged.exc_info is not None and "hunter2" in str(logged.exc_info[1])


async def test_cors_allows_the_vite_dev_server(api: Api) -> None:
    response = await api.client.options(
        "/api/sim/state",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )

    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
