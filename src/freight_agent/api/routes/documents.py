"""Attachments and documents: `POST /api/loads/{load_id}/attachments` (multipart: blob,
then `ingest_message` with the attachment), `GET /api/documents/{document_id}` and
`GET /api/loads/{load_id}/documents`.

Owner: card W3-DOC (empty router from C0). Bodies: `api.schemas.AttachmentUploadResult`,
`DocumentView`. The simulation exposes `SimulationService.blobs` and `.uow_factory`.
"""

from fastapi import APIRouter

router = APIRouter(tags=["documents"])
