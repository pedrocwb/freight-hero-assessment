"""`SopIndex` over the published sections in Postgres (pgvector cosine plus full text,
fused by reciprocal rank).

Owner: card W3-KNW.

Contract with the composition root: `knowledge_index(settings) -> SopIndex`, under that
name. `bootstrap.build_knowledge` loads it by name for `PERSISTENCE=postgres`. The index
serves what was published (`agent sop import sops/` after `agent db migrate`, then each
broker's publishes); a database with nothing published serves no section, so every run
hands its event to a Hero ("no SOP for this situation") rather than improvising.

A broker reads the published version of the standard layer and of its own layer (the
latest `published_at` of each, `sop_versions`), merged by `domain.sop` exactly as in
memory: `by_applies` is `domain.sop.resolve`. Each call reads in a short read-only
transaction of its own pool, outside any unit of work.

`search` ranks only the rows whose text the broker's merged sections hold (a section a
client replaced does not count), twice: by cosine distance between the query's
embedding and each row's (rows embedded by the same model only; `OPERATOR(public.<=>)`,
F9), and by `ts_rank` of the row's `tsvector` against the query's words (any of them).
The two ranks are fused by reciprocal rank (`1 / (60 + rank)`, summed), a section scores
its best row, and the top `k` sections come back. When the query cannot be embedded,
the text rank alone decides.
"""

import contextlib
import logging
import re
from collections.abc import AsyncIterator, Sequence
from typing import Any, Final, LiteralString

from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from freight_agent.adapters.knowledge.embeddings import embedder_for
from freight_agent.adapters.knowledge.memory import SectionIndex, compose_version
from freight_agent.adapters.persistence.postgres.connection import (
    CONNECTION_KWARGS,
    begin_command,
)
from freight_agent.adapters.persistence.postgres.sops import (
    EMBEDDING_DIM,
    owner_key,
    section_from,
    vector_literal,
)
from freight_agent.domain.broker import STANDARD_BROKER_ID
from freight_agent.domain.sop import (
    ResolvedSection,
    SearchHit,
    SopSection,
    contributors,
    effective_sections,
    layer_owner,
)
from freight_agent.ports.knowledge import Embedder, EmbeddingError
from freight_agent.settings import Settings

logger = logging.getLogger(__name__)

RRF_K: Final = 60  # the reciprocal rank constant of the original paper
_WORD: Final = re.compile(r"[a-z0-9]+")

# The published version of each owner asked for: the latest publication.
_PUBLISHED: Final = (
    "SELECT DISTINCT ON (broker_id) broker_id, version FROM sop_versions"
    " WHERE broker_id = ANY(%(owners)s) ORDER BY broker_id, published_at DESC, seq DESC"
)
_SECTIONS: Final = (
    "SELECT s.broker_id, s.version, s.position, s.section_id, s.layer, s.load_type, s.title,"
    " s.description, s.intents, s.stages, s.mode, s.body, s.source"
    f" FROM sop_sections s JOIN ({_PUBLISHED}) p USING (broker_id, version)"
    " ORDER BY s.broker_id <> 'standard', s.position"
)
# The ranks of the given rows: by vector (rows of the query's model) and by text.
_RANKS: Final = (
    "WITH live AS ("
    "  SELECT * FROM unnest(%(owners)s::text[], %(versions)s::text[], %(positions)s::int[])"
    "  AS l(broker_id, version, position)"
    "), candidates AS ("
    "  SELECT s.broker_id, s.version, s.position, s.embedding, s.embedding_model, s.tsv"
    "  FROM sop_sections s JOIN live USING (broker_id, version, position)"
    "), by_vector AS ("
    "  SELECT broker_id, version, position, row_number() OVER ("
    "    ORDER BY embedding OPERATOR(public.<=>) %(vector)s::public.vector, position"
    "  ) AS rank"
    "  FROM candidates WHERE %(vector)s::public.vector IS NOT NULL AND embedding_model = %(model)s"
    "), by_text AS ("
    "  SELECT broker_id, version, position, row_number() OVER ("
    "    ORDER BY ts_rank(tsv, query) DESC, position"
    "  ) AS rank"
    "  FROM candidates, to_tsquery('english', %(words)s) AS query WHERE tsv @@ query"
    ")"
    " SELECT broker_id, version, position, v.rank AS vector_rank, t.rank AS text_rank"
    " FROM by_vector v FULL JOIN by_text t USING (broker_id, version, position)"
)

Key = tuple[str, str, int]  # (owner, version, position): one row of sop_sections


class PostgresSopIndex(SectionIndex):
    """Implements `ports.knowledge.SopIndex` over the published sections in Postgres.

    The pool opens on the first call, in the caller's event loop; `close()` belongs in
    the shutdown of whoever built the index.
    """

    def __init__(
        self,
        conninfo: str,
        *,
        embedder: Embedder,
        schema: str | None = None,
        min_size: int = 1,
        max_size: int = 4,
    ) -> None:
        self._embedder = embedder
        self._begin = begin_command(schema)  # an invalid schema name fails here, not later
        self._pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
            conninfo,
            min_size=min_size,
            max_size=max_size,
            kwargs=CONNECTION_KWARGS,
            check=AsyncConnectionPool.check_connection,
            open=False,
        )
        self._opened = False

    async def close(self) -> None:
        if self._opened:
            await self._pool.close()
            self._opened = False

    async def sections_for(self, broker_id: str) -> Sequence[SopSection]:
        return [section for _, section in await self._rows(broker_id)]

    async def version(self, broker_id: str) -> str | None:
        found = await self._read(_PUBLISHED, {"owners": _owners(broker_id)})
        versions = {row["broker_id"]: row["version"] for row in found}
        own = layer_owner(broker_id)
        return compose_version(
            versions.get(STANDARD_BROKER_ID), None if own is None else versions.get(own)
        )

    async def search(
        self, broker_id: str, load_types: Sequence[str], query: str, k: int = 5
    ) -> tuple[SearchHit, ...]:
        words = _WORD.findall(query.lower())
        if not words or k <= 0:
            return ()
        rows = await self._rows(broker_id)
        live = _live_keys(rows, broker_id, load_types)
        if not live:
            return ()
        section_of = {key: section.section_id for key, section in rows}
        vector = await self._query_vector(query)
        ranks = await self._read(
            _RANKS,
            {
                "owners": [key[0] for key in live],
                "versions": [key[1] for key in live],
                "positions": [key[2] for key in live],
                "vector": None if vector is None else vector_literal(vector),
                "model": self._embedder.model_id,
                "words": " | ".join(dict.fromkeys(words)),
            },
        )
        best: dict[str, float] = {}
        for row in ranks:
            score = sum(1.0 / (RRF_K + row[r]) for r in ("vector_rank", "text_rank") if row[r])
            section_id = section_of[(row["broker_id"], row["version"], row["position"])]
            best[section_id] = max(best.get(section_id, 0.0), score)
        merged = {
            s.section_id: s
            for s in effective_sections(
                [section for _, section in rows], client_id=broker_id, load_types=load_types
            )
        }
        ordered = sorted(best.items(), key=lambda hit: (-hit[1], hit[0]))[:k]
        return tuple(_hit(merged[section_id], score) for section_id, score in ordered)

    async def _rows(self, broker_id: str) -> list[tuple[Key, SopSection]]:
        """The published rows the broker may see: the standard layer's and its own."""
        found = await self._read(_SECTIONS, {"owners": _owners(broker_id)})
        return [((r["broker_id"], r["version"], r["position"]), section_from(r)) for r in found]

    async def _query_vector(self, query: str) -> list[float] | None:
        if self._embedder.dim != EMBEDDING_DIM:
            return None  # cannot be compared with the stored vectors
        try:
            (vector,) = await self._embedder.embed([query])
        except EmbeddingError as exc:
            logger.warning("search by text only, the query could not be embedded: %s", exc)
            return None
        return vector

    async def _read(self, query: LiteralString, params: dict[str, Any]) -> list[DictRow]:
        if not self._opened:
            await self._pool.open(wait=True)
            self._opened = True
        async with self._pool.connection() as conn:
            await conn.execute(self._begin)
            try:
                async with conn.cursor(row_factory=dict_row) as cursor:
                    await cursor.execute(query, params)
                    return await cursor.fetchall()
            finally:
                await conn.rollback()


def _owners(broker_id: str) -> list[str]:
    own = owner_key(broker_id)
    return [STANDARD_BROKER_ID] if own == STANDARD_BROKER_ID else [STANDARD_BROKER_ID, own]


def _live_keys(
    rows: Sequence[tuple[Key, SopSection]], broker_id: str, load_types: Sequence[str]
) -> list[Key]:
    """The rows whose text the broker's merged sections hold, for these load types."""
    key_of = {id(section): key for key, section in rows}
    merged = contributors([s for _, s in rows], client_id=broker_id, load_types=load_types)
    return [key_of[id(section)] for parts in merged.values() for section in parts]


def _hit(section: ResolvedSection, score: float) -> SearchHit:
    return SearchHit(
        section_id=section.section_id,
        title=section.title,
        description=section.description,
        score=score,
    )


def knowledge_index(settings: Settings, *, embedder: Embedder | None = None) -> PostgresSopIndex:
    """The entry point `bootstrap` loads by name for `PERSISTENCE=postgres`: the index on
    `DATABASE_URL`, embedding queries with `embedder` (default: the one of `LLM_MODE`,
    `embedder_for`; the composition root passes its own)."""
    return PostgresSopIndex(
        settings.require_database_url().get_secret_value(),
        embedder=embedder_for(settings) if embedder is None else embedder,
    )


@contextlib.asynccontextmanager
async def open_index(
    conninfo: str, *, embedder: Embedder, schema: str | None = None
) -> AsyncIterator[PostgresSopIndex]:
    """An index for the duration of the block (tests, scripts)."""
    index = PostgresSopIndex(conninfo, embedder=embedder, schema=schema)
    try:
        yield index
    finally:
        await index.close()
