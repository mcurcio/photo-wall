-- Roadmap 1b (slice C2): readiness is worked out from Position commits, never stored as a yes/no.
-- A Position commit records the Frame generation it was made at and the Display last seen on the
-- bound Output (NULL: none seen). The rule lives in app code (central/displays/model.py
-- `readiness`); there is no trigger and no stored procedure here.
ALTER TABLE frames
    ADD COLUMN position_generation BIGINT NULL,
    ADD COLUMN position_display_id UUID NULL REFERENCES displays(id) ON DELETE SET NULL;

-- Every Frame ready before stays ready: its commit is current and names no Display, so the first
-- Display reported on its port is adopted (the deploy cost on the design page).
UPDATE frames SET position_generation = generation WHERE calibration_valid;

ALTER TABLE frames DROP COLUMN calibration_valid;
