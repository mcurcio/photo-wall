-- Who changed the Runtime, and what the last planning pass could not do (planning never refuses
-- the wall, fix/planning-without-time-cap). No trigger or function: the stores write every column.
--
-- runtime_state gains its audit fields. Its time columns stay on the Runtime's own clock (seconds,
-- as `updated_at` was), so `changed_at` and `created_at` read on one scale with the snapshot's
-- `now`. The existing row was written by an unknown actor.
ALTER TABLE runtime_state RENAME COLUMN updated_at TO changed_at;
ALTER TABLE runtime_state ADD COLUMN created_at DOUBLE PRECISION,
    ADD COLUMN created_by TEXT, ADD COLUMN changed_by TEXT;
UPDATE runtime_state SET created_at = changed_at, created_by = 'unknown', changed_by = 'unknown';
ALTER TABLE runtime_state ALTER COLUMN created_at SET NOT NULL,
    ALTER COLUMN created_by SET NOT NULL, ALTER COLUMN changed_by SET NOT NULL;

-- One row per operator Runtime command (central/runtime_store.py `command`, `command_current`),
-- append-only: who ran which command when, and the Scene, Run and Program it named.
CREATE TABLE runtime_changes (
    id BIGSERIAL PRIMARY KEY,
    at TIMESTAMPTZ NOT NULL,
    by TEXT NOT NULL,
    command TEXT NOT NULL,
    scene_id TEXT,
    run_id TEXT,
    program_id TEXT,
    scene_revision INTEGER);

-- The last planning pass's issues (central/planning_outcome.py), one row, rewritten only when the
-- set changes; `since` is when this set began.
CREATE TABLE planning_outcome (
    one BOOLEAN PRIMARY KEY CHECK (one),
    issues JSONB NOT NULL CHECK (jsonb_typeof(issues) = 'array'),
    dropped INTEGER NOT NULL CHECK (dropped >= 0),
    since TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    created_by TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL,
    changed_by TEXT NOT NULL);
