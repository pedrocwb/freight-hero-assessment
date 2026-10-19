"""The SOP viewer: effective sections per client and load type, and the resolver preview."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.integration.test_api_harness import Api, open_api

pytestmark = pytest.mark.unit


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[Api]:
    async with open_api(tmp_path) as api:
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

    async with open_api(tmp_path, sops_dir=sops) as api:
        response = await api.client.get("/api/sops", params={"client_id": "northline"})

    assert response.status_code == 422
    assert "standard/broken.md" in response.json()["detail"]
