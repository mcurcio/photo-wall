-- Central MVP fix (PR #22 P2): the unauthenticated netboot and package routes must not cost a
-- scan of every device row (a client can mint device rows with fake serials) or of every
-- release. The catalog reads DISTINCT tags per role over active devices, an EXISTS per role for
-- a package's tags, and a package's releases by sha; each has a partial index here. Fake-serial
-- rows never carry a known-good or a pin, so the frontier read never touches them.
--
-- Forward-only (central/db.py). Rollback is manual: DROP INDEX for each index below.
CREATE INDEX devices_active_known_good ON devices (known_good_tag)
    WHERE retired_at IS NULL AND known_good_tag IS NOT NULL;
CREATE INDEX devices_active_pinned ON devices (attached_tag)
    WHERE retired_at IS NULL AND attached_tag IS NOT NULL;
CREATE INDEX devices_active_served ON devices (last_served_tag)
    WHERE retired_at IS NULL AND last_served_tag IS NOT NULL;
CREATE INDEX app_releases_asset_sha256 ON app_releases (asset_sha256)
    WHERE asset_sha256 IS NOT NULL;
