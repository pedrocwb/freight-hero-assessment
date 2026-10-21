"""A load as a conversation: send and wait, the recording, and saving it as a scenario.

`POST /api/loads/{id}/messages` and `/pings` ingest, let the latency pass, tick, and
return the runs the input caused, so the web UI shows the agent's answer from one
request. Every input (and every clock move that ran the agent) is recorded with what it
changed (`GET /api/loads/{id}/recording`), and `POST /api/scenarios` writes the recording
with the chosen expectations as a scenario file that plays back and passes.

The evals tree is a copy in a temporary folder, so the tests never write the repository.
"""

import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from freight_agent.api.scenario_files import render
from freight_agent.evalkit.spec import Scenario, load_scenario
from tests.fakes import FakeModel
from tests.integration.test_api_harness import (
    ASK_AGAIN,
    ASK_ETA,
    DECIDE,
    DRIVER,
    FOLLOW_UP_IN_30,
    GATE,
    LOAD_ID,
    PO_REPLY,
    QUESTION,
    ROOT,
    TEST_KEY,
    Api,
    local,
    open_api,
    plan,
)
from tests.integration.test_api_scenarios import finished, start

pytestmark = pytest.mark.unit

SCENARIO_ID = "recorded_po_question"


@pytest.fixture
def evals(tmp_path: Path) -> Path:
    target = tmp_path / "evals"
    shutil.copytree(ROOT / "evals", target)
    return target


@pytest.fixture
async def api(evals: Path) -> AsyncIterator[Api]:
    async with open_api(openrouter_api_key=TEST_KEY, evals_dir=evals) as api:
        yield api


def queue(model: FakeModel) -> None:
    """The agent's side of the conversation: the ETA request, the PO answer, the
    follow-up at 07:31."""
    model.push(DECIDE, plan(ASK_ETA, FOLLOW_UP_IN_30))
    model.push(GATE, QUESTION)
    model.push(DECIDE, plan(PO_REPLY), plan(ASK_AGAIN, FOLLOW_UP_IN_30))


async def post(api: Api, path: str, body: dict[str, Any], status: int = 200) -> Any:
    response = await api.client.post(path, json=body)
    assert response.status_code == status, response.text
    return response.json()


async def play(api: Api) -> None:
    """The high-risk ping at 07:00, the driver's question at 07:12, the 07:31 timer."""
    await api.create_load()
    queue(api.model)
    await post(
        api, f"/api/loads/{LOAD_ID}/pings", {"delay_risk": "high", "occurred_at": local(7, 0)}
    )
    await post(
        api,
        f"/api/loads/{LOAD_ID}/messages",
        {
            "channel": "sms",
            "sender_contact_id": DRIVER,
            "body": "What's the PO number?",
            "occurred_at": local(7, 12),
        },
    )
    state = await api.get("/api/sim/state")
    await api.advance(to=state["next_timer_due_at"])


async def test_a_message_returns_the_run_it_caused_and_the_clock_after_it(api: Api) -> None:
    await api.create_load()
    queue(api.model)

    ping = await post(
        api, f"/api/loads/{LOAD_ID}/pings", {"delay_risk": "high", "occurred_at": local(7, 0)}
    )
    reply = await post(
        api,
        f"/api/loads/{LOAD_ID}/messages",
        {"channel": "sms", "sender_contact_id": DRIVER, "body": "What's the PO number?"},
    )

    assert [r["status"] for r in ping["runs"]] == ["acted"]
    assert ping["state"]["now"] == local(7, 1)
    assert reply["accepted"] and not reply["duplicate"]
    [run] = reply["runs"]
    assert (run["status"], run["gate"]["intent"], run["started_at"]) == (
        "acted",
        "question",
        local(7, 2),
    )
    assert reply["state"]["now"] == local(7, 2)
    texts = [o["payload"]["text"] for o in await api.outbox("sms")]
    assert texts == [ASK_ETA.text, PO_REPLY.text]


async def test_a_redelivered_message_returns_no_run(api: Api) -> None:
    await api.create_load()
    api.model.push(GATE, QUESTION)
    api.model.push(DECIDE, plan(PO_REPLY))
    body = {"message_id": "m1", "channel": "sms", "sender_contact_id": DRIVER, "body": "PO?"}

    await post(api, f"/api/loads/{LOAD_ID}/messages", body)
    again = await post(api, f"/api/loads/{LOAD_ID}/messages", body)

    assert (again["duplicate"], again["runs"]) == (True, [])
    assert len(await api.get(f"/api/loads/{LOAD_ID}/recording")) == 1


async def test_an_unknown_load_is_a_404(api: Api) -> None:
    response = await api.client.post(
        "/api/loads/nope/messages", json={"channel": "sms", "body": "hi"}
    )
    assert response.status_code == 404
    assert (await api.client.get("/api/loads/nope/recording")).status_code == 404


async def test_each_input_and_clock_move_is_recorded_with_what_it_changed(api: Api) -> None:
    await play(api)

    steps = await api.get(f"/api/loads/{LOAD_ID}/recording")

    assert [(s["kind"], s["at"], s["llm_calls"], s["run_statuses"]) for s in steps] == [
        ("ping", local(7, 0), 1, ["acted"]),
        ("message", local(7, 12), 2, ["acted"]),
        ("wait", local(7, 31), 1, ["acted"]),
    ]
    assert [s["obligations"]["eta_request"]["attempts"] for s in steps] == [1, 1, 2]
    assert steps[0]["obligations"]["eta_request"]["max_attempts"] == 2
    assert [s["timers"] for s in steps] == [
        {"pending": 1, "due_at": "07:31"},
        {"pending": 1, "due_at": "07:31"},
        {"pending": 1, "due_at": "08:01"},
    ]
    assert steps[1]["message"] == {
        "channel": "sms",
        "sender": DRIVER,
        "body": "What's the PO number?",
        "attachments": [],
    }
    assert all(len(s["outbox_ids"]) == 1 for s in steps)


def draft(**changes: Any) -> dict[str, Any]:
    sms = {"kind": "sms", "to": DRIVER, "count": 1}
    return {
        "load_id": LOAD_ID,
        "id": SCENARIO_ID,
        "title": "The driver asks for the PO while the ETA request is open",
        "tags": ["regression", "sop:delay_risk_high", "sop:driver_questions"],
        "steps": [
            {
                "index": 0,
                "note": "The risk goes high: the first ETA request",
                "expect": {
                    "actions": [sms],
                    "obligations": {"eta_request": {"status": "open", "attempts": 1}},
                    "timers": {"pending": 1, "due_at": "07:31"},
                    "run_status": ["acted"],
                    "llm_calls": 1,
                },
            },
            {
                "index": 1,
                "note": "The answer goes out; the request stays open",
                "expect": {"actions": [sms], "run_status": ["acted"], "llm_calls": 2},
                "forbid": [{"kind": "email"}, {"kind": "hero_task"}],
            },
            {
                "index": 2,
                "note": "No ETA after 30 minutes: the second request",
                "expect": {
                    "actions": [sms],
                    "obligations": {"eta_request": {"status": "open", "attempts": 2}},
                    "timers": {"pending": 1, "due_at": "08:01"},
                },
            },
        ],
        **changes,
    }


async def test_a_conversation_saved_as_a_scenario_is_listed_and_plays_back(
    api: Api, evals: Path
) -> None:
    await play(api)

    saved = await post(api, "/api/scenarios", draft(), status=201)

    path = evals / saved["path"]
    assert saved == {
        "id": SCENARIO_ID,
        "suite": "core",
        "path": f"scenarios/core/{SCENARIO_ID}.yaml",
    }
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# The driver asks for the PO")
    assert "- {kind: sms, to: drv_20931, count: 1}" in text
    scenario = load_scenario(path)
    assert (scenario.load, scenario.load_overrides, scenario.start) == (
        "fixtures/load_481207.json",
        {},
        "06:55",
    )
    assert [(s.at, s.kind) for s in scenario.steps] == [
        ("07:00", "ping"),
        ("07:12", "message"),
        ("07:31", "wait"),
    ]
    listed = {s["id"]: s for s in await api.get("/api/scenarios")}
    assert [s["kind"] for s in listed[SCENARIO_ID]["steps"]] == ["ping", "message", "wait"]

    queue(api.model)
    await start(api, SCENARIO_ID)
    run = await finished(api)
    assert run["status"] == "passed", run


async def test_a_played_scenario_is_recorded_and_saves_again_as_a_new_one(api: Api) -> None:
    await play(api)
    await post(api, "/api/scenarios", draft(), status=201)
    queue(api.model)
    await start(api, SCENARIO_ID)
    await finished(api)

    steps = await api.get(f"/api/loads/{LOAD_ID}/recording")
    again = await post(api, "/api/scenarios", draft(id="recorded_po_question_again"), status=201)

    assert [(s["kind"], s["llm_calls"]) for s in steps] == [
        ("ping", 1),
        ("message", 2),
        ("wait", 1),
    ]
    assert again["id"] == "recorded_po_question_again"


async def test_a_taken_id_is_refused(api: Api, evals: Path) -> None:
    await play(api)
    await post(api, "/api/scenarios", draft(), status=201)

    again = await api.client.post("/api/scenarios", json=draft(suite="memory"))
    existing = await api.client.post("/api/scenarios", json=draft(id="a_client_a_high_risk"))

    assert again.status_code == existing.status_code == 409
    assert not (evals / "scenarios" / "memory" / f"{SCENARIO_ID}.yaml").exists()


async def test_a_scenario_the_spec_refuses_is_a_422_and_writes_nothing(
    api: Api, evals: Path
) -> None:
    await play(api)
    bad = draft()
    bad["steps"][0]["expect"] = {"run_status": ["thought_about_it"]}

    response = await api.client.post("/api/scenarios", json=bad)
    bad_id = await api.client.post("/api/scenarios", json=draft(id="Not An Id"))

    assert response.status_code == 422
    assert "run_status" in response.json()["detail"]
    assert bad_id.status_code == 400
    assert not list((evals / "scenarios").rglob(f"{SCENARIO_ID}.yaml"))


async def test_a_load_without_a_recording_cannot_be_saved(api: Api) -> None:
    await api.create_load()
    response = await api.client.post("/api/scenarios", json=draft())
    assert response.status_code == 404


def test_a_rendered_scenario_reads_back_with_its_times_as_written(tmp_path: Path) -> None:
    # YAML 1.1 reads an unquoted 10:30 as 630 (minutes in base 60): times are quoted.
    scenario = Scenario.model_validate(
        {
            "id": "late_morning",
            "title": "A late morning",
            "suite": "core",
            "load": "fixtures/load_481207.json",
            "date": "2026-10-13",
            "start": "10:00",
            "steps": [
                {
                    "at": "10:30",
                    "note": "Wait: it is 10:30",
                    "expect": {"timers": {"due_at": "11:00"}},
                }
            ],
        }
    )
    path = tmp_path / "late_morning.yaml"

    path.write_text(
        render(scenario, load_id=LOAD_ID, broker="Northline Logistics"), encoding="utf-8"
    )

    assert load_scenario(path) == scenario
    assert 'start: "10:00"' in path.read_text(encoding="utf-8")
