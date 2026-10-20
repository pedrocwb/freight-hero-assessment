"""Brokers as data over the API: create, read, change, list versions, roll out.

Owner: card W3-BRK. Each test gets its own app on an in-memory simulation
(`test_api_harness`); the brokers live in the session's store, the one its runs read.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from freight_agent.domain.broker import standard_broker
from tests.integration.test_api_harness import (
    ASK_AGAIN,
    DECIDE,
    FOLLOW_UP_IN_30,
    LOAD_ID,
    Api,
    open_api,
    plan,
)

pytestmark = pytest.mark.unit

DETENTION = {
    "intent_id": "detention_request",
    "title": "Detention request",
    "description": "The truck waited over 2 hours at a stop, or the driver asks for detention.",
    "examples": ["We've been here 3 hours, can you start detention?"],
    "priority": 55,
}
POD = {
    "kind": "pod",
    "title": "Proof of delivery",
    "description": "The signed POD, due after the truck arrives at the receiver.",
    "max_attempts": 1,
    "on_exhausted": ["hero_task"],
}


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[Api]:
    async with open_api(tmp_path) as api:
        yield api


async def create(api: Api, broker_id: str = "acme", **fields: Any) -> dict[str, Any]:
    body = {"broker_id": broker_id, "name": fields.pop("name", broker_id.title()), **fields}
    response = await api.client.post("/api/brokers", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def put(api: Api, path_id: str = "acme", /, **fields: Any) -> dict[str, Any]:
    """PUT `fields` (which may name `broker_id` too) to the broker at `path_id`."""
    response = await api.client.put(f"/api/brokers/{path_id}", json=fields)
    assert response.status_code == 200, response.text
    return response.json()


async def roll_out(api: Api, broker_id: str = "acme", **body: Any) -> dict[str, Any]:
    response = await api.client.post(f"/api/brokers/{broker_id}/rollout", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def intent_ids(broker: dict[str, Any]) -> list[str]:
    return [i["intent_id"] for i in broker["intents"]]


# --- read -----------------------------------------------------------------------------------


async def test_the_standard_catalog_is_always_there(api: Api) -> None:
    standard = await api.get("/api/brokers/standard")
    brokers = await api.get("/api/brokers")
    versions = await api.get("/api/brokers/standard/versions")

    assert standard == standard_broker().model_dump(mode="json")
    assert [b["broker_id"] for b in brokers] == ["standard"]
    assert [(v["config_version"], v["current"]) for v in versions] == [(1, True)]
    assert versions[0]["intents"] == intent_ids(standard)
    assert versions[0]["obligations"] == ["eta_request"]


async def test_an_unknown_broker_is_not_found(api: Api) -> None:
    for path in ("/api/brokers/nope", "/api/brokers/nope/versions"):
        response = await api.client.get(path)

        assert response.status_code == 404
        assert "nope" in response.json()["detail"]


# --- create ---------------------------------------------------------------------------------


async def test_a_new_broker_is_a_copy_of_the_standard_catalog(api: Api) -> None:
    created = await create(api, "acme", name="Acme Freight")

    expected = {**standard_broker().model_dump(mode="json"), "broker_id": "acme"}
    assert created == {**expected, "name": "Acme Freight"}
    assert await api.get("/api/brokers/acme") == created
    assert [b["broker_id"] for b in await api.get("/api/brokers")] == ["standard", "acme"]
    [version] = await api.get("/api/brokers/acme/versions")
    assert (version["config_version"], version["current"], version["name"]) == (
        1,
        True,
        "Acme Freight",
    )


async def test_a_new_broker_takes_the_fields_it_is_given(api: Api) -> None:
    limits = {**standard_broker().limits.model_dump(mode="json"), "max_steps": 0}

    created = await create(api, "acme", triage=False, limits=limits)

    assert created["triage"] is False
    assert created["limits"]["max_steps"] == 0
    assert created["config_version"] == 1


async def test_brokers_are_listed_by_id_after_the_standard_catalog(api: Api) -> None:
    for broker_id in ("zeta", "acme", "beta"):
        await create(api, broker_id)

    brokers = await api.get("/api/brokers")

    assert [b["broker_id"] for b in brokers] == ["standard", "acme", "beta", "zeta"]


async def test_a_broker_id_is_created_once_and_standard_is_reserved(api: Api) -> None:
    await create(api, "acme")

    again = await api.client.post("/api/brokers", json={"broker_id": "acme", "name": "Acme 2"})
    standard = await api.client.post("/api/brokers", json={"broker_id": "standard", "name": "Mine"})

    assert again.status_code == 409 and "acme" in again.json()["detail"]
    assert standard.status_code == 409 and "standard" in standard.json()["detail"]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"name": "Acme"}, "broker_id"),
        ({"broker_id": "acme"}, "name"),
        ({"broker_id": "Acme Corp", "name": "Acme"}, "broker id 'Acme Corp'"),
        ({"broker_id": "acme", "name": "Acme", "obligations": []}, "eta_request is missing"),
    ],
)
async def test_a_broker_that_cannot_work_is_refused_with_the_reason(
    api: Api, body: dict[str, Any], expected: str
) -> None:
    response = await api.client.post("/api/brokers", json=body)

    assert response.status_code == 422
    assert expected in response.json()["detail"]
    assert [b["broker_id"] for b in await api.get("/api/brokers")] == ["standard"]


async def test_a_body_of_the_wrong_shape_is_refused(api: Api) -> None:
    response = await api.client.post(
        "/api/brokers", json={"broker_id": "acme", "name": "Acme", "colour": "red"}
    )

    assert response.status_code == 422


# --- change ---------------------------------------------------------------------------------


async def test_a_change_is_a_new_version_and_the_previous_one_is_kept(api: Api) -> None:
    created = await create(api, "acme", name="Acme")

    changed = await put(api, "acme", name="Acme Freight", intents=[*created["intents"], DETENTION])

    assert changed["config_version"] == 2
    assert changed["rollout"] == {"percent": 100, "previous_version": 1}
    assert "detention_request" in intent_ids(changed)
    assert await api.get("/api/brokers/acme") == changed
    versions = await api.get("/api/brokers/acme/versions")
    assert [(v["config_version"], v["current"], v["name"]) for v in versions] == [
        (1, False, "Acme"),
        (2, True, "Acme Freight"),
    ]
    assert "detention_request" not in versions[0]["intents"]
    assert "detention_request" in versions[1]["intents"]


async def test_a_change_keeps_what_it_does_not_name(api: Api) -> None:
    created = await create(api, "acme", triage=False)

    changed = await put(api, "acme", broker_id="acme", name="Acme Freight")

    assert changed["triage"] is False
    assert changed["intents"] == created["intents"]
    assert changed["sop_version"] == created["sop_version"]


async def test_the_same_change_twice_makes_one_version(api: Api) -> None:
    await create(api, "acme")

    first = await put(api, "acme", name="Acme Freight")
    second = await put(api, "acme", name="Acme Freight")

    assert first == second
    assert len(await api.get("/api/brokers/acme/versions")) == 2


async def test_a_new_obligation_gets_its_follow_up_intent(api: Api) -> None:
    created = await create(api, "acme")
    requirement = {
        "type_id": "pod",
        "at_stage": "at_delivery",
        "within_minutes": 120,
        "opens_obligation": "pod",
    }

    changed = await put(
        api,
        "acme",
        obligations=[*created["obligations"], POD],
        documents=[requirement],
    )

    follow_up = next(i for i in changed["intents"] if i["intent_id"] == "pod_follow_up_due")
    assert (follow_up["system"], follow_up["must_act"]) == (True, True)
    assert changed["documents"] == [requirement]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"documents": [{"type_id": "invoice", "at_stage": "delivered"}]}, "invoice"),
        ({"limits": {"max_steps": 99}}, "limits.max_steps"),
        ({"intents": []}, "delay_risk_high is missing"),
    ],
)
async def test_a_change_that_breaks_the_catalog_is_refused_and_nothing_is_saved(
    api: Api, change: dict[str, Any], expected: str
) -> None:
    await create(api, "acme")

    response = await api.client.put("/api/brokers/acme", json=change)

    assert response.status_code == 422
    assert expected in response.json()["detail"]
    assert len(await api.get("/api/brokers/acme/versions")) == 1


async def test_a_change_needs_a_saved_broker_and_its_own_id(api: Api) -> None:
    await create(api, "acme")

    unknown = await api.client.put("/api/brokers/nope", json={"name": "Nope"})
    standard = await api.client.put("/api/brokers/standard", json={"name": "Mine"})
    other_id = await api.client.put("/api/brokers/acme", json={"broker_id": "beta"})

    assert unknown.status_code == 404
    assert standard.status_code == 409 and "standard" in standard.json()["detail"]
    assert other_id.status_code == 400 and "beta" in other_id.json()["detail"]


# --- roll out -------------------------------------------------------------------------------


async def test_a_canary_and_a_rollback_keep_the_version_number(api: Api) -> None:
    await create(api, "acme")
    await put(api, "acme", name="Acme Freight")

    canary = await roll_out(api, "acme", percent=10)
    rolled_back = await roll_out(api, "acme", percent=0)

    assert (canary["config_version"], canary["rollout"]) == (
        2,
        {"percent": 10, "previous_version": 1},
    )
    assert rolled_back["rollout"] == {"percent": 0, "previous_version": 1}
    assert await api.get("/api/brokers/acme") == rolled_back
    versions = await api.get("/api/brokers/acme/versions")
    assert [(v["config_version"], v["current"]) for v in versions] == [(1, False), (2, True)]
    assert versions[1]["rollout"] == rolled_back["rollout"]


async def test_a_rollout_may_fall_back_to_any_earlier_version(api: Api) -> None:
    await create(api, "acme")
    await put(api, "acme", name="Acme 2")
    await put(api, "acme", name="Acme 3")

    rolled_out = await roll_out(api, "acme", percent=50, previous_version=1)

    assert rolled_out["rollout"] == {"percent": 50, "previous_version": 1}


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"percent": 50, "previous_version": 7}, "no version 7"),
        ({"percent": 50, "previous_version": 2}, "earlier"),
    ],
)
async def test_a_rollout_to_a_version_that_cannot_serve_is_a_conflict(
    api: Api, body: dict[str, Any], expected: str
) -> None:
    await create(api, "acme")
    await put(api, "acme", name="Acme Freight")

    response = await api.client.post("/api/brokers/acme/rollout", json=body)

    assert response.status_code == 409
    assert expected in response.json()["detail"]
    assert (await api.get("/api/brokers/acme"))["rollout"]["percent"] == 100


async def test_a_first_version_has_nothing_to_roll_back_to(api: Api) -> None:
    await create(api, "acme")

    partial = await api.client.post("/api/brokers/acme/rollout", json={"percent": 50})
    full = await roll_out(api, "acme", percent=100)

    assert partial.status_code == 409
    assert "no earlier version" in partial.json()["detail"]
    assert full["rollout"] == {"percent": 100, "previous_version": None}


async def test_a_rollout_needs_a_saved_broker_and_a_percent(api: Api) -> None:
    await create(api, "acme")

    unknown = await api.client.post("/api/brokers/nope/rollout", json={"percent": 0})
    standard = await api.client.post("/api/brokers/standard/rollout", json={"percent": 0})
    too_much = await api.client.post("/api/brokers/acme/rollout", json={"percent": 150})

    assert unknown.status_code == 404
    assert standard.status_code == 404
    assert too_much.status_code == 422


# --- the runs -------------------------------------------------------------------------------


async def test_a_load_runs_on_its_broker_s_version_and_a_rollback_moves_it_back(
    api: Api,
) -> None:
    """The fixture load's broker is northline: version 2 first, then version 1."""
    await create(api, "northline", name="Northline Logistics")
    await put(api, "northline", name="Northline Logistics LLC")
    await api.high_risk_at_seven()  # the 07:01 run sets a timer for 07:31
    await roll_out(api, "northline", percent=0)
    api.model.push(DECIDE, plan(ASK_AGAIN, FOLLOW_UP_IN_30))

    await api.advance(minutes=31)

    runs = await api.get(f"/api/loads/{LOAD_ID}/runs")
    assert [(r["broker_id"], r["broker_config_version"]) for r in runs] == [
        ("northline", 2),
        ("northline", 1),
    ]


async def test_a_load_whose_broker_was_never_saved_runs_on_the_standard_catalog(
    api: Api,
) -> None:
    await api.high_risk_at_seven()

    [run] = await api.get(f"/api/loads/{LOAD_ID}/runs")

    assert (run["broker_id"], run["broker_config_version"]) == ("standard", 1)
