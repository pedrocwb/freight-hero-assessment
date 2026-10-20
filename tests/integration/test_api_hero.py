"""The escalation to the Hero team, and the run inspector: what each run saw and did.

The Hero console's routes are gone (the web reads the load's snapshot): the Hero task
and the dispatcher's email are outbox items of the load.
"""

from collections.abc import AsyncIterator

import pytest

from freight_agent.application.escalation import UNKNOWN_SENDER_TITLE
from tests.integration.test_api_harness import (
    ASK_AGAIN,
    ASK_ETA,
    CALL_DRIVER,
    DECIDE,
    DISPATCHER,
    EMAIL_DISPATCHER,
    EXHAUSTED,
    FOLLOW_UP_IN_30,
    LOAD_ID,
    Api,
    local,
    open_api,
    plan,
)
from tests.prompts import PROMPTS

pytestmark = pytest.mark.unit


@pytest.fixture
async def api() -> AsyncIterator[Api]:
    async with open_api() as api:
        yield api


async def play_until_the_hero_task(api: Api) -> None:
    """A1, A3 and A4: the ETA SMS, the follow-up, then the email and the Hero task at 08:01."""
    await api.high_risk_at_seven()
    api.model.push(DECIDE, plan(ASK_AGAIN, FOLLOW_UP_IN_30))
    api.model.push(DECIDE, plan(EMAIL_DISPATCHER, CALL_DRIVER, EXHAUSTED))
    await api.advance(to=local(8, 1))


# --- the escalation -------------------------------------------------------------------------


async def test_the_escalation_emails_the_dispatcher_and_creates_a_hero_task(api: Api) -> None:
    await play_until_the_hero_task(api)

    [task] = await api.outbox("hero_task")
    [email] = await api.outbox("email")

    assert (task["created_at"], task["status"]) == (local(8, 1), "sent")
    assert task["payload"] == {
        "title": CALL_DRIVER.title,
        "description": CALL_DRIVER.description,
        "urgent": True,
    }
    assert email["payload"]["to_contact_ids"] == [DISPATCHER]
    assert email["payload"]["subject"] == EMAIL_DISPATCHER.subject
    snapshot = await api.get(f"/api/loads/{LOAD_ID}")
    [obligation] = snapshot["obligations"]
    assert (obligation["status"], obligation["attempts"]) == ("exhausted", 2)


async def test_an_unknown_sender_becomes_a_hero_task_without_a_model_call(api: Api) -> None:
    await api.create_load()

    await api.message("send me the rate", local(7, 0), sender="stranger")

    [task] = await api.outbox("hero_task")
    assert (task["load_id"], task["payload"]["title"]) == (LOAD_ID, UNKNOWN_SENDER_TITLE)
    [run] = await api.get(f"/api/loads/{LOAD_ID}/runs")
    assert (run["status"], run["nodes"]) == ("escalated", ["prefilter"])
    assert api.model.calls == []


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
    # One usage per model round: the round that staged the actions, then the finish.
    assert [(u["role"], u["model"]) for u in run["usage"]] == [("agent", "fake")] * 2
    assert run["cost_usd"] == 0.0
    assert (run["skip_reason"], run["error"], run["trace_url"]) == (None, None, None)


async def test_a_run_lists_the_graph_nodes_it_went_through(api: Api) -> None:
    """A risk change needs no gate call, but the run still passes the gate node; the agent
    stages in one round, the tools node runs the calls, the agent finishes."""
    await api.high_risk_at_seven()

    [run] = await api.get(f"/api/loads/{LOAD_ID}/runs")

    assert run["nodes"] == ["prefilter", "gate", "knowledge", "agent", "tools", "agent", "validate"]
    assert set(run["nodes"]) <= set((await api.get("/api/graph"))["nodes"])


async def test_a_run_shows_the_version_of_each_prompt_it_sent(api: Api) -> None:
    """The repository copy here (no LangSmith key): `local:<sha256 prefix>`; with the Hub,
    its commit. A risk change sends no gate prompt."""
    await api.high_risk_at_seven()

    [run] = await api.get(f"/api/loads/{LOAD_ID}/runs")

    assert run["prompts"] == {"agent": PROMPTS.agent.version}
    assert run["prompts"]["agent"].startswith("local:")


async def test_the_trajectory_shows_each_tool_call_and_whether_it_was_accepted(
    api: Api,
) -> None:
    await api.high_risk_at_seven()

    [run] = await api.get(f"/api/loads/{LOAD_ID}/runs")

    assert [(s["tool"], s["accepted"]) for s in run["trajectory"]] == [
        ("send_sms", True),
        ("schedule_timer", True),
    ]
    sms = run["trajectory"][0]
    assert sms["args"]["contact_id"] == ASK_ETA.contact_id
    assert "accepted as action #1" in sms["result"]
    assert set(sms) == {"tool", "args", "result", "accepted", "cost_usd"}


async def test_runs_are_listed_in_order(api: Api) -> None:
    await play_until_the_hero_task(api)

    runs = await api.get(f"/api/loads/{LOAD_ID}/runs")

    assert [r["started_at"] for r in runs] == [local(7, 1), local(7, 31), local(8, 1)]
