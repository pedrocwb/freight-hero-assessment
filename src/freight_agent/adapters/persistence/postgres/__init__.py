"""Postgres persistence: the same ports and contract as the memory store, for production.

Two connection strings, as in `.env.example` (Neon gives both for one database):

- `DATABASE_URL`, the pooled endpoint (PgBouncer in transaction mode): the API. It
  opens many short units of work, and the pooler keeps the server's connection count
  low however many API processes run.
- `DATABASE_URL_DIRECT`, the direct endpoint: migrations (`migrate.py`; DDL and its
  advisory lock should not depend on the pooler) and the worker (a few long-lived
  connections that claim queued events, fire timers and dispatch the outbox).

The adapter itself works on either endpoint, because it uses nothing that needs a
server session: no LISTEN/NOTIFY, no session advisory locks, no session `SET` (a
schema is selected per transaction with `SET LOCAL`), and no server-side prepared
statements. Concurrency rests on row locks, `FOR UPDATE SKIP LOCKED`, a partial unique
index and transaction-scoped advisory locks only (the locking rules are in
`repositories.py`).

Modules: `store` (pool and unit of work), `repositories`, `rows` (mapping to and from
domain models), `connection` (settings and the transaction session), `migrate`.
"""
