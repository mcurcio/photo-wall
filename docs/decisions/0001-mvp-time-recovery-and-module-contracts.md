# MVP time, recovery, and module contracts

Status: accepted for implementation; physical and integration qualification pending.
Date: 2026-09-05. Decision owner: implementation orchestrator, subject to independent review.
Resolves initial D02–D05, D07, D09, D11 policy; narrows D01/D06/D08/D10/D12 implementation choices without claiming qualification.

## Time before persistence

All wire and durable execution instants are UTC Unix seconds (finite numbers). Installation calendar input uses an explicit IANA timezone and is converted to UTC; ambiguous/nonexistent local times require an explicit offset. Initial Programs contain concrete UTC windows; recurrence authoring can expand windows later without changing execution. A Program's stable ID together with its window start is the activation identity, so reconciliation after restart cannot duplicate it. Logical Run position is `now - started_at`, never pipeline elapsed time. Covered Runs continue at that position. Descendant definitions, sources, and typed participants are snapshotted at root admission; live query **results** remain mutable data. Delayed children keep the admitted tree's revisions and fixed Frame roles.

System UTC disciplines clocks; each Player maps a received UTC sample onto its own monotonic clock. A detected step greater than 250 ms or uncertainty greater than 100 ms invalidates new coordinated execution readiness until remapped. Clock samples expire after 30 seconds and must be refreshed from measured clock health; nonfinite samples fail closed. Already visible content is retained. On reboot, discard the monotonic mapping, establish clock health, and reconcile before accepting new authority. These are conservative initial control thresholds, not measured visible synchronization guarantees.

Current lifecycle advancement and future projection use the same deterministic domain transition function. Projection runs on a copy and returns data only: no mutations of current Runs, writes, media acquisition, or Actuator calls. Natural Program expiry stops new cycles/children, lets current work and children finish, then runs an optional outro and releases. A successor starts at its own scheduled time and outranks its outgoing background; an independently activated overlay retains its own lifetime. Cancellation affects a selected subtree only. Missed transient cues are never replayed; current continuous Actuator values are reconciled.

Default repeat behavior ignores an active root of the same Scene. Explicit restart cancels that root; bounded queue has 16 entries and a caller-supplied expiry. Priority wins; equal priority uses later admission order. Protection reserves all participating Frames against a conflicting root unless force is explicitly requested; manual activation does not imply force. Actuators arbitrate independently. A parent's target union is a reservation, not an instruction to its later children's targets.

## Failure and reboot before storage

An authorized, prepared assignment can execute within its validity interval during an outage. Future plans have a configurable 300-second horizon by default. On horizon exhaustion, retain the most recent compatible still on each Output; otherwise use local configured black fallback. Expired overlays cannot stay on top forever. Videos stop at their authorized interval; expired instructions and missed cues are not replayed. A cold reboot requires central reconciliation before playback authority, while its kiosk fallback remains black. Thus warm outages can retain pictures indefinitely within healthy process/storage limits, but this MVP does not promise offline cold boot or a numerical outage guarantee.

Exact bytes become pinned before reporting `secured`; central receipt locks that assignment's content in a transaction. Lock and execution authorization are separate states. Playback preparation, aggregate capacity, clock health, and current binding authority must additionally pass before commitment. Deadline failure skips the coordinated work and retains fallback; it does not silently reroll locked content. Unsecured dynamic work may be replaced by an eligible candidate. Authored alternatives stay on their authored Frame. A returned readiness failure invalidates execution authorization and reaches both planning and lifecycle ownership.

Player cache pins cover scheduled-and-secured work, committed work, current picture, and one retained valid still per Output. Expired/released work releases its assignment pin; a long Run does not pin its history. Downloads go to bounded temporary files and are size/hash verified before atomic publication. Storage pressure rejects acquisition rather than evicting pins. A crash between blob rename and indexing leaves an untrusted orphan, not readiness.

## Storage and deployment

Use Python 3.12, FastAPI/Pydantic, PostgreSQL 16, a separate media worker, and one active central scheduler. PostgreSQL transactions own admission/deduplication, binding retirement, content lock, and commit records. Projections can be rebuilt; Run identities/snapshots and locked assignments cannot. SQLite owns the Player's bounded cache index and accepted state. OS clock/compositor/supervision are outside the single Python Player process; networking, cache, execution, and embedded GStreamer/GTK stay inside it.

Media metadata and acquisition exist only centrally. Player contracts carry content hashes and same-origin relative gateway paths, never upstream URLs, queries, credentials, or redirects. Reject redirecting gateway responses and mismatched content. Initial media profiles are silent SDR stills and H.264 video; eligibility uses original dimensions before derivative generation. Landscape stills are ineligible for portrait Frames, and video below 1080 source pixels on its shorter dimension is ineligible on Frames at least 40 inches. Empty eligible pools retain valid still/fallback without relaxing policy.

## Delegation contracts and authoritative owners

| Module / owner | State and inputs | Outputs / invariants / checks |
|---|---|---|
| `contracts` / root | Strict versioned transport models, typed targets, Frame profiles, exact variants, calibration, plans/readiness; time interface | The only wire representation; no upstream knowledge. Validate finite times, IDs, same-origin paths, intervals and revisions. |
| `central/runtime.py` / runtime leaf | Scene tree snapshots, activation ledger, Programs, Runs; explicit `now` | Current contributions and side-effect-free projections, deterministic serialization/restart. No transport, DB, selection or physical effects. Check nested/cancel/expiry/repeat/protection/calendar/current-position semantics. |
| `central/planner.py` / later planning leaf | Runtime contributions, live catalog, Frame profiles, locked assignments | Pure eligible selections within bounded horizon; keeps locked identity, enforces authored roles. Acquisition and failures returned to coordination. |
| `central` API/registry/repository / root | PostgreSQL, operator configuration, automatic enrollment, equipment observations | Authenticated control/media, persistent Frames and replacement generations, transactional readiness/commit; single scheduling authority. |
| `media` / later media leaf | Versioned central source query and selected asset | Bounded refresh/acquisition/FFmpeg preparation and exact bytes; no Player integration. Real declared-version integration gate. |
| `player/cache.py` / cache leaf | Neutral variant and supplied byte iterator; byte limit, SQLite | Atomic validated acquisition, explicit pins/release/LRU, restart integrity. No networking source selection or renderer decisions. |
| `player` execution/rendering / later leaf | Authenticated current bindings/plans, cache, monotonic mapping | Prepared/capacity/observed feedback separately; persistent surfaces, retained content, reveal seeks. Renderer protocol has a recording substitute; physical claims require Pi evidence. |
| `appliance` / later leaf | One common revisioned image plus public installation trust config | Automated discovery/enrollment; no shared permanent secret; PXE and update/rollback artifacts remain outside Git and must be boot tested. |

Root owns integration and shared contract changes. Parallel siblings have disjoint files. Complex leaves must decompose/design before implementation; they may delegate when slots permit. Independent reviews inspect requirements, architecture, and final correctness. Spark is absent from this runtime's supported subagent models; supported Luna is the fallback for suitable bounded work and Astra for design/review.

## Remaining trust and hardware decisions

The PXE network is a centrally controlled provisioning trust boundary; unauthenticated DHCP/TFTP cannot establish hardware identity against a hostile LAN. A common image may include public installation CA/configuration and must not include a permanent shared fleet secret. Automatically generated per-device keys plus central unbound registration and separately authorized Frame binding are required. Credential issuance/rotation, boot artifact trust, local storage installation and update rollback require a further D10 record before appliance implementation. Do not present a MAC address as cryptographic identity.

Physical dual-output/coordination/PXE evidence, image boot test, declared-version Immich integration, and repository-owner license/security contact remain explicit gates. This decision does not replace [requirements](../requirements.md) or [validation](../validation.md).
