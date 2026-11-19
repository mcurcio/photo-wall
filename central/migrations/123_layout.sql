-- Decision 0034, S2g part 1: the wall's layout. A Frame's binding is on its row (`frames.player_id`,
-- `output_id`: both NULL or both set, one Frame per Output), so `bindings` goes. A Player's control
-- session is one document on its row (`players.control_session`, the columns of
-- `player_control_sessions` as keys; its one writer, central/registry.py, already locks that row).
-- The enrolment nonce lives in Central's memory (central/registry.py `_EnrolmentNonces`), so
-- `enrollment_nonces` goes: a challenge outstanding at the deploy is refused and the Player retries.
-- `node_calibration_trials` is renamed `frame_trials`; `node_output_losses` is renamed `output_losses`
-- with its cause inline (`cause_layer`, its producer's owner), no longer tied to `node_producers` or
-- `node_evidence`. `walls`, `frames`, `players`, `frame_trials` and `output_losses` gain their audit
-- fields, each backfilled from the row's own time where it has one and 'unknown' as its writer.
-- Plain statements, no trigger or function, forward-only.

-- The binding on the Frame.
ALTER TABLE frames
    ADD COLUMN player_id TEXT REFERENCES players(id),
    ADD COLUMN output_id TEXT,
    ADD CONSTRAINT frames_binding_both CHECK ((player_id IS NULL) = (output_id IS NULL)),
    ADD CONSTRAINT frames_binding_output FOREIGN KEY (player_id, output_id) REFERENCES outputs(player_id, output_id);
CREATE UNIQUE INDEX frames_one_per_output ON frames (player_id, output_id) WHERE player_id IS NOT NULL;
UPDATE frames f SET player_id = b.player_id, output_id = b.output_id FROM bindings b WHERE b.frame_id = f.id;

-- The control session on the Player.
ALTER TABLE players
    ADD COLUMN control_session JSONB CHECK (jsonb_typeof(control_session) = 'object'),
    ADD COLUMN created_at DOUBLE PRECISION,
    ADD COLUMN created_by TEXT,
    ADD COLUMN changed_at DOUBLE PRECISION,
    ADD COLUMN changed_by TEXT;
UPDATE players p SET control_session = to_jsonb(c) - 'player_id'
FROM player_control_sessions c WHERE c.player_id = p.id;
UPDATE players SET created_at = registered_at, created_by = 'unknown',
                   changed_at = greatest(registered_at, last_seen, coalesce(retired_at, registered_at)),
                   changed_by = 'unknown';
ALTER TABLE players
    ALTER COLUMN created_at SET NOT NULL,
    ALTER COLUMN created_by SET NOT NULL,
    ALTER COLUMN changed_at SET NOT NULL,
    ALTER COLUMN changed_by SET NOT NULL;

-- Audit fields on the wall and its Frames (neither kept a time before this release).
ALTER TABLE walls
    ADD COLUMN created_at DOUBLE PRECISION,
    ADD COLUMN created_by TEXT,
    ADD COLUMN changed_at DOUBLE PRECISION,
    ADD COLUMN changed_by TEXT;
UPDATE walls SET created_at = EXTRACT(EPOCH FROM now()), created_by = 'unknown',
                 changed_at = EXTRACT(EPOCH FROM now()), changed_by = 'unknown';
ALTER TABLE walls
    ALTER COLUMN created_at SET NOT NULL,
    ALTER COLUMN created_by SET NOT NULL,
    ALTER COLUMN changed_at SET NOT NULL,
    ALTER COLUMN changed_by SET NOT NULL;

ALTER TABLE frames
    ADD COLUMN created_at DOUBLE PRECISION,
    ADD COLUMN created_by TEXT,
    ADD COLUMN changed_at DOUBLE PRECISION,
    ADD COLUMN changed_by TEXT;
UPDATE frames SET created_at = EXTRACT(EPOCH FROM now()), created_by = 'unknown',
                  changed_at = EXTRACT(EPOCH FROM now()), changed_by = 'unknown';
ALTER TABLE frames
    ALTER COLUMN created_at SET NOT NULL,
    ALTER COLUMN created_by SET NOT NULL,
    ALTER COLUMN changed_at SET NOT NULL,
    ALTER COLUMN changed_by SET NOT NULL;

-- A Frame's Position trial.
ALTER TABLE node_calibration_trials RENAME TO frame_trials;
ALTER INDEX node_calibration_trials_pkey RENAME TO frame_trials_pkey;
ALTER INDEX node_active_calibration_trial RENAME TO frame_trials_active;
ALTER TABLE frame_trials
    ADD COLUMN created_by TEXT,
    ADD COLUMN changed_at DOUBLE PRECISION,
    ADD COLUMN changed_by TEXT;
UPDATE frame_trials SET created_by = 'unknown', changed_at = greatest(created_at, touched_at), changed_by = 'unknown';
ALTER TABLE frame_trials
    ALTER COLUMN created_by SET NOT NULL,
    ALTER COLUMN changed_at SET NOT NULL,
    ALTER COLUMN changed_by SET NOT NULL;

-- An Output's loss, its cause inline.
ALTER TABLE node_output_losses RENAME TO output_losses;
ALTER INDEX node_output_losses_pkey RENAME TO output_losses_pkey;
ALTER TABLE output_losses
    ADD COLUMN cause_layer TEXT,
    ADD COLUMN created_at DOUBLE PRECISION,
    ADD COLUMN created_by TEXT,
    ADD COLUMN changed_at DOUBLE PRECISION,
    ADD COLUMN changed_by TEXT;
UPDATE output_losses l SET cause_layer = p.owner FROM node_producers p WHERE p.producer_id = l.cause_producer_id;
UPDATE output_losses SET created_at = interrupted_at, created_by = 'unknown',
                         changed_at = coalesce(resolved_at, interrupted_at), changed_by = 'unknown';
ALTER TABLE output_losses
    ALTER COLUMN cause_layer SET NOT NULL,
    ALTER COLUMN created_at SET NOT NULL,
    ALTER COLUMN created_by SET NOT NULL,
    ALTER COLUMN changed_at SET NOT NULL,
    ALTER COLUMN changed_by SET NOT NULL,
    DROP COLUMN cause_producer_id,
    DROP COLUMN cause_evidence_id;

DROP TABLE bindings, player_control_sessions, enrollment_nonces;
