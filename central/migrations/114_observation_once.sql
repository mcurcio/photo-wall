-- A Player's observation is written once (central/coordination.py `observe`): each report carries
-- its Player's number for it within the authority epoch (contracts/models.py `Observation`), and a
-- report sent again (Central committed it, then answered lock_busy or late) adds no row. On
-- 2026-11-16 one still photo wrote 553 rows in six minutes: the Player reported it presented every
-- half second and every report was a new row.
--
-- Columns added without a default rewrite no table; every existing row and every other event kind
-- keeps NULLs, which never conflict. The table holds about 10,000 rows (central/infra/
-- execution_event_retention.py), so the index builds at once. Plain statements only: no trigger,
-- stored procedure or function. Forward-only (central/db.py).
--
-- migrate() runs in central/db.py's transaction, so the ALTER waits at most its 5 s lock_timeout
-- behind an open transaction on the table (a slow COMMIT), and the queue behind it no longer: past
-- it the start fails with nothing changed and the pod tries again.
ALTER TABLE execution_events ADD COLUMN authority_epoch BIGINT,
  ADD COLUMN report_sequence BIGINT;
CREATE UNIQUE INDEX execution_events_report_once
  ON execution_events (player_id, authority_epoch, report_sequence);
