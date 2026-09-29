-- Short-lived, read-only evaluations of unsaved Source queries.
CREATE TABLE source_previews (
    request_id UUID PRIMARY KEY,
    query JSONB NOT NULL CHECK (jsonb_typeof(query) = 'object'),
    status TEXT NOT NULL CHECK (status IN ('pending', 'complete', 'failed')),
    count BIGINT CHECK (count >= 0),
    image_count BIGINT CHECK (image_count >= 0),
    video_count BIGINT CHECK (video_count >= 0),
    error TEXT CHECK (error IS NULL OR error ~ '^[a-z_]{1,64}$'),
    created_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL CHECK (expires_at > created_at),
    CHECK ((status = 'complete' AND count IS NOT NULL AND image_count IS NOT NULL
            AND video_count IS NOT NULL AND count = image_count + video_count AND error IS NULL)
        OR (status = 'failed' AND error IS NOT NULL)
        OR (status = 'pending' AND count IS NULL AND image_count IS NULL
            AND video_count IS NULL AND error IS NULL))
);
CREATE INDEX source_previews_expiry ON source_previews(expires_at);
