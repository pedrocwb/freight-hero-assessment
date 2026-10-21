"""The facility memory's LangGraph store on Postgres: `AsyncPostgresStore` with a pgvector
index on the facts' `text`, in the database of the load memory.

The composition root is synchronous, and `AsyncPostgresStore` binds the running event
loop when it is built. `LazyPostgresStore` is therefore a `BaseStore` that opens its pool
and builds the LangGraph store on the first operation, in the caller's loop, and hands
every batch of operations to it, the way `PostgresUnitOfWorkFactory` opens its pool on the
first unit of work.

Tables (LangGraph's own, created by its `setup()`, which `agent db migrate` runs through
`setup_store`): `store` (one row per item, its value as JSONB), `store_vectors` (one
pgvector row per embedded field, `vector(dims)`, with an HNSW index), and their migration
counters `store_migrations` and `vector_migrations`. The `vector` extension is the
`0002_vector_extension.sql` migration's.

The dimensions are fixed when the tables are created, so every process that writes them
embeds with the same `dims` (1536: `openai/text-embedding-3-small`; the offline hash is
built at that width on Postgres, see `bootstrap.build_platform_memory`).
"""

import asyncio
from collections.abc import Iterable
from typing import Any

from langchain_core.embeddings import Embeddings
from langgraph.store.base import BaseStore, Op, Result
from langgraph.store.postgres import AsyncPostgresStore
from langgraph.store.postgres.base import PostgresIndexConfig
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from freight_agent.adapters.memory.embeddings import HashEmbeddings
from freight_agent.adapters.persistence.postgres.connection import check_schema

INDEXED_FIELDS = ("text",)


def postgres_index(embeddings: Embeddings, dims: int) -> PostgresIndexConfig:
    """The pgvector index on the facts' `text`, embedded by `embeddings`."""
    return {"dims": dims, "embed": embeddings, "fields": list(INDEXED_FIELDS)}


class LazyPostgresStore(BaseStore):
    """A LangGraph store on Postgres whose pool opens on the first operation.

    `schema` puts the tables in a schema other than the connection's default (the tests,
    each in a scratch schema): it is set per connection, so unlike the load memory's
    `SET LOCAL` it does not go through a transaction-mode pooler. `close()` belongs in
    the caller's shutdown.
    """

    def __init__(
        self,
        conninfo: str,
        *,
        index: PostgresIndexConfig,
        schema: str | None = None,
        max_size: int = 4,
    ) -> None:
        kwargs: dict[str, Any] = {
            "autocommit": True,  # LangGraph's store manages its own transactions
            "prepare_threshold": None,
            "row_factory": dict_row,
        }
        if schema is not None:
            kwargs["options"] = f"-c search_path={check_schema(schema)},public"
        self.index = index
        self.pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
            conninfo,
            min_size=1,
            max_size=max_size,
            kwargs=kwargs,
            check=AsyncConnectionPool.check_connection,
            open=False,
        )
        self._store: AsyncPostgresStore | None = None
        self._lock = asyncio.Lock()

    async def opened(self) -> AsyncPostgresStore:
        """The LangGraph store, its pool open."""
        if self._store is None:
            async with self._lock:
                if self._store is None:
                    await self.pool.open(wait=True)
                    self._store = AsyncPostgresStore(self.pool, index=self.index)
        return self._store

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        return await (await self.opened()).abatch(ops)

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        raise NotImplementedError("the facility memory's Postgres store is async only")

    async def setup(self) -> None:
        """Create or migrate LangGraph's tables and the vector index (idempotent)."""
        await (await self.opened()).setup()

    async def close(self) -> None:
        if self._store is not None:
            await self.pool.close()
            self._store = None


async def setup_store(conninfo: str, dims: int, *, schema: str | None = None) -> None:
    """Run LangGraph's store migrations on `conninfo`: the tables and the vector index at
    `dims`. Setting up embeds nothing, so the offline hash stands in for the embedder."""
    store = LazyPostgresStore(
        conninfo, index=postgres_index(HashEmbeddings(dims), dims), schema=schema
    )
    try:
        await store.setup()
    finally:
        await store.close()
