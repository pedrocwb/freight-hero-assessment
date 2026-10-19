"""The Hero console and the run inspector."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from freight_agent.domain.actions import (
    CloseObligation,
    CloseStatus,
    CreateHeroTask,
    Purpose,
    SendEmail,
)
from freight_agent.domain.obligations import ObligationKind
from tests.integration.test_api_harness import (
    ASK_AGAIN,
    ASK_ETA,
    DECIDE,
    DISPATCHER,
    FOLLOW_UP_IN_30,
    LOAD_ID,
    Api,
    local,
    open_api,
    plan,
)

pytestmark = pytest.mark.unit

CALL_DRIVER = CreateHeroTask(
    title="Call the driver for the ETA",
    description="Two ETA texts got no answer; the dispatcher was emailed.",
    urgent=True,
)
EMAIL_DISPATCHER = SendEmail(
    to_contact_ids=(DISPATCHER,),
    subject="Load 481207: driver ETA needed",
    body="Your driver does not answer. Please send the ETA.",
    purpose=Purpose.ESCALATION,
)


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[Api]:
    async with open_api(tmp_path) as api:
        yield api


async def play_until_the_hero_task(api: Api) -> None:
    """A1, A3 and A4: the ETA SMS, the follow-up, then the email and the Hero task at 08:01."""
    await api.high_risk_at_seven()
    api.model.push(DECIDE, plan(ASK_AGAIN, FOLLOW_UP_IN_30))
    exhausted = CloseObligation(kind=ObligationKind.ETA_REQUEST, status=CloseStatus.EXHAUSTED)
    api.model.push(DECIDE, plan(EMAIL_DISPATCHER, CALL_DRIVER, exhausted))
    await api.advance(to=local(8, 1))


# --- Hero tasks -----------------------------------------------------------------------------


async def test_hero_tasks_list_the_agent_tasks_across_loads(api: Api) -> None:
    await play_until_the_hero_task(api)

    tasks = await api.get("/api/hero/tasks")

    assert len(tasks) == 1
    task = tasks[0]
    assert task == {
        "task_id": task["task_id"],
        "load_id": LOAD_ID,
        "title": CALL_DRIVER.title,
        "description": CALL_DRIVER.description,
        "urgent": True,
        "created_at": local(8, 1),
        "status": "open",
    }
    assert task["task_id"].startswith("out_")
    email = await api.get(f"/api/loads/{LOAD_ID}/conversation", channel="email")
    assert [(e["direction"], e["subject"], e["to"]) for e in email] == [
        ("out", EMAIL_DISPATCHER.subject, [DISPATCHER])
    ]


async def test_completing_a_task_records_the_time_and_the_note(api: Api) -> None:
    await play_until_the_hero_task(api)
    [task] = await api.get("/api/hero/tasks", status="open")
    await api.advance(minutes=9)

    response = await api.client.post(
        f"/api/hero/tasks/{task['task_id']}/complete", json={"note": "called the driver, ETA 9:40"}
    )

    assert response.status_code == 200
    done = response.json()
    assert (done["status"], done["done_at"], done["note"]) == (
        "done",
        local(8, 10),
        "called the driver, ETA 9:40",
    )
    assert await api.get("/api/hero/tasks", status="open") == []
    assert await api.get("/api/hero/tasks", status="done") == [done]
    assert await api.get("/api/hero/tasks", status="all") == [done]


async def test_completing_twice_keeps_the_first_completion(api: Api) -> None:
    await play_until_the_hero_task(api)
    [task] = await api.get("/api/hero/tasks")
    path = f"/api/hero/tasks/{task['task_id']}/complete"

    first = (await api.client.post(path, json={"note": "done"})).json()
    await api.advance(minutes=5)
    second = (await api.client.post(path, json={})).json()

    assert second == first


async def test_an_unknown_sender_becomes_an_urgent_task_without_a_model_call(api: Api) -> None:
    await api.create_load()

    await api.message("send me the rate", local(7, 0), sender="stranger")

    [task] = await api.get("/api/hero/tasks")
    assert task["load_id"] == LOAD_ID
    assert api.model.calls == []


async def test_completing_an_unknown_task_is_a_404(api: Api) -> None:
    response = await api.client.post("/api/hero/tasks/out_nope/complete", json={"note": "x"})

    assert response.status_code == 404
    assert response.json() == {"detail": "unknown Hero task 'out_nope'"}


async def test_an_unknown_status_filter_is_a_422(api: Api) -> None:
    response = await api.client.get("/api/hero/tasks", params={"status": "later"})

    assert response.status_code == 422


# --- run inspector --------------------------------------------------------------------------


async def test_the_inspector_shows_what_each_run_saw_decided_and_cost(api: Api) -> None:
    await api.high_risk_at_seven()

    [run] = await api.get(f"/api/loads/{LOAD_ID}/runs")

    assert run["status"] == "acted"
    assert run["event_id"].startswith("risk:")
    assert run["gate"]["intent"] == "delay_risk_high"
    assert "delay_risk_high" in run["sop_section_ids"]
    assert [a["type"] for a in run["plan"]["actions"]] == ["send_sms", "schedule_timer"]
    assert run["plan"]["actions"][0]["text"] == ASK_ETA.text
    assert run["violations"] == []
    assert [(u["role"], u["model"]) for u in run["usage"]] == [("decide", "scripted")]
    assert run["cost_usd"] == 0.0
    assert (run["skip_reason"], run["error"]) == (None, None)


async def test_runs_are_listed_in_order(api: Api) -> None:
    await play_until_the_hero_task(api)

    runs = await api.get(f"/api/loads/{LOAD_ID}/runs")

    assert [r["started_at"] for r in runs] == [local(7, 1), local(7, 31), local(8, 1)]
