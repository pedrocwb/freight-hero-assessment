-- Brokers as data (docs/decisions.md, "Brokers as data"; card W3-BRK): the catalog a
-- broker's runs follow (`domain.broker.Broker`), and every version of it, so a canary can
-- split the loads between the current version and an earlier one
-- (`domain.broker.choose_version`).
--
-- A broker is only ever read back whole, so its configuration is one JSONB document, the
-- whole `Broker` as `model_dump(mode="json")` writes it; the columns are what the queries
-- key on, and a CHECK keeps each one equal to the document's field. `loads.broker_id`
-- does not reference this table: a broker that was never saved runs on the standard
-- catalog, which is code.
--
-- The timestamps are the database's wall clock, for operations (when a version was
-- published); the agent's own time comes from its Clock and is not stored here.

-- Every saved version, whole: a run on the previous version of a canary reads it.
-- Saving a version again (a rollout change) replaces its document and keeps
-- `published_at`, the time it was first saved.
CREATE TABLE broker_versions (
    broker_id    text COLLATE "C" NOT NULL,
    version      integer NOT NULL CHECK (version >= 1),
    config       jsonb NOT NULL,  -- the whole Broker at config_version = version
    published_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (broker_id, version),
    CHECK (config ->> 'broker_id' = broker_id),
    CHECK ((config ->> 'config_version')::integer = version)
);

-- The current version of each broker: what a run's context reads first. Its document is
-- the one its row in `broker_versions` holds (the repository writes both in one
-- statement pair); the foreign key keeps the current version among the saved ones.
CREATE TABLE brokers (
    broker_id      text COLLATE "C" PRIMARY KEY,
    config         jsonb NOT NULL,  -- the whole Broker
    config_version integer NOT NULL,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (broker_id, config_version) REFERENCES broker_versions (broker_id, version)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK (config ->> 'broker_id' = broker_id),
    CHECK ((config ->> 'config_version')::integer = config_version)
);

-- And every version belongs to a broker that has a current version.
ALTER TABLE broker_versions ADD FOREIGN KEY (broker_id) REFERENCES brokers
    DEFERRABLE INITIALLY DEFERRED;
