-- The legacy calibration preview goes (R7): only Registry.calibrate wrote a Frame's preview and
-- its lease, and that write path is deleted. A Frame's calibration changes only through a node
-- calibration trial's Save, which never had a preview. A stale preview still stored is dropped
-- with its columns, so no Player is sent one again.
ALTER TABLE frames DROP COLUMN preview;
ALTER TABLE frames DROP COLUMN preview_expires;
