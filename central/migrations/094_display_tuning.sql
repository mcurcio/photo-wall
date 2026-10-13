-- Display tuning (decision 0024, 2026-10-13): the Display's signal range (the Pi's HDMI RGB
-- quantization range) and the picture session's test pattern. The rules live in app code
-- (central/displays/picture.py, central/infra/display_store.py); there is no trigger and no stored
-- procedure here. No backwards compatibility in the data.

-- The Display's signal range (contracts/node_picture.py SignalRange): stored, kept by Done, put
-- back by Revert, moves with the display. Every Display starts at 'auto', which changes nothing.
ALTER TABLE displays
    ADD COLUMN signal_range TEXT NOT NULL DEFAULT 'auto'
        CHECK (signal_range IN ('auto', 'full', 'limited'));

-- The session's test pattern (contracts/tuning.py TestPattern) and the sequence of the edit that
-- asked for it (contracts/node_picture.py TestPatternAsk.ask): never stored on the Display, gone
-- with the session.
ALTER TABLE picture_sessions
    ADD COLUMN test_pattern TEXT NULL
        CHECK (test_pattern IN ('white', 'grey-ramp', 'black-level')),
    ADD COLUMN test_pattern_ask BIGINT NULL CHECK (test_pattern_ask >= 1),
    ADD CONSTRAINT picture_sessions_test_pattern_ask CHECK ((test_pattern IS NULL) = (test_pattern_ask IS NULL));

-- Open picture sessions are dropped: their drafts lack the signal range (a session's draft is a
-- DisplayPicture under the displays columns' names).
DELETE FROM picture_sessions;
