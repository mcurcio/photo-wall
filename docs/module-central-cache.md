# Central cache subsystem (unified cache root)

Status: Slice 1 (netboot / `os-images`) landed on `feat/0013-unified-cache-root`
(tracer + serve-seam self-heal + orphan sweep/floor/cap). A follow-on
**device-less-fleet boot** tracer — one unpinned resolver, a base-eligibility
exclude-list, a fetch retry/backoff gate (migration `020`), and a `/readyz`
readiness probe — landed on `claude/rpi-pxe-boot-failure-1290ea`; see
[Device-less-fleet boot](#device-less-fleet-boot-one-resolver-eligibility-retry-backoff-and-readiness-0013-tracer).
The `apps/` (`.deb`) and `media/` domains become fully cache-correct in Slices 2
and 3 — **not yet**. The
design of record is [decision 0013](decisions/0013-unified-cache-root.md); this
module doc is the operator-facing summary of what it means for Central's on-disk
served assets. (Distinct from the Player-side disposable cache in
[module-cache.md](module-cache.md), which is a per-process RAM/temp cache on the Pi.)

## Responsibility and boundary

Central's served assets — photo **media** (a cache of Immich), the Player **`.deb`**
(a cache of GitHub Releases), and the **OS squashfs** (a cache of GitHub Releases) —
are treated as **one disposable cache** that the app owns and Kubernetes places.
None is a master copy; each source of truth lives upstream. The subsystem owns the
on-disk layout, miss-tolerant serving, per-domain GC, and orphan removal. It does
**not** own release discovery policy, Immich selection, or PVC placement — those
belong to the worker's release service, the media pipeline, and the deployment
respectively.

## The single cache root and its derived layout

The app reads **one optional** environment variable, `PHOTO_WALL_CACHE_ROOT`
(baked default `/var/cache/photo-wall`), and derives the three domain
subdirectories as **internal constants** — it never reads a per-domain path env.
The resolver is `central/cache_layout.py`:

| Domain | In-container path | Source of truth | Constant |
|---|---|---|---|
| media | `<cache-root>/media/` | Immich | `MEDIA_SUBDIR` |
| apps | `<cache-root>/apps/` | GitHub Releases (`.deb`) | `APPS_SUBDIR` |
| os-images | `<cache-root>/os-images/` | GitHub Releases (squashfs) | `OS_IMAGES_SUBDIR` |

Because the root is optional-with-a-default, `cache_layout.cache_root()` always
returns a path, so **release sourcing and base serving are unconditional
(always-on)** — never gated on env presence. The per-domain
`PHOTO_WALL_{MEDIA,APP,BASE}_ROOT` inputs that older decisions (0009/0010/0012)
described are **retired from the deploy contract**; the four former readers
(`media/worker.py`, `central/app.py`, `netboot_base.resolve_base_root`,
`app_release_service.from_env`) now all resolve through `cache_layout`. Relocating
one domain onto another medium is a Kubernetes `subPath` mount at the fixed
in-container subdir path; the app never sees it.

## Cache posture

Three rules make the subsystem correct while files vanish:

1. **The filesystem is a cache; the database is intent; every read tolerates a
   miss.** An absent file is normal. A miss-tolerant read enqueues a
   fetch/reprocess and returns a transient `503`; the operation resolves on retry.
   Correctness never depends on "filesystem matches database." This holds
   **per-domain once that domain's serve seam is miss-tolerant** — `os-images` in
   Slice 1, `.deb` in Slice 2, media in Slice 3.
2. **The app owns the layout; Kubernetes owns the placement** (the single root
   above).
3. **Every domain bounds itself and removes its own orphans** — a byte budget, a
   DB-authoritative GC, and a filesystem-enumerating orphan sweep. `os-images` has
   a reserved floor so nothing starves netboot.

## Ownership and the entrypoint (single writer)

The **worker is the single writer**; **central mounts the cache read-only** and
creates nothing, so central serves correctly only when colocated with a
materializing worker (the single-pod topology guarantees it).

`docker-entrypoint.sh` materializes the layout **only when the container starts as
root** (a Kubernetes `securityContext` concern, not a Compose one). It validates
that `PHOTO_WALL_PUID`/`PHOTO_WALL_PGID` are canonical positive decimals (a
single `case` rejects empty, non-digit, bare `0`, and any leading-zero spelling
such as `00`/`010`, exiting `78`/`EX_CONFIG`), then runs for each domain subdir:

```sh
install -d -o "$PUID" -g "$PGID" -m 0700 "$CACHE_ROOT/$sub"
```

`install -d` is atomic (owner+mode in one step) and non-recursive by intent — the
worker owns the files it creates, so there is nothing beneath to chown. It runs
**before** the `VOLUME` declaration in the Dockerfile (writes to a declared volume
path are otherwise discarded), then drops to `PUID:PGID` via `gosu`. Under Compose
the image runs as the baked non-root `USER wall`, `id -u` is not `0`, the root
branch is skipped, and the command execs directly. A bare, root-owned PVC root is
made writable by this step; a baked or Docker-populated volume is already writable.

## os-images miss-tolerance and self-heal (Slice 1)

The OS serve seam (`GET /v1/netboot/base`) keys off a `base_cache` DB row, not a
filesystem stat, so a **dangling `cached` row** (the row says `cached` but the file
was lost out-of-band — node eviction, admin delete, backend loss) would otherwise
`503` forever. The serve seam now **self-heals**: on an open-failure of a `cached`
row it **demotes the row and re-enqueues the tag** (`enqueue_base_fetch_in`), so the
`503` is transient and the next read regenerates from GitHub. This — not the GC
ordering — is what closes the external-loss class for `os-images` (see the
correction at [decision 0013 §"How the cache stays correct"](decisions/0013-unified-cache-root.md#how-the-cache-stays-correct-while-files-vanish-requirements-6-7);
setting `base_cache.state` before the unlink in GC's transaction only **shrinks**
the dangling window because `unlink` is non-transactional).

## os-images orphan sweep, floor, and cap (Slice 1)

The poll-tail **orphan sweep** (`central/netboot_base.py`, modeled on media
`store.recover`) enumerates the `os-images/` directory and unlinks any
`base-<tag>.squashfs` with no owning `base_cache` row. "Owning row" **includes
in-flight states** (`caching`, not only `cached`), so a file mid-fetch is never
treated as an orphan. Two guards enforce the **never-sweep-mid-fetch** invariant
without a shared writer lock (`fetch_base`'s `os.replace` runs on the event loop
while the sweep runs in `asyncio.to_thread`; no lock object spans that split):

- a **per-tag ownership re-confirm** (`_base_tag_owned`) re-`SELECT`s each row
  immediately before its `unlink`, honouring a fetch that committed after the
  initial snapshot; and
- an **mtime grace** (`_ORPHAN_MTIME_GRACE_SECONDS` = 300s, 10× the fetch download
  timeout) — never unlink a file whose wall-clock mtime is within the grace of now,
  so a just-`os.replace`d file (even one whose `cached` write threw, leaving a
  `failed` row with fresh bytes) is spared; a genuine orphan ages past the grace
  and is swept on a later tick.

A missing `os-images/` dir (fresh cache root before the first fetch) yields an
empty sweep rather than a crash.

**Reserved floor — by construction, not an active gate.** `os-images` has a
reserved-floor placeholder (`OS_IMAGES_RESERVED_FLOOR_BYTES`, 4 GiB). It is
enforced **by construction**: the GC only ever removes surplus/orphan bytes and
**never a keep-set member** (0012's frozen keep-set semantics evict every non-keep
tag unconditionally), so `os-images` always retains its bootable working set. An
active floor-gate on eviction was **rejected** — gating surplus eviction on a byte
floor would retain orphaned/retired-device tags below the floor, contradicting the
frozen keep-set-departure eviction.

**Byte cap — best-effort below the keep-set, else an alarm.** The cap
(`OS_IMAGES_BYTE_CAP_BYTES`, 12 GiB placeholder) is enforced best-effort: because
all surplus bytes are already evicted, the only bytes that can exceed the cap are
the protected keep-set itself. A keep-set larger than the cap therefore surfaces an
**alarm** (`keepset_over_cap`, sizes logged) — it never evicts a protected tag and
never silently overflows. Both budgets are named placeholders pending owner
confirmation (decision 0013 decision 2).

## Device-less-fleet boot: one resolver, eligibility, retry backoff, and readiness (0013 tracer)

This tracer keeps a cluster with **no enrolled devices** bootable, and — once booted
— **stays** bootable under file loss, eviction, and a failing upstream. Four pieces,
one invariant.

**The invariant — the GC keep-set is a superset of what the serve seam resolves.**
The unpinned / no-serial serve path, the GC keep-set, `/readyz`, and the poll-tail
self-heal all route through the **one** primitive
`resolve_unpinned(conn, base_root) -> (served_tag, want_tag)`
(`central/netboot_base.py`), and eligibility lives in the **single**
`_eligible_discovered_tags` filter. `served_tag` is the tag whose bytes to serve (or
key GC on) **now**; `want_tag` is the tag the fleet is converging **to** (it drives
the self-heal only and is never written to a device row). They differ only in the
**fallback** case: when the newest discovered tag's bytes are absent or terminally
failed, serve prefers the **newest cached-and-present eligible base** on an empty
frontier and returns it (a 200), while still *wanting* the newer tag. Because GC
keeps exactly `{served, want}` (on top of latest-verified ∪ non-retired pins ∪
non-retired known-good ∪ in-flight `caching`), GC can never evict the one base serve
is about to hand out — the failure that re-opened the outage is unrepresentable.
Guarantee strength: **construction** (a single resolver, and a keep-set derived
directly from it — there is no parallel resolver to drift). **Cost:** on a
device-less cold cluster serve can hand out an older base than the newest discovered
while the newer one is still being fetched — bootable-but-not-newest, which the
self-heal closes on the next tick.

**Eligibility — an exclude-list, orthogonal to the `.deb` (F3/F4).** A release is
base-boot-eligible when it carries base facts (`base_tarball_sha256`/`base_tarball_url`
non-null), is **not** a prerelease, and its `mirror_state` is
**`NOT IN ('withdrawn','divergent')`** (the two frozen-bytes states). It is an
**exclude-list, not an allow-list**: `undeployable`, `mirror_failed`, and `mirroring`
stay base-eligible **on purpose**, because the base OS is versioned **independently of
the `.deb`** (0009). A `.deb`-undeployable release can still carry a perfectly
bootable base; gating base eligibility on the `.deb` mirror state would re-introduce
this outage. **This orthogonality is the property a future change must not break** —
widen the exclude-list only for states that mean the base *bytes* are unusable, never
for a `.deb`-only condition.

**Retry / backoff marker (F2/F5).** `base_cache` carries three columns (migration
`020_base_cache_retry.sql`):

- `fetch_attempts` — consecutive **transient** failures; drives the backoff; reset to
  `0` the moment a fetch caches successfully.
- `next_retry_at` — earliest wall-clock a transient-failed tag may be re-fetched
  (`now + backoff(fetch_attempts)`, exponential from 30 s, capped at 1 h); `NULL`
  means eligible now / not backing off (also the value on a fresh or cached row).
- `failure_terminal` — reserved for the **six archive-integrity faults ONLY**
  (`base_digest_mismatch`, `base_member_missing`, `base_member_not_file`,
  `base_too_large`, `base_sums_too_large`, `base_sums_no_squashfs`), where re-fetching
  the same bytes could only reproduce the fault, so the self-heal skips the tag.

**`base_root_unwritable` and `base_facts_missing` are TRANSIENT, never terminal** —
they are recoverable/environmental (a repaired volume, restored facts), so they back
off and retry; marking them terminal would strand re-fetch after a volume repair. The
terminal flag is cleared on a base-facts refresh **only when `base_tarball_sha256`
actually changes** (errata E17): the steady-state poll re-discovers the same unchanged
release every tick, so an *unconditional* clear would reopen a terminal fault every
tick and the gate would never hold across polls; genuinely new bytes (a changed sha)
warrant a fresh attempt, so the belt-and-suspenders clear fires only then.

**Readiness (`/readyz`) vs liveness (`/healthz`).** `/healthz` is **unchanged** — a
pure **liveness** signal (database reachable + a recent scheduler tick); it says the
process is alive, nothing about whether any Pi can boot. `/readyz` is **new**: the same
DB + scheduler preconditions **plus netboot-servability**, resolved through the same
`resolve_unpinned`. The resolver unifies **tag selection** across the four consumers
(serve, GC, self-heal, `/readyz`) — there is no parallel resolver to drift on *which*
tag is chosen. It does **not** unify the byte-level servability check, and by design:
`/readyz` (and `resolve_unpinned`'s fallback) stat the file (`_base_servable`:
`cached` **and** the file present on disk), while the final serve gate (`_decision`)
checks the **DB flag only** (`cached` **and** a non-null `squashfs_sha256`, no file
stat) — `cached` is a DB flag, never a filesystem stat. So in the **dangling-row
window** (row `cached`, bytes vanished) the two legitimately differ: `/readyz` reports
NotReady while serve proceeds on the DB flag and then 503s via the dangling-row
self-heal (demote + re-enqueue). Both resolve to a **503**, never a wrong serve, so the
divergence is safe and intentional. Ready (200) iff liveness holds **and** either the resolved `served_tag`'s
bytes are servable (`base_cache` `cached` **and** the file present on disk) **or** the
catalog is legitimately empty (`(None, None)` — an empty cluster is Ready, not a
fault). NotReady (503) **only** when a deployable release exists but nothing is
servable — the exact prod-bug state. The split is deliberate: **a base-fetch delay
must never trip a liveness restart** (a restart cannot fetch a missing artifact — it
only thrashes); "cannot serve boots" is a drop-from-rotation + alarm condition, which
a 503 on `/readyz` signals to Kubernetes.

**Known limitation.** `/readyz` is a **global** signal built on the *unpinned*
resolver. It **cannot see a device pinned to an uncached tag**: that device 503s on
serve while `/readyz` still reports Ready, because a per-device pin is outside the
unpinned result the predicate reads. This gap is closed **only** by the deferred Shape
B below.

**Deferred — Shape B (full serve-precedence reorder).** A larger change would reorder
serve precedence to prefer-cached **across pins and all recovery arms** (not only the
unpinned path), which additionally closes the pinned-to-uncached readiness gap above.
It is **deferred, not a TODO**: its cost is that it touches the recovery arms, the
per-device `.deb` coupling (device tag → served `.deb`), and the pin tests — a
materially larger blast radius than this device-less-fleet tracer, and not needed to
unbreak the device-less boot this slice targets.

## Observability

Every eviction, GC pass, orphan removal, and miss-driven refetch emits a structured
**log line** in the slice that adds the op. OTEL **metrics** (per-domain
hits/misses/evictions/orphans-removed/refetches; bytes vs budget) are a **separate
post-Slice-1 bead** — there is no metrics seam in `central/` today, only logging —
so the log fields are the interim observability floor.

The base retry gate is also surfaced for the operator: `operator_base_status`
(`GET /v1/operator/netboot`) now reports each `base_cache` row's `fetch_attempts`,
`next_retry_at`, and `failure_terminal`. This is where a `want` tag that is backing
off or terminally failed becomes visible even while `/readyz` reports Ready — a
fallback base is serving, so the global readiness signal cannot show that the newest
tag is stuck.

## What is not yet cache-correct

- **`.deb` (`apps/`)** — the bytes route still assumes-present; it becomes
  miss-tolerant with its own GC + orphan sweep + budget in **Slice 2**, alongside
  the release-asset split and the withdraw half of the release-list sync.
- **media (`media/`)** — serve-time regeneration (distinguishing a miss from
  corruption, adding the requeue transition) lands in **Slice 3**.
- **Hand-staging removal is pending (bead B5)** — GitHub is the intended sole
  `.deb` source, but the operator hand-staging routes (`POST /v1/operator/app`,
  `PUT /v1/operator/app/current`) are **still live**; their removal is gated on a
  live-DB precondition and is not done here. Do not treat hand-staging as gone.

Between slices, an out-of-band loss in an unlanded domain (`.deb`, media) still
errors rather than self-healing — the one disclosed interim cost.

## Related documents

- [Decision 0013 — unified cache root](decisions/0013-unified-cache-root.md) (design of record)
- [PXE service](module-pxe-service.md) — the netboot transport that serves `os-images`
- [Central release / app-package contract](module-appliance-release.md)
- [Base-image auto-mirror runbook](runbook.md#base-image-auto-mirror-0012)
- [Player disposable cache](module-cache.md) — the unrelated Pi-side cache
