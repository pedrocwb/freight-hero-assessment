"""Ingress and channels: the assignment's sequence played through the API.

A ping or a message goes in through `/api/v1`, the API lets `latency_seconds` of virtual
time pass and ticks, and what the agent sent shows up in the load's conversation.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from freight_agent.domain.actions import WriteTmsNote
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.ports.llm import ModelRole
from tests.integration.test_api_harness import (
    ASK_AGAIN,
    ASK_ETA,
    BROKER,
    DECIDE,
    DRIVER,
    FOLLOW_UP_IN_30,
    GATE,
    LOAD_ID,
    PO_REPLY,
    QUESTION,
    Api,
    local,
    open_api,
    plan,
)

pytestmark = pytest.mark.unit


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[Api]:
    async with open_api(tmp_path) as api:
        yield api


def outbound_sms(at: str, text: str) -> dict[str, object]:
    return {
        "at": at,
        "direction": "out",
        "channel": "sms",
        "who": "agent",
        "role": "agent",
        "text": text,
        "status": "sent",
        "to": [DRIVER],
    }


async def test_a_ping_to_high_runs_the_agent_and_its_sms_is_in_the_conversation(api: Api) -> None:
    await api.create_load()
    api.model.push(DECIDE, plan(ASK_ETA, FOLLOW_UP_IN_30))

    result = await api.ping("high", local(7, 0))

    assert result == {
        "accepted": True,
        "duplicate": False,
        "event_id": result["event_id"],
        "reason": None,
    }
    assert result["event_id"].startswith("risk:")
    runs = await api.get(f"/api/loads/{LOAD_ID}/runs")
    assert [(r["status"], r["started_at"]) for r in runs] == [("acted", local(7, 1))]
    conversation = await api.get(f"/api/loads/{LOAD_ID}/conversation", channel="sms")
    assert conversation == [outbound_sms(local(7, 1), ASK_ETA.text)]


async def test_a_driver_sms_goes_through_the_gate_and_decide_and_gets_a_reply(api: Api) -> None:
    await api.high_risk_at_seven()
    api.model.push(GATE, QUESTION)
    api.model.push(DECIDE, plan(PO_REPLY))

    result = await api.message("What's the PO number?", local(7, 12))

    assert result["accepted"] is True
    assert result["event_id"]  # the API fills the message id
    assert [call.role for call in api.model.calls] == [DECIDE, GATE, DECIDE]
    conversation = await api.get(f"/api/loads/{LOAD_ID}/conversation", channel="sms")
    assert conversation == [
        outbound_sms(local(7, 1), ASK_ETA.text),
        {
            "at": local(7, 12),
            "direction": "in",
            "channel": "sms",
            "who": DRIVER,
            "role": "driver",
            "text": "What's the PO number?",
        },
        outbound_sms(local(7, 13), PO_REPLY.text),
    ]
    [_, reply] = await api.get(f"/api/loads/{LOAD_ID}/runs")
    assert reply["gate"] == {
        "intent": "question",
        "act": True,
        "reason": "The driver asks for the PO.",
    }


async def test_advancing_to_0731_sends_the_follow_up(api: Api) -> None:
    await api.high_risk_at_seven()
    api.model.push(GATE, QUESTION)
    api.model.push(DECIDE, plan(PO_REPLY))
    await api.message("What's the PO number?", local(7, 12))
    api.model.push(DECIDE, plan(ASK_AGAIN, FOLLOW_UP_IN_30))

    result = await api.advance(to=local(7, 31))

    assert [(r["status"], r["gate"]["intent"]) for r in result["runs"]] == [
        ("acted", "eta_follow_up_due")
    ]
    assert result["state"]["next_timer_due_at"] == local(8, 1)
    conversation = await api.get(f"/api/loads/{LOAD_ID}/conversation", channel="sms")
    assert conversation[-1] == outbound_sms(local(7, 31), ASK_AGAIN.text)
    snapshot = await api.get(f"/api/loads/{LOAD_ID}")
    [obligation] = snapshot["obligations"]
    assert (obligation["status"], obligation["attempts"]) == ("open", 2)


async def test_latency_seconds_sets_when_the_run_happens(api: Api) -> None:
    await api.create_load()
    api.model.push(DECIDE, plan(ASK_ETA, FOLLOW_UP_IN_30))

    response = await api.client.post(
        "/api/v1/pings",
        params={"latency_seconds": 0},
        json={"load_id": LOAD_ID, "lat": 37.9, "lon": -121.2, "delay_risk": "high"},
    )

    assert response.status_code == 200
    # No occurred_at: the ping happens at the simulation's current time, 06:55.
    [run] = await api.get(f"/api/loads/{LOAD_ID}/runs")
    assert run["started_at"] == "2026-10-13T06:55:00-07:00"


async def test_a_duplicate_message_id_is_reported_and_runs_once(api: Api) -> None:
    await api.create_load()
    api.model.push(GATE, GateDecision(intent=Intent.ACKNOWLEDGEMENT, act=False, reason="ok"))
    body = {
        "message_id": "m-1",
        "load_id": LOAD_ID,
        "channel": "sms",
        "sender_contact_id": DRIVER,
        "body": "ok",
        "occurred_at": local(7, 0),
    }

    first = (await api.client.post("/api/v1/events", json=body)).json()
    second = (await api.client.post("/api/v1/events", json=body)).json()

    assert (first["event_id"], first["duplicate"]) == ("m-1", False)
    assert (second["event_id"], second["duplicate"]) == ("m-1", True)
    assert [r["status"] for r in await api.get(f"/api/loads/{LOAD_ID}/runs")] == ["noop"]


async def test_a_duplicate_message_moves_no_clock_and_fires_no_timer(api: Api) -> None:
    """A redelivery changes nothing, even one stamped later than the first delivery: the
    clock stays, so the 07:31 follow-up neither fires nor runs on its account."""
    await api.high_risk_at_seven()
    api.model.push(GATE, GateDecision(intent=Intent.ACKNOWLEDGEMENT, act=False, reason="ok"))
    body = {
        "message_id": "m-1",
        "load_id": LOAD_ID,
        "channel": "sms",
        "sender_contact_id": DRIVER,
        "body": "ok",
        "occurred_at": local(7, 5),
    }
    await api.client.post("/api/v1/events", json=body)
    before = await api.get("/api/sim/state")

    again = await api.client.post("/api/v1/events", json={**body, "occurred_at": local(7, 40)})

    assert (again.json()["event_id"], again.json()["duplicate"]) == ("m-1", True)
    after = await api.get("/api/sim/state")
    assert (before["now"], after["now"]) == (local(7, 6), local(7, 6))
    assert after["next_timer_due_at"] == local(7, 31)
    runs = await api.get(f"/api/loads/{LOAD_ID}/runs")
    assert [r["event_id"] for r in runs][1:] == ["m-1"]


async def test_a_duplicate_ping_moves_no_clock(api: Api) -> None:
    await api.create_load()
    api.model.push(DECIDE, plan(ASK_ETA, FOLLOW_UP_IN_30))
    body = {
        "ping_id": "p-1",
        "load_id": LOAD_ID,
        "lat": 37.9577,
        "lon": -121.2908,
        "occurred_at": local(7, 0),
        "delay_risk": "high",
    }
    await api.client.post("/api/v1/pings", json=body)

    again = (await api.client.post("/api/v1/pings", json=body)).json()

    assert (again["duplicate"], again["reason"]) == (True, "ping already received")
    assert (await api.get("/api/sim/state"))["now"] == local(7, 1)
    assert len(await api.get(f"/api/loads/{LOAD_ID}/runs")) == 1


async def test_ingress_for_an_unknown_load_is_a_404(api: Api) -> None:
    message = {"load_id": "nope", "channel": "sms", "sender_contact_id": DRIVER, "body": "hi"}
    ping = {"load_id": "nope", "lat": 1.0, "lon": 2.0, "delay_risk": "high"}

    responses = [
        await api.client.post("/api/v1/events", json=message),
        await api.client.post("/api/v1/pings", json=ping),
    ]

    assert [r.status_code for r in responses] == [404, 404]
    assert responses[0].json() == {"detail": "unknown load 'nope'"}


async def test_the_slack_conversation_resolves_the_broker_role(api: Api) -> None:
    await api.create_load()
    api.model.push(GATE, GateDecision(intent=Intent.ACKNOWLEDGEMENT, act=False, reason="thanks"))

    await api.message("Thanks team", local(7, 0), sender=BROKER, channel="slack")

    conversation = await api.get(f"/api/loads/{LOAD_ID}/conversation", channel="slack")
    assert [(c["who"], c["role"], c["direction"]) for c in conversation] == [
        (BROKER, "broker", "in")
    ]
    assert await api.get(f"/api/loads/{LOAD_ID}/conversation", channel="sms") == []


async def test_an_unknown_sender_shows_as_unknown(api: Api) -> None:
    await api.create_load()

    await api.message("hello?", local(7, 0), sender="stranger")

    [entry] = await api.get(f"/api/loads/{LOAD_ID}/conversation", channel="sms")
    assert (entry["who"], entry["role"]) == ("stranger", "unknown")


async def test_tms_notes_are_listed_with_their_time(api: Api) -> None:
    await api.create_load()
    api.model.push(GATE, GateDecision(intent=Intent.STATUS_UPDATE, act=True, reason="update"))
    api.model.push(DECIDE, plan(WriteTmsNote(text="Driver loading at door 4.")))

    await api.message("loading at door 4", local(7, 0))

    assert await api.get(f"/api/loads/{LOAD_ID}/tms") == [
        {"at": local(7, 1), "text": "Driver loading at door 4."}
    ]
    assert api.model.remaining(ModelRole.DECIDE) == 0
