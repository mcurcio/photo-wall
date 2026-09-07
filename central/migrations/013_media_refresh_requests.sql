ALTER TABLE media_sources
    ADD COLUMN refresh_requested_revision BIGINT NOT NULL DEFAULT 0 CHECK(refresh_requested_revision >= 0),
    ADD COLUMN refresh_completed_revision BIGINT NOT NULL DEFAULT 0 CHECK(refresh_completed_revision >= 0),
    ADD COLUMN refresh_lease_until DOUBLE PRECISION,
    ADD CONSTRAINT media_source_refresh_revision_order
        CHECK(refresh_completed_revision <= refresh_requested_revision);
