"""Attachments and documents through the API: upload, pending, tick, processed, the run.

Owner: card W3-DOC. The app runs the real composition root, so the document pipeline
reads the fixture PDFs with pypdfium2; the scripted model answers the `extract` role
(and the gate and the agent for the runs that follow).
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from freight_agent.api.routes import documents as documents_route
from freight_agent.domain.actions import UpdateStage, WriteTmsNote
from freight_agent.domain.documents import ExtractedValue, Extraction
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.load import Stage
from freight_agent.ports.llm import ModelRole
from tests.integration.test_api_harness import (
    DECIDE,
    DRIVER,
    GATE,
    LOAD_ID,
    ROOT,
    Api,
    local,
    open_api,
    plan,
)

pytestmark = pytest.mark.unit

DOCS = ROOT / "evals" / "fixtures" / "docs"
EXTRACT = ModelRole.EXTRACT
DOCUMENT = GateDecision(intent=Intent.DOCUMENT, act=True, reason="The driver sends the POD.")
DELIVERED = UpdateStage(stage=Stage.DELIVERED)
POD_NOTE = WriteTmsNote(text="POD received, signed by the receiver at 09:42")


def clean_pod(**values: str) -> Extraction:
    fields = {
        "receiver": "Valley DC",
        "po_numbers": "55821",
        "signed": "true",
        "delivered_at": "2026-10-14T09:42",
        "exceptions": "",
        **values,
    }
    return Extraction(
        type_id="pod",
        confidence=0.96,
        fields=tuple(ExtractedValue(name=k, value=v) for k, v in fields.items()),
    )


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[Api]:
    async with open_api(tmp_path) as api:
        yield api


async def upload(
    api: Api,
    name: str = "pod_clean.pdf",
    *,
    data: bytes | None = None,
    load_id: str = LOAD_ID,
    status: int = 201,
    **form: str,
) -> dict[str, Any]:
    content = (DOCS / name).read_bytes() if data is None else data
    response = await api.client.post(
        f"/api/loads/{load_id}/attachments",
        files={"file": (name, content, "application/pdf")},
        data={"sender_contact_id": DRIVER, "body": "here is the POD", **form},
    )
    assert response.status_code == status, response.text
    return response.json()


async def tick(api: Api) -> list[dict[str, Any]]:
    response = await api.client.post("/api/sim/tick")
    assert response.status_code == 200, response.text
    return response.json()["runs"]


async def test_an_uploaded_pod_is_pending_until_the_tick_reads_it_and_runs_it(api: Api) -> None:
    await api.create_load()

    uploaded = await upload(api)

    document_id = uploaded["document_id"]
    assert uploaded["attachment_id"].startswith("att_")
    document = await api.get(f"/api/documents/{document_id}")
    assert (document["status"], document["filename"], document["load_id"]) == (
        "pending",
        "pod_clean.pdf",
        LOAD_ID,
    )
    assert "blob_ref" not in document
    assert api.model.calls == []  # nothing ran yet
    (message,) = await api.get(f"/api/loads/{LOAD_ID}/conversation")
    assert (message["direction"], message["who"], message["text"]) == (
        "in",
        DRIVER,
        "here is the POD",
    )

    api.model.push(EXTRACT, clean_pod())
    api.model.push(GATE, DOCUMENT)
    api.model.push(DECIDE, plan(), plan(DELIVERED, POD_NOTE))
    runs = await tick(api)

    assert [r["event_id"] for r in runs] == [uploaded["event_id"], f"doc:{document_id}"]
    assert runs[1]["gate"]["intent"] == "document_processed"
    assert "documents" in runs[1]["sop_section_ids"]
    assert runs[1]["status"] == "acted"
    document = await api.get(f"/api/documents/{document_id}")
    assert (document["status"], document["type_id"], document["pages"]) == ("processed", "pod", 1)
    assert [c["field"] for c in document["checks"] if c["ok"]] == [
        "receiver",
        "po_numbers",
        "signed",
        "delivered_at",
    ]
    assert "PO NO.\n55821" in document["text"]
    (extract,) = [c for c in api.model.calls if c.role is EXTRACT]
    assert extract.request.images == ()  # the text layer was enough
    assert "PO NO.\n55821" in extract.request.user
    load_runs = await api.get(f"/api/loads/{LOAD_ID}/runs")
    assert f"doc:{document_id}" in [r["event_id"] for r in load_runs]
    assert (await api.get(f"/api/loads/{LOAD_ID}"))["load"]["stage"] == "delivered"
    assert await tick(api) == []  # the document is read once


async def test_a_scanned_pod_goes_to_the_model_as_a_page_image(api: Api) -> None:
    await api.create_load()
    await upload(api, "pod_scanned.pdf")
    api.model.push(EXTRACT, clean_pod())
    api.model.push(GATE, DOCUMENT)
    api.model.push(DECIDE, plan(), plan())

    await tick(api)

    (extract,) = [c for c in api.model.calls if c.role is EXTRACT]
    (image,) = extract.request.images
    assert image.media_type == "image/png" and image.data.startswith(b"\x89PNG")
    assert "The images attached are pages 1" in extract.request.user


async def test_a_pod_with_the_wrong_po_needs_review(api: Api) -> None:
    await api.create_load()
    uploaded = await upload(api, "pod_exceptions.pdf")
    api.model.push(EXTRACT, clean_pod(po_numbers="55812", exceptions="2 cases damaged, 1 short"))
    api.model.push(GATE, DOCUMENT)
    api.model.push(DECIDE, plan(), plan())

    await tick(api)

    document = await api.get(f"/api/documents/{uploaded['document_id']}")
    assert document["status"] == "needs_review"
    po = next(c for c in document["checks"] if c["field"] == "po_numbers")
    assert (po["expected"], po["found"], po["ok"], po["check"]) == (
        "55821",
        "55812",
        False,
        "po_number",
    )


async def test_a_loads_documents_are_listed_oldest_first(api: Api) -> None:
    await api.create_load()
    first = await upload(api, "bol.pdf", occurred_at=local(10, 5))
    second = await upload(api, "pod_clean.pdf", occurred_at=local(10, 6))

    listed = await api.get(f"/api/loads/{LOAD_ID}/documents")

    assert [d["document_id"] for d in listed] == [first["document_id"], second["document_id"]]
    assert [d["received_at"] for d in listed] == [local(10, 5), local(10, 6)]
    assert {d["status"] for d in listed} == {"pending"}


async def test_the_sender_file_name_loses_its_folders(api: Api) -> None:
    await api.create_load()
    response = await api.client.post(
        f"/api/loads/{LOAD_ID}/attachments",
        files={"file": ("C:\\Users\\drv\\..\\pod.pdf", b"%PDF-1.7", "application/pdf")},
    )
    assert response.status_code == 201, response.text

    document = await api.get(f"/api/documents/{response.json()['document_id']}")

    assert (document["filename"], document["content_type"]) == ("pod.pdf", "application/pdf")


async def test_an_upload_for_an_unknown_load_is_a_404(api: Api) -> None:
    result = await upload(api, load_id="999999", status=404)

    assert result["detail"] == "unknown load '999999'"


async def test_an_empty_file_is_refused(api: Api) -> None:
    await api.create_load()

    result = await upload(api, data=b"", status=400)

    assert result["detail"] == "the file is empty"


async def test_a_file_over_the_limit_is_refused(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    await api.create_load()
    monkeypatch.setattr(documents_route, "MAX_UPLOAD_BYTES", 10)

    result = await upload(api, data=b"x" * 11, status=413)

    assert "larger than" in result["detail"]
    assert await api.get(f"/api/loads/{LOAD_ID}/documents") == []


async def test_an_upload_with_a_message_id_already_logged_is_a_conflict(api: Api) -> None:
    await api.create_load()
    await upload(api, message_id="m-1")

    result = await upload(api, message_id="m-1", status=409)

    assert result["detail"] == "message 'm-1' was already received"
    assert len(await api.get(f"/api/loads/{LOAD_ID}/documents")) == 1


async def test_a_channel_that_is_not_a_contact_channel_is_refused(api: Api) -> None:
    await api.create_load()

    response = await api.client.post(
        f"/api/loads/{LOAD_ID}/attachments",
        files={"file": ("pod.pdf", b"%PDF-1.7", "application/pdf")},
        data={"channel": "system"},
    )

    assert response.status_code == 422


async def test_an_unknown_document_or_load_is_a_404(api: Api) -> None:
    missing = await api.client.get("/api/documents/doc_404")
    no_load = await api.client.get("/api/loads/999999/documents")

    assert (missing.status_code, missing.json()["detail"]) == (404, "unknown document 'doc_404'")
    assert (no_load.status_code, no_load.json()["detail"]) == (404, "unknown load '999999'")
