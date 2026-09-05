ALTER TABLE media_jobs ADD COLUMN result JSONB;
ALTER TABLE media_jobs ADD COLUMN cleanup_state TEXT CHECK(cleanup_state IN ('ready','retry','failed'));
CREATE TABLE media_orphans (
    path TEXT PRIMARY KEY,
    size BIGINT NOT NULL CHECK(size>=0)
);
