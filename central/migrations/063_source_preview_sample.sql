-- A completed Source preview keeps its newest members (console DDD §38, G9):
-- `shown` is the served shape (no library id or checksum); `members` adds the
-- library identity Central keeps and no route serializes; `limited` says the
-- query selects more than the worker currently accepts (counts are lower bounds);
-- `observed_at` is when the worker wrote the answer.
-- Forward-only (central/db.py). Rows live at most an hour past expiry, so a
-- completed row from before this migration is retired as expired, not back-filled.
ALTER TABLE source_previews
    ADD COLUMN shown JSONB CHECK (shown IS NULL OR (jsonb_typeof(shown) = 'array'
                                                    AND jsonb_array_length(shown) <= 24)),
    ADD COLUMN members JSONB CHECK (members IS NULL OR (jsonb_typeof(members) = 'array'
                                                        AND jsonb_array_length(members) <= 24)),
    ADD COLUMN limited BOOLEAN,
    ADD COLUMN observed_at DOUBLE PRECISION;

UPDATE source_previews SET status = 'failed', error = 'preview_expired' WHERE status = 'complete';

ALTER TABLE source_previews DROP CONSTRAINT source_previews_check1;
ALTER TABLE source_previews ADD CONSTRAINT source_previews_state_check CHECK (
    (status = 'complete' AND count IS NOT NULL AND image_count IS NOT NULL
        AND video_count IS NOT NULL AND count = image_count + video_count AND error IS NULL
        AND shown IS NOT NULL AND members IS NOT NULL
        AND jsonb_array_length(shown) = jsonb_array_length(members)
        AND limited IS NOT NULL AND observed_at IS NOT NULL)
    OR (status = 'failed' AND error IS NOT NULL)
    OR (status = 'pending' AND count IS NULL AND image_count IS NULL AND video_count IS NULL
        AND error IS NULL AND shown IS NULL AND members IS NULL AND limited IS NULL
        AND observed_at IS NULL));
