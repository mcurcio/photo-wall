-- 0013 tracer (device-less-fleet boot): base_cache fetch retry + terminal gating.
-- Additive over 018's base_cache. Three columns that let the poll-tail self-heal
-- and the serve-side resolver rate-limit a failing base fetch instead of either
-- hammering it every tick or stranding it forever:
--   * fetch_attempts  -- consecutive TRANSIENT failures, drives the backoff; reset
--                        to 0 on a successful cache.
--   * next_retry_at    -- earliest wall-clock a transient-failed tag may be re-fetched
--                        (now + backoff(fetch_attempts)); NULL => eligible now / not
--                        currently backing off (also the value on a fresh or cached row).
--   * failure_terminal -- TRUE only for an archive-INTEGRITY fault (digest mismatch,
--                        missing/oversize member, unreadable SHA256SUMS): re-fetching
--                        the same bytes would only reproduce it, so the self-heal skips
--                        it until upsert_discovered refreshes the row's base facts.
--                        FALSE for every recoverable/environmental fault (unwritable
--                        base root, missing base facts, transport error), which retry
--                        after the backoff so a repaired volume / restored uplink heals.
--
-- No down-migration machinery (forward-only, checksum-pinned; central/db.py).
-- Rollback: ALTER TABLE base_cache DROP COLUMN fetch_attempts, DROP COLUMN
-- next_retry_at, DROP COLUMN failure_terminal; delete this file.
ALTER TABLE base_cache ADD COLUMN fetch_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE base_cache ADD COLUMN next_retry_at DOUBLE PRECISION;      -- NULL = eligible now / not failed
ALTER TABLE base_cache ADD COLUMN failure_terminal BOOLEAN NOT NULL DEFAULT FALSE;
