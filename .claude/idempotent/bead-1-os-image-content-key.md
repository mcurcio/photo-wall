# Bead 1: `os-image-content-key` (the tracer)

**Design:** `docs/central-idempotent-jobs.md`: rule 1, §4 (the `.deb` and OS image rows), §5, §8
(migration 028), §13 (the tracer). Read those sections only.
**Base branch:** `claude/central-followups`. **Follows:** none.
**Followed by:** beads 2, 3 and 4, which all branch from this bead once it lands.
**Packages:** `central/kernel`, `central/assets`, `central/content_catalog`, `central/infra`,
`central/migrations`, `central/content_routes.py`.

## Behaviour

- An OS image's identity is the sha256 of its base tarball: the manifest's `base_image.sha256`,
  already stored as `app_releases.base_tarball_sha256`.
- Its file is `os-images/base-<tarball sha256>.squashfs`.
- A re-cut is a new key: the sync retires the tag's old key and references the new one.
- Facts are never cleared, and retiring a reference never deletes the asset row.
- Any reference may supply the bytes. Production tries every reference, newest first, and falls
  through on a rejection, for both kinds.

## Frozen page

**Kernel**
- `FetchOsImage` (`central/kernel/job_types.py`): its one field is `tarball_sha256: Sha256`; `tag`
  is removed. The job name stays `os_image.fetch`.
- `AssetRecords` (`central/kernel/ports.py`):
  - `forget_produced` is deleted.
  - `retire`'s docstring: "Delete that reference; the asset row and its facts stay; absent ->
    no-op."
  - `Candidates` is **unchanged**.

**Assets**
- `WriteFn` becomes `Callable[[Path, OriginLocator], Awaitable[None]]` (`central/assets/production.py`).
- `AssetProduction.produce` owns the reference loop that today lives in
  `FetchPackageHandler._write` (`central/assets/handlers.py:66-79`):
  - try each reference, newest first;
  - on `OriginRejected`, try the next reference;
  - remember an `OriginUnavailable`, and raise it if no reference succeeds;
  - if every reference is rejected, raise `TerminalFailure("all_references_rejected")`.

  This is safe because every download is checked against its locator's sha
  (`central/origins/github.py:280-283`). The digest checks follow as today.
- `FetchPackageHandler._write(temp, locator)`: one download.
- `FetchOsImageHandler._write(temp, locator)`: one tarball download, then extraction.
- Delete `_recut_since` and the `reference_changed` branch. `not_reproducible` stays, and now means
  a bug.
- `central/assets/layout.py`: the code is unchanged. The docstring says OS images are named by
  tarball sha.

**Catalog** (`central/content_catalog/catalog.py`)
- `NetbootCandidates(Candidates)`: a frozen dataclass defined in the catalog, adding
  `tags: tuple[str, ...]` in the same order as `jobs`. `.deb` candidates stay plain `Candidates`.
- `_resolve_base` returns `NetbootCandidates`:
  - candidates follow `choice.tags` order;
  - there is **one per distinct tarball sha**, tagged with the first tag that maps to it;
  - two tags sharing a tarball must not raise `duplicate_candidate`.
- `record_served(self, request: NetbootBaseRequest, resolution: NetbootCandidates,
  job: FetchOsImage) -> None` records `resolution.tags[resolution.jobs.index(job)]`.
- `desired_in` builds `FetchOsImage` from the row's tarball sha.
- `pin` does the same, and **skips the OS-image fetch when the release has no OS image**. Today it
  publishes one unconditionally (`:296`).

**Route** (`central/content_routes.py`): `netboot_base` calls
`catalog.record_served(base, resolution, served.job)`.

**Sync** (`central/content_catalog/sync.py`, `_record`)
- The image key comes from `release.os_image.sha256`; the reference is otherwise as today.
- When the previous row's image sha differs, or the image was dropped, retire
  `(os-image, previous sha, tag)`. This mirrors the `.deb` branch.
- `_forget_recut_image` is deleted. Update the docstring.

**Infra** (`central/infra/asset_records.py`)
- `retire`: one `DELETE FROM asset_references ...`. There is no asset-row lock and no row
  deletion.
- `reference`: the asset-row insert becomes `ON CONFLICT (kind, identity) DO NOTHING`. With no row
  deletion there is nothing to lock against; delete the `:69` comment and the module docstring's
  concurrency note.
- `forget_produced`: deleted.

**Migration `central/migrations/028_os_image_content_key.sql`**
1. `DELETE FROM assets WHERE kind = 'os-image'` (the references cascade).
2. Insert one `assets` row per DISTINCT `base_tarball_sha256`, and one reference per tag (owner =
   tag, locator = the tarball facts, expected facts NULL). Use exactly 021's validity filter.
3. Carry **no** produced facts. Delete `job_outcomes` rows whose `job_name = 'os_image.fetch'`.
4. In a `DO $$ ... IF to_regclass('procrastinate_jobs') IS NOT NULL` block, as in 022, for
   `task_name = 'photo_wall.os_image.fetch'`:
   - set `todo` rows to `cancelled`;
   - set `doing` rows to `failed`.

   Otherwise a stranded old-shape `doing` row fails to decode in rescue every minute, forever
   (`central/infra/queue_ops.py:80-84`).
- The header comment states the rollback: revert the code, then
  `UPDATE app_release_poll SET etag = NULL`.

## Files

- **Code:** `central/kernel/job_types.py`, `central/kernel/ports.py`,
  `central/assets/production.py`, `central/assets/handlers.py`, `central/assets/layout.py`,
  `central/content_catalog/catalog.py`, `central/content_catalog/sync.py`,
  `central/content_routes.py`, `central/infra/asset_records.py`,
  `central/migrations/028_os_image_content_key.sql` (new).
- **Tests:** `tests/test_os_image_content_key.py` (new), plus the carried files below.

## Existing tests it carries

- `FetchOsImage(tag=...)` becomes `FetchOsImage(tarball_sha256=...)` in:
  - `tests/test_assets_handlers.py`, `tests/test_assets_reader.py`, `tests/test_content_routes_http.py`;
  - `tests/test_content_catalog_catalog.py`, `tests/test_content_catalog_sync.py`;
  - `tests/test_infra_execution.py`, `tests/test_infra_job_queue.py`, `tests/test_infra_queue_ops.py`;
  - `tests/test_kernel_jobs.py`, `tests/test_kernel_ports.py`, `tests/test_publisher_conformance.py`;
  - `tests/test_netboot_e2e_wire.py`, `tests/test_netboot_fresh_install_e2e.py`;
  - `tests/test_infra_catalog_records.py`, `tests/content_db.py`.
- `tests/test_assets_handlers.py`: the `.deb` fall-through tests now exercise `AssetProduction`,
  for both kinds.
- `tests/test_assets_production.py`:
  - `:240` (`..._recut_meanwhile_is_discarded_and_retried`): delete it;
  - `:257`: rewrite as "a re-cut is a new key and is produced".
- `tests/test_infra_asset_records.py`:
  - `:90`: retire keeps the row and its facts, and `get` returns None;
  - `:114` (`forget_produced`): delete it.
- `tests/test_content_catalog_sync.py:252`, `:281` and `:291`: the OS-image re-cut, unchanged bytes
  and a dropped image, now keyed by sha.
- `tests/test_content_catalog_catalog.py:287`, `:295` and `tests/test_content_routes_http.py:168`:
  the new `record_served`. Add a test that pin skips an OS image a release does not have.
- `tests/test_two_pods.py` (CI only, since it skips locally without the database):
  - `:186`: `args->>'tag'` becomes `args->>'tarball_sha256'`, with the release's `tarball_sha`;
  - `:213`: wait for `identity = release.tarball_sha`;
  - `:325`, `:342`, `:386`, `:410`: `base-{release.tarball_sha}.squashfs`;
  - `:435`: the job query uses the sha.
- `tests/test_migration_carry_served_package.py`: must still pass across 028.

## Acceptance criteria (`tests/test_os_image_content_key.py`, PostgreSQL, fake origin)

1. **T1 re-cut:** v1 at tarball S1 is synced and produced, then re-cut to S2. Expect:
   - v1 references only `(os-image, S2)`;
   - S1's row keeps its facts;
   - after S2 is fetched, the boot route serves S2's bytes with S2's digest.
2. **T2 zombie on the old key:** an S1 handler is held mid-download while v1 is re-cut and S2 is
   produced. Then S1's run finishes. Expect: S2's file and facts are unchanged, and the route
   still serves S2.
3. **T3 flip-flop:** the listings S2, then S1, then S2. Expect: S2's facts survive, and S2's tarball
   is downloaded once.
4. **T4 shared tarball:** two tags share one tarball. Expect one asset row, one file, and one
   candidate, tagged with the preferred tag.
5. **T5 fallback:** as T4, but the newest reference's URL answers 404. Expect: the fetch succeeds
   from the other tag's URL.
6. **T6 substitute:** an unpinned device served its known-good image. Expect: `last_served_tag` is
   the known-good tag.
7. **T7 migration:** on the 027 schema, with `(os-image, tag)` rows holding facts, an
   `os_image.fetch` note, and one `todo` and one `doing` old-shape row. After 028:
   - rows are keyed by sha, with no facts and no note;
   - the `todo` row is `cancelled` and the `doing` row is `failed`.

## Mutation probes (each must turn the named test red; restore by reversing the edit)

- M1: skip retiring the previous image key (T1).
- M2: `retire` deletes the last reference's asset row (T3: S2 is downloaded twice).
- M3: `produce` uses only `references[0]` (T5).
- M4: drop the one-candidate-per-tarball rule (T4: `duplicate_candidate`).
- M5: `record_served` records `resolution.tags[0]` (T6).
- M6: carry `produced_*` in 028, or leave the `doing` row (T7).

## Report back

Report the net line delta, the reuse you considered, and any errata appended to `.claude/errata.md`.
**Report where the spec is wrong.**
