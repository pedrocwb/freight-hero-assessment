"""Persistent broker editing, with compare-and-swap to prevent lost browser edits."""

from psycopg.types.json import Jsonb

from freight_agent.adapters.persistence.postgres.store import PostgresUnitOfWorkFactory
from freight_agent.domain.errors import DomainError


class PostgresProfiles:
    def __init__(self, store: PostgresUnitOfWorkFactory) -> None:
        self.store = store

    async def get_or_create(self, broker_id: str, default: dict) -> dict:
        async with self.store() as uow:
            await uow.session.run(
                "INSERT INTO broker_profiles (broker_id, profile) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (broker_id, Jsonb(default)),
            )
            row = await uow.session.one(
                "SELECT profile, revision FROM broker_profiles WHERE broker_id = %s",
                (broker_id,),
            )
            await uow.commit()
        assert row is not None
        return {**row["profile"], "revision": row["revision"]}

    async def save(self, broker_id: str, revision: int, profile: dict) -> dict:
        async with self.store() as uow:
            row = await uow.session.one(
                "UPDATE broker_profiles SET profile = %s, revision = revision + 1, "
                "updated_at = now() WHERE broker_id = %s AND revision = %s RETURNING revision",
                (Jsonb(profile), broker_id, revision),
            )
            if row is None:
                raise DomainError("O SOP foi alterado em outra janela. Recarregue antes de salvar.")
            await uow.commit()
        return {**profile, "revision": row["revision"]}

    async def loads(self, broker_id: str) -> list[dict]:
        async with self.store() as uow:
            rows = await uow.session.all(
                "SELECT metadata FROM loads WHERE broker_id = %s ORDER BY load_id DESC",
                (broker_id,),
            )
        return [row["metadata"] for row in rows]
