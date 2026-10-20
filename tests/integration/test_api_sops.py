"""The SOP viewer: effective sections per client and load type, the resolver preview and
search."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.integration.test_api_harness import Api, open_api

pytestmark = pytest.mark.unit


@pytest.fixture
async def api() -> AsyncIterator[Api]:
    async with open_api() as api:
        yield api


def by_id(sections: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(s["section_id"]): s for s in sections}


async def test_a_client_sees_the_standard_sections_with_its_overrides(api: Api) -> None:
    sop = await api.get("/api/sops", client_id="northline")

    assert len(sop["version"]) == 12
    sections = by_id(sop["sections"])
    assert sections["delay_risk_high"]["sources"] == ["clients/northline/delay_risk_high.md"]
    assert sections["communication_rules"]["sources"] == ["standard/communication_rules.md"]
    assert list(sections) == sorted(sections)


async def test_a_load_type_extends_the_client_sections(api: Api) -> None:
    plain = by_id((await api.get("/api/sops", client_id="keystone"))["sections"])
    reefer = by_id(
        (await api.get("/api/sops", client_id="keystone", load_types="reefer"))["sections"]
    )

    assert plain["driver_departed"]["sources"] == ["standard/driver_departed.md"]
    assert reefer["driver_departed"]["sources"] == [
        "standard/driver_departed.md",
        "clients/keystone/types/reefer/driver_departed.md",
    ]


async def test_the_preview_resolves_the_sections_for_an_intent_and_stage(api: Api) -> None:
    resolved = await api.get(
        "/api/sops/preview", client_id="northline", intent="delay_risk_high", stage="to_pickup"
    )

    ids = [s["section_id"] for s in resolved["sections"]]
    assert {"delay_risk_high", "communication_rules", "escalation"} <= set(ids)
    assert "eta_received" not in ids
    catalog = [entry["section_id"] for entry in resolved["catalog"]]
    assert "eta_received" in catalog


async def test_the_preview_rejects_an_unknown_intent(api: Api) -> None:
    response = await api.client.get(
        "/api/sops/preview", params={"client_id": "northline", "intent": "dance", "stage": "x"}
    )

    assert response.status_code == 422
    assert "detail" in response.json()


async def test_a_broken_sop_file_is_reported_by_name(tmp_path: Path) -> None:
    sops = tmp_path / "sops"
    (sops / "standard").mkdir(parents=True)
    (sops / "standard" / "broken.md").write_text("no header here\n", encoding="utf-8")

    async with open_api(sops_dir=sops) as api:
        response = await api.client.get("/api/sops", params={"client_id": "northline"})

    assert response.status_code == 422
    assert "standard/broken.md" in response.json()["detail"]


async def test_search_ranks_the_reefer_section_first(api: Api) -> None:
    hits = await api.get(
        "/api/sops/search", q="trailer temperature seal", broker_id="keystone", load_types="reefer"
    )
    alias = await api.get(
        "/api/sops/search", q="trailer temperature seal", client_id="keystone", load_types="reefer"
    )

    assert hits[0]["section_id"] == "driver_departed"
    assert set(hits[0]) == {"section_id", "title", "description", "score"}
    assert alias == hits
    assert len(await api.get("/api/sops/search", q="driver", broker_id="northline", k=2)) == 2


async def test_search_needs_a_broker_and_a_sane_k(api: Api) -> None:
    missing = await api.client.get("/api/sops/search", params={"q": "eta"})
    too_many = await api.client.get(
        "/api/sops/search", params={"q": "eta", "broker_id": "acme", "k": 50}
    )

    assert missing.status_code == 400
    assert too_many.status_code == 422


async def test_the_preview_takes_an_intent_of_the_broker_s_catalog(api: Api) -> None:
    resolved = await api.get(
        "/api/sops/preview", client_id="northline", intent="document_processed", stage="at_delivery"
    )

    assert [s["section_id"] for s in resolved["sections"]] == [
        "communication_rules",
        "documents",
        "escalation",
    ]
