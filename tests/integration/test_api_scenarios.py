"""Scenarios on the platform: list them, play one inside the simulation, follow it.

`POST /api/scenarios/{id}/run` resets the simulation at the scenario's start and plays
its steps in a background task on the app's event loop, which is the test's loop here
(`httpx.ASGITransport`): a test waits for the run by polling `GET /api/scenarios/run`,
or reads the `scenario` events of the stream. The fake model has the rounds of the
scenario's steps queued, and the real graph, rules and graders do the rest.

A scenario plays only when the model is ready, which the OpenRouter key alone decides:
the settings carry a test key, which nothing uses since the model is the injected fake.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest

from freight_agent.api.simulation import SimulationService
from freight_agent.domain.actions import AgentOutcome, WriteTmsNote
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.interfaces.llm import ChatReply, ChatRequest, LlmRequest, LlmResponse, SchemaT
from tests.fakes import FakeModel, Round, act
from tests.integration.test_api_harness import (
    ASK_AGAIN,
    ASK_ETA,
    CALL_DRIVER,
    DECIDE,
    DRIVER,
    EMAIL_DISPATCHER,
    EXHAUSTED,
    FOLLOW_UP_IN_30,
    GATE,
    LOAD_ID,
    PO_REPLY,
    QUESTION,
    TEST_KEY,
    Api,
    local,
    open_api,
    plan,
)
from tests.integration.test_api_stream import StreamReader

pytestmark = pytest.mark.unit

CLIENT_A = "a_client_a_high_risk"  # four steps: a ping, a message, two timers
ACK = "b_ack_with_nothing_open"  # one step: the driver's "ok", which ends at the gate
ACKNOWLEDGED = GateDecision(intent=Intent.ACKNOWLEDGEMENT, act=False, reason="Just an ok.")
WAIT_S = 5.0


@dataclass
class HeldModel(FakeModel):
    """The fake model, but every call waits until the test lets it go: the scenario
    stays in progress, mid-step, for as long as the test needs."""

    called: asyncio.Event = field(default_factory=asyncio.Event)
    go: asyncio.Event = field(default_factory=asyncio.Event)

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        self.called.set()
        await self.go.wait()
        return await super().generate(request, schema)

    async def chat(self, request: ChatRequest, schema: type[SchemaT]) -> ChatReply[SchemaT]:
        self.called.set()
        await self.go.wait()
        return await super().chat(request, schema)


@pytest.fixture
async def api() -> AsyncIterator[Api]:
    async with open_api(openrouter_api_key=TEST_KEY) as api:
        yield api


def queue_client_a(model: FakeModel) -> None:
    """What scenario A expects, step by step: the ETA request (its two rounds cost
    something), the PO answer after the gate, the follow-up, then the escalation."""
    model.push(
        DECIDE,
        Round(calls=(act(ASK_ETA), act(FOLLOW_UP_IN_30)), cost_usd=0.01),
        Round(answer=AgentOutcome(rationale="delay_risk_high step 1"), cost_usd=0.02),
    )
    model.push(GATE, QUESTION)
    model.push(
        DECIDE,
        plan(PO_REPLY),
        plan(ASK_AGAIN, FOLLOW_UP_IN_30),
        plan(EMAIL_DISPATCHER, CALL_DRIVER, EXHAUSTED),
    )


async def start(api: Api, scenario_id: str) -> dict[str, Any]:
    response = await api.client.post(f"/api/scenarios/{scenario_id}/run")
    assert response.status_code == 202, response.text
    return response.json()


async def finished(api: Api) -> dict[str, Any]:
    """The scenario run once it ended. Sleeping gives the background task its turns."""
    async with asyncio.timeout(WAIT_S):
        while (run := await api.get("/api/scenarios/run"))["status"] == "running":
            await asyncio.sleep(0.01)
    return run


def failed_checks(step: dict[str, Any]) -> list[str]:
    return [c["name"] for c in step["checks"] if not c["passed"]]


# --- the catalog ------------------------------------------------------------------------------


async def test_scenarios_are_listed_with_their_steps(api: Api) -> None:
    scenarios = {s["id"]: s for s in await api.get("/api/scenarios")}

    scenario = scenarios[CLIENT_A]
    assert set(scenario) == {"id", "title", "suite", "tags", "description", "steps"}
    assert scenario["suite"] == "core"
    assert scenario["title"].startswith("A: Client A")
    assert "sop:delay_risk_high" in scenario["tags"]
    assert scenario["description"]
    assert [(s["index"], s["at"], s["kind"]) for s in scenario["steps"]] == [
        (0, "07:00", "ping"),
        (1, "07:12", "message"),
        (2, "07:31", "wait"),
        (3, "08:01", "wait"),
    ]
    assert set(scenario["steps"][0]) == {"index", "at", "note", "kind"}
    assert scenario["steps"][1]["note"].startswith("A2: the driver asks for the PO")
    assert len(scenarios) >= 20


async def test_the_catalog_is_ordered_by_suite_then_id_and_knows_attachments(api: Api) -> None:
    scenarios = await api.get("/api/scenarios")

    keys = [(s["suite"], s["id"]) for s in scenarios]
    assert keys == sorted(keys)
    assert {"agent", "core", "docs"} <= {s["suite"] for s in scenarios}
    kinds = {step["kind"] for s in scenarios for step in s["steps"]}
    assert kinds == {"message", "ping", "attachment", "wait"}


async def test_the_catalog_is_listed_without_the_model(api: Api) -> None:
    async with open_api() as without_key:
        assert await without_key.get("/api/scenarios") == await api.get("/api/scenarios")


async def test_the_old_eval_routes_are_gone(api: Api) -> None:
    listed = await api.client.get("/api/evals/scenarios")
    run = await api.client.post("/api/evals/run", json={"scenario": CLIENT_A})

    assert (listed.status_code, run.status_code) == (404, 404)


# --- starting a run ---------------------------------------------------------------------------


async def test_no_run_is_null_before_the_first_one(api: Api) -> None:
    response = await api.client.get("/api/scenarios/run")

    assert (response.status_code, response.json()) == (200, None)


async def test_an_unknown_scenario_is_a_404(api: Api) -> None:
    response = await api.client.post("/api/scenarios/nope/run")

    assert response.status_code == 404
    assert response.json() == {"detail": "no scenario 'nope' (GET /api/scenarios lists them)"}
    assert (await api.client.get("/api/scenarios/run")).json() is None


async def test_without_the_key_a_scenario_is_a_503_and_nothing_is_reset() -> None:
    async with open_api() as api:
        await api.create_load()

        response = await api.client.post(f"/api/scenarios/{CLIENT_A}/run")

        assert response.status_code == 503
        assert "OPENROUTER_API_KEY" in response.json()["detail"]
        assert [s["load_id"] for s in await api.loads()] == [LOAD_ID]
        assert (await api.client.get("/api/scenarios/run")).json() is None
        assert api.model.calls == []


# --- a run that passes ------------------------------------------------------------------------


async def test_a_scenario_plays_in_a_fresh_simulation_and_passes(api: Api) -> None:
    await api.create_load("load_481207_keystone")  # gone with the reset
    queue_client_a(api.model)

    started = await start(api, CLIENT_A)
    run = await finished(api)

    assert started == {"scenario_id": CLIENT_A, "load_id": LOAD_ID}
    assert set(run) == {
        "scenario_id",
        "load_id",
        "status",
        "error",
        "cost_usd",
        "started_at",
        "finished_at",
        "steps",
    }
    assert set(run["steps"][0]) == {"index", "at", "note", "status", "checks", "run_ids"}
    assert (run["scenario_id"], run["load_id"], run["status"], run["error"]) == (
        CLIENT_A,
        LOAD_ID,
        "passed",
        None,
    )
    assert run["cost_usd"] == pytest.approx(0.03)
    started_at, finished_at = (
        datetime.fromisoformat(run[k]) for k in ("started_at", "finished_at")
    )
    assert started_at <= finished_at
    assert [(s["index"], s["at"], s["status"]) for s in run["steps"]] == [
        (0, "07:00", "passed"),
        (1, "07:12", "passed"),
        (2, "07:31", "passed"),
        (3, "08:01", "passed"),
    ]
    assert [failed_checks(s) for s in run["steps"]] == [[], [], [], []]
    assert api.model.remaining() == 0
    state = await api.get("/api/sim/state")
    assert [s["load_id"] for s in state["loads"]] == [LOAD_ID]
    assert state["now"] == local(8, 1)


async def test_each_step_shows_its_declared_checks(api: Api) -> None:
    queue_client_a(api.model)

    await start(api, CLIENT_A)
    run = await finished(api)

    first, *_, last = run["steps"]
    assert [c["name"] for c in first["checks"]] == [
        "expect.actions[0]",
        "forbid[0]",
        "forbid[1]",
        "expect.obligations.eta_request",
        "expect.timers",
        "expect.run_status",
        "expect.llm_calls",
    ]
    assert set(first["checks"][0]) == {"name", "passed", "detail"}
    assert {c["detail"] for c in first["checks"]} == {None}
    # The scenario-wide invariants are shown with the last step.
    assert [c["name"] for c in last["checks"]][-2:] == [
        "invariant.idempotency",
        "invariant.recipients",
    ]


async def test_the_scenario_s_load_and_runs_show_on_the_load_endpoints(api: Api) -> None:
    queue_client_a(api.model)

    await start(api, CLIENT_A)
    run = await finished(api)

    runs = await api.get(f"/api/loads/{LOAD_ID}/runs")
    assert [r["run_id"] for r in runs] == [i for s in run["steps"] for i in s["run_ids"]]
    assert [len(s["run_ids"]) for s in run["steps"]] == [1, 1, 1, 1]
    assert [(r["started_at"], r["status"]) for r in runs] == [
        (local(7, 1), "acted"),
        (local(7, 13), "acted"),
        (local(7, 31), "acted"),
        (local(8, 1), "acted"),
    ]
    agent_loop = ["prefilter", "gate", "knowledge", "agent", "tools", "agent", "validate"]
    assert [r["nodes"] for r in runs] == [agent_loop] * 4
    assert {r["trace_url"] for r in runs} == {None}
    assert [(s["tool"], s["accepted"]) for s in runs[1]["trajectory"]] == [("send_sms", True)]
    assert sum(r["cost_usd"] for r in runs) == pytest.approx(run["cost_usd"])
    snapshot = await api.get(f"/api/loads/{LOAD_ID}")
    kinds = sorted(o["kind"] for o in snapshot["outbox"])
    assert kinds == ["email", "hero_task", "sms", "sms", "sms"]
    [obligation] = snapshot["obligations"]
    assert (obligation["status"], obligation["attempts"]) == ("exhausted", 2)


# --- a run that fails, or breaks --------------------------------------------------------------


async def test_a_step_the_agent_gets_wrong_fails_with_the_reasons(api: Api) -> None:
    """The gate reads the "ok" as an update and the agent writes a note: B1 wanted no
    action, a noop run and one model call."""
    api.model.push(GATE, GateDecision(intent=Intent.STATUS_UPDATE, act=True, reason="update"))
    api.model.push(DECIDE, plan(WriteTmsNote(text="The driver says ok.")))

    await start(api, ACK)
    run = await finished(api)

    assert (run["status"], run["error"]) == ("failed", None)
    [step] = run["steps"]
    assert (step["status"], step["at"]) == ("failed", "07:05")
    assert failed_checks(step) == ["expect.no_actions", "expect.run_status", "expect.llm_calls"]
    detail = {c["name"]: c["detail"] for c in step["checks"]}
    assert detail["expect.llm_calls"].startswith("expected 1 model call(s), got 2")
    assert "tms_note" in detail["expect.no_actions"]
    assert detail["expect.timers"] is None


async def test_the_same_scenario_passes_when_the_gate_ends_the_run(api: Api) -> None:
    api.model.push(GATE, ACKNOWLEDGED)

    await start(api, ACK)
    run = await finished(api)

    assert run["status"] == "passed"
    [run_id] = run["steps"][0]["run_ids"]
    [record] = await api.get(f"/api/loads/{LOAD_ID}/runs")
    assert (record["run_id"], record["status"]) == (run_id, "noop")
    assert record["nodes"] == ["prefilter", "gate"]


async def test_an_error_stops_the_run_at_its_step_and_the_platform_goes_on(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    service: SimulationService = api.app.state.service

    async def broken(*_: object) -> None:
        raise RuntimeError("the store went away")

    monkeypatch.setattr(service, "play_step", broken)

    await start(api, ACK)
    run = await finished(api)

    assert (run["status"], run["error"]) == ("error", "step 0: RuntimeError: the store went away")
    assert run["finished_at"] is not None
    assert run["steps"][0]["status"] == "failed"
    assert service.playing is None
    assert (await api.client.post("/api/sim/tick")).status_code == 200


# --- one run at a time ------------------------------------------------------------------------


async def write_statuses(api: Api) -> dict[str, int]:
    """Every client write the platform has, in an order that works when nothing plays
    (the reset first, the scenario last), and the status each one gets now."""
    fixture = await api.fixture()
    message = {"load_id": LOAD_ID, "channel": "sms", "sender_contact_id": DRIVER, "body": "ok"}
    ping = {"load_id": LOAD_ID, "lat": 1.0, "lon": 2.0, "delay_risk": "high"}
    pod = {"file": ("pod.pdf", b"%PDF-1.7", "application/pdf")}
    post = api.client.post
    responses = {
        "reset": await post("/api/sim/reset", json={}),
        "load": await post("/api/loads", json=fixture),
        "message": await post("/api/v1/events", json=message),
        "ping": await post("/api/v1/pings", json=ping),
        "advance": await post("/api/sim/advance", json={"minutes": 5}),
        "tick": await post("/api/sim/tick"),
        "attachment": await post(f"/api/loads/{LOAD_ID}/attachments", files=pod),
        "scenario": await post(f"/api/scenarios/{ACK}/run"),
    }
    return {name: response.status_code for name, response in responses.items()}


async def test_while_a_scenario_plays_every_write_is_a_409_and_reads_still_answer() -> None:
    model = HeldModel()
    model.push(GATE, ACKNOWLEDGED)
    async with open_api(model, openrouter_api_key=TEST_KEY) as api:
        await start(api, ACK)
        async with asyncio.timeout(WAIT_S):
            await model.called.wait()  # step 0 is in the gate's call

        statuses = await write_statuses(api)
        refused = await api.client.post("/api/sim/tick")
        run = await api.get("/api/scenarios/run")
        loads = await api.loads()

        model.go.set()
        done = await finished(api)

    assert set(statuses.values()) == {409}, statuses
    assert refused.json()["detail"] == (
        f"the scenario {ACK} is playing: wait for it to finish (GET /api/scenarios/run)"
    )
    assert (run["status"], run["steps"][0]["status"], run["finished_at"]) == (
        "running",
        "running",
        None,
    )
    assert [s["load_id"] for s in loads] == [LOAD_ID]
    assert done["status"] == "passed"


async def test_a_second_scenario_is_a_409_while_the_first_plays() -> None:
    model = HeldModel()
    model.push(GATE, ACKNOWLEDGED)
    async with open_api(model, openrouter_api_key=TEST_KEY) as api:
        await start(api, ACK)

        second = await api.client.post(f"/api/scenarios/{CLIENT_A}/run")

        model.go.set()
        await finished(api)

    assert second.status_code == 409
    assert second.json() == {"detail": f"the scenario {ACK} is still running"}


async def test_writes_work_again_once_the_scenario_finished(api: Api) -> None:
    api.model.push(GATE, ACKNOWLEDGED)
    await start(api, ACK)
    await finished(api)
    api.model.push(GATE, ACKNOWLEDGED, ACKNOWLEDGED)  # the message, then the scenario again
    api.model.push(DECIDE, plan(ASK_ETA, FOLLOW_UP_IN_30))  # the ping

    statuses = await write_statuses(api)
    again = await finished(api)

    assert statuses == {
        "reset": 200,
        "load": 201,
        "message": 200,
        "ping": 200,
        "advance": 200,
        "tick": 200,
        "attachment": 201,
        "scenario": 202,
    }
    assert (again["scenario_id"], again["status"]) == (ACK, "passed")
    assert api.model.remaining() == 0


async def test_a_scenario_run_replaces_the_previous_one(api: Api) -> None:
    api.model.push(GATE, ACKNOWLEDGED)
    await start(api, ACK)
    first = await finished(api)
    queue_client_a(api.model)

    await start(api, CLIENT_A)
    second = await finished(api)

    assert (first["scenario_id"], second["scenario_id"]) == (ACK, CLIENT_A)
    assert (await api.get("/api/scenarios/run")) == second
    assert len(await api.get(f"/api/loads/{LOAD_ID}/runs")) == 4  # B1's run is gone


# --- the event stream -------------------------------------------------------------------------


async def test_the_stream_sends_the_run_after_every_step(api: Api) -> None:
    queue_client_a(api.model)
    async with StreamReader(api.app) as stream:
        await stream.event("clock")
        await start(api, CLIENT_A)
        events = [await stream.event("scenario")]
        while events[-1]["status"] == "running":
            events.append(await stream.event("scenario"))

    assert events[-1] == await api.get("/api/scenarios/run")
    first = events[0]
    assert (first["scenario_id"], first["load_id"], first["status"]) == (
        CLIENT_A,
        LOAD_ID,
        "running",
    )
    assert {s["status"] for s in first["steps"]} == {"pending"}
    progress = [[s["status"] for s in e["steps"]] for e in events]
    for index in range(4):
        before = ["passed"] * index
        after = ["pending"] * (3 - index)
        assert [*before, "running", *after] in progress
        assert [*before, "passed", *after] in progress
    assert events[-1]["status"] == "passed"


async def test_the_event_after_a_step_carries_the_cost_so_far(api: Api) -> None:
    """The event is the body of `GET /api/scenarios/run` at that moment: the one that
    marks step 0 done counts step 0's two rounds (0.01 + 0.02)."""
    queue_client_a(api.model)
    async with StreamReader(api.app) as stream:
        await stream.event("clock")
        await start(api, CLIENT_A)
        event = await stream.event("scenario")
        while event["steps"][0]["status"] != "passed":
            event = await stream.event("scenario")
        await finished(api)

    assert event["cost_usd"] == pytest.approx(0.03)


async def test_the_stream_shows_the_scenario_s_runs_as_they_happen(api: Api) -> None:
    api.model.push(GATE, ACKNOWLEDGED)
    async with StreamReader(api.app) as stream:
        await stream.event("clock")
        await start(api, ACK)
        load = await stream.event("load")  # registered before the first step
        run = await stream.event("run")
        await finished(api)

    assert (run["load_id"], run["status"], run["nodes"]) == (LOAD_ID, "noop", ["prefilter", "gate"])
    assert load["load_id"] == LOAD_ID


# --- the run graph ----------------------------------------------------------------------------


async def test_the_graph_is_drawn_with_the_node_ids_runs_use(api: Api) -> None:
    graph = await api.get("/api/graph")

    assert set(graph) == {"mermaid", "nodes"}
    assert graph["nodes"] == ["prefilter", "gate", "knowledge", "agent", "tools", "validate"]
    for node in graph["nodes"]:
        assert node in graph["mermaid"]
