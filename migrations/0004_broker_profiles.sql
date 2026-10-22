CREATE TABLE broker_profiles (
    broker_id text PRIMARY KEY,
    revision integer NOT NULL DEFAULT 1,
    profile jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
