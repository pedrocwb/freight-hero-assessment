"""`GET /api/loads/{load_id}/facility-memory`: the notes of each stop of a load."""

from collections.abc import AsyncIterator

import pytest

from freight_agent.domain.memory import FacilityFact, FactKind
from tests.integration.test_api_harness import LOAD_ID, Api, local, open_api

pytestmark = pytest.mark.unit


@pytest.fixture
async def api() -> AsyncIterator[Api]:
    async with open_api() as api:
        yield api


async def test_the_facility_memory_lists_each_stop_with_its_clients_notes(api: Api) -> None:
    await api.create_load()
    runtime = api.app.state.service.session.simulation.runtime
    memory = runtime.facility_memory
    assert memory is not None
    told = local(7, 0)
    note = FacilityFact(
        fact_id="f1",
        client_id="northline",
        facility_key="riverbend-plant~stockton-ca",
        facility_name="Riverbend plant",
        text="Check in at gate 4",
        kind=FactKind.CHECK_IN,
        source_load_id="470113",
        source_event_id="m1",
        source_at=told,
        created_at=told,
        updated_at=told,
    )
    await memory.apply("northline", upserts=[note])
    await memory.apply("acme", upserts=[note.model_copy(update={"client_id": "acme"})])

    stops = await api.get(f"/api/loads/{LOAD_ID}/facility-memory")

    assert [(s["sequence"], s["name"], len(s["facts"])) for s in stops] == [
        (1, "Riverbend plant", 1),
        (2, "Valley DC", 0),
    ]
    assert stops[0]["facts"][0]["text"] == "Check in at gate 4"
    assert stops[0]["facility_key"] == "riverbend-plant~stockton-ca"


async def test_an_unknown_load_has_no_facility_memory(api: Api) -> None:
    response = await api.client.get("/api/loads/nope/facility-memory")

    assert response.status_code == 404
