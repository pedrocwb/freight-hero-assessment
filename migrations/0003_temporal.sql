-- Fences late Activity attempts after another worker has reclaimed the event.
ALTER TABLE event_queue ADD COLUMN claim_token text;
CREATE INDEX outbox_pending_load ON outbox (load_id) WHERE status = 'pending';
ALTER TABLE outbox ADD COLUMN accepted_at timestamptz;
