"""Postgres `SopStore` (tables `sop_documents`, `sop_drafts`, `sop_versions` and
`sop_sections`, migration 0004).

Owner: card W3-KNW. The standard layer (`broker_id` None in the domain) is stored under
the owner 'standard' (`STANDARD_BROKER_ID`), because the owner is part of the keys.

A version is one `sop_versions` row and one `sop_sections` row per section, in order
(`position`). The section rows carry what the index searches: the text (a generated
`tsvector`) and the embedding, with the model that made it. A version's sections are
written once; publishing it again updates its publication (time, author, lint) and,
when new ones are given, its embeddings. The published version has the latest
`published_at`; `seq` (the first publication's order) breaks ties and orders `versions`.
"""

import builtins
from collections.abc import Sequence
from typing import Any, Final, LiteralString

from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from freight_agent.adapters.persistence.postgres import rows
from freight_agent.adapters.persistence.postgres.connection import Session
from freight_agent.domain.broker import STANDARD_BROKER_ID
from freight_agent.domain.load import Stage
from freight_agent.domain.sop import (
    MergeMode,
    SopDocument,
    SopDraft,
    SopLayer,
    SopSection,
    SopVersion,
    layer_owner,
)
from freight_agent.ports.knowledge import SectionEmbeddings

# The width of `sop_sections.embedding` (`public.vector(1536)`).
EMBEDDING_DIM: Final = 1536

_DOCUMENT: Final = (
    "document_id, broker_id, filename, content_type, blob_ref, pages, sha256, uploaded_at"
)
_VERSION: Final = "broker_id, version, published_at, published_by, lint"
SECTION_COLUMNS: Final = (
    "broker_id, version, position, section_id, layer, load_type, title, description,"
    " intents, stages, mode, body, source"
)


def owner_key(broker_id: str | None) -> str:
    """The owner column of a layer: the broker id, or 'standard'."""
    return layer_owner(broker_id) or STANDARD_BROKER_ID


def vector_literal(vector: Sequence[float]) -> str:
    """A vector as pgvector's text input, `[0.1,0.2,...]`, cast with `::public.vector`."""
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


def section_from(row: DictRow) -> SopSection:
    """A section row back as the section it was published from."""
    layer = SopLayer(row["layer"])
    return SopSection(
        section_id=row["section_id"],
        layer=layer,
        client_id=None if layer is SopLayer.STANDARD else layer_owner(row["broker_id"]),
        load_type=row["load_type"],
        title=row["title"],
        description=row["description"],
        intents=tuple(row["intents"]),
        stages=tuple(Stage(s) for s in row["stages"]),
        mode=MergeMode(row["mode"]),
        body=row["body"],
        source=row["source"],
    )


class PostgresSopStore:
    def __init__(self, session: Session) -> None:
        self._s = session

    # --- documents -----------------------------------------------------------------------

    async def save_document(self, document: SopDocument) -> None:
        params = rows.columns(document)
        params["broker_id"] = owner_key(document.broker_id)
        await self._s.run(
            f"INSERT INTO sop_documents ({_DOCUMENT}) VALUES (%(document_id)s, %(broker_id)s,"
            " %(filename)s, %(content_type)s, %(blob_ref)s, %(pages)s, %(sha256)s,"
            " %(uploaded_at)s)"
            " ON CONFLICT (document_id) DO UPDATE SET broker_id = EXCLUDED.broker_id,"
            " filename = EXCLUDED.filename, content_type = EXCLUDED.content_type,"
            " blob_ref = EXCLUDED.blob_ref, pages = EXCLUDED.pages, sha256 = EXCLUDED.sha256,"
            " uploaded_at = EXCLUDED.uploaded_at",
            params,
        )

    async def get_document(self, document_id: str) -> SopDocument | None:
        row = await self._s.one(
            f"SELECT {_DOCUMENT} FROM sop_documents WHERE document_id = %s", (document_id,)
        )
        return None if row is None else SopDocument.model_validate(row)

    async def list_documents(self, broker_id: str | None) -> builtins.list[SopDocument]:
        found = await self._s.all(
            f"SELECT {_DOCUMENT} FROM sop_documents WHERE broker_id = %s"
            " ORDER BY uploaded_at, document_id",
            (owner_key(broker_id),),
        )
        return [SopDocument.model_validate(row) for row in found]

    # --- drafts --------------------------------------------------------------------------

    async def save_draft(self, draft: SopDraft) -> None:
        await self._s.run(
            "INSERT INTO sop_drafts (broker_id, draft, updated_at) VALUES (%s, %s, %s)"
            " ON CONFLICT (broker_id) DO UPDATE SET draft = EXCLUDED.draft,"
            " updated_at = EXCLUDED.updated_at",
            (
                owner_key(draft.broker_id),
                Jsonb(draft.model_dump(mode="json")),
                rows.aware(draft.updated_at, "SopDraft.updated_at"),
            ),
        )

    async def get_draft(self, broker_id: str | None) -> SopDraft | None:
        row = await self._s.one(
            "SELECT draft FROM sop_drafts WHERE broker_id = %s", (owner_key(broker_id),)
        )
        return None if row is None else SopDraft.model_validate(row["draft"])

    # --- versions ------------------------------------------------------------------------

    async def save_version(
        self, version: SopVersion, embeddings: SectionEmbeddings | None = None
    ) -> None:
        if embeddings is not None:
            _check(embeddings, len(version.sections))
        owner = owner_key(version.broker_id)
        row = await self._s.one(
            f"INSERT INTO sop_versions ({_VERSION}) VALUES (%s, %s, %s, %s, %s)"
            " ON CONFLICT (broker_id, version) DO UPDATE SET"
            " published_at = EXCLUDED.published_at, published_by = EXCLUDED.published_by,"
            " lint = EXCLUDED.lint"
            " RETURNING (xmax = 0) AS inserted",
            (
                owner,
                version.version,
                rows.aware(version.published_at, "SopVersion.published_at"),
                version.published_by,
                Jsonb(list(version.lint)),
            ),
        )
        assert row is not None
        if row["inserted"]:
            await self._insert_sections(owner, version, embeddings)
        elif embeddings is not None:
            await self._embed(owner, version.version, embeddings)

    async def _insert_sections(
        self, owner: str, version: SopVersion, embeddings: SectionEmbeddings | None
    ) -> None:
        params: list[dict[str, Any]] = []
        for position, section in enumerate(version.sections):
            vector = embeddings.vectors[position] if embeddings else None
            params.append(
                {
                    "broker_id": owner,
                    "version": version.version,
                    "position": position,
                    "section_id": section.section_id,
                    "layer": section.layer.value,
                    "load_type": section.load_type,
                    "title": section.title,
                    "description": section.description,
                    "intents": Jsonb(list(section.intents)),
                    "stages": Jsonb([s.value for s in section.stages]),
                    "mode": section.mode.value,
                    "body": section.body,
                    "source": section.source,
                    "embedding": None if vector is None else vector_literal(vector),
                    "embedding_model": None if embeddings is None else embeddings.model,
                }
            )
        if not params:
            return
        async with self._s.conn.cursor() as cursor:
            await cursor.executemany(
                f"INSERT INTO sop_sections ({SECTION_COLUMNS}, embedding, embedding_model)"
                " VALUES (%(broker_id)s, %(version)s, %(position)s, %(section_id)s, %(layer)s,"
                " %(load_type)s, %(title)s, %(description)s, %(intents)s, %(stages)s,"
                " %(mode)s, %(body)s, %(source)s, %(embedding)s::public.vector,"
                " %(embedding_model)s)",
                params,
            )

    async def _embed(self, owner: str, version: str, embeddings: SectionEmbeddings) -> None:
        params = [
            (vector_literal(vector), embeddings.model, owner, version, position)
            for position, vector in enumerate(embeddings.vectors)
        ]
        if not params:
            return
        async with self._s.conn.cursor() as cursor:
            await cursor.executemany(
                "UPDATE sop_sections SET embedding = %s::public.vector, embedding_model = %s"
                " WHERE broker_id = %s AND version = %s AND position = %s",
                params,
            )

    async def get_version(self, broker_id: str | None, version: str) -> SopVersion | None:
        found = await self._versions(
            "broker_id = %s AND version = %s", (owner_key(broker_id), version)
        )
        return found[0] if found else None

    async def published(self, broker_id: str | None) -> SopVersion | None:
        found = await self._versions(
            "seq = (SELECT seq FROM sop_versions WHERE broker_id = %s"
            " ORDER BY published_at DESC, seq DESC LIMIT 1)",
            (owner_key(broker_id),),
        )
        return found[0] if found else None

    async def versions(self, broker_id: str | None) -> builtins.list[SopVersion]:
        return await self._versions("broker_id = %s", (owner_key(broker_id),))

    async def _versions(
        self, where: LiteralString, params: tuple[Any, ...]
    ) -> builtins.list[SopVersion]:
        """The versions matching `where`, by `seq`, with their sections in order."""
        heads = await self._s.all(
            f"SELECT {_VERSION} FROM sop_versions WHERE {where} ORDER BY seq",
            params,
        )
        if not heads:
            return []
        sections = await self._s.all(
            f"SELECT {SECTION_COLUMNS} FROM sop_sections"
            " WHERE (broker_id, version) IN (SELECT broker_id, version FROM sop_versions"
            f" WHERE {where}) ORDER BY version, position",
            params,
        )
        by_version: dict[str, list[SopSection]] = {}
        for row in sections:
            by_version.setdefault(row["version"], []).append(section_from(row))
        return [
            SopVersion(
                broker_id=layer_owner(head["broker_id"]),
                version=head["version"],
                sections=tuple(by_version.get(head["version"], ())),
                published_at=head["published_at"],
                published_by=head["published_by"],
                lint=tuple(head["lint"]),
            )
            for head in heads
        ]


def _check(embeddings: SectionEmbeddings, sections: int) -> None:
    if len(embeddings.vectors) != sections:
        raise ValueError(
            f"{len(embeddings.vectors)} embeddings for {sections} sections: give one per section"
        )
    for vector in embeddings.vectors:
        if len(vector) != EMBEDDING_DIM:
            raise ValueError(
                f"an embedding of {embeddings.model} has {len(vector)} dimensions; the index "
                f"stores {EMBEDDING_DIM} (config/models.yaml embedding.dim)"
            )
