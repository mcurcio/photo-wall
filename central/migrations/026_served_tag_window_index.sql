-- Central MVP follow-up (issue #25): a device's last served tag is desired only while it was
-- served within the catalog's window (`central/content_catalog/catalog.py`,
-- SERVED_TAG_WINDOW). The served-role reads now filter on `last_served_at` too, so the served
-- index carries it: `names_any`'s EXISTS (tag = ANY(...) AND last_served_at >= ...) and
-- `named_tags`' DISTINCT stay index-only over the same partial predicate. It replaces 024's
-- `devices_active_served`, whose leading column it keeps.
--
-- Forward-only (central/db.py). Rollback is manual: DROP INDEX devices_active_served_at, then
-- CREATE INDEX devices_active_served ON devices (last_served_tag)
--     WHERE retired_at IS NULL AND last_served_tag IS NOT NULL;
CREATE INDEX devices_active_served_at ON devices (last_served_tag, last_served_at)
    WHERE retired_at IS NULL AND last_served_tag IS NOT NULL;
DROP INDEX devices_active_served;
