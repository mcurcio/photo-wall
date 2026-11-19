-- Decision 0034, slice S1. One-of-a-kind documents get one table, written only by
-- central/infra/singletons.py, each row with its audit fields (epoch seconds on the database's
-- clock, as house_settings.updated_at was). The names are the code's (SingletonName); no CHECK.
CREATE TABLE singletons (
    name TEXT PRIMARY KEY,
    document JSONB NOT NULL CHECK (jsonb_typeof(document) = 'object'),
    revision BIGINT NOT NULL CHECK (revision >= 1),
    created_at DOUBLE PRECISION NOT NULL,
    created_by TEXT NOT NULL,
    changed_at DOUBLE PRECISION NOT NULL,
    changed_by TEXT NOT NULL);

-- Settings › House moves in; its writer before this release is unknown.
INSERT INTO singletons (name, document, revision, created_at, created_by, changed_at, changed_by)
SELECT 'house',
       jsonb_build_object(
           'time', jsonb_build_object(
               'zone', zone,
               'place', CASE WHEN latitude IS NULL THEN NULL
                             ELSE jsonb_build_object('latitude', latitude, 'longitude', longitude) END),
           'units', units, 'clock', clock_format, 'zone_confirmed', zone_confirmed),
       1, updated_at, 'unknown', updated_at, 'unknown'
FROM house_settings;
DROP TABLE house_settings;

-- Class C: durable daily counters used as rate limits (owner, 2026-10-07: UX over security).
DROP TABLE node_intake_quotas;
DROP TABLE fleet_t0_daily_quotas;

-- Class A: the hub's "last looked" stamp, rewritten every 2 s. Its readers now take the worker's
-- queue heartbeat and the hub's reachability (singletons 'hub_reachability', written on change).
DROP TABLE node_bus_hub_looks;
