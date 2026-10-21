-- The memory of every load: the tables behind the persistence interfaces
-- (src/freight_agent/interfaces/repositories.py and interfaces/documents.py).
--
-- A table keeps as columns what its queries filter and sort on. Documents that are only
-- ever read back whole, a load's metadata and a run record, are JSONB.
--
-- Ids are text in the "C" collation, so they compare by code point as in Python; the
-- database's own collation would put `out_a` before `out_B`.
--
-- Foreign keys are checked at COMMIT (DEFERRABLE INITIALLY DEFERRED): a unit of work may
-- write a child before its parent, as long as the whole is consistent when it commits.

-- Lists that tie on their timestamp are ordered by natural id, so `out_9` comes before
-- `out_10`: the same key as the memory store's `_id_key` (the text before the last `_`
-- and the number after it, or the whole id and -1 when it does not end in a number).
CREATE TYPE id_order_key AS (prefix text COLLATE "C", number numeric);

CREATE FUNCTION id_order(id text) RETURNS id_order_key
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
        WHEN id ~ '^[0-9]+$' THEN ROW('', id::numeric)::id_order_key
        WHEN id ~ '_[0-9]+$' THEN ROW(
            regexp_replace(id, '_[0-9]+$', ''),
            substring(id FROM '_([0-9]+)$')::numeric
        )::id_order_key
        ELSE ROW(id, -1)::id_order_key
    END
$$;

CREATE TABLE loads (
    load_id    text COLLATE "C" PRIMARY KEY,
    broker_id  text NOT NULL,
    stage      text NOT NULL,
    delay_risk text NOT NULL,
    metadata   jsonb NOT NULL  -- the whole Load
);

CREATE TABLE risk_trackers (
    load_id       text COLLATE "C" PRIMARY KEY
                  REFERENCES loads DEFERRABLE INITIALLY DEFERRED,
    band          text NOT NULL,
    pending       text,
    pending_count integer NOT NULL,
    -- The ids of the last pings the tracker counted, oldest first (a JSON array of
    -- text), so that a redelivered ping changes nothing after a restart.
    recent_ping_ids jsonb NOT NULL DEFAULT '[]'::jsonb
);

-- Append-only. `seq` is the order of the log: a message carries the time it was sent and
-- can arrive after a later event, so `recent()` sorts by `seq`, never by `occurred_at`.
CREATE TABLE events (
    event_id          text COLLATE "C" PRIMARY KEY,
    seq               bigserial UNIQUE,
    load_id           text COLLATE "C" NOT NULL
                      REFERENCES loads DEFERRABLE INITIALLY DEFERRED,
    type              text NOT NULL,
    occurred_at       timestamptz NOT NULL,
    channel           text NOT NULL,
    direction         text NOT NULL,
    sender_contact_id text,
    body              text NOT NULL,
    attachments       jsonb NOT NULL,
    data              jsonb NOT NULL
);
CREATE INDEX events_by_load ON events (load_id, seq);

CREATE FUNCTION reject_event_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'the event log is append-only (% on event %)', TG_OP, OLD.event_id;
END
$$;

CREATE TRIGGER events_append_only
    BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION reject_event_change();

CREATE TABLE obligations (
    obligation_id   text COLLATE "C" PRIMARY KEY,
    load_id         text COLLATE "C" NOT NULL
                    REFERENCES loads DEFERRABLE INITIALLY DEFERRED,
    kind            text NOT NULL,
    status          text NOT NULL,
    attempts        integer NOT NULL,
    opened_at       timestamptz NOT NULL,
    last_action_at  timestamptz,
    deferred_until  timestamptz,
    deferrals_used  integer NOT NULL,
    active_timer_id text COLLATE "C",  -- no foreign key: the timer may be saved later, or never
    sop_version     text
);
CREATE INDEX obligations_by_load ON obligations (load_id);

CREATE TABLE timers (
    timer_id      text COLLATE "C" PRIMARY KEY,
    load_id       text COLLATE "C" NOT NULL
                  REFERENCES loads DEFERRABLE INITIALLY DEFERRED,
    obligation_id text COLLATE "C" NOT NULL
                  REFERENCES obligations DEFERRABLE INITIALLY DEFERRED,
    due_at        timestamptz NOT NULL,
    created_at    timestamptz NOT NULL,
    status        text NOT NULL,
    reason        text NOT NULL
);
CREATE INDEX timers_pending_by_due_at ON timers (due_at) WHERE status = 'pending';
CREATE INDEX timers_by_obligation ON timers (obligation_id);
CREATE INDEX timers_by_load ON timers (load_id);

CREATE TABLE runs (
    run_id     text COLLATE "C" PRIMARY KEY,
    event_id   text COLLATE "C" NOT NULL
               REFERENCES events DEFERRABLE INITIALLY DEFERRED,
    load_id    text COLLATE "C" NOT NULL
               REFERENCES loads DEFERRABLE INITIALLY DEFERRED,
    started_at timestamptz NOT NULL,
    status     text NOT NULL,
    record     jsonb NOT NULL  -- the whole RunRecord
);
CREATE INDEX runs_by_event ON runs (event_id);
CREATE INDEX runs_by_load ON runs (load_id);

-- `seq` is the insertion order: the last tie-break, after the time and the natural id.
CREATE TABLE outbox (
    outbox_id       text COLLATE "C" PRIMARY KEY,
    seq             bigserial UNIQUE,
    idempotency_key text COLLATE "C" NOT NULL UNIQUE,
    run_id          text COLLATE "C" NOT NULL
                    REFERENCES runs DEFERRABLE INITIALLY DEFERRED,
    load_id         text COLLATE "C" NOT NULL
                    REFERENCES loads DEFERRABLE INITIALLY DEFERRED,
    kind            text NOT NULL,
    payload         jsonb NOT NULL,
    created_at      timestamptz NOT NULL,
    status          text NOT NULL,
    attempts        integer NOT NULL,
    last_error      text
);
CREATE INDEX outbox_pending_by_created_at ON outbox (created_at) WHERE status = 'pending';
CREATE INDEX outbox_by_load ON outbox (load_id);

-- The per-load FIFO. `seq` is the arrival order, and a released event keeps it, so it
-- goes back to its place. A completed event leaves the table; a dead letter stays.
-- `enqueued_at` is the database's wall clock, for operations (queue age). `claimed_at` is
-- the runtime's clock at the claim: the claim lease (`EventQueue.reclaim_expired`)
-- compares it with the runtime's `now`, and the simulation runs on a virtual clock.
CREATE TABLE event_queue (
    event_id    text COLLATE "C" PRIMARY KEY
                REFERENCES events DEFERRABLE INITIALLY DEFERRED,
    seq         bigserial UNIQUE,
    load_id     text COLLATE "C" NOT NULL,
    state       text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'in_flight', 'dead')),
    failures    integer NOT NULL DEFAULT 0,
    last_error  text,
    enqueued_at timestamptz NOT NULL DEFAULT now(),
    claimed_at  timestamptz,
    -- An event in flight always has a claim time, or the lease could never take it back.
    CONSTRAINT event_queue_in_flight_has_claim_time
        CHECK (state <> 'in_flight' OR claimed_at IS NOT NULL)
);
-- At most one event per load in flight, whatever the code does: a second claim of the
-- same load fails here instead of running two events of one load at once.
CREATE UNIQUE INDEX event_queue_one_in_flight_per_load
    ON event_queue (load_id) WHERE state = 'in_flight';
CREATE INDEX event_queue_waiting ON event_queue (load_id, seq) WHERE state = 'queued';
CREATE INDEX event_queue_in_flight_by_claim ON event_queue (claimed_at)
    WHERE state = 'in_flight';

-- Documents: one row per attachment of a message, read by the document pipeline. One
-- column per field of `domain.documents.Document`; the values read from the document and
-- their checks against the load are JSONB (`extracted` is an object by field name,
-- `checks` a list of FieldCheck).
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
