-- Decision 0036 (slice 3, the independent review): an everyday action whose own row does not tell
-- who asked it, or does not outlive the request, is one more kind in Central's event log,
-- 'everyday' (central/infra/events.py `EventKind`, `everyday_in`), kept a week like every event,
-- `recorded_by` the person who asked (decision 0035's audit value):
-- * a Pause request, body {"action": "pause", "request_id", "answer"}: its answer, so a repeat of
--   the request is known after a later pause replaced its `frame_pauses` rows (one per request,
--   the partial unique index below, as `skips` keeps one row per request);
-- * Show again, body {"action": "show-again", "asset_id"}: its `never_show` row is deleted, so the
--   event is what says who took the photo off the list, and when.
-- Plain statements, no trigger or function, forward-only.
ALTER TABLE events DROP CONSTRAINT events_kind_check;
ALTER TABLE events ADD CONSTRAINT events_kind_check
    CHECK (kind IN ('record', 'gap', 'link-action', 'refusal', 'frame-status', 'everyday'));

CREATE UNIQUE INDEX events_everyday_request ON events ((body->>'action'), (body->>'request_id'))
    WHERE kind = 'everyday' AND body ? 'request_id';
