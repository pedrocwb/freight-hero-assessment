"""Postgres `BrokerRepository`: tables `brokers` and `broker_versions` (migration 0003).

Owner: card W3-BRK.

A broker is stored whole as JSONB (`model_dump(mode="json")`) and validated on the way
back (`Broker.model_validate`, which runs the catalog checks), once as the current
version and once per version. `save` writes both rows; the foreign keys between them
are checked at COMMIT, so the order of the two statements does not matter.

Reads take no lock: every run reads its broker in its context transaction, and a lock
would make the runs of all the broker's loads wait for each other. Two units of work
that save the same version of a broker at once both succeed, and the last COMMIT wins,
as the port allows (saving a version again replaces that snapshot).
"""

import builtins
from typing import Any, Final

from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from freight_agent.adapters.persistence.postgres.connection import Session
from freight_agent.domain.broker import Broker

_SAVE_VERSION: Final = (
    "INSERT INTO broker_versions (broker_id, version, config)"
    " VALUES (%(broker_id)s, %(version)s, %(config)s)"
    " ON CONFLICT (broker_id, version) DO UPDATE SET config = EXCLUDED.config"
)
_SAVE_CURRENT: Final = (
    "INSERT INTO brokers (broker_id, config, config_version)"
    " VALUES (%(broker_id)s, %(config)s, %(version)s)"
    " ON CONFLICT (broker_id) DO UPDATE SET config = EXCLUDED.config,"
    " config_version = EXCLUDED.config_version, updated_at = now()"
)


def _params(broker: Broker) -> dict[str, Any]:
    return {
        "broker_id": broker.broker_id,
        "version": broker.config_version,
        "config": Jsonb(broker.model_dump(mode="json")),
    }


def _broker(row: DictRow) -> Broker:
    return Broker.model_validate(row["config"])


class PostgresBrokerRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    async def get(self, broker_id: str) -> Broker | None:
        row = await self._s.one("SELECT config FROM brokers WHERE broker_id = %s", (broker_id,))
        return None if row is None else _broker(row)

    async def get_version(self, broker_id: str, version: int) -> Broker | None:
        row = await self._s.one(
            "SELECT config FROM broker_versions WHERE broker_id = %s AND version = %s",
            (broker_id, version),
        )
        return None if row is None else _broker(row)

    async def save(self, broker: Broker) -> None:
        params = _params(broker)
        await self._s.run(_SAVE_VERSION, params)
        await self._s.run(_SAVE_CURRENT, params)

    async def versions(self, broker_id: str) -> builtins.list[Broker]:
        found = await self._s.all(
            "SELECT config FROM broker_versions WHERE broker_id = %s ORDER BY version",
            (broker_id,),
        )
        return [_broker(row) for row in found]

    async def list(self) -> builtins.list[Broker]:
        found = await self._s.all("SELECT config FROM brokers ORDER BY broker_id")
        return [_broker(row) for row in found]
