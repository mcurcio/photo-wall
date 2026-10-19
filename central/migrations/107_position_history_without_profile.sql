-- A Position entry no longer records the Frame profile it was made for: the box is the Frame's
-- shape, so Restore checks only the turn and the Display seen. Entries stored as
-- kept-profile-changed keep their kind and still list.
ALTER TABLE position_entries DROP COLUMN profile_width_px, DROP COLUMN profile_height_px;
