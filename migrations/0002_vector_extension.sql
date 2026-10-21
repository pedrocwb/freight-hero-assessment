-- pgvector, for embeddings kept next to the load memory in the same database. Nothing
-- here uses it yet. It goes in `public` whatever the migration's schema, so a schema
-- dropped after a test run never takes the extension with it.
CREATE EXTENSION IF NOT EXISTS vector SCHEMA public;
