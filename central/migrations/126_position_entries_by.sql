-- 126_position_entries_by.sql: who appended each Position entry (decision 0034, S2f's erratum).
-- An append-only history meets the audit rule by its rows' `at` and `by`, as picture_history does
-- (122b). Entries appended before this migration did not record who: they read 'unknown'.
ALTER TABLE position_entries ADD COLUMN by TEXT;
UPDATE position_entries SET by = 'unknown';
ALTER TABLE position_entries ALTER COLUMN by SET NOT NULL;
