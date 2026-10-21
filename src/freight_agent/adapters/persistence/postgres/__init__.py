"""Postgres persistence: the same interfaces and contract suites as the memory store, and
durable. `PERSISTENCE=postgres` puts the platform's load memory here (`bootstrap`).

One `DATABASE_URL` serves the API and the migrations (`agent db migrate`). The adapter
uses nothing that needs a server session (no LISTEN/NOTIFY, no session advisory locks, no
session `SET`, no server-side prepared statements), so it works behind a
transaction-mode pooler too. Concurrency rests on row locks, `FOR UPDATE SKIP LOCKED`, a
partial unique index and transaction-scoped advisory locks only, so several workers can
share the queue and the timers (the locking rules are in `repositories.py`).

Modules: `store` (pool and unit of work), `repositories` and `documents`, `rows` (mapping
to and from domain models), `connection` (settings and the transaction session),
`migrate` (the numbered files of `migrations/`).
"""

from freight_agent.adapters.persistence.postgres.store import (
    PostgresStore,
    PostgresUnitOfWorkFactory,
    open_store,
)

__all__ = ["PostgresStore", "PostgresUnitOfWorkFactory", "open_store"]
