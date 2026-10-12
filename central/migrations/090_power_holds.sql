-- Holds (F5, #90, #91): one row per held Frame; the newest hold on a Frame replaces the older one; Resume
-- schedule deletes the row. `hold_id` is the console's request id (a double tap makes one hold). `started_at`
-- and `ends_at` are Central's clock, compared only with Central's clock; NULL ends_at: until resumed.
CREATE TABLE power_holds (
  frame_id TEXT PRIMARY KEY REFERENCES frames(id) ON DELETE CASCADE,
  hold_id UUID NOT NULL,
  power TEXT NOT NULL CHECK (power IN ('on', 'off')),
  source TEXT NOT NULL CHECK (source IN ('console')),
  started_at DOUBLE PRECISION NOT NULL,
  ends_at DOUBLE PRECISION,
  CHECK (ends_at IS NULL OR ends_at > started_at));
CREATE INDEX power_holds_hold ON power_holds (hold_id);

-- How long a hold lasts (#90): one row, one rule for every hold, set beside All off and All on. Seeded
-- 'next-scheduled-change': each Frame's own next change (central/power/timetable.py hold_end).
CREATE TABLE hold_settings (
  singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
  hold_lasts TEXT NOT NULL CHECK (hold_lasts IN ('next-scheduled-change', 'until-resumed')),
  updated_at DOUBLE PRECISION NOT NULL);
INSERT INTO hold_settings (singleton, hold_lasts, updated_at)
VALUES (TRUE, 'next-scheduled-change', EXTRACT(EPOCH FROM clock_timestamp()));
