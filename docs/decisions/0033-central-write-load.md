# 0033 — Central's database write load: write what changed, keep node records a week

**Date:** 2026-11-15 · **Layer:** module (Central's pool check, node records, the app link, the job queue) · **Status:** accepted design, built on the branch `fix/central-write-load` (one pull request, two slices). **Decision owner:** the owner, in chat. His words below are his; everything not quoted from him is a design choice, not his ruling.

## The problem

| # | The owner's words | Source |
|---|---|---|
| P1 | "Central is inducing high db write load -- high iops over its iscsi store. Why?" | chat, 2026-11-15 |
| P2 | "Yes. That work is the highest priority." | chat, 2026-11-15 |

Measured on 2026-11-15 (read-only, on the replica): about 26 commits a second, most sessions waiting in COMMIT (each fsync over iSCSI takes 3 to 7 s), the 10 GiB volume 74% full. Two causes were in Central's own code:

1. **The pool check rewrote every row.** Every 15 minutes the 21,000-photo pool was read again and every row of `library_assets`, `asset_revisions` and `photo_pool_rows` was upserted, changed or not (`last_seen`, `seen_check`), plus a `photo_check_marks` row per mark: about 63,000 row writes a pass.
2. **Node records were never trimmed.** `node_display_exchanges` (1.0 M rows, 2.2 GB), `node_evidence` (929 k, 1.3 GB), `node_reconciliation_work` (929 k), the observation tables, `node_app_links` (278,167 rows of history, one current; 588 of the last 658 relinks restated it) and the media queue's finished jobs (392,000 of 410,000 job rows: the hourly purge covered the upkeep queues only).

## Decision

1. **A pass writes what changed, plus one page per step for what it saw** (`central/infra/photo_library_store.py` `record_step`). Stored facts are read without a lock and only the rows that differ are upserted; a guarded upsert (`ON CONFLICT DO UPDATE ... WHERE changed`) would still lock and log the unchanged row, so those rows are filtered out before the write. What a step saw, and each kind of mark, is one `photo_check_pages` row holding the ids. A photo that left Immich is in none of the pass's `seen` pages and is asked about and removed as before. Migration 112.
2. **Central keeps the current app link, not every link** (`central/fleet/node_app_links.py` `link_standing`, `admit`). One row per device generation. A link that restates the current one (equal but for nonce and sample time, the sample not earlier) or is older (the same kernel boot, a lower `(app_epoch, sampled_boottime_ms)`) writes nothing and answers 200, so the Pi's outbox clears. Migration 113.
3. **Node records are kept 7 days and trimmed in whole batches** (`central/infra/node_record_retention.py`), on the scheduler's `node-retention` step: 500 rows a batch, each its own transaction, no advisory lock, never inside the coordinator's locks. A batch is whole (exactly `batch_rows` rows or none), so a table commits a delete about twice an hour, never every tick. Execution events' retention (`retention`) follows the same rule, and every queue's finished jobs go through `central/infra/queue_ops.py` `FinishedJobs`. The loop has one home: `central/infra/batched_retention.py` `drain`.

**Why a week:** the owner, 2026-10-05 (said as a supposition, guidance): "having Central throw away good debugging and observability data is silly." The week itself is this design's choice. **Open question for the owner (general guidance): a week of node records for debugging, or only what Central itself reads?**

## What each retention keeps

| Table | Kept | Readers |
|---|---|---|
| `node_app_links` | each device generation's current link | the link loaders, the reconciler, a retry of the current nonce |
| `node_display_exchanges`, handoffs, withdrawals | 7 days; at any age, each current boot's newest 128 per Output; a handoff or withdrawal goes with its exchange | `node_display.py` (the newest per Output; a withdrawal looks through the newest 128), `node_acceptance.py` |
| `node_reconciliation_work`, `node_evidence` | 7 days; at any age: unfinished work, the evidence an Output loss names, every event | the reconciler, the losses' foreign key, the reboot audit |
| `node_host_observations`, `node_manager_observations` | 7 days; at any age, for each producer of a device's current or previous boot, its newest host sample and its newest 8 App Manager samples | `node_observations.py`: the newest per producer, the newest 8 |
| `procrastinate_jobs`, `procrastinate_events` | finished jobs 7 days, every queue | none |
| `execution_events` | between 10,000 and 15,000 newest | the Planner's and console's reads |

**Migration 113 truncates in place**: it locks the nine tables it empties (lock timeout 9 s, outlasting a 7 s COMMIT, under the 10 s statement timeout), raises its own statement timeout to 300 s for the copies (they read whole tables, never timed on the deployment's disk; past 300 s the start rolls back whole and the runbook's deploy steps say how to recover), copies aside what Central reads now (the table above, at any age), truncates, and inserts the copies back. Every index, constraint, foreign key and the `work_id` sequence stays; it drops the six immutability triggers, `node_app_links.superseded_at` and its nonce key, and adds four `received_at` indexes and the link's audit fields.

## Row writes an hour (estimate, from the 2026-11-15 measurements)

| Writer | Before | After |
|---|---|---|
| Pool check: 3 tables, 21,287 photos, 4 passes | 253,000 | 3 per changed photo |
| Pool check: marks, now pages | 54,600 | 250 |
| App links (27 relinks) | 55 | 6 |
| Node evidence, work, exchanges | 4,400 | 4,400 + 3,300 trimmed |
| Queue jobs and events | 2,900 | 2,900 + 1,900 trimmed |
| **In scope** | **315,000** | **about 13,000** |

Commits an hour in scope go from about 9,350 to 5,760 (execution events' retention: 3,600 to 6). The owner's symptom, sessions queued in COMMIT, is commits: this removes 3,600 of about 79,000 an hour. Out of scope and unchanged: the Pi's reports (`execution_events`, up to 28,600 commits an hour, branch `feat/player-standalone`), the Runtime saved every tick (3,600, `coordination.py`) and about 37,000 commits not yet attributed; they need an owner and a measurement on the primary.

## What this gives up

- **Node records from before the deploy go at once**, all but what Central reads now. Truncating frees the disk; batch deletes of 3.5 million rows would not.
- The database stops refusing UPDATE and DELETE on six node tables. A boot that is neither its device's current nor its previous one keeps nothing older than a week: its sessions on the device page show no last sample. A Pi silent for over a week keeps its last sample, its previous boot's, and its Outputs.
- Kept rows past the week (each kept boot's newest records, unfinished work, work an Output loss names) stay at the head of the `received_at` index and every trim walks past them. Their number is bounded by the fleet, not by time; a partial index on finished work would skip them but make every completion non-HOT.
- A Re-check that finds a photo still in Immich writes one `seen` page for it alone, not one per step. Such photos are rare (a photo left out of the paging but still matching), and the pages go when the check ends.
- While old pods run after the migration, their link admissions and check steps fail; the Pi and the queue retry. A resent link whose control receipt has since been replaced is refused as not current (409), where it was answered as a duplicate.
- An existing library row's `changed_at` is the last pass that saw it; existing pool rows carry the migration's time; a current link's `created_at` is its admission.
- The reconciler still scans the work table every tick: an index on unfinished work would make every completion a full (non-HOT) update.
- **Not trimmed:** `node_link_records` (51 MB): the runbook gives it to E5; its reader needs the newest record per key at any age.
