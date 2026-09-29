-- Historical Source revisions remain immutable, but their live working sets
-- can retire once Runtime has no future/current reference to them.
ALTER TABLE media_sources ADD COLUMN refresh_active BOOLEAN NOT NULL DEFAULT TRUE;
CREATE INDEX media_sources_active_refresh ON media_sources(next_refresh,source_ref)
    WHERE refresh_active;
