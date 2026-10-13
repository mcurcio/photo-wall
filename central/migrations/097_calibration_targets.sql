-- Calibration targets (decision 0026; owner, 2026-10-13: "build the calibration targets"): two more
-- test patterns, the screen target drawn raw (target-pre-software) or through Photo Wall's steps
-- (target-post-software), contracts/tuning.py TestPattern. A picture session's ask (094) and a sent
-- picture entry (096) may name them. Forward only; no row changes, so this alone changes no picture.
ALTER TABLE picture_sessions DROP CONSTRAINT picture_sessions_test_pattern_check;
ALTER TABLE picture_sessions ADD CONSTRAINT picture_sessions_test_pattern_check
    CHECK (test_pattern IN ('white', 'grey-ramp', 'black-level', 'target-pre-software',
                            'target-post-software'));
ALTER TABLE picture_sent_entries DROP CONSTRAINT picture_sent_entries_test_pattern_check;
ALTER TABLE picture_sent_entries ADD CONSTRAINT picture_sent_entries_test_pattern_check
    CHECK (test_pattern IN ('white', 'grey-ramp', 'black-level', 'target-pre-software',
                            'target-post-software'));
