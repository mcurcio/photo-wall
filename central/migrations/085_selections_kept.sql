-- Roadmap 5a review (PR 82, round 1): each Frame's kept still is read from what was offered as
-- well as what was reported presented (central/infra/shown_history_store.py `wall_history_in`),
-- since a covered layer that keeps its photo is drawn but never reported presented. One latest
-- row per Frame is read through this index, as shown_photos_kept serves shown_photos.
--
-- Plain statements only: no trigger, stored procedure or function. Forward-only (central/db.py).

CREATE INDEX photo_selections_kept ON photo_selections(frame_id, starts_at DESC)
    WHERE after_end <> 'leave_as_is';
