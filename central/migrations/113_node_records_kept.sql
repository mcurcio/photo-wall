-- Node records are kept a week and trimmed in whole batches (decision 0033,
-- central/infra/node_record_retention.py); Central keeps each device generation's current app link
-- only (central/fleet/node_app_links.py). Until now nothing trimmed these tables: 3.5 million rows,
-- 4.5 GB of a 10 GiB volume on 2026-11-15.
--
-- The tables are truncated in place, keeping only what Central reads at any age: every index,
-- constraint, foreign key and the work_id sequence stays, and nothing large is written (a batch
-- delete of 3.5 million rows would write log and free no disk). Every copy below runs on an
-- existing index or a small table. Plain statements only: no trigger, stored procedure or
-- function. Forward-only (central/db.py).

-- 1. Hold every table this migration empties, so no Node write lands between a copy and the
-- truncate (it would be lost). 9 s outlasts a 7 s COMMIT over iSCSI and stays under central/db.py's
-- 10 s statement timeout; a lock timeout fails the start with nothing changed and the pod tries
-- again. The locks hold until migrate() commits; a Node post waiting past its own 5 s is answered
-- 503 lock_busy and retried.
SET LOCAL lock_timeout = '9s';
LOCK TABLE node_reconciliation_work, node_evidence, node_output_losses, node_display_exchanges,
    node_display_handoffs, node_display_withdrawals, node_host_observations,
    node_manager_observations, node_app_links IN ACCESS EXCLUSIVE MODE;
SET LOCAL lock_timeout = '5s';
-- The copies read whole tables (unfinished work has no index: one would make every completion a
-- non-HOT update) under central/db.py's 10 s statement timeout, which applies to each statement
-- here. On 2026-11-15 the tables held 3.5 million rows, 4.5 GB, behind 3-7 s fsyncs over iSCSI,
-- and no copy was timed there. A copy past 10 s would roll everything back and the next start
-- would repeat it: Central down in a loop. 300 s holds a cold read of all 4.5 GB at 15 MB/s; a
-- copy past even that still rolls back whole, and the runbook's 0033 deploy steps say how to
-- recover. Reset to central/db.py's value at the end, for the migrations after this one.
SET LOCAL statement_timeout = '300s';

-- 2. What Central reads now, copied aside.
-- Unfinished work (the reconciler's scan) and every Output loss, with the evidence each names,
-- and every event (the reboot audit reads events at any age).
CREATE TEMP TABLE m113_work ON COMMIT DROP AS
    SELECT * FROM node_reconciliation_work WHERE completed_at IS NULL;
CREATE TEMP TABLE m113_losses ON COMMIT DROP AS SELECT * FROM node_output_losses;
CREATE TEMP TABLE m113_evidence ON COMMIT DROP AS
    SELECT e.* FROM node_evidence e JOIN (
        SELECT producer_id, kind, evidence_id FROM m113_work
        UNION SELECT cause_producer_id, cause_kind, cause_evidence_id FROM m113_losses
        UNION SELECT producer_id, kind, evidence_id FROM node_evidence WHERE kind = 'event'
    ) k USING (producer_id, kind, evidence_id);

-- A current boot's newest Display Host producers (central/fleet/node_display.py
-- `_MAX_PRODUCERS`), each Output's newest 128 exchanges (its retry read), walked on
-- node_display_output_latest by a skip-scan, with their handoffs and withdrawals.
CREATE TEMP TABLE m113_exchanges ON COMMIT DROP AS
    WITH RECURSIVE producers AS (
        SELECT x.producer_id FROM node_boot_admissions b
        CROSS JOIN LATERAL (SELECT p.producer_id FROM node_producers p
            WHERE p.admission_id = b.admission_id AND p.owner = 'display_host'
            ORDER BY p.admitted_at DESC, p.producer_id DESC LIMIT 4) x
        WHERE b.superseded_at IS NULL),
    outputs(producer_id, output_id) AS (
        SELECT pr.producer_id, (SELECT e.output_id FROM node_display_exchanges e
            WHERE e.producer_id = pr.producer_id ORDER BY e.output_id LIMIT 1) FROM producers pr
        UNION ALL
        SELECT o.producer_id, (SELECT e.output_id FROM node_display_exchanges e
            WHERE e.producer_id = o.producer_id AND e.output_id > o.output_id
            ORDER BY e.output_id LIMIT 1) FROM outputs o WHERE o.output_id IS NOT NULL)
    SELECT x.* FROM outputs o
    CROSS JOIN LATERAL (SELECT e.* FROM node_display_exchanges e
        WHERE e.producer_id = o.producer_id AND e.output_id = o.output_id
        ORDER BY e.sampled_boottime_ms DESC LIMIT 128) x
    WHERE o.output_id IS NOT NULL;
CREATE TEMP TABLE m113_handoffs ON COMMIT DROP AS
    SELECT h.* FROM node_display_handoffs h JOIN m113_exchanges x USING (decision_id);
CREATE TEMP TABLE m113_withdrawals ON COMMIT DROP AS
    SELECT w.* FROM node_display_withdrawals w JOIN m113_exchanges x USING (decision_id);

-- Each device generation's current link.
CREATE TEMP TABLE m113_links ON COMMIT DROP AS
    SELECT * FROM node_app_links WHERE superseded_at IS NULL;

-- The newest 8 samples of each producer of a device's current and latest superseded boot
-- (central/fleet/node_observations.py: the newest sample per producer, the newest 8 App
-- Manager samples), on each table's primary key.
CREATE TEMP TABLE m113_producers ON COMMIT DROP AS
    SELECT p.producer_id FROM node_producers p WHERE p.admission_id IN (
        SELECT admission_id FROM node_boot_admissions WHERE superseded_at IS NULL
        UNION
        (SELECT DISTINCT ON (device_id, device_generation) admission_id FROM node_boot_admissions
            WHERE superseded_at IS NOT NULL
            ORDER BY device_id, device_generation, superseded_at DESC, admission_id DESC));
CREATE TEMP TABLE m113_host_observations ON COMMIT DROP AS
    SELECT x.* FROM m113_producers p CROSS JOIN LATERAL (SELECT o.* FROM node_host_observations o
        WHERE o.producer_id = p.producer_id ORDER BY o.sequence DESC LIMIT 8) x;
CREATE TEMP TABLE m113_manager_observations ON COMMIT DROP AS
    SELECT x.* FROM m113_producers p CROSS JOIN LATERAL (SELECT o.* FROM node_manager_observations o
        WHERE o.producer_id = p.producer_id ORDER BY o.sequence DESC LIMIT 8) x;

-- 3. Empty the nine tables in one statement (TRUNCATE fires no row trigger, keeps the work_id
-- sequence, and frees the disk at once).
TRUNCATE node_reconciliation_work, node_evidence, node_output_losses, node_display_exchanges,
    node_display_handoffs, node_display_withdrawals, node_host_observations,
    node_manager_observations, node_app_links;

-- One row per device generation: its current link. Its audit fields, stamped by
-- central/fleet/node_app_links.py (`node`: a Pi's own request), with no default.
ALTER TABLE node_app_links DROP COLUMN superseded_at,
    DROP CONSTRAINT node_app_links_pkey,
    ADD PRIMARY KEY (device_id, device_generation),
    ADD COLUMN created_at DOUBLE PRECISION NOT NULL,
    ADD COLUMN created_by TEXT NOT NULL,
    ADD COLUMN changed_at DOUBLE PRECISION NOT NULL,
    ADD COLUMN changed_by TEXT NOT NULL;

-- The retention deletes what was immutable.
DROP TRIGGER node_evidence_immutable ON node_evidence;
DROP TRIGGER node_observation_immutable ON node_host_observations;
DROP TRIGGER node_manager_observation_immutable ON node_manager_observations;
DROP TRIGGER node_display_exchange_immutable ON node_display_exchanges;
DROP TRIGGER node_display_handoff_immutable ON node_display_handoffs;
DROP TRIGGER node_display_withdrawal_immutable ON node_display_withdrawals;

-- Each trim finds its oldest rows here (the tables are empty: instant).
CREATE INDEX node_reconciliation_work_received ON node_reconciliation_work (received_at);
CREATE INDEX node_display_exchanges_received ON node_display_exchanges (received_at);
CREATE INDEX node_host_observations_received ON node_host_observations (received_at);
CREATE INDEX node_manager_observations_received ON node_manager_observations (received_at);

-- The copies back, parents first.
INSERT INTO node_evidence SELECT * FROM m113_evidence;
INSERT INTO node_reconciliation_work SELECT * FROM m113_work;
INSERT INTO node_output_losses SELECT * FROM m113_losses;
INSERT INTO node_display_exchanges SELECT * FROM m113_exchanges;
INSERT INTO node_display_handoffs SELECT * FROM m113_handoffs;
INSERT INTO node_display_withdrawals SELECT * FROM m113_withdrawals;
INSERT INTO node_host_observations SELECT * FROM m113_host_observations;
INSERT INTO node_manager_observations SELECT * FROM m113_manager_observations;
-- A current link's first and last admission were never recorded apart: both take its admission.
INSERT INTO node_app_links (nonce, producer_id, device_id, device_generation, player_id,
        authority_epoch, payload, admitted_at, created_at, created_by, changed_at, changed_by)
    SELECT nonce, producer_id, device_id, device_generation, player_id, authority_epoch, payload,
        admitted_at, admitted_at, 'node', admitted_at, 'node' FROM m113_links;

SET LOCAL statement_timeout = '10s';
