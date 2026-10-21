"""Attachments and documents (card W3-DOC).

- `POST /api/loads/{load_id}/attachments` (multipart): the file goes to the blob store
  and arrives as a message with that attachment (`SimulationService.upload_attachment`,
  through `ingest_message`); its document is pending until the next tick
  (`POST /api/sim/tick`), which reads it before it runs the queue.
- `GET /api/loads/{load_id}/documents`: the load's documents, oldest first.

Bodies: `api.schemas.AttachmentUploadResult` and `DocumentView` (the blob reference
stays internal).
"""

from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Literal

from fastapi import APIRouter, File, Form, UploadFile

from freight_agent.api.deps import Service
from freight_agent.api.errors import ApiError, BadRequest, NotFound
from freight_agent.api.schemas import AttachmentUploadResult, DocumentView
from freight_agent.api.simulation import AttachmentUpload
from freight_agent.domain.load import Channel

router = APIRouter(tags=["documents"])

# A phone photo or a scanned packet; larger files are refused before they are stored.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_FILENAME_LIMIT = 200


class PayloadTooLarge(ApiError):
    status_code = 413


@router.post("/loads/{load_id}/attachments", status_code=201)
async def upload_attachment(
    service: Service,
    load_id: str,
    file: Annotated[UploadFile, File(description="The document: a PDF, a photo, a text file")],
    sender_contact_id: Annotated[
        str | None, Form(description="The load contact who sends it (a driver id)")
    ] = None,
    body: Annotated[str, Form(description="The text of the message that carries it")] = "",
    channel: Annotated[
        Literal["sms", "email", "slack"], Form(description="The channel it arrives on")
    ] = "sms",
    message_id: Annotated[str | None, Form(description="Default: a new uuid")] = None,
    occurred_at: Annotated[datetime | None, Form(description="Default: simulation now")] = None,
) -> AttachmentUploadResult:
    """Store the file, log it as a message with the attachment, and leave its document
    pending: the next tick reads it, then runs the message and the document's event."""
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise PayloadTooLarge(f"the file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    if not data:
        raise BadRequest("the file is empty")
    upload = AttachmentUpload(
        load_id=load_id,
        data=data,
        filename=_filename(file.filename),
        content_type=(file.content_type or "application/octet-stream").strip(),
        channel=Channel(channel),
        sender_contact_id=sender_contact_id or None,
        body=body,
        message_id=message_id or None,
        occurred_at=occurred_at,
    )
    return await service.upload_attachment(upload)


@router.get("/loads/{load_id}/documents")
async def load_documents(service: Service, load_id: str) -> list[DocumentView]:
    async with service.uow_factory() as uow:
        if await uow.loads.get(load_id) is None:
            raise NotFound(f"unknown load {load_id!r}")
        documents = await uow.documents.list_for_load(load_id)
    return [DocumentView.of(d) for d in documents]


def _filename(name: str | None) -> str:
    """The sender's file name without any folder, as a person would read it."""
    base = PureWindowsPath(PurePosixPath(name or "").name).name.strip()
    return base[:_FILENAME_LIMIT] or "attachment"
