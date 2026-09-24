-- Central MVP follow-up (issue #25): a device's last served tag is desired only while it was
-- served within the catalog's window (`central/content_catalog/catalog.py`,
-- SERVED_TAG_WINDOW). The served-role reads now filter on `last_served_at` too, so the served
-- index carries it. It replaces 024's `devices_active_served`, whose leading column it keeps.
--
-- What the index buys: `names_any`'s EXISTS (last_served_tag = ANY(...) AND last_served_at >=
-- ...), on the package request path, is an index-only scan over it when few rows match (the
-- costly case, a miss); when many match, the planner may seq-scan, which stops at the first
-- match. It is the only read the index serves under the default planner. `named_tags`' served
-- branch (a DISTINCT over every active served device) seq-scans `devices` with the default
-- planner settings; the index helps it only when sequential scans are disabled. That read runs
-- in the sync and Prefetch jobs, not per request, and is bounded by the device count.
--
-- The served window reads `last_served_at`, so a row with a served tag and no serve time would
-- silently name nothing in the served role. Every writer sets the two together
-- (`record_served`; main's netboot_base.py did too); the CHECK makes the pair a schema rule.
-- It is added NOT VALID, then validated. `Database.migrate` applies every pending migration in
-- one transaction, so the ADD's ACCESS EXCLUSIVE lock is held until commit either way: the two
-- steps buy no concurrency here, only a split that moves the VALIDATE into its own migration
-- mechanically. A hand-edited violating row fails VALIDATE, and the upgrade with it.
--
-- Forward-only (central/db.py). Rollback is manual: ALTER TABLE devices DROP CONSTRAINT
-- devices_served_at_with_tag; DROP INDEX devices_active_served_at; then
-- CREATE INDEX devices_active_served ON devices (last_served_tag)
--     WHERE retired_at IS NULL AND last_served_tag IS NOT NULL;
CREATE INDEX devices_active_served_at ON devices (last_served_tag, last_served_at)
    WHERE retired_at IS NULL AND last_served_tag IS NOT NULL;
DROP INDEX IF EXISTS devices_active_served;
ALTER TABLE devices ADD CONSTRAINT devices_served_at_with_tag
    CHECK (last_served_tag IS NULL OR last_served_at IS NOT NULL) NOT VALID;
ALTER TABLE devices VALIDATE CONSTRAINT devices_served_at_with_tag;
