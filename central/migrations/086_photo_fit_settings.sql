-- Follow-ups 2026-10-11 (run ledger .claude/runs/photo-5a.md, slice B1): the photo fit rule's
-- tuning values, one row, validated by central.wall_plan.fit_settings.FitSettings (the one home
-- of the ranges). No row: today's values. Plain statements only. Forward-only.
CREATE TABLE photo_fit_settings (
  singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
  settings JSONB NOT NULL,
  updated_at DOUBLE PRECISION NOT NULL);
