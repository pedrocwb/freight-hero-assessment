-- Documents: one row per attachment of a message, read by the document pipeline
-- (docs/decisions.md, Documents; card W3-DOC; src/freight_agent/ports/documents.py).
--
-- One column per field of `domain.documents.Document`, so a row is the model as it
-- serializes; the values read from the document and their checks against the load are
-- JSONB (`extracted` is an object by field name, `checks` a list of FieldCheck).
CREATE TABLE documents (
    document_id   text COLLATE "C" PRIMARY KEY,
    load_id       text COLLATE "C" NOT NULL
                  REFERENCES loads DEFERRABLE INITIALLY DEFERRED,
    attachment_id text COLLATE "C" NOT NULL,
    broker_id     text NOT NULL,
    blob_ref      text NOT NULL,
    filename      text NOT NULL,
    content_type  text NOT NULL,
    status        text NOT NULL
                  CHECK (status IN ('pending', 'processed', 'needs_review', 'failed')),
    type_id       text,
    confidence    double precision,
    extracted     jsonb NOT NULL,
    checks        jsonb NOT NULL,
    text          text NOT NULL,
    pages         integer NOT NULL,
    received_at   timestamptz NOT NULL,
    processed_at  timestamptz
);

-- `list_for_load` and `for_attachment`: a load's documents, oldest first.
CREATE INDEX documents_by_load ON documents (load_id, received_at);
-- `list_pending`: the pipeline's work, oldest first, across loads.
CREATE INDEX documents_pending ON documents (received_at) WHERE status = 'pending';
