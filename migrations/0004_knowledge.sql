-- The knowledge base (docs/decisions.md, Knowledge base; card W3-KNW): the SOP documents
-- editors upload, each broker's draft, the published versions and their sections, which
-- the SOP index searches by vector (pgvector) and by text (tsvector).
--
-- A layer's owner is `broker_id`; the standard layer, which every broker inherits, is
-- the row owner 'standard' (the standard catalog's id, `domain.broker.STANDARD_BROKER_ID`;
-- the domain reads it as None). A sentinel rather than NULL, because it is part of
-- primary keys.

-- pgvector goes in `public`: a scratch schema's `search_path` is that schema alone, so
-- the type and its operators are always named `public.` (wave 3 spec, F9). Migrations
-- of different schemas do not wait for each other, so two creating the extension at
-- once would collide: they take one database-wide lock first.
SELECT pg_advisory_xact_lock(hashtext('freight_agent:create extension vector'));
CREATE EXTENSION IF NOT EXISTS vector SCHEMA public;

CREATE TABLE sop_documents (
    document_id  text COLLATE "C" PRIMARY KEY,
    broker_id    text COLLATE "C" NOT NULL,
    filename     text NOT NULL,
    content_type text NOT NULL,
    blob_ref     text NOT NULL,
    pages        integer NOT NULL,
    sha256       text NOT NULL,
    uploaded_at  timestamptz NOT NULL
);
CREATE INDEX sop_documents_by_broker ON sop_documents (broker_id, uploaded_at, document_id);

-- One draft per layer, read and written whole.
CREATE TABLE sop_drafts (
    broker_id  text COLLATE "C" PRIMARY KEY,
    draft      jsonb NOT NULL,  -- the whole SopDraft
    updated_at timestamptz NOT NULL
);

-- A version's sections never change: `version` is their hash. Publishing a version again
-- updates its publication (a reverted edit brings its old version back). The published
-- version has the latest `published_at`; `seq`, the order of first publication, breaks
-- ties and orders the history.
CREATE TABLE sop_versions (
    broker_id    text COLLATE "C" NOT NULL,
    version      text COLLATE "C" NOT NULL,
    published_at timestamptz NOT NULL,
    published_by text NOT NULL,
    lint         jsonb NOT NULL,  -- the warnings the publish accepted, a JSON array of text
    seq          bigserial NOT NULL UNIQUE,
    PRIMARY KEY (broker_id, version)
);
CREATE INDEX sop_versions_published ON sop_versions (broker_id, published_at DESC, seq DESC);

-- One row per section of a version, in the version's order (`position`). `source` is the
-- section's provenance as the run shows it (a file of sops/, or an upload and its pages):
-- text, as `SopSection.source` is. The embedding is optional (a store may publish with
-- none) and is compared only with a query embedded by the same model.
CREATE TABLE sop_sections (
    broker_id       text COLLATE "C" NOT NULL,
    version         text COLLATE "C" NOT NULL,
    position        integer NOT NULL,
    section_id      text COLLATE "C" NOT NULL,
    layer           text NOT NULL,
    load_type       text,
    title           text NOT NULL,
    description     text NOT NULL,
    intents         jsonb NOT NULL,  -- JSON array of intent ids; empty: every intent
    stages          jsonb NOT NULL,  -- JSON array of stages; empty: every stage
    mode            text NOT NULL,
    body            text NOT NULL,
    source          text NOT NULL,
    embedding       public.vector(1536),
    embedding_model text,
    tsv             tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', title || ' ' || description), 'A')
        || setweight(to_tsvector('english', body), 'B')
    ) STORED,
    PRIMARY KEY (broker_id, version, position),
    FOREIGN KEY (broker_id, version) REFERENCES sop_versions
        ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
    CHECK ((embedding IS NULL) = (embedding_model IS NULL))
);
CREATE INDEX sop_sections_by_embedding ON sop_sections
    USING hnsw (embedding public.vector_cosine_ops);
CREATE INDEX sop_sections_by_text ON sop_sections USING gin (tsv);
