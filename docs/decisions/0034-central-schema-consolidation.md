# 0034 — Central's database: one table per thing it keeps, and a write only when something changed

**Date:** 2026-11-17 · **Layer:** module (Central's persistence: which aggregates it keeps and the tables each needs; the write load) · **Status:** accepted design, built on the branch `feat/schema-consolidation` (one pull request, slices S1 to S4). **Builds on:** [0033](0033-central-write-load.md) (write what changed, node records kept a week) and [0017](0017-node-redesign-r3.md) (the Node bus).

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
| S4 | The old Pi path: reboot becomes a bus method and Install reads its completion from the `host` line, where the host's facts and metrics move too; the Display Host decides its handoff from the Output document, and a Frame's Position trial travels on its Output's own document key; Output losses judged from the `display` line; the `/v2/node/*` routes other than boot offers removed, with 16 tables | yes |

## Deploying it

**Central first, then each Pi rebooted once by hand** (engineering call 2026-11-19, superseding the Pi-first order of 2026-11-18). The release names no manager root (S4c part 2b-i), and the Central before it requires one in every release it ingests (`parse_node_release`), so the old Central cannot ingest this release and nothing can Install it through the old Central. So:

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
- **Deferred:** moving every repository onto a query builder that stamps audit fields itself. This delivery adds the columns and one repository per aggregate, so that move can later go one repository at a time.
- **Not planned:** changing Procrastinate's tables.

## Table map

S1 to S4 are the slices; "child" means written by its aggregate's repository. Rows and writes a minute are production's on 2026-11-17.

| # | Table today | Rows | Writes/min | Fate | Into | Why | Slice |
|---|---|---|---|---|---|---|---|
| 1 | node_app_effects | 0 | 0 | drop | — | Old path; empty | S4 |
| 2 | node_app_links | 1 | 0.5 | drop | node_states (`player` line) | Old path | S4 |
| 3 | node_app_operations | 0 | 0 | drop | — | Route never used | S2 |
| 4 | node_app_qualification_samples | 0 | 0 | drop | — | Qualification never ran | S2 |
| 5 | node_app_qualifications | 0 | 0 | drop | — | Same | S2 |
| 6 | node_app_responses | 0 | 0 | drop | events | Old path | S4 |
| 7 | node_base_managers | 67 | 0 | merge | node_releases (inline) | Immutable release document | S2 |
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
| 18 | node_deployment_assets | 201 | 0 | merge | asset_references | Duplicates `owner = node-deployment:<id>` | S2 |
| 19 | node_deployments | 67 | 0 | merge | node_releases | One release | S2 |
| 20 | node_display_exchanges | 42,992 | 19.5 | drop | node_states (`display` line) | Old path; 39,535 "already admitted" | S4 |
| 21 | node_display_handoffs | 2 | 0 | drop | — | The Pi decides from the Output document | S4 |
| 22 | node_display_withdrawals | 0 | 0 | drop | — | Same | S4 |
| 23 | node_environment_acceptances | 0 | 0 | drop | — | Never ran | S2 |
| 24 | node_environment_catalog | 100 | 0 | merge | node_releases (inline list) | Immutable document | S2 |
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
| 38 | node_reboot_commands | 14 | 0 | drop | events | Reboot is a bus method | S4 |
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
| 51 | authored_candidates | 0 | 0 | drop | — | The Scene keeps ids | S2 |
| 52 | bindings | 1 | 0 | merge | frames | 1:1 | S2 |
| 53 | colour_sensors | 1 | 0 | merge | singletons (room-light setup) | One setup | S2 |
| 54 | colour_words | 2 | 0 | merge | room_light_facts | Facts by reference | S2 |
| 55 | coordination_groups | 0 | 0 | keep | coordination_groups | Cues | — |
| 56 | devices | 1 | 0 | keep | devices | Exists before any Player | — |
| 57 | displays | 4 | 0 | keep | displays | Takes the session and the shared serial | — |
| 58 | enrollment_nonces | 0 | 0 | memory | the enrolment handshake | One-time, short-lived; retried | S2 |
| 59 | execution_commits | 0 | 0 | keep (child) | execution_commits | Queried by cue | — |
| 60 | execution_events | 14,292 | 0 | drop | player_show_state (observation numbers) + a skip-number sequence | Its uses get their own homes: a repeated observation is known by its number, a skipped group's revocation number comes from a sequence | S2 |
| 61 | fleet_device_lifecycle | 1 | 0 | merge | devices | 1:1; trigger dropped | S2 |
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
| 76 | media_references | 2 | 0.5 | drop | derived from plan_offers + assignment_locks; a download protected by assets.last_served_at | One row set per offer revision | S2 |
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
| 102 | player_control_sessions | 1 | 0 | merge | players | Same writer and lock | S2 |
| 103 | player_feedback | 1 | 0.5 | merge | player_show_state | Per Player | S2 |
| 104 | player_output_identification | 0 | 0 | merge | player_show_state | Per Player | S2 |
| 105 | player_versions | 1 | 0 | merge | devices | Decided at first boot | S2 |
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
