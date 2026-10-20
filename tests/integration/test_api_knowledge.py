"""A broker's SOP onboarding through the API: upload the Acme PDF, review the draft,
publish, list the versions; and search.

The compile model is the scripted one of the test app (`app.state.knowledge_llm`); the
PDF goes through the real page reader. The last tests wire the session's index to its
store (`StoreSopIndex`), the composition asked of card W3-OPS, so a publish reaches the
viewer, search and the runs.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from freight_agent import bootstrap
from freight_agent.adapters.knowledge.fake_embedder import FakeEmbedder
from freight_agent.adapters.knowledge.memory import StoreSopIndex
from freight_agent.adapters.sop.files import FileSopRepository
from freight_agent.ports.llm import LlmError, ModelRole
from freight_agent.settings import Settings
from tests.integration.test_api_harness import (
    DECIDE,
    FOLLOW_UP_IN_30,
    LOAD_ID,
    ROOT,
    Api,
    local,
    open_api,
    plan,
)
from tests.unit.application.test_sop_ingestion import acme_compiled

pytestmark = pytest.mark.unit

ACME_PDF = ROOT / "evals" / "fixtures" / "sops" / "acme_sop.pdf"
COMPILE = ModelRole.COMPILE


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[Api]:
    async with open_api(tmp_path) as api:
        api.app.state.knowledge_llm = api.model
        yield api


async def upload(api: Api, *, name: str = "acme_sop.pdf", data: bytes | None = None) -> Any:
    content = ACME_PDF.read_bytes() if data is None else data
    response = await api.client.post(
        "/api/brokers/acme/sops", files={"file": (name, content, "application/pdf")}
    )
    assert response.status_code == 201, response.text
    return response.json()


def reviewed(draft: dict[str, Any]) -> dict[str, Any]:
    """The draft as the editor sends it back: every proposal accepted, sections reviewed."""
    return {
        "sections": [{**s, "proposed": False} for s in draft["sections"]],
        "proposed_intents": [{**i, "accepted": True} for i in draft["proposed_intents"]],
        "proposed_requirements": draft["proposed_requirements"],
        "proposed_obligations": draft["proposed_obligations"],
    }


async def test_upload_review_and_publish_the_acme_sop(api: Api) -> None:
    api.model.push(COMPILE, acme_compiled())

    uploaded = await upload(api)

    assert (uploaded["pages"], uploaded["status"]) == (3, "compiled")
    (call,) = api.model.calls
    assert "Late truck: asking the driver for an ETA" in call.request.user  # read from the PDF
    draft = await api.get("/api/brokers/acme/sops/draft")
    assert [s["section_id"] for s in draft["sections"]][:2] == ["delay_risk_high", "detention"]
    assert draft["proposed_intents"][0]["intent_id"] == "detention_request"
    assert draft["proposed_requirements"][0]["opens_obligation"] == "pod"
    assert draft["document_ids"] == [uploaded["document_id"]]
    assert any("detention_request is proposed and not accepted" in p for p in draft["lint"])

    refused = await api.client.post("/api/brokers/acme/sops/publish", json={"percent": 100})
    assert refused.status_code == 422
    assert "detention_request is proposed and not accepted" in refused.json()["detail"]

    edited = await api.client.put("/api/brokers/acme/sops/draft", json=reviewed(draft))
    assert edited.status_code == 200, edited.text
    assert edited.json()["lint"] == []
    assert edited.json()["document_ids"] == [uploaded["document_id"]]

    published = await api.client.post("/api/brokers/acme/sops/publish", json={"percent": 100})
    assert published.status_code == 200, published.text
    first = published.json()
    assert (first["config_version"], first["lint"]) == (1, [])
    assert len(first["version"]) == 12

    again = reviewed(draft)
    again["sections"][1]["body"] += "\n6. One more step."
    assert (await api.client.put("/api/brokers/acme/sops/draft", json=again)).status_code == 200
    second = (await api.client.post("/api/brokers/acme/sops/publish", json={"percent": 25})).json()
    assert second["config_version"] == 2
    versions = await api.get("/api/brokers/acme/sops/versions")
    assert [v["version"] for v in versions] == [first["version"], second["version"]]
    assert versions[0]["section_ids"][:2] == ["delay_risk_high", "detention"]
    assert versions[0]["published_by"] == "api"
    assert await api.get("/api/brokers/standard/sops/versions") == []


async def test_a_broker_without_a_draft_gets_404(api: Api) -> None:
    draft = await api.client.get("/api/brokers/acme/sops/draft")
    publish = await api.client.post("/api/brokers/acme/sops/publish", json={"percent": 100})

    assert draft.status_code == publish.status_code == 404
    assert "upload a document first" in draft.json()["detail"]


async def test_the_standard_layer_is_not_uploaded(api: Api) -> None:
    response = await api.client.post(
        "/api/brokers/standard/sops", files={"file": ("x.md", b"# X", "text/markdown")}
    )

    assert response.status_code == 400
    assert "agent sop import" in response.json()["detail"]


async def test_a_file_that_is_not_a_pdf_is_stored_as_failed(api: Api) -> None:
    result = await upload(api, name="notes.docx", data=b"PK\x03\x04")

    assert (result["pages"], result["status"]) == (0, "failed")
    assert api.model.calls == []


@pytest.mark.parametrize("error", [LlmError("compile timed out"), RuntimeError("no cassette")])
async def test_a_compile_that_fails_keeps_the_upload_stored(api: Api, error: Exception) -> None:
    api.model.push(COMPILE, error)

    result = await upload(api)

    assert result["status"] == "stored"
    assert (await api.client.get("/api/brokers/acme/sops/draft")).status_code == 404


async def test_a_markdown_upload_is_read_as_text(api: Api) -> None:
    api.model.push(COMPILE, acme_compiled())
    text = (ROOT / "evals" / "fixtures" / "sops" / "acme_sop.md").read_bytes()

    response = await api.client.post(
        "/api/brokers/acme/sops",
        files={"file": ("acme_sop.md", text, "application/octet-stream")},
    )

    assert response.json()["pages"] == 1
    assert "## 2. Detention: truck held at a facility" in api.model.calls[0].request.user


async def test_a_file_too_large_is_refused(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    from freight_agent.api.routes import knowledge

    monkeypatch.setattr(knowledge, "MAX_UPLOAD_BYTES", 10)
    response = await api.client.post(
        "/api/brokers/acme/sops", files={"file": ("big.pdf", b"x" * 11, "application/pdf")}
    )

    assert response.status_code == 413


# --- search and the viewer ---------------------------------------------------------------


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


# --- the session's index over its store (asked of card W3-OPS) ---------------------------


@pytest.fixture
async def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Api]:
    """The session reads the SOPs through its own store, with the files for what was
    never published: `bootstrap.build_knowledge` gives the memory session a
    `StoreSopIndex` over the session's store."""
    async with open_api(tmp_path) as api:
        assert isinstance(api.app.state.service.knowledge, StoreSopIndex)
        api.app.state.knowledge_llm = api.model
        yield api


async def publish_acme(api: Api) -> str:
    api.model.push(COMPILE, acme_compiled())
    await upload(api)
    draft = await api.get("/api/brokers/acme/sops/draft")
    await api.client.put("/api/brokers/acme/sops/draft", json=reviewed(draft))
    published = await api.client.post("/api/brokers/acme/sops/publish", json={"percent": 100})
    assert published.status_code == 200, published.text
    return published.json()["version"]


async def test_a_publish_reaches_the_viewer_and_search(wired: Api) -> None:
    version = await publish_acme(wired)

    sop = await wired.get("/api/sops", client_id="acme")
    sections = {s["section_id"]: s for s in sop["sections"]}
    assert sections["detention"]["sources"] == ["acme_sop.pdf pp. 1, 2"]
    assert sections["escalation"]["sources"] == ["standard/escalation.md"]
    assert sop["version"] == f"{FileSopRepository(ROOT / 'sops').version()}+{version}"
    hits = await wired.get("/api/sops/search", q="detention facility hours", broker_id="acme")
    assert hits[0]["section_id"] == "detention"
    northline = await wired.get("/api/sops/search", q="detention facility", broker_id="northline")
    assert "detention" not in {h["section_id"] for h in northline}
    preview = await wired.get(
        "/api/sops/preview", client_id="acme", intent="detention_request", stage="at_delivery"
    )
    assert "detention" in [s["section_id"] for s in preview["sections"]]


async def test_a_run_on_an_acme_load_follows_the_published_acme_procedure(wired: Api) -> None:
    version = await publish_acme(wired)
    metadata = await wired.fixture()
    metadata["broker"] = {"id": "acme", "name": "Acme Freight"}
    assert (await wired.client.post("/api/loads", json=metadata)).status_code == 201
    wired.model.push(DECIDE, plan(FOLLOW_UP_IN_30))

    await wired.ping("high", local(7, 0))

    (run,) = await wired.get(f"/api/loads/{LOAD_ID}/runs")
    assert "delay_risk_high" in run["sop_section_ids"]
    assert run["sop_version"].endswith(f"+{version}")
    (decide,) = [c for c in wired.model.calls if c.role is DECIDE]
    assert "every 20 minutes, up to three times" in decide.request.user


async def test_a_publish_embeds_with_the_composition_root_s_embedder(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    modes: list[str] = []

    def build_embedder(settings: Settings, *, mode: str | None = None) -> FakeEmbedder:
        modes.append(mode or settings.llm_mode)
        return FakeEmbedder(dim=8)

    monkeypatch.setattr(bootstrap, "build_embedder", build_embedder, raising=False)
    api.model.push(COMPILE, acme_compiled())
    await upload(api)
    draft = await api.get("/api/brokers/acme/sops/draft")
    await api.client.put("/api/brokers/acme/sops/draft", json=reviewed(draft))

    published = await api.client.post("/api/brokers/acme/sops/publish", json={"percent": 100})

    assert published.status_code == 200, published.text
    assert modes and set(modes) == {"scripted"}
