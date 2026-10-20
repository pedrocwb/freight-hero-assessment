"""The broker catalog store, on the unit of work.

Owner: card W3-BRK. The memory implementation is in
`adapters/persistence/memory/brokers.py`, the Postgres one in
`adapters/persistence/postgres/brokers.py` (migration 0003); `tests/contract/
test_brokers.py` holds both to this contract.

The store never numbers a version: the caller does. A configuration change saves the
broker at `config_version + 1` with `rollout.previous_version` set to the old one
(`PUT /api/brokers/{id}`, publishing an SOP); a rollout change (a canary, a rollback)
saves the current version again with a new `rollout`. Read with a unit of work and
change with `Broker.revised`, which validates, in the same one.
"""

import builtins
from typing import Protocol

from freight_agent.domain.broker import Broker


class BrokerRepository(Protocol):
    async def get(self, broker_id: str) -> Broker | None:
        """The current version of the broker, or None when it was never saved."""
        ...

    async def get_version(self, broker_id: str, version: int) -> Broker | None:
        """The broker as it was saved with `config_version == version`."""
        ...

    async def save(self, broker: Broker) -> None:
        """Make `broker` the current version and keep its `(broker_id, config_version)`
        snapshot. Saving the same version again replaces that snapshot."""
        ...

    async def versions(self, broker_id: str) -> builtins.list[Broker]:
        """Every saved version of the broker, oldest `config_version` first."""
        ...

    async def list(self) -> builtins.list[Broker]:
        """The current version of every broker, by `broker_id`."""
        ...
