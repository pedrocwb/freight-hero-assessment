-- The claim lease and the durable ping dedupe (docs/decisions.md, Run pipeline and
-- Intents and the gate).

-- `claimed_at` is now the runtime's clock at the claim, not the database's: the lease
-- (`EventQueue.reclaim_expired`) compares it with the runtime's `now`, and the eval
-- harness and the simulation run on a virtual clock. 0001 says otherwise for this column;
-- `enqueued_at` is still the database's wall clock. An event in flight always has a claim
-- time, or the lease could never take it back.
COMMENT ON COLUMN event_queue.claimed_at IS
    'When the event was claimed, by the runtime''s clock; the claim lease counts from it';
ALTER TABLE event_queue ADD CONSTRAINT event_queue_in_flight_has_claim_time
    CHECK (state <> 'in_flight' OR claimed_at IS NOT NULL);
CREATE INDEX event_queue_in_flight_by_claim ON event_queue (claimed_at)
    WHERE state = 'in_flight';

-- The ids of the last pings the tracker counted, oldest first (a JSON array of text), so
-- that a redelivered ping changes nothing after a restart or on another process.
ALTER TABLE risk_trackers ADD COLUMN recent_ping_ids jsonb NOT NULL DEFAULT '[]'::jsonb;
