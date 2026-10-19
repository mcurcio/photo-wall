-- A Frame keeps only where it hangs on the wall (its top-left) and its own "Shows videos"
-- (frames.video, 105). Its shape and pixels are its Position box on its screen, its size the box's
-- share of the Display's own size (103, 104): the Frame profile and the typed wall size go
-- (frame box run, .claude/runs/frame-box/page.md fb-14).
ALTER TABLE frames DROP COLUMN profile, DROP COLUMN width_mm, DROP COLUMN height_mm;
