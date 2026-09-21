-- 0012 bead 9 (observability): persist the BASE_ROOT boot-assertion outcome so an
-- operator can SEE a failed (or healthy) base volume, not only find it in a log.
--
-- Errata E4: the fail-loud boot assertion (netboot_base.assert_base_root_writable)
-- raises a BaseRootError at worker startup, but boot_autopull runs as a
-- fire-and-forget task whose exception the worker's done-callback logs and
-- SWALLOWS (a hard crash would couple a base-volume misconfig to killing 0010's
-- .deb mirroring in the same worker). The design's "fail loud" guarantee is
-- therefore satisfied by (a) the ERROR log and (b) THIS: a single-row status the
-- worker upserts each boot -- ok=TRUE on a passed assertion, ok=FALSE + the
-- BaseRootError code on a failed one -- surfaced read-only at
-- GET /v1/operator/netboot. Additive; no down-migration machinery (forward-only,
-- checksum-pinned, central/db.py). Rollback: DROP TABLE base_boot_status; delete
-- this file.
CREATE TABLE base_boot_status (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),  -- one row, latest boot wins
    ok BOOLEAN NOT NULL,                                           -- did the BASE_ROOT assertion pass this boot
    code TEXT,                                                     -- BaseRootError.code on failure, NULL when ok
    checked_at DOUBLE PRECISION NOT NULL                           -- when the worker last checked
);
