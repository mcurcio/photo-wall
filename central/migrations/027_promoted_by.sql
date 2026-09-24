-- Central MVP follow-up (issue #23, owner ruling "remember who promoted"): the periodic release
-- sync auto-promotes the newest release, and must never move an operator's promotion. The
-- policy row now records who set `promoted_tag`: 'auto' (the sync) or 'operator' (the promote
-- route). Every writer states it; there is no default.
--
-- Backfill: every existing promotion is 'operator'. Nothing before this migration recorded who
-- promoted: main's boot auto-pull and the operator route both called the same set_promoted, and
-- 023's carry promoted main's served `.deb`, which (with nothing promoted) an operator had set
-- through /v1/operator/app/current. A row cannot be proven automatic, and treating an automatic
-- row as the operator's only stops it following new releases until the next operator promotion.
--
-- Forward-only (central/db.py). Rollback is manual: ALTER TABLE app_release_policy DROP COLUMN
-- promoted_by.
ALTER TABLE app_release_policy ADD COLUMN promoted_by TEXT;
UPDATE app_release_policy SET promoted_by = 'operator';
ALTER TABLE app_release_policy
    ALTER COLUMN promoted_by SET NOT NULL,
    ADD CONSTRAINT app_release_policy_promoted_by CHECK (promoted_by IN ('auto', 'operator'));
