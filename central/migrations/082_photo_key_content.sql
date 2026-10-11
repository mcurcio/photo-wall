-- Roadmap 5a (run ledger .claude/runs/photo-5a.md, slice M1): each photo's key content, measured
-- by the media worker (media/key_content.py) and read by Central's candidate hydration. One row
-- per asset id (the id already pins the photo's revision) at one analysis version; a new
-- measurement replaces the row. central/infra/key_content_store.py is its only reader and writer.
--
-- Plain statements only: no trigger, stored procedure or function. Forward-only (central/db.py).

CREATE TABLE photo_key_content (
    asset_id TEXT PRIMARY KEY REFERENCES asset_revisions(asset_id) ON DELETE CASCADE,
    analysis_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('measured', 'failed')),
    key JSONB,
    failure TEXT CHECK (failure IN ('decode', 'timeout')),
    measured_at DOUBLE PRECISION NOT NULL,
    retry_at DOUBLE PRECISION,
    CHECK ((status = 'measured') = (key IS NOT NULL)),
    CHECK ((status = 'failed') = (failure IS NOT NULL AND retry_at IS NOT NULL)));
