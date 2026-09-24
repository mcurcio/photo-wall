-- Central MVP follow-up (issue #23, owner ruling "remember who promoted"): the periodic release
-- sync auto-promotes the newest release, and must never move an operator's promotion. The
-- policy row now records who set `promoted_tag`: 'auto' (the sync) or 'operator' (the promote
-- route). Every writer states it; there is no default.
--
-- Backfill (owner ruling: match main's rule). Nothing before this migration recorded who
-- promoted, so an existing promotion is classified by what main would do with it next. Main's
-- rule is `_autopull_deb` (`central/app_release_boot.py` before ca75879), ported unchanged to
-- the release sync: hold the promotion iff `cached > 0 AND bound > 0`, else move it to the
-- newest deployable release. So:
--   * 'operator' where main would hold it: a player is bound AND a `.deb` is cached;
--   * 'auto' everywhere else: main would have moved it, and the sync now still will.
-- `bound` is main's `count(DISTINCT player_id) FROM bindings` (> 0 iff any binding exists:
-- player_id is NOT NULL). `cached` was `count(*) FROM app_packages`; on this schema it is what
-- the sync reads (`_any_package_produced`): a `player-deb` Asset with produced facts whose sha
-- is the `.deb` of a release with a complete locator. On an upgrade from main's 019, 021 seeded
-- those produced facts from `app_packages` for every sha a release references, so the two
-- agree except for a `.deb` no release ships (a manual upload), which main counted and the MVP
-- cannot serve.
--
-- Forward-only (central/db.py). Rollback is manual: ALTER TABLE app_release_policy DROP COLUMN
-- promoted_by. That drop must come first: main's code writes the policy row without this
-- column, which fails NOT NULL.
ALTER TABLE app_release_policy ADD COLUMN promoted_by TEXT;
UPDATE app_release_policy SET promoted_by = CASE
    WHEN EXISTS (SELECT 1 FROM bindings)
     AND EXISTS (SELECT 1 FROM app_releases r
                 JOIN assets a ON a.kind = 'player-deb' AND a.identity = r.asset_sha256
                 WHERE r.asset_url IS NOT NULL AND r.asset_size IS NOT NULL
                   AND a.produced_sha256 IS NOT NULL)
    THEN 'operator' ELSE 'auto' END;
ALTER TABLE app_release_policy
    ALTER COLUMN promoted_by SET NOT NULL,
    ADD CONSTRAINT app_release_policy_promoted_by CHECK (promoted_by IN ('auto', 'operator'));
