-- Shows videos (frame box run, .claude/runs/frame-box/page.md, bead fb-5): whether the Planner may
-- put a video on this Frame, the Frame's own setting. It was the Frame profile's `video`; the
-- profile leaves (owner, 2026-10-19: "the profile shrinks to what is not geometry"), so the setting
-- moves to the Frame. Set by PUT /v1/operator/frames/{frame_id}/video (`Registry.set_frame_video`),
-- last write wins. No CHECK, trigger or function.
--
-- Upgrade data: every Frame keeps what its profile said (a profile with no `video` shows videos, as
-- `FrameProfile.video` defaults). No generation moves, so every ready Frame stays ready.
ALTER TABLE frames ADD COLUMN video BOOLEAN NOT NULL DEFAULT TRUE;
UPDATE frames SET video = COALESCE((profile->>'video')::boolean, TRUE);
