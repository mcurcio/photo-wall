# 0034 — Central's database: one table per thing it keeps, and a write only when something changed

**Date:** 2026-11-17 · **Layer:** module (Central's persistence: which aggregates it keeps and the tables each needs; the write load) · **Status:** accepted design, built on the branch `feat/schema-consolidation` (one pull request, slices S1 to S4 and the docs slice); [as built](#as-built) 2026-11-19: 41 tables at migration 129. **Builds on:** [0033](0033-central-write-load.md) (write what changed, node records kept a week) and [0017](0017-node-redesign-r3.md) (the Node bus).

**Decision owner:** the owner. His words below are his; everything not quoted from him is a design choice, not his ruling.

## The owner's words

| # | Words | Source | Kind |
|---|---|---|---|
| G1 | "119 database tables is insane and screams of a poor db design. File a bead to improve that" | owner, 2026-10-19 ([#117](https://github.com/mcurcio/photo-wall/issues/117)) | guidance |
| G1 | "i want you to go deeper than raw row counts and also look at the database schema holistically. the last count i heard was something like 120 different tables when there should be a couple dozen" | owner, 2026-11-17 | guidance |
| G3 | Central's write load "is the highest priority"; "yes" to `runtime_state` and `node_intake_quotas` next | owner, 2026-11-15 and 2026-11-17 | guidance |
| G4 | "every event from the node should be received and logged by Central … having Central throw away good debugging and observability data is silly" | owner, 2026-10-05 | guidance |
| G5 | "not a fan of db logic, and prefer to use managed application code" | owner, 2026-10-04 | guidance |
| Q1 | Asked whether this delivery also moves the Pi off its old HTTP reporting path; answered "A" (yes, in this delivery) | owner, 2026-11-17 | a pick, revisable |

Background rules, followed and not requirements here: one concept, one domain; one source of truth; events, not timers; one repository per aggregate; audit fields on every record; no logic in the database; no backwards compatibility; the Players self-sufficient.

## The problem

Production held **125 tables** on 2026-11-17 (115 migrations). Four belong to the job-queue library (Procrastinate). Of Central's own 121, about a third were empty or had no reader, and most of the rest were one concept spread over two to eight tables: 43 `node_*` tables, 22 of them serving only the Pi's old HTTP reporting path (`appliance/central_session`, `appliance/node`, marked retiring), whose facts already travel on the Node bus's `health`, `display`, `player` and `host` lines.

Measured on the primary the same day (5-minute and 60-second samples): about **330 row writes** and **613 transactions holding a transaction id** a minute. Each such transaction is a WAL flush at commit, and on Central's iSCSI storage a flush can stall 5 to 15 s, long enough to time out the 5 s lock wait (`lock_busy`). About two of every three of those transactions wrote no row at all: they took an id for a row lock, or for a value rewritten unchanged. Five kinds of mistake cause the load, not five tables:

| Class | Mistake | Tables (rows a minute) |
|---|---|---|
| A | An unchanged value saved every cycle | `runtime_state` 49 (only `now` moved), `node_bus_hub_looks` 29 (a "last looked" stamp every 2 s), `procrastinate_workers` 24 |
| B | Stored first, judged after | `node_evidence` 20, `node_reconciliation_work` 41, `node_display_exchanges` 20, observations 7 (99.5% restated); room-light facts 7.5, `home_assistant_entities` 7.5 |
| C | A durable counter as a rate limit | `node_intake_quotas` 48, `fleet_t0_daily_quotas` |
| D | Row locks as read guards under the Coordination lock | `FOR SHARE` in every planning and settle pass: a flush, and a wake of every Player socket, each pass |
| E | Polling run as durable jobs | Procrastinate's tables, about 60: due checks, a rescue kept twice, one worker per queue |
| F | A timer where an event exists | a 1 s scheduler tick with five retention probes; the Player's 0.5 s poll beside its socket |

## Decision

### Three design rules

1. **One aggregate, one repository; one writing context per row.** An aggregate is one thing Central keeps and changes as a whole (a Frame, a Display, a Release), with one repository and one main table. Other contexts reach it through its repository.
2. **Write only what changes.** A restated value, a "last looked" stamp or a read guard writes no row. A message is judged first, and only what it changes is written.
3. **Current state and history are different tables.** History is kept only where something reads it or the owner asked for it, with a stated retention.

### The target: 40 tables, 21 kinds of thing

Each bounded context keeps a few aggregates. An aggregate has one main table, and child tables only where its rows are many or written often.

| Context | Aggregates (main table) | Child, current-record or history tables |
|---|---|---|
| Wall layout | `walls`, `frames` (with its binding), `outputs` (one row per HDMI port: the seen Display, the documents, the picture report) | `frame_trials`, `power_holds`, `power_requests` |
| Fleet equipment | `devices` (with its generation and version), `players` (with its control session), `node_links` (cursors, adopted documents, presence), `node_boot_offers`, `node_releases` (catalogue entry, upstream observation and deployment, documents inline) | `player_contacts`, `node_states` |
| Displays and tuning | `displays` (with its open picture session and the shared-serial flag), `paper_profiles` | `picture_history`, `position_entries` |
| Show | `runtime_state`, `player_show_state` (configuration, readiness feedback, the Identify request), `coordination_groups` | `plan_offers`, `execution_commits`, `assignment_locks`, `output_losses`, `photo_selections`, `shown_photos` |
| Photo library | `library_connections` (with its lists), `library_assets` (with its revision, copies and key content), `media_sources` (with its names and drafts), `photo_pools` | `photo_pool_rows`, `photo_check_pages` |
| Assets and jobs | `assets`, `media_recipes`, `job_outcomes` | `asset_references` |
| House | `singletons`: one row per one-of-a-kind document (the house, the power policy, the room-light setup, photo fit, media settings, the planning outcome, Galleria's birth, the WALL mark, the hub's reachability, the release sync state) | `room_light_facts`, `home_assistant_entities` |
| Everywhere | | `events`: every Pi event and every refused report, one append-only table, kept a week |

**40 domain tables** (21 aggregate tables and 19 others), plus `schema_migrations` and Procrastinate's four, which this delivery does not change.

**Decided here:**
- **A Pi and its Player stay two rows.** A device row exists from the Pi's first boot offer, before any Player (`central/fleet/node_boot.py`), and a Compose or demo Player has no device. `fleet_device_lifecycle` and `player_versions` fold into `devices`; the control session folds into `players` (its one writer, `registry.py`, already locks that row).
- **Every Pi event is kept a week** in `events`, under G4 and as [0033](0033-central-write-load.md) keeps node records.
- **`now` stays in the Runtime's saved state.** Program admission reads it (`Runtime._pending_windows`). The Runtime is saved only when its state differs apart from `now`; Settings › House saves the processed instant first when it moves the zone or the place, so a window the change moved into the past is never admitted backdated.
- **The settings documents share one table.** Each document keeps its own model and repository; the table and its audit stamping are one shared module (`central/infra/singletons.py`). The cost: per-field SQL constraints on these settings become model validation (most were JSON already).

**Not merged, because each would join two concepts:** `walls` and `paper_profiles` into `singletons` (Frames and Players reference them); `picture_history` with `position_entries` ([0025](0025-configuration-snapshots.md): each owner keeps its own history); `photo_check_pages` into the pool row (a large row rewritten every check step).

### One fix per class

| Class | Fix | Slice | Rows a minute after (estimate) |
|---|---|---|---|
| A | The Runtime saved only when it differs apart from `now`; `node_bus_hub_looks` dropped (the console reads the worker's queue heartbeat, and the hub's reachability written when it changes); one Central worker beside the media worker | S1, S3 | about 12 |
| B | The old HTTP reporting path removed with its tables; room lights judged first, facts kept only for named references, the house lock taken only when the document changes | S2, S4 | about 1 |
| C | Both quota tables and their code deleted ("UX over security", owner, 2026-10-07) | S1 | 0 |
| D | Planning and settle read without row locks under the Coordination lock (as the delivery already does); locks that order a write outside that lock stay | S1 | one flush and one Player wake fewer per pass |
| E | A due check scheduled for its due time and re-armed on completion; one rescue; `releases.sync` stays periodic (GitHub sends no event), and a slow cache sweep | S3 | about 10 |
| F | The tick at the Runtime's next event; retention on a slow timer; the Player on its socket only, so `player_contacts` is written at socket open and close | S3 | about 1,000 read-only commits a minute fewer |

Total: about 330 row writes a minute down to **about 25 to 35**, and 613 flush-taking transactions down to **about 60 to 100**. Both are estimates, measured on the primary after each deploy. Every pool sets its own `application_name` (S1), so the next measurement is attributed by source, not estimated by cadence.

### Build order

One pull request; the slices are build order within it. Migrations are forward-only and number from 116.

| Slice | What it delivers | Changes the Pi |
|---|---|---|
| S1 (tracer) | The Runtime saved on change only; both quota tables and `node_bus_hub_looks` dropped; the planning and settle row locks gone; `application_name` per pool; `singletons` created with its audit fields and Settings › House moved into it | no |
| S2 | Central's consolidation: every merge and drop below marked S2; the bus link store becomes `node_links`, `node_states` and `events`; one release table; Central's 18 triggers and 2 functions dropped; audit fields on every kept table | no |
| S3 | The queue and the timers (classes E and F); one Central worker; the Player pushed over its socket, never polling | yes (the Player) |
| S4 | The old Pi path: reboot becomes a bus method and Install reads its completion from the `host` line, where the host's facts and metrics move too; the Display Host decides its handoff from the Output document, and a Frame's Position trial travels on its Output's own document key; Output losses judged from the `display` line; the `/v2/node/*` routes other than boot offers removed, with 20 tables (the four app-operation tables among them, moved from S2); the App Manager and its release root retired | yes |

## Deploying it

**Central first, then each Pi rebooted once by hand** (engineering call 2026-11-19, superseding the Pi-first order of 2026-11-18, [E2](#s4bs-errata-and-their-decisions-2026-11-18)). The release names no manager root (S4c part 2b-i), and the Central before it requires one in every release it ingests (`parse_node_release`), so the old Central cannot ingest this release and nothing can Install it through the old Central: E2's Pi-first order is impossible. The orchestrator running this delivery confirmed the Central-first order on 2026-11-19, before part 2b-i ran (an engineering call, recorded with the delivery's decisions; not the owner's ruling). The steps, and the recovery if the order breaks, are in the [runbook](../runbook.md#deploying-the-schema-consolidation-release). So:

1. The merge releases as usual (semantic-release; images and Pi packages). The old Central records the new tag as a release it cannot ingest; harmless: migration 129 forgets that record.
2. Deploy Central (an iac pull request) **stop-then-start**: every old pod stops before the new one starts and migrates (Kubernetes `Recreate`), migrations 116 to 129 running. In the same deploy, the TFTP boot tree is staged from this release: stage 1 parses a boot offer strictly, so an old `initrd.img` refuses the new offers and a new one the old, and they move together (check what the iac stager stages before deploying). Central is down for about a minute.
3. Central's release sync ingests this release and fetches its files. Migration 129 cleared every Pi's version, so each Pi is offered the newest ready stable release at its next boot: this one, once its files are in Central's cache. Until then an offer is refused and stage 1 retries.
4. Reboot each Pi once by hand (the owner's one Pi: the agent, over SSH): software from before this release has no bus reboot method, so neither Install nor the console's reboot reaches it.
5. Measure the write rates on the primary (pg_stat deltas by `application_name`).

**Why stop-then-start:** the migrations copy rows into merged tables and drop the old ones (copy-then-drop, *Refactoring Databases*, Sadalage and Ambler, in one forward migration), and an old pod's SQL fails against a merged table; rolling deploys overlap (`central/infra/node_link_store.py`).

**Costs of this order:** between steps 2 and 4 a Pi still on the old release keeps its last plan, but its Display Host and Player call routes this Central no longer serves: it may show its last picture or go dark until its reboot (not verified). A Pi that reboots by itself before step 3 finishes is refused an offer until the release is ready. Every older tag shows on the Releases page as a release that cannot be ingested. Boot offer history and calibration mode reset until each Pi's next boot. A rollback is a forward fix: the migrations are forward-only, and an older release could not run against this Central anyway.

## What it gives up

- `authored_candidates` (no rows): the Scene keeps photo ids and reads current facts.
- `execution_events`, `audit_events` and `runtime_changes`, which nothing reads. Each record's audit columns say who changed it last; the history of who changed what before that is gone.
- Home Assistant's "last heard" is written at most once a minute, not on every state.
- The settings documents lose per-field SQL constraints; their models validate them.
- A reboot Central asks of a Pi (an Install's restart or the operator's, a `RebootPi` job the worker runs over the Pi's fleet-pipe link) is kept a week, as `events` rows (the operator's ask and its outcome), where `node_reboot_commands` kept every reboot: the Pi page's reboot history shows the last week. The operator's reboot names no node session and has no offer window: it is asked once, and a Pi with no link then reads "Not delivered".
- The console's "Central has not looked at its hub for …" becomes two facts: the worker is not running (its queue heartbeat lapsed), or Central cannot reach its hub. A worker whose bus task hangs while its job loops still beat reads as running.
- A one-time enrolment nonce lives in memory; one issued before a Central restart is refused and retried.
- Central is down about a minute at the deploy.
- A release published before this delivery can no longer be ingested (it names a manager root), and migration 129 forgets every stored release, deployment and boot offer: each Pi's version is cleared and its next boot takes the newest ready stable release. Each Pi is rebooted once by hand at the deploy ([Deploying it](#deploying-it)).
- A Home Assistant reference named after its source last spoke reads "never heard" until its next state: facts are kept only for named references (S2d).
- A Display's shared serial is one flag on the Display: a (maker, product, serial) triple with no Display loses the fact until the judge sees both Outputs again. A picture session no longer ends with its Frame's deletion; it ends by its idle window (`PICTURE_IDLE_SECONDS`) (S2f).
- "Check now" and a failed library check step: an immediate check cancels a queued later one and defers anew (never absorbed by it); a step stalled with its worker is re-published by the one rescue, so the stale-step rule of the old poll is gone (S3a).
- A dead worker's jobs wait up to the one rescue's 5 minutes, where they waited 30 s (S3a).
- Central's hub session looks at its state every 60 s, where it looked every 2 s (`CENTRAL_STATE_LOOK_SECONDS`, [`central_state.py`](../../central/infra/central_state.py)): a change that sends no NOTIFY (a release download finishing, a Pi's address on the host line) can take up to 60 s to reach Galleria's releases and installation keys.
- An Output loss is written only for a disconnected display and resolves when it is connected again; a Player exit or a surface invalidation no longer writes one, and the picture's return no longer resolves one (S4b).
- A Position trial's Save needs the Pi's current statement that it shows the draft, and its bus link; no longer a presentation within 5 s (S4b).
- A Display Host restart can time a trial's lease once more from the document's redelivery: at most its 110 s hard lease, and Central clears the document at its own deadline (S4b).
- The host read's App Manager `preparation` and the device read's session and admission facts are gone (S4c-2a-iii); the local recovery deadlines and their faults are gone (S4c-2a-ii).
- The deploy window of the Central-first order: until its reboot an old Pi keeps its last plan while its programs call removed routes (it may show its last picture or go dark; not verified), a Pi that reboots before the new release is ready is refused an offer until it is, and calibration mode reads `not_admitted` until each Pi's next boot (S4c-2b-i). E2's window (a new Pi showing nothing on the old Central for minutes) does not occur, as E2's order is superseded.
- A rollback is a forward fix: the migrations are forward-only, and an older release could not run against this Central (S4c).
- **Deferred:** moving every repository onto a query builder that stamps audit fields itself. This delivery adds the columns and one repository per aggregate, so that move can later go one repository at a time.
- **Not planned:** changing Procrastinate's tables.

## Table map

S1 to S4 are the slices; "child" means written by its aggregate's repository. Rows and writes a minute are production's on 2026-11-17.

| # | Table today | Rows | Writes/min | Fate | Into | Why | Slice |
|---|---|---|---|---|---|---|---|
| 1 | node_app_effects | 0 | 0 | drop | — | Old path; empty | S4 |
| 2 | node_app_links | 1 | 0.5 | drop | node_states (`player` line) | Old path | S4 |
| 3 | node_app_operations | 0 | 0 | drop | — | Route never used | S4 (moved from S2) |
| 4 | node_app_qualification_samples | 0 | 0 | drop | — | Qualification never ran | S4 (moved from S2) |
| 5 | node_app_qualifications | 0 | 0 | drop | — | Same | S4 (moved from S2) |
| 6 | node_app_responses | 0 | 0 | drop | events | Old path | S4 |
| 7 | node_base_managers | 67 | 0 | merge | node_releases (derived from the deployment documents; gone with the manager root in S4) | Immutable release document | S2 |
| 8 | node_boot_admissions | 75 | 0 | drop | — | Old session; `calibration_mode_in` reads boot offers | S4 |
| 9 | node_boot_offers | 83 | 0 | keep | node_boot_offers | Netboot; audit fields | — |
| 10 | node_bus_hub_looks | 1 | 28.5 | drop | worker heartbeat + singletons (hub reachability, on change) | Freshness stamp | S1 |
| 11 | node_bus_presence | 1 | 0 | merge | node_links | The link's presence | S2 |
| 12 | node_bus_wall | 1 | 0.5 | merge | singletons (WALL mark) | One integer that must persist | S2 |
| 13 | node_calibration_trials | 58 | 0 | rename | frame_trials | Generation and idempotent retry read closed trials | S2 |
| 14 | node_ci_publication_heads | 0 | 0 | drop | — | No code | S2 |
| 15 | node_ci_publications | 0 | 0 | drop | — | No code | S2 |
| 16 | node_ci_revoked_qualifications | 0 | 0 | drop | — | No code | S2 |
| 17 | node_command_responses | 6 | 0 | drop | events | Reboot answer on the bus | S4 |
| 18 | node_deployment_assets | 201 | 0 | merge | asset_references (owner `node-deployment:<hex>`) | Duplicates `owner = node-deployment:<id>` | S2 |
| 19 | node_deployments | 67 | 0 | merge | node_releases | One release | S2 |
| 20 | node_display_exchanges | 42,992 | 19.5 | drop | node_states (`display` line) | Old path; 39,535 "already admitted" | S4 |
| 21 | node_display_handoffs | 2 | 0 | drop | — | The Pi decides from the Output document | S4 |
| 22 | node_display_withdrawals | 0 | 0 | drop | — | Same | S4 |
| 23 | node_environment_acceptances | 0 | 0 | drop | — | Never ran | S4 (moved from S2) |
| 24 | node_environment_catalog | 100 | 0 | merge | node_releases (derived from the deployment documents) | Immutable document | S2 |
| 25 | node_evidence | 39,335 | 19.5 | drop | node_states / events | 99.5% restated snapshots | S4 |
| 26 | node_host_facts | 74 | 0 | drop | node_states (`host` line) | Install reads the base tag there | S4 |
| 27 | node_host_observations | 7,765 | 4 | drop | node_states (`host` line) | Duplicate | S4 |
| 28 | node_intake_quotas | 14 | 46.5 | drop | — | Durable rate limit (C) | S1 |
| 29 | node_link_actions | 8 | 0 | merge | events (link-action kind) | nodeapi's action log (`LinkStore.action`); kept a week | S2 |
| 30 | node_link_cursors | 8 | 2.5 | merge | node_links | Resume point | S2 |
| 31 | node_link_documents | 6 | 0 | merge | node_links | Adopted digests | S2 |
| 32 | node_link_gaps | 662 | 0 | drop | events (gap kind) | Tests only | S2 |
| 33 | node_link_records | 86,567 | 2.5 | split | node_states (newest per key) + events (records, keyed device, stream, epoch, sequence) | Only the newest is read; a repeat is a no-op | S2 |
| 34 | node_manager_observations | 6,109 | 3 | drop | — | Old App Manager | S4 |
| 35 | node_offer_contexts | 78 | 0 | drop | — | Always `node_v2` | S2 |
| 36 | node_output_losses | 26 | 0 | rename | output_losses (Show; full fence key; cause inline) | Fences commits; its producer moves to the `display` judge in S4 | S2 |
| 37 | node_producers | 383 | 0 | drop | — | Old session; the loss cause inlined first | S4 |
| 38 | node_reboot_commands | 14 | 0 | drop | events (link-action rows: the operator's ask and the `RebootPi` job's outcome) | Reboot is a bus method | S4 |
| 39 | node_reconciliation_work | 39,134 | 39 | drop | — | A second queue for no-ops | S4 |
| 40 | node_release_catalog | 67 | 0 | merge | node_releases | 1:1 | S2 |
| 41 | node_release_observations | 67 | 0 | merge | node_releases | 1:1 | S2 |
| 42 | node_release_verifications | 2 | 0 | drop | — | Neither written nor read | S2 |
| 43 | node_sessions | 3,726 | 0 | drop | — | Old credentials; `NodeControlConfig` moves | S4 |
| 44 | app_release_poll | 1 | 0 | merge | singletons (release sync state) | One ETag | S2 |
| 45 | asset_copies | 54 | 0 | merge | library_assets | One photo's copies | S2 |
| 46 | asset_references | 335 | 0 | keep (child) | asset_references | Takes the deployment assets | — |
| 47 | asset_revisions | 21,023 | 0 | merge | library_assets | Same keys | S2 |
| 48 | assets | 340 | 0 | keep | assets | Aggregate | — |
| 49 | assignment_locks | 1 | 0 | keep (child) | assignment_locks | Keyed by assignment | — |
| 50 | audit_events | 150 | 0 | drop | — | No reader; audit columns | S2 |
| 51 | authored_candidates | 0 | 0 | drop | — (planning reads the Scene's photo ids' current facts, `MediaRepository.authored_in`) | The Scene keeps ids | S2 |
| 52 | bindings | 1 | 0 | merge | frames (`player_id`, `output_id`) | 1:1 | S2 |
| 53 | colour_sensors | 1 | 0 | merge | singletons (room-light setup) | One setup | S2 |
| 54 | colour_words | 2 | 0 | merge | room_light_facts | Facts by reference | S2 |
| 55 | coordination_groups | 0 | 0 | keep | coordination_groups | Cues | — |
| 56 | devices | 1 | 0 | keep | devices | Exists before any Player | — |
| 57 | displays | 4 | 0 | keep | displays | Takes the session and the shared serial | — |
| 58 | enrollment_nonces | 0 | 0 | memory | the enrolment handshake | One-time, short-lived; retried | S2 |
| 59 | execution_commits | 0 | 0 | keep (child) | execution_commits | Queried by cue | — |
| 60 | execution_events | 14,292 | 0 | drop | player_show_state (`observed_sequence`) + the sequence `coordination_skip_sequence` | Its uses get their own homes: a repeated observation is known by its number, a skipped group's revocation number comes from a sequence | S2 |
| 61 | fleet_device_lifecycle | 1 | 0 | merge | devices (`revoked_at` as `retired_at`) | 1:1; trigger dropped | S2 |
| 62 | fleet_t0_daily_quotas | 26 | 0 | drop | — | Durable rate limit | S1 |
| 63 | frames | 1 | 0 | keep | frames | Takes the binding | — |
| 64 | galleria_birth | 1 | 0 | merge | singletons | Must persist | S2 |
| 65 | hold_settings | 1 | 0 | merge | singletons (power policy) | House-wide | S2 |
| 66 | home_assistant_entities | 138 | 7.5 | keep | home_assistant_entities | The web reads it; written on change, heard at most once a minute | S2 |
| 67 | house_settings | 1 | 0 | merge | singletons (house) | One row | S1 |
| 68 | job_outcomes | 112 | 1 | keep | job_outcomes | The executor's status for late waiters | — |
| 69 | library_assets | 21,023 | 0 | keep | library_assets | Takes the revision, copies, key content | — |
| 70 | library_connections | 1 | 0 | keep | library_connections | Takes the lists | — |
| 71 | library_list_items | 210 | 0 | merge | library_connections | One writer | S2 |
| 72 | library_lists | 4 | 0 | merge | library_connections | Same | S2 |
| 73 | light_words | 120 | 4.5 | merge | room_light_facts (named references only) | 117 lights no Frame names | S2 |
| 74 | media_orphans | 0 | 0 | drop | — | No code | S2 |
| 75 | media_recipes | 9 | 0 | keep | media_recipes | In variant keys | — |
| 76 | media_references | 2 | 0.5 | drop | derived: an offer or secured lock naming a variant is its pin; a download protected by `assets.last_served_at` | One row set per offer revision | S2 |
| 77 | media_settings | 1 | 0 | merge | singletons | One row | S2 |
| 78 | media_source_name_versions | 5 | 0 | merge | media_sources | One Source | S2 |
| 79 | media_source_names | 4 | 0 | merge | media_sources | Same | S2 |
| 80 | media_sources | 5 | 0 | keep | media_sources | Takes the names and drafts | — |
| 81 | night_off | 1 | 0 | merge | singletons (power policy) | House-wide | S2 |
| 82 | output_displays | 2 | 0 | merge | outputs | Same port key | S2 |
| 83 | output_documents | 2 | 0 | merge | outputs | Same | S2 |
| 84 | outputs | 2 | 0 | keep | outputs | The port aggregate | — |
| 85 | paper_profiles | 3 | 0 | keep | paper_profiles | Players fetch LUTs | — |
| 86 | photo_check_pages | 0 | 0 | keep (child) | photo_check_pages | Large pages per step | — |
| 87 | photo_drafts | 0 | 0 | merge | media_sources | A Source being edited | S2 |
| 88 | photo_fit_settings | 1 | 0 | merge | singletons | One row | S2 |
| 89 | photo_key_content | 107 | 0 | merge | library_assets | One photo's facts | S2 |
| 90 | photo_pool_rows | 21,287 | 0 | keep (child) | photo_pool_rows | Membership | — |
| 91 | photo_pools | 3 | 0 | keep | photo_pools | One per query | — |
| 92 | photo_selections | 19 | 0 | keep | photo_selections | The current plan, a day | — |
| 93 | picture_documents | 2 | 0 | merge | outputs | Same port key | S2 |
| 94 | picture_kept_entries | 8 | 0 | merge | picture_history (kept kind) | Ids kept; trimmed per Display | S2 |
| 95 | picture_reports | 2 | 0 | merge | outputs | Same port key | S2 |
| 96 | picture_sent_entries | 96 | 0 | merge | picture_history (sent kind) | Trimmed per port | S2 |
| 97 | picture_sessions | 0 | 0 | merge | displays | One per Display | S2 |
| 98 | plan_offers | 1 | 0 | keep (child) | plan_offers | Keyed by revision; queried across Players | — |
| 99 | planning_outcome | 1 | 0 | merge | singletons | One row | S2 |
| 100 | player_configurations | 1 | 0 | merge | player_show_state | Coordination's, per Player | S2 |
| 101 | player_contacts | 1 | 12 | keep (child) | player_contacts | Off the players lock; socket open and close after S3 | S3 |
| 102 | player_control_sessions | 1 | 0 | merge | players (`control_session`) | Same writer and lock | S2 |
| 103 | player_feedback | 1 | 0.5 | merge | player_show_state | Per Player | S2 |
| 104 | player_output_identification | 0 | 0 | merge | player_show_state | Per Player | S2 |
| 105 | player_versions | 1 | 0 | merge | devices (`deployment_id`, `previous_deployment_id`, `install`, `version_changed_at`) | Decided at first boot | S2 |
| 106 | players | 1 | 0 | keep | players | Takes the control session | — |
| 107 | position_entries | 67 | 0 | keep | position_entries | [0025](0025-configuration-snapshots.md) history | — |
| 108 | power_holds | 0 | 0 | keep (child) | power_holds | Per Frame, pruned on expiry | — |
| 109 | power_requests | 0 | 0 | keep (child) | power_requests | The console's Test, per Frame | — |
| 110 | procrastinate_events | 245,886 | 15–22 | library | — | Shrinks with class E | S3 |
| 111 | procrastinate_jobs | 81,962 | 15–22 | library | — | A week of polls | S3 |
| 112 | procrastinate_periodic_defers | 9 | 16 | library | — | Fewer periodic jobs | S3 |
| 113 | procrastinate_workers | 4 | 24 | library | — | One Central worker and the media worker | S3 |
| 114 | reading_words | 16 | 3 | merge | room_light_facts | Facts by reference | S2 |
| 115 | report_refusals | 20 | 0 | merge | events (refusal kind) | Read by the console | S2 |
| 116 | room_lights | 3 | 0 | merge | singletons (room-light setup) | One aggregate | S2 |
| 117 | room_lights_change | 1 | 0.5 | merge | singletons (the setup's revision) | Same | S2 |
| 118 | room_lights_drawn | 1 | 2 | drop | node_states (`player` line), judged on read | A copy of a bus record | S2 |
| 119 | runtime_changes | 1 | 0 | drop | — | No reader | S2 |
| 120 | runtime_state | 1 | 55.5 | keep | runtime_state | Keeps `now`; saved on change | S1 |
| 121 | schema_migrations | 115 | 0 | keep | schema_migrations | The ledger | — |
| 122 | sensors | 1 | 0 | merge | singletons (room-light setup) | One aggregate | S2 |
| 123 | shared_serials | 0 | 0 | merge | displays (flag) | A Display fact | S2 |
| 124 | shown_photos | 27,802 | 0 | keep | shown_photos | History, 400 days | — |
| 125 | walls | 1 | 0 | keep | walls | Frames reference it | — |

**New or renamed (10):** `singletons`, `player_show_state`, `picture_history`, `room_light_facts`, `node_links`, `node_states`, `node_releases`, `events`, `output_losses`, `frame_trials`. **Kept (30):** the rest marked keep above.

## As built

Recorded 2026-11-19, after the last code slice (S4c-2b-ii). The design above stands; this section says how it was built and what the build found. Every decision here is an engineering call made during the build, not the owner's ruling.

### The tables after each migration

Counted on a fresh database migrated one file at a time, 2026-11-19 (Procrastinate's four are not in these counts). At 130 it holds **41 tables**: the 40 domain tables of [the target](#the-target-40-tables-21-kinds-of-thing) and `schema_migrations`; 45 with Procrastinate's four.

Since then (added 2026-11-22; the counts above are as they were on 2026-11-19): migration 131 ([0035](0035-operator-sign-in-through-an-identity-provider.md)) adds `operators` and `operator_sessions`, one table per aggregate as this record asks. From 131 Central holds **43 tables**, 42 domain tables and `schema_migrations`; 47 with Procrastinate's four. Migration 132 ([0036](0036-home-frame-status-and-wall-actions.md#since-when)) adds `frame_status`, one row per Frame, and one more kind in `events`: from 132 Central holds **44 tables**, 43 domain tables and `schema_migrations`; 48 with Procrastinate's four. `tests/test_migration_128.py` holds the set.

| Migration | Slice | Central's tables after |
|---|---|---|
| 115 | (production, 2026-11-17) | 121 |
| 116 | S1 | 118 |
| 117 to 120 | S2a to S2d | 110, 104, 100, 91 |
| `121_devices`, `121b_releases` | S2e | 89, 84 |
| `122_outputs`, `122b_displays_and_picture_history` | S2f | 80, 77 |
| `123_layout`, `123b_show_state` | S2g | 74, 71 |
| `124_photo_library` | S2h | 61 |
| 125 | S3b part 2 (`player_contacts.connected`) | 61 |
| 126 | S4b part 1 (`position_entries.by`) | 61 |
| 127 | S4b part 3 (`frame_trials` on the bus, `outputs.trial_*`) | 61 |
| `128_old_node_path` | S4c-2a-v (the old path's 20 tables) | 41 |
| `129_releases_without_the_manager_root` | S4c-2b-i (data only: every stored release, deployment reference and boot offer cleared) | 41 |
| `130_home_assistant_entities_audit` | End review (`home_assistant_entities` gains its audit fields, stamped `home-assistant`) | 41 |

Slices landed in parts, each its own commit and green alone: S2e, S2f, S2g, S3b and S4a in two; S4b in four (126 and `by`; Output losses from the `display` line; the Position trial on the bus; the handoff on the Pi); S4c in eight (part 1, the operator's reboot over the bus; 2a-i, the PID1 scenarios on the bus path; 2a-ii, the Pi stops speaking the old path; 2a-iii, what the console showed of it; 2a-iv, the old node routes behind no page; 2a-v, node sessions, app links and the 20 tables; 2b-i, releases and boot offers without the manager root; 2b-ii, the App Manager and `appliance/node` deleted). `Actor` gained `home-assistant` (Galleria's `installPlayer`, S2e).

### How the merged tables were built

- The four app-operation tables (`node_app_operations`, `node_app_qualifications`, `node_app_qualification_samples`, `node_environment_acceptances`) dropped in S4 with their routes and console pages, not in S2: their writers lived on the old path. So S4 dropped 20 tables, not 16.
- `node_base_managers` and `node_environment_catalog` are not copied inline: they are derived from the deployment documents `node_releases` holds. `write_deployment` checks a digest's reference against the other releases under `RELEASE_WRITER_LOCK` (and, until S4c-2b-i retired the manager root, a base's manager pins). A deployment's files are its `asset_references` rows (owner `node-deployment:<hex>`) (S2e).
- `fleet_device_lifecycle.revoked_at` became `devices.retired_at`, and `player_versions` became `devices.deployment_id`, `previous_deployment_id`, `install` and `version_changed_at` (S2e).
- `media_references`: an offer or secured lock naming a variant is its pin (`media_authorized_in` checks `valid_until` on Central's clock), and the cache keeps a file while an offer or lock row names it or it was served within `SERVED_SPARE_SECONDS` (equal to `TRANSFER_SECONDS`), stamped on `assets.last_served_at` by the grant (S2h).
- `authored_candidates`: planning reads a Scene's photo ids' current facts (`MediaRepository.authored_in`).
- `execution_events`: `player_show_state.observed_sequence` and the sequence `coordination_skip_sequence` (S2g).
- `node_reboot_commands`: `events` link-action rows, the `RebootPi` job's outcome (S4a) and the operator route's ask (S4c).
- `bindings`: `frames.player_id` and `output_id`; `player_control_sessions`: `players.control_session`.

### Errata from S2e to S2h

- `position_entries` had no `by` after S2f (its writers carried no actor). S4b part 1 added it (migration 126, existing entries `unknown`): every append is stamped with its writer, and a change made outside Photo Wall that a Save or a turn finds carries that writer's `by`, as `picture_history` does.
- Per-Player planning refusals (`offer_unavailable`, with `plan_shortened` and `offer_backpressure`) are log lines (`central/coordination.py` `_left_out`), not stored; what a pass could not plan is the planning outcome singleton. `ExecutionOutcome` and its router are deleted.
- A photo is one `library_assets` row: a new revision replaces the old one on the row and clears its copies and key content; pool rows follow it (foreign key `ON UPDATE CASCADE`). An older revision is not kept.
- A Source draft is a `media_sources` row of its own (no query, its pool, `draft`). A Source name's uniqueness is held by constraints: the unique index `media_sources_current_name` on the name among heads, and `UNIQUE (name, revision)`.
- The enrolment nonce lives in one process's memory (`central/registry.py` `_EnrolmentNonces`): this assumes one Central pod serving the API. S3b's `connected` flag rests on the same assumption (cleared when the API process starts). Two API processes would need both revisited.
- `picture_history` keeps `kind` (`kept`, `sent`) and `entry_kind` (the old kinds: `kept-done`, `kept-copied`, `kept-unrecorded`, `sent-draft`, `sent-back`, `sent-kept`) tied by a CHECK, with single-table-inheritance CHECKs on the kind-only columns.
- A Display's shared serial is the flag `displays.shared_serial`; a picture session ends by its idle window, not with its Frame (costs in [What it gives up](#what-it-gives-up)).
- A tag once catalogued keeps its manifest: another one is refused `node_release_identity_conflict`.

### S4b's errata and their decisions (2026-11-18)

- **E1.** The Output document carries the Frame binding (`OutputBinding`: Frame id, generation, configuration revision, or none), projected from the `frames` row; its `change` rises when the binding moves. The `display` line's schema major is 12.
- **E2.** No compatibility path. The deploy order was then Pi first (Install through the old Central, then Central), at the cost of a new Pi showing nothing on the old Central until Central was deployed. **Superseded** by S4c-2b-i's Central-first order ([Deploying it](#deploying-it)).
- **E3.** The Display Host learns the Player's run from the app broker, which pushes its current run (the `RunDocument` it reports as `app_started`, `app_epoch` included) over the Display Host's root-only ingress (`app_run`) when the run or the ingress socket changes; never through Central. The ingress socket became mode `0660`, group `pw-display`, and the broker joined that group (it has no `CAP_DAC_OVERRIDE`). Rejected: the broker's node feed (polled, retiring), the Player line (no `app_epoch`, and a self-report), dropping `app_epoch` (it reaches the health judge).
- **E4.** The Position trial rides `trial-<port>` keys on the `display` line (Central's `TrialDocument`, the Pi's `TrialReport`), its leases as durations timed on the Pi from receipt; the candidate hash covers the trial's scope (Output, Frame, binding generation, configuration revision), not the Pi's whole surface. `frame_trials` keeps that scope (migration 127) and lost `producer_id`, `baseline` and `presentation_request_id`; a trial's times are the database's clock, the one the document source reads.
- Output losses are judged from the `display` line by the Output report judge (`OutputLossLedger`), which takes the Coordination and Runtime locks only when a report changes a bound Output's connection, before any row lock.

### S4c's re-cut and its findings (2026-11-19)

- The PID1 scenarios failed with HTTP 409 at f039d158 because the fixture's stage read `current_cohort_in` (`central/fleet/node_acceptance.py`), which needs display exchange rows S4b part 4b had stopped writing (`node_output_cohort_unavailable`). The scenarios now prove the new path: Install's reboot over the bus and the Pi's next offer, the Pi's own handoff of a bound Output, the operator's reboot and a second boot of the same device, and a Pi that keeps playing through a Central outage.
- The App Manager never started the Player (the app broker does). It idled through 2a and was deleted in 2b-ii with its root, packages, launchers, unit and memory lines.
- The local recovery deadlines (HostCore's `RecoverySupervisor`, the broker's `RecoveryClient`) were deleted, not moved: the online broker's stage was the only thing that armed one. The `local_recovery_*` host metrics went with them.
- The probe channel's listener moved into `appliance/apps` (`ProbeListener`) and admits only `probe_open`; the relink message went with the app link; `DownloadPreparer` moved to `appliance/boot`.
- Migration 129 removes every stored release, deployment reference and boot offer and the release listing's ETag, rather than rewriting the deployment documents: a canonical document (sorted keys, compact separators) cannot be rewritten byte-exactly in SQL, and the Pi software those releases carry speaks the removed path. A Pi with no version is offered the newest ready stable release at its next boot.
- A Player that exited within the broker's first turn produced no `app_started` or `app_exited` fact, so its Frame was dark with no card (a product bug of the [#123](https://github.com/mcurcio/photo-wall/issues/123) to [#125](https://github.com/mcurcio/photo-wall/issues/125) class). Fixed at the broker and the judge (`appliance/kernel/app_facts.py`), proven by the PID1 failure scenario and a unit test.
- The deploy order became Central first ([Deploying it](#deploying-it)).

### Tests of older schemas (2026-11-17)

Tests that ran today's code on a schema older than production's (production was at migration 115) were retired, with the helpers only they used (`tests/support/offer_contexts.py`, `content_db.schema_before` and the like): there is no backwards compatibility and the one database is past those migrations; a fresh database still applies every migration. Tests of migrations 116 and later stay, seeded with raw SQL as the schema before them stood, never through today's code.

## Prior art

- Aggregate and Repository (Evans, *Domain-Driven Design*; Fowler, *Patterns of Enterprise Application Architecture*).
- Serialized LOB (Fowler) for parts an aggregate always loads whole: lists, open sessions, documents.
- Single Table Inheritance (Fowler) where one shape has kinds: `events`, `picture_history`, `room_light_facts`.
- A last-value-per-key store, as NATS KV and Kafka log compaction keep, for `node_states`.
- Idempotent consumer (Hohpe and Woolf, *Enterprise Integration Patterns*): bus records keyed (device, stream, epoch, sequence); a repeat is a no-op.
- Copy-then-drop in one forward migration (Sadalage and Ambler, *Refactoring Databases*), with a stop-then-start deploy.
- The settings-table pattern for `singletons`, the one departure from one table per aggregate.

## Evidence

- Production SELECTs on the replica, 2026-11-17: 125 base tables; Central's 18 triggers and 2 functions; primary keys; `node_link_records` by stream; job outcomes and Procrastinate failures by task.
- pg_stat deltas on the primary, 2026-11-17: row writes by table, `xact_commit`, and transactions holding an id (`pg_snapshot_xmax(pg_current_snapshot())`).
- Code: `origin/main` at f126c730.
