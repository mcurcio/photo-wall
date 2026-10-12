-- One cache budget for media (follow-up to 5a, slice S5). Migration 087 moved every prepared copy
-- onto an Asset record (kind `media-variant`) and re-keyed its pins by name; nothing reads or
-- writes the media store's tables since. Their files stay where they are (`media/blobs/<sha256>`),
-- now kept by the media rule of `central.assets.maintenance` (pins first, then least recently
-- served, under `CacheSettings.media_budget_bytes`).
DROP TABLE media_jobs;
DROP TABLE media_blobs;
