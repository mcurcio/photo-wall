-- Provenance for centrally authored media references.  Nullable keeps legacy
-- authored rows readable while new rows identify the source query that admitted
-- them.  The candidate JSON remains the immutable central snapshot.
ALTER TABLE authored_candidates ADD COLUMN IF NOT EXISTS source_ref TEXT;
ALTER TABLE authored_candidates ADD COLUMN IF NOT EXISTS authored_at DOUBLE PRECISION;
CREATE INDEX IF NOT EXISTS authored_candidates_source_ref ON authored_candidates(source_ref);
