-- Names (#94) and the wall's name and room (#92, one wall today; 6 adds more). Plain statements.
ALTER TABLE frames ADD COLUMN name TEXT;
UPDATE frames f SET name = 'Frame ' || n.rank
  FROM (SELECT id, row_number() OVER (ORDER BY id) AS rank FROM frames) n WHERE f.id = n.id;
ALTER TABLE frames ALTER COLUMN name SET NOT NULL;
CREATE UNIQUE INDEX frames_name ON frames (lower(name));
CREATE TABLE walls (
  surface_id TEXT PRIMARY KEY,
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 60),
  room TEXT NOT NULL DEFAULT '' CHECK (length(room) <= 60));
INSERT INTO walls (surface_id, name)
SELECT surface_id, 'Wall ' || row_number() OVER (ORDER BY surface_id) FROM (SELECT DISTINCT surface_id FROM frames) s;
