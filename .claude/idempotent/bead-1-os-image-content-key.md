# Bead 1: `os-image-content-key` (the tracer)

**Design:** `docs/central-idempotent-jobs.md`, rule 1, §4 (OS image and Reference rows), §5 first
walkthrough, §8 (migration 028), §13 (tracer). Read those sections only.
**Base branch:** `claude/central-followups`. **Follows:** none. **Followed by:** beads 2, 3 and 4
(all branch from this bead once it lands).
**Packages:** `central/kernel`, `central/assets`, `central/content_catalog`, `central/infra`,
`central/migrations`, plus `central/content_routes.py`.

## Behaviour

An OS image's identity is the sha256 of its base tarball (the manifest's `base_image.sha256`,
already stored as `app_releases.base_tarball_sha256`). Its file is
`os-images/base-<tarball sha256>.squashfs`. A re-cut of a tag is a new key: the sync retires the
tag's reference to the old key and references the new one. Produced facts are never cleared, and
retiring the last reference never deletes the asset row, so a key's facts stay true forever.

## Frozen page

**Kernel**
- `FetchOsImage` (`central/kernel/job_types.py`): the one field is `tarball_sha256: Sha256`; `tag`
  is removed. The job name stays `os_image.fetch`.
- `Candidates` (`central/kernel/ports.py`) gains `labels: tuple[str, ...]`, no default: the
  catalog's name for each candidate, in the same order (a release tag for an OS image; the sha256
  for a `.deb`). `__post_init__` raises `ValueError("invalid_labels")` unless it is a tuple of
  `str` with the same length as `jobs`.
- `AssetRecords.forget_produced` is deleted. `AssetRecords.retire`'s docstring becomes: "Delete
  that reference; the asset row and its facts stay (the cache sweep removes unreferenced rows);
  absent -> no-op."

**Catalog** (`central/content_catalog/catalog.py`)
- `_os_image_job(row: ReleaseRow) -> FetchOsImage | None`: from `row.os_image.sha256`; None when the
  row has no OS image.
- `_resolve_base`: candidates in `choice.tags` order, **one per distinct tarball sha**, each
  labelled with the first tag that maps to it. Two tags sharing a tarball must not raise
  `duplicate_candidate`.
- `desired_in` and `pin` build `FetchOsImage` from the release row's tarball sha.
- `record_served(self, request: NetbootBaseRequest, tag: str) -> None` takes the served tag.
- `_resolve_package` passes `labels=(sha256,)`.

**Route** (`central/content_routes.py`): `netboot_base` passes
`resolution.labels[resolution.jobs.index(served.job)]` to `record_served`.

**Sync** (`central/content_catalog/sync.py`, `_record`)
- The image key is `asset_key(FetchOsImage(tarball_sha256=release.os_image.sha256))`, referenced
  with `expected_size=None, expected_sha256=None` (as today).
- When the previous row's image sha differs from the new one, or the release dropped its image,
  retire `(os-image, previous sha, tag)`. This mirrors the `.deb` re-cut branch.
- `_forget_recut_image` is deleted. Update the module docstring: an OS image re-cut is a new key.

**Assets**
- `AssetProduction` (`central/assets/production.py`): delete `_recut_since` and the
  `reference_changed` branch. `not_reproducible` stays (it now means a bug).
- `central/assets/layout.py`: code unchanged; the docstring says OS images are named by tarball
  sha, and legacy `base-<tag>.squashfs` files are left for the cache sweep.

**Infra** (`central/infra/asset_records.py`)
- `retire`: one `DELETE FROM asset_references WHERE kind=%s AND identity=%s AND owner=%s`. No
  asset-row lock and no row deletion. Update the module docstring's concurrency note.
- `forget_produced`: deleted.

**Migration `central/migrations/028_os_image_content_key.sql`**
1. `DELETE FROM assets WHERE kind = 'os-image'` (references cascade).
2. Insert one `assets` row per DISTINCT `base_tarball_sha256`, and one `asset_references` row per
   tag (owner = tag, locator = the tarball facts, expected facts NULL). Use exactly 021's validity
   filter (http(s) URL of at most 2048 chars, sha and size present, tag of at most 128 chars).
3. Carry **no** produced facts.
4. `DELETE FROM job_outcomes WHERE job_name = 'os_image.fetch'`.
5. Leave procrastinate's tables alone. A pending old-shape row fails its decode once.
- The header comment states the rollback: revert the code, then
  `UPDATE app_release_poll SET etag = NULL`.

## Files

- **Code:** `central/kernel/job_types.py`, `central/kernel/ports.py`,
  `central/content_catalog/catalog.py`, `central/content_catalog/sync.py`,
  `central/content_routes.py`, `central/assets/production.py`, `central/assets/layout.py`,
  `central/infra/asset_records.py`, `central/migrations/028_os_image_content_key.sql` (new).
- **Tests:** `tests/test_os_image_content_key.py` (new), plus the carried files below.

## Existing tests it carries

Every `FetchOsImage(tag=...)` becomes `FetchOsImage(tarball_sha256=...)`, and every
`Candidates(...)` gains `labels`. In these files:
- `tests/test_assets_handlers.py`, `tests/test_assets_reader.py`, `tests/test_content_routes_http.py`,
  `tests/test_content_catalog_catalog.py`, `tests/test_infra_execution.py`,
  `tests/test_infra_job_queue.py`, `tests/test_infra_queue_ops.py`, `tests/test_kernel_jobs.py`,
  `tests/test_kernel_ports.py`, `tests/test_publisher_conformance.py`, `tests/test_netboot_e2e_wire.py`,
  `tests/test_netboot_fresh_install_e2e.py`, `tests/test_infra_catalog_records.py`, `tests/content_db.py`.

Rewritten or deleted:
- `tests/test_assets_production.py:240` (`..._recut_meanwhile_is_discarded_and_retried`): delete.
  `:257` (`after_a_recut_forgot_the_facts...`): rewrite as "a re-cut is a new key and is produced".
- `tests/test_infra_asset_records.py:90` (`retire_of_the_last_reference_deletes_the_asset`): now
  "keeps the row and its facts; `get` returns None". `:114` (`forget_produced...`): delete.
- `tests/test_content_catalog_sync.py:252` and `:281` (OS-image re-cut and unchanged bytes): rewrite
  for keys by sha. `:291` (dropped image): retires the sha key.
- `tests/test_content_catalog_catalog.py:287` and `tests/test_content_routes_http.py:168`
  (`record_served`): pass the tag.
- `tests/test_two_pods.py`: `base-{tag}.squashfs` becomes `base-{release.tarball_sha}.squashfs`
  (lines 325, 342, 386, 410), and the `%os_image.fetch%{tag}%` job query uses the tarball sha
  (line 435).
- `tests/test_migration_carry_served_package.py`: must still pass across 028.

## Acceptance criteria (`tests/test_os_image_content_key.py`, PostgreSQL, fake origin)

1. **T1 re-cut:** tag v1 at tarball S1 is synced and produced. Re-cut v1 to S2 and sync. Expect:
   v1 references only `(os-image, S2)`; S1's row keeps its facts. After fetching S2, the boot route
   serves S2's bytes with S2's digest. One tarball GET per sha.
2. **T2 zombie on the old key:** a `FetchOsImage(S1)` handler is held mid-download. v1 is re-cut to
   S2, and S2 is produced. Then S1's run is released and finishes. Expect: `base-S2.squashfs` and
   S2's facts unchanged; the boot route still serves S2.
3. **T3 flip-flop:** listings S2, then S1 (stale), then S2 again. Expect: S2's facts survive, and
   S2's tarball is downloaded exactly once.
4. **T4 shared tarball:** two tags whose manifests name the same tarball. Expect: one asset row,
   one file, one candidate labelled with the preferred tag; `record_served` records that tag.
5. **T5 migration:** on the 027 schema, with `(os-image, tag)` rows holding facts and an
   `os_image.fetch` outcome. After 028: rows keyed by sha, no facts, no such outcome, one
   reference per tag.
6. **T6 substitute label:** an unpinned device whose wanted tag is not on disk is served its
   known-good tag's image. Expect: `last_served_tag` is the known-good tag.

## Mutation probes (each must turn the named test red; restore by reversing the edit)

- M1: skip retiring the previous image key on a re-cut (T1: v1 still references S1).
- M2: let `retire` delete the last reference's asset row (T3: S2 downloaded twice).
- M3: drop the one-candidate-per-tarball rule (T4: `duplicate_candidate`).
- M4: pass `resolution.labels[0]` instead of the served candidate's label (T6).
- M5: carry `produced_*` in 028 (T5).

## Report back

Report the net line delta, the reuse you considered, and any errata appended to `.claude/errata.md`
where this page is wrong. **Report where the spec is wrong.**
