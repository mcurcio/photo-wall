-- Decision 0034, S2g part 2: one show state per Player, no execution log. A Player's show state is
-- one row, `player_show_state`, written only by Coordination (central/coordination.py): its last
-- configuration (was `player_configurations`), its readiness (was `player_feedback`), the high-water
-- mark of its observation reports (was `execution_events`' unique report number, migration 114: a
-- report at or below it is a repeat, answered and not written) and the request to identify an
-- Output (was `player_output_identification`, its own epoch inside the document). The row's
-- `authority_epoch` is its configuration's, readiness's and observations' epoch: a newer epoch's
-- configuration clears the other two. A skipped group's revocation number comes from the sequence
-- `coordination_skip_sequence` (it came from `execution_events`' sequence), started past every number
-- that table ever gave, so a Player that already holds one never sees a number repeat.
-- `execution_events` goes with its retention; `coordination_groups`, `plan_offers`,
-- `execution_commits` and `assignment_locks` gain their audit fields, `photo_selections` and
-- `shown_photos` their writer (`by`; each row carries its own time). Plain statements, no trigger or
-- function, forward-only.

CREATE TABLE player_show_state (
    player_id TEXT PRIMARY KEY REFERENCES players(id) ON DELETE CASCADE,
    authority_epoch BIGINT NOT NULL,
    configuration_revision BIGINT,
    configuration JSONB CHECK (jsonb_typeof(configuration) = 'object'),
    readiness_sequence BIGINT,
    readiness JSONB CHECK (jsonb_typeof(readiness) = 'object'),
    readiness_received_at DOUBLE PRECISION,
    observed_sequence BIGINT,
    identify JSONB CHECK (jsonb_typeof(identify) = 'object'),
    created_at DOUBLE PRECISION NOT NULL,
    created_by TEXT NOT NULL,
    changed_at DOUBLE PRECISION NOT NULL,
    changed_by TEXT NOT NULL,
    CHECK ((configuration IS NULL) = (configuration_revision IS NULL)),
    CHECK ((readiness IS NULL) = (readiness_sequence IS NULL)
           AND (readiness IS NULL) = (readiness_received_at IS NULL)));

-- Each Player's newest epoch among the three tables wins; a fact of an older epoch is not copied.
INSERT INTO player_show_state(player_id, authority_epoch, configuration_revision, configuration,
                              readiness_sequence, readiness, readiness_received_at, observed_sequence,
                              identify, created_at, created_by, changed_at, changed_by)
SELECT e.player_id, e.epoch,
       c.revision, c.configuration,
       f.sequence, f.readiness, f.received_at,
       (SELECT max(v.report_sequence) FROM execution_events v
         WHERE v.player_id = e.player_id AND v.authority_epoch = e.epoch),
       CASE WHEN i.player_id IS NOT NULL THEN jsonb_build_object(
           'request_id', i.request_id::text, 'output_id', i.output_id,
           'authority_epoch', i.authority_epoch, 'created_at', i.created_at,
           'expires_at', i.expires_at) END,
       coalesce(least(f.received_at, i.created_at), EXTRACT(EPOCH FROM now())), 'unknown',
       coalesce(greatest(f.received_at, i.created_at), EXTRACT(EPOCH FROM now())), 'unknown'
FROM (SELECT player_id, max(authority_epoch) AS epoch FROM (
          SELECT player_id, authority_epoch FROM player_configurations
          UNION ALL SELECT player_id, authority_epoch FROM player_feedback
          UNION ALL SELECT player_id, authority_epoch FROM player_output_identification) AS known
      GROUP BY player_id) AS e
LEFT JOIN player_configurations c ON c.player_id = e.player_id AND c.authority_epoch = e.epoch
LEFT JOIN player_feedback f ON f.player_id = e.player_id AND f.authority_epoch = e.epoch
LEFT JOIN player_output_identification i ON i.player_id = e.player_id;

-- A skipped group's revocation number: past every number execution_events ever gave.
CREATE SEQUENCE coordination_skip_sequence;
SELECT setval('coordination_skip_sequence',
              greatest(coalesce((SELECT max(skip_sequence) FROM coordination_groups), 0),
                       (SELECT last_value FROM execution_events_sequence_seq)) + 1, false);

-- Audit fields on Coordination's tables, backfilled from each row's own time and 'unknown'.
ALTER TABLE coordination_groups
    ADD COLUMN created_at DOUBLE PRECISION,
    ADD COLUMN created_by TEXT,
    ADD COLUMN changed_at DOUBLE PRECISION,
    ADD COLUMN changed_by TEXT;
UPDATE coordination_groups SET created_at = EXTRACT(EPOCH FROM now()), created_by = 'unknown',
                               changed_at = EXTRACT(EPOCH FROM now()), changed_by = 'unknown';
ALTER TABLE coordination_groups
    ALTER COLUMN created_at SET NOT NULL,
    ALTER COLUMN created_by SET NOT NULL,
    ALTER COLUMN changed_at SET NOT NULL,
    ALTER COLUMN changed_by SET NOT NULL;

ALTER TABLE plan_offers
    ADD COLUMN created_by TEXT,
    ADD COLUMN changed_at DOUBLE PRECISION,
    ADD COLUMN changed_by TEXT;
UPDATE plan_offers SET created_by = 'unknown', changed_at = created_at, changed_by = 'unknown';
ALTER TABLE plan_offers
    ALTER COLUMN created_by SET NOT NULL,
    ALTER COLUMN changed_at SET NOT NULL,
    ALTER COLUMN changed_by SET NOT NULL;

ALTER TABLE execution_commits
    ADD COLUMN created_at DOUBLE PRECISION,
    ADD COLUMN created_by TEXT,
    ADD COLUMN changed_at DOUBLE PRECISION,
    ADD COLUMN changed_by TEXT;
UPDATE execution_commits SET created_at = committed_at, created_by = 'unknown',
                             changed_at = committed_at, changed_by = 'unknown';
ALTER TABLE execution_commits
    ALTER COLUMN created_at SET NOT NULL,
    ALTER COLUMN created_by SET NOT NULL,
    ALTER COLUMN changed_at SET NOT NULL,
    ALTER COLUMN changed_by SET NOT NULL;

ALTER TABLE assignment_locks
    ADD COLUMN created_at DOUBLE PRECISION,
    ADD COLUMN created_by TEXT,
    ADD COLUMN changed_at DOUBLE PRECISION,
    ADD COLUMN changed_by TEXT;
UPDATE assignment_locks SET created_at = secured_at, created_by = 'unknown',
                            changed_at = secured_at, changed_by = 'unknown';
ALTER TABLE assignment_locks
    ALTER COLUMN created_at SET NOT NULL,
    ALTER COLUMN created_by SET NOT NULL,
    ALTER COLUMN changed_at SET NOT NULL,
    ALTER COLUMN changed_by SET NOT NULL;

ALTER TABLE photo_selections ADD COLUMN by TEXT;
UPDATE photo_selections SET by = 'unknown';
ALTER TABLE photo_selections ALTER COLUMN by SET NOT NULL;
ALTER TABLE shown_photos ADD COLUMN by TEXT;
UPDATE shown_photos SET by = 'unknown';
ALTER TABLE shown_photos ALTER COLUMN by SET NOT NULL;

DROP TABLE player_configurations, player_feedback, player_output_identification, execution_events;
