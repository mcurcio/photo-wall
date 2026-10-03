# Central media worker

Status: Procrastinate-backed central worker implementation. It connects [source/job metadata](module-media.md), [preparation](module-media-preparation.md), and [authoritative storage](module-media-store.md). PostgreSQL and the task queue are central-only dependencies.

## Boundary and queue ownership

The central scheduler creates a domain `media_jobs` record and defers `photo_wall.media.prepare` through `ProcrastinateMediaQueue.enqueue_in(conn, job_id)` using the same caller-owned Psycopg transaction. The domain request and queue job therefore commit or roll back together. Production has no in-process fallback queue and no second polling dispatcher. Unit tests may inject a small queue port that records the same atomic enqueue call.

Procrastinate owns dispatch, its worker heartbeat state, task attempts, retry timing, queueing locks, and stalled-job semantics. Photo Wall does not schedule parallel heartbeat or stalled-retry tasks. The media repository retains the preparation/publication lifecycle that is meaningful to the Planner: requested recipe, capacity reservation, exact attempt token, publication journal, ready/failure state, references, and stale-attempt fence. A queue retry never bypasses the repository lease or grants media readiness by itself.

The worker app uses queue `photo-wall-media`. Preparation jobs use a per-domain-job queueing lock so duplicate scheduler requests converge. Preparation and maintenance share `photo-wall-media-storage`, keeping the authoritative filesystem's single-writer requirement while allowing worker concurrency for independent tasks. Explicit refresh jobs use `photo-wall-media-refresh:<source_ref>` execution locks, so one Source is serialized without blocking independent Sources. Periodic ticks use their queueing lock to coalesce pending ticks, have no queue execution lock, and rely on the same per-Source repository lease and `SKIP LOCKED` selection as explicit work. The default worker concurrency is four.

## Source refresh requests

The authenticated `POST /v1/operator/sources/{source_ref}/refresh` operation accepts a request for one configured Source. It increments that Source's persisted `refresh_requested_revision` and calls `MediaTaskQueue.enqueue_refresh_in(conn, source_ref)` in the same PostgreSQL transaction. A missing Source or unavailable queue fails the request; enqueue failure rolls back the revision. The 202 receipt contains `source_ref`, `requested_revision`, `completed_revision`, and `coalesced`. It acknowledges accepted work, without claiming that the upstream request has finished.

Queued requests for one Source may coalesce under its queueing lock. Each caller still receives its own requested revision. A running refresh cannot absorb a later request merely by finishing: its repository lease captures the requested revision at acquisition. The exact-source task continues until the persisted completed revision catches up, with bounded retries for a busy or superseded lease. Requests arriving during upstream I/O remain pending for another refresh. Work and revisions survive central or worker restart.

Both explicit and periodic refresh acquire the same repository lease, identified by Source, generation, and start time, with bounded expiry for recovery after worker loss. No database transaction spans upstream I/O. Publication checks that lease identity before changing catalog state and advancing `refresh_completed_revision` through the revision captured by that lease. A replaced attempt cannot overwrite the newer catalog or acknowledge its requests. Source, connection, and membership validation still apply.

Completion means an outcome was published. That outcome can be `ok`, `permission`, `unavailable`, or another bounded source failure; callers assess status separately. Last successful membership remains distinct from latest status. `/v1/operator/media` exposes both revision counters with that status. The [demo](module-wall-demo.md) uses them to wait for its exact accepted request; queue delivery alone and an unrelated Source's refresh are insufficient evidence.

Periodic refresh remains the ordinary mechanism for discovering live upstream changes. It can also satisfy pending revisions through the same lease and publication boundary. Explicit requests provide deterministic observation after an operator action or acceptance fault injection; they do not replace the runtime schedule or authorize content selection.

## Unsaved Source previews

The authenticated operator console can request a preview for the filters, tags and connection in an unsaved Source draft. Central validates the query and defers a worker task in the same transaction that records an opaque, short-lived preview request; the task carries only the request id. The media worker uses its private connection configuration and the same bounded Immich search and eligibility checks as Source refresh, walking each media kind once with `tagIds` when the draft has tags. The preview reads each candidate's library metadata (sizes and duration, orientation-corrected; null when unusable) but no original download, preparation, or Source/catalog write. Central then serves the request's pending, complete, or bounded failure state. The UI polls that request; queue acceptance is not a completed library observation.

A complete preview reports matching images, videos, and their total only when the entire bounded search is observed. The upstream search response's `total` is a page count for the qualified Immich version, so it cannot establish a query-wide total. A search limit, permission failure, unsupported version, lost connection, timeout, or absent worker remains a non-success or pending outcome, never a zero count. A preview describes current filter matches; Source refresh and Frame compatibility still determine whether a saved Scene has usable media. Preview data expires independently of saved Source revisions and live Run assignments.

A complete preview also records its newest 24 members. What Central **serves** and what it **stores** are split ([media module](module-media.md#tags-previews-and-thumbnails)): the served member carries only Central's one-way `asset_id`, kind, capture time and sizes; the stored member metadata adds the library id and checksum the thumbnail fetch needs, in a column no route serializes. Only the call that completes the preview then **prefetches** those members' thumbnails: it publishes one `FetchLibraryThumbnail` job each on the asset layer's FETCH queue ([central cache](module-central-cache.md)). A failed prefetch never fails the preview; a tile's own request fetches it. Expiry (600 s with no answer, stored as `failed`/`preview_expired`) and deletion an hour later are judged on the database clock (below).

## Library tag lists

The worker reads each connection's tag list with its private key and stores it in `library_tags` (migration 064; one row per connection: the list, `observed_at` for the list's own age, status and error, and `checked_at` for the last attempt). On the existing 30-second refresh tick (`photo_wall.media.refresh`), after the Source refresh, `MediaWorker.list_tags()` re-lists every connection whose last **attempt** is at least 300 seconds old by the database clock, so a failing library is asked every five minutes, not every tick. A failed re-list keeps the last list and records the failure beside it. At boot, `list_tags(boot=True)` re-lists every connection regardless of age and drops the rows of connections the worker no longer holds; there are no connection fingerprints, because repointing a connection needs a worker restart. Central's `GET /v1/operator/library/tags` reads the stored list only and never publishes work.

## One media clock

Every media time that another process compares (Central, or another worker process) is stamped and compared on the database clock, read on the connection the transaction already holds: `TransactionClock.now_in(conn)` (`central/db.py`), whose production implementation, `DatabaseTransactionClock`, runs `clock_timestamp()` on that connection and cannot hold a pool of its own. Central and every worker process compose it into `MediaRepository` (`times=`, required). It covers the worker's check-in, job and blob writes, the refresh and preparation leases, the catalog retry cooldown, preview `observed_at`/`read_at` and expiry, and the tag list's `checked_at`/`observed_at`; the operator console ages media facts against the media read's own `read_at`. The process clock stays for monotonic budgets and process-local decisions. The worker never reads `media_references.expires_at`: Central writes it (Runtime layer ends, transfer grants) and only Central expires it, on its own clock (the coordinator's `expire_pins_in` every pass, a read lease's close), so storage collection treats any reference row as protecting its blob. If Central stops running coordination passes, pins stay and their blobs are never evicted: safe for playback, at a cost in disk ([console design §42](operator-console-ddd.md#42-backend-gates)).

## Configuration and tasks

`load_connections(path)` reads a private JSON document with `schema: 1` and at most 128 connections. It must be a nonsymlink regular file owned by the worker with mode 0600 and no larger than 1 MiB. Duplicate fields/IDs, nonfinite JSON, changed files, and unknown fields fail with bounded codes. Credentials remain only in process and adapter memory; diagnostics exclude file contents, identifiers, URLs, tokens, and raw exceptions.

At each media status check-in, the worker also publishes only its configured `connection_id` values to the central media-health projection. The operator console uses those names to offer a Source connection choice. A null list means no worker has reported this projection; an empty list means a worker reported that it has no connections. This does not test upstream reachability or key permissions: Source refresh supplies that result. The projection never carries the connection document, upstream URL, owner ID, API key, or CA path. It ages with the existing worker check-in timestamp and is not a credential-management interface.

`MediaWorker.process_job(job_id, attempt=...)` is the task execution seam. It registers `Preparer.describe_recipe()`, claims exactly the named domain job, validates its recipe and attempt token, uses the existing reservation, creates private staging paths, obtains the exact original, prepares it, and publishes through MediaStore. No database transaction spans upstream I/O or native preparation. Publication rechecks the attempt token, so an expired or superseded task cannot publish or clean up a newer attempt.

`media.task_queue.create_worker_app(dsn)` defines these tasks:

- `photo_wall.media.prepare`: execute one exact job ID with bounded retry.
- `photo_wall.media.refresh`: refresh due active source metadata every 30 seconds, then re-list due tag lists (above).
- `photo_wall.media.refresh_source`: complete persisted requests for the named Source, independently of its next periodic deadline.
- `photo_wall.media.preview_source`: evaluate one unsaved, short-lived Source query and publish its bounded counts and newest-24 sample, or its failure, then prefetch the sample's thumbnails.
- `photo_wall.media.maintenance`: recover publication state and collect storage every five minutes under the storage lock; expire and delete preview rows, delete the thumbnail records no live preview selects, and sweep their files from `previews/`.

Thumbnail fetches are not media-queue tasks: every worker process also runs the asset layer's job runtime, and its `FetchLibraryThumbnailHandler` (Central's) calls the worker's injected `ThumbnailOrigin` (`media/library_thumbnails.py`), which re-checks servability and the library's version and owner, fetches the thumbnail and re-encodes it without metadata.

Photo Wall records bounded last-activity/error status when these domain tasks run; it does not maintain an independent liveness loop. Procrastinate's own worker state is the queue-worker liveness source.

The entry point is `python -m media.worker`. It reads `PHOTO_WALL_DATABASE_URL`, the single `PHOTO_WALL_CACHE_ROOT` (optional; baked default `/var/cache/photo-wall`, from which the media store's `media/` subdir is derived as a constant — [decision 0013](decisions/0013-unified-cache-root.md) retired the per-domain `PHOTO_WALL_MEDIA_ROOT`), and `PHOTO_WALL_CONNECTIONS_FILE`, applies Photo Wall migrations and Procrastinate's versioned schema, creates the cache root's `previews/` subdirectory if an existing volume lacks it (`ensure_previews_directory`), replaces its connections' tag lists, then runs the Procrastinate worker. SIGINT/SIGTERM use the task runner's graceful shutdown behavior.

### Storage ownership on shared-storage platforms

The image runs the worker as the baked non-root `wall` user (uid 10001), and `MediaStore._directory` tightens the media root to `0700`. Under Compose the `media` volume is seeded from the image's `wall:wall 0700` path, so the worker owns its root and nothing further is needed. On a platform that mounts a pre-existing, differently-owned export over the media root — a Kubernetes NFS PVC root is owned by uid 0 — the worker cannot take ownership of its own storage and would otherwise crash at startup with `media_io`.

The supported shared-storage deployment runs the worker container **as root and lets it drop itself**: the `media-worker` image ships `docker-entrypoint.sh` (installed with `gosu`) that wraps the `python -m media.worker` command, plus two environment variables:

- `PHOTO_WALL_PUID` — worker uid to own storage and run as (default `10001`).
- `PHOTO_WALL_PGID` — worker gid (default `10001`).

The entrypoint is conditional on the container's starting euid:

- **Started as root** (a `securityContext` choice, not a Compose one — e.g. `runAsUser: 0` on the pod): it validates that `PHOTO_WALL_PUID`/`PHOTO_WALL_PGID` are canonical positive decimals (empty, non-numeric, bare `0`, or any leading-zero spelling is refused with a message and a non-zero exit, so a misconfiguration can never silently keep the worker running as root or chown storage to an unintended owner), then (0013) runs `install -d -o "$PUID" -g "$PGID" -m 0700` for each derived cache subdir (`media/`, `apps/`, `os-images/`, `previews/`) under `PHOTO_WALL_CACHE_ROOT`, and drops to that uid via `gosu` before exec'ing the command. The dropped worker now **owns** its cache subdirs (it is the single writer), so `_directory` performs the strict `0700` tighten normally. This is the path a Kubernetes deployment on shared storage should use.
- **Started non-root** (Compose keeps the baked `USER wall`, `cap_drop:[ALL]`, `no-new-privileges:true`): it execs the command directly with no chown, no gosu, and no new privilege — byte-for-byte the prior behavior.

**A pure non-root + `fsGroup` deployment is not sufficient on its own.** `_directory` tolerates a root-owned, group-owned root only when it carries no OTHER-access bits, but kubelet's `fsGroup` recursively *adds* group access (`g+rwxs`, i.e. `0o2770`) and never *clears* existing OTHER bits. On storage that arrives with OTHER access (a pre-existing export, or a driver/`fsGroupChangePolicy` that leaves the world bits set), a non-root worker can neither tighten the root (it does not own it) nor safely serve from it, so `_directory` deliberately refuses with `media_perms` (500) rather than the misleading `media_io`. Starting as root and dropping via the entrypoint sidesteps this entirely: after the chown the worker owns the root and enforces `0700` itself, independent of how the volume arrived. That refusal is correct and is not reached on the root-entrypoint path.

Whether the single-writer `flock` on `.worker.lock` then succeeds is a separate property of the NFS/mount configuration (version and `lock`/`nolock`), independent of ownership, and is validated per deployment.

## Retry and failure behavior

Each acquisition retains the existing 330-second outer deadline, with tighter adapter and preparation bounds. A repository lease is at least 30 seconds longer. Original integrity, size, invalid metadata, and unsupported preparation results are permanent for that original/recipe. Operational failures raise the typed `RetryableMediaTask`; Procrastinate retries after 5, 15, and 60 seconds, then stops after the fourth failed attempt. Retries are new queue attempts and new repository leases, never recursion or an independent `retry_at` dispatcher.

On storage pressure, the worker collects unreferenced content to leave room for the worst original-plus-derivative reservation, then attempts the exact job once more. References and failed-cleanup accounting remain protected. A collected result is not ready until the scheduler explicitly requests and atomically defers it again.

Cancellation waits for adapter/preparer cleanup before releasing the storage lock. Stale attempt cleanup is refused. Publication recovery remains because database and filesystem publication cannot be one transaction; this is domain recovery, separate from queue delivery recovery.

## Acceptance boundary

Focused PostgreSQL tests cover atomic enqueue with caller commit/rollback, exact job execution, bounded typed retries, task registration, Procrastinate worker consumption, publication recovery, capacity pressure, and stale-attempt publication/cleanup refusal. Source-refresh checks cover persisted requested/completed revisions, coalesced requests, enqueue rollback, exact-source dispatch, requests during active work, periodic/explicit lease races, and stale publication refusal. Generated fixtures establish orchestration and storage behavior. Real Immich, exact-image conversion, and physical presentation remain separate acceptance work.
