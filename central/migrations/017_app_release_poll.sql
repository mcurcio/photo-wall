-- 0010 bead 3: durable ETag store for the release poller's conditional GETs.
-- 0010 mandates ETag / If-None-Match hygiene on the releases-list poll but names
-- no storage location (flagged in the bead report). A singleton row here holds
-- the last releases-list ETag across worker restarts, so a steady state of
-- already-known releases costs ~one conditional request per cadence. Losing the
-- row only forces one full (unconditional) re-poll -- never incorrect, only less
-- cheap -- which is why it is a plain nullable column with no constraints beyond
-- the singleton guard. Additive over 016; forward-only (central/db.py). Rollback
-- is manual: DROP TABLE app_release_poll; and delete this file before boot.
CREATE TABLE app_release_poll (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
    etag TEXT
);
