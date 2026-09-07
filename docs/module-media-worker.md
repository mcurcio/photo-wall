# Central media worker

Status: Procrastinate-backed central worker implementation. It connects [source/job metadata](module-media.md), [preparation](module-media-preparation.md), and [authoritative storage](module-media-store.md). PostgreSQL and the task queue are central-only dependencies.

## Boundary and queue ownership

The central scheduler creates a domain `media_jobs` record and defers `photo_wall.media.prepare` through `ProcrastinateMediaQueue.enqueue_in(conn, job_id)` using the same caller-owned Psycopg transaction. The domain request and queue job therefore commit or roll back together. Production has no in-process fallback queue and no second polling dispatcher. Unit tests may inject a small queue port that records the same atomic enqueue call.

Procrastinate owns dispatch, its worker heartbeat state, task attempts, retry timing, queueing locks, and stalled-job semantics. Photo Wall does not schedule parallel heartbeat or stalled-retry tasks. The media repository retains the preparation/publication lifecycle that is meaningful to the Planner: requested recipe, capacity reservation, exact attempt token, publication journal, ready/failure state, references, and stale-attempt fence. A queue retry never bypasses the repository lease or grants media readiness by itself.

The worker app uses queue `photo-wall-media`. Preparation jobs use a per-domain-job queueing lock so duplicate scheduler requests converge. Preparation and maintenance share `photo-wall-media-storage`, keeping the authoritative filesystem's single-writer requirement while allowing worker concurrency for independent tasks. Explicit and periodic source refresh share `photo-wall-media-refresh`. The default worker concurrency is four.

## Source refresh requests

The authenticated `POST /v1/operator/sources/{source_ref}/refresh` operation accepts a request for one configured Source. It increments that Source's persisted `refresh_requested_revision` and calls `MediaTaskQueue.enqueue_refresh_in(conn, source_ref)` in the same PostgreSQL transaction. A missing Source or unavailable queue fails the request; enqueue failure rolls back the revision. The 202 receipt contains `source_ref`, `requested_revision`, `completed_revision`, and `coalesced`. It acknowledges accepted work, without claiming that the upstream request has finished.

Queued requests for one Source may coalesce under its queueing lock. Each caller still receives its own requested revision. A running refresh cannot absorb a later request merely by finishing: its repository lease captures the requested revision at acquisition. The exact-source task continues until the persisted completed revision catches up, with bounded retries for a busy or superseded lease. Requests arriving during upstream I/O remain pending for another refresh. Work and revisions survive central or worker restart.

Both explicit and periodic refresh acquire the same repository lease, identified by Source, generation, and start time, with bounded expiry for recovery after worker loss. No database transaction spans upstream I/O. Publication checks that lease identity before changing catalog state and advancing `refresh_completed_revision` through the revision captured by that lease. A replaced attempt cannot overwrite the newer catalog or acknowledge its requests. Source, connection, and membership validation still apply.

Completion means an outcome was published. That outcome can be `ok`, `permission`, `unavailable`, or another bounded source failure; callers assess status separately. Last successful membership remains distinct from latest status. `/v1/operator/media` exposes both revision counters with that status. The [demo](module-wall-demo.md) uses them to wait for its exact accepted request; queue delivery alone and an unrelated Source's refresh are insufficient evidence.

Periodic refresh remains the ordinary mechanism for discovering live upstream changes. It can also satisfy pending revisions through the same lease and publication boundary. Explicit requests provide deterministic observation after an operator action or acceptance fault injection; they do not replace the runtime schedule or authorize content selection.

## Configuration and tasks

`load_connections(path)` reads a private JSON document with `schema: 1` and at most 128 connections. It must be a nonsymlink regular file owned by the worker with mode 0600 and no larger than 1 MiB. Duplicate fields/IDs, nonfinite JSON, changed files, and unknown fields fail with bounded codes. Credentials remain only in process and adapter memory; diagnostics exclude file contents, identifiers, URLs, tokens, and raw exceptions.

`MediaWorker.process_job(job_id, attempt=...)` is the task execution seam. It registers `Preparer.describe_recipe()`, claims exactly the named domain job, validates its recipe and attempt token, uses the existing reservation, creates private staging paths, obtains the exact original, prepares it, and publishes through MediaStore. No database transaction spans upstream I/O or native preparation. Publication rechecks the attempt token, so an expired or superseded task cannot publish or clean up a newer attempt.

`media.task_queue.create_worker_app(dsn)` defines these tasks:

- `photo_wall.media.prepare`: execute one exact job ID with bounded retry.
- `photo_wall.media.refresh`: refresh due active source metadata every 30 seconds.
- `photo_wall.media.refresh_source`: complete persisted requests for the named Source, independently of its next periodic deadline.
- `photo_wall.media.maintenance`: recover publication state and collect storage every five minutes under the storage lock.

Photo Wall records bounded last-activity/error status when these domain tasks run; it does not maintain an independent liveness loop. Procrastinate's own worker state is the queue-worker liveness source.

The entry point is `python -m media.worker`. It reads `PHOTO_WALL_DATABASE_URL`, `PHOTO_WALL_MEDIA_ROOT`, and `PHOTO_WALL_CONNECTIONS_FILE`, applies Photo Wall migrations and Procrastinate's versioned schema, then runs the Procrastinate worker. SIGINT/SIGTERM use the task runner's graceful shutdown behavior.

## Retry and failure behavior

Each acquisition retains the existing 330-second outer deadline, with tighter adapter and preparation bounds. A repository lease is at least 30 seconds longer. Original integrity, size, invalid metadata, and unsupported preparation results are permanent for that original/recipe. Operational failures raise the typed `RetryableMediaTask`; Procrastinate retries after 5, 15, and 60 seconds, then stops after the fourth failed attempt. Retries are new queue attempts and new repository leases, never recursion or an independent `retry_at` dispatcher.

On storage pressure, the worker collects unreferenced content to leave room for the worst original-plus-derivative reservation, then attempts the exact job once more. References and failed-cleanup accounting remain protected. A collected result is not ready until the scheduler explicitly requests and atomically defers it again.

Cancellation waits for adapter/preparer cleanup before releasing the storage lock. Stale attempt cleanup is refused. Publication recovery remains because database and filesystem publication cannot be one transaction; this is domain recovery, separate from queue delivery recovery.

## Acceptance boundary

Focused PostgreSQL tests cover atomic enqueue with caller commit/rollback, exact job execution, bounded typed retries, task registration, Procrastinate worker consumption, publication recovery, capacity pressure, and stale-attempt publication/cleanup refusal. Source-refresh checks cover persisted requested/completed revisions, coalesced requests, enqueue rollback, exact-source dispatch, requests during active work, periodic/explicit lease races, and stale publication refusal. Generated fixtures establish orchestration and storage behavior. Real Immich, exact-image conversion, and physical presentation remain separate acceptance work.
