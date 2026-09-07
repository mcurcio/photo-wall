# Player execution module

Status: implemented platform-independent, stateless execution leaf. Recording-renderer tests establish semantics; native and physical evidence remain separate.

`player/executor.py` accepts authenticated current-session configuration, Plans, commits, and revocations; enforces epochs, revisions, binding generations, leases, clock health, readiness, and exact assignment identity; owns process-local cache pins; and drives the Renderer. It imports no central, media, database, queue, appliance, or persistence package.

## Authority and API

The constructor is `Executor(player_id, cache, renderer, clock, mapping, prepare_lead=5)`. It has no state path and never restores identity, plans, commitments, execution history, cache metadata, pins, or observations.

- `accept_configuration`, `accept_plan`, `accept_commit`, and `accept_revocation` accept only models delivered through the authenticated Player service. Exact duplicates are idempotent; stale or mismatched Player, epoch, Plan, revision, readiness sequence, binding, and validity are rejected.
- `resolve_cached` checks an optional surviving content-addressed file before any media request. The cache verifies its exact size and SHA-256 and then creates only a process-local pin. Missing or corrupt files remain unsecured.
- `acquire` streams bounded bytes into the disposable cache without holding the execution lock, verifies completeness, and rechecks current authority before reporting success.
- `verify_secured` rehashes secured files away from the render thread. Loss invalidates preparation/readiness and triggers reacquisition of the same central assignment.
- `prepare_imminent` and `tick` run on the Renderer owner thread. They do no download or file hashing. Preparation, whole-Player capacity, commitment, and successful presentation remain distinct facts.
- `readiness` returns an epoch-local increasing sequence. Every central Commit names the exact readiness sequence it authorized. `invalidate`, `cancel`, and `release` revoke the corresponding process authority and release resources.

The Executor is the sole local authority owner. Player service workers may fetch bytes and pass typed messages, while the Renderer may prepare and present immutable compositions; neither may independently select content or extend an execution lease.

## Session reconciliation

Every Player process starts empty and enrolls with fresh credentials. Central returns the current equipment configuration and a higher authority epoch. Accepting a newer epoch clears all assignments, commits, observations, retained pictures, readiness history, and process-local pins before current state is rebuilt.

Within one epoch, the Plan ID is stable and revisions increase. Configuration or binding-generation changes invalidate incompatible work. Exact secured media identity cannot change in a rolling revision. Omission from a complete newer Plan revokes execution immediately. Cancellation prevents an older in-flight download or duplicate message from resurrecting authority.

No local journal bridges a crash. A restarted process initially shows black, establishes fresh clock health, enrolls, validates any surviving cache candidates, and reacquires anything missing. Central retained assignments determine the exact content; cache loss never rerolls it.

## Timing, continuity, and presentation

Only active committed layers execute. Preparation is limited to current and imminent work. The authenticated `/v1/player/time` mapping gates readiness and local scheduling. A stale, stepped, or uncertain mapping prevents new clock-dependent work while already authorized visible output may remain until its lease expires.

Layer start, end, media origin, fades, and ordering remain central facts. Covered video resumes at current logical position. A successful compatible opaque still may be retained for the current process and binding only when central authorizes retention. Cache presence or preroll alone cannot become fallback. Failed replacement preparation or presentation preserves a still-valid existing composition; expired or revoked authority falls through to an authorized retained still or black.

Running-process control outages may preserve the current authorized composition. Process restart and cold boot require central connectivity and fresh authority. The Executor makes no reboot-survival promise.

Tests cover two Outputs, foreign Player rejection, secured-before-commit behavior, cache survivor validation, corruption/loss, cancellation during acquisition, imminent preparation, capacity, stale/duplicate/reordered messages, epoch rotation, clock step/staleness, current video reveal, fades, failure retention, expiry, preview expiry, and renderer resource release. These tests do not establish Pi capacity, PXE, HDMI continuity, or visible synchronization.
