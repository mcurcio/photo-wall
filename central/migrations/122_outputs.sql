-- Decision 0034, S2f part 1. One Output row per HDMI port: `outputs` (keyed (player_id, output_id))
-- holds the Pi's observation (enrollment's, nullable now: a port first heard in an Output report has
-- none), the Display seen there and its Output report (was `output_displays`), the Output document
-- and the Frame region document with their change numbers and digests (`output_documents`), the
-- picture document (`picture_documents`) and the picture report (`picture_reports`). Displays owns
-- the row; central/infra/display_store.py is its one repository (the Registry's observation writes
-- and Output reads go through it). Each writer writes the row only when a column it holds changes.
-- The row gains its audit fields; a row's writer before this release is unknown. Plain statements,
-- no trigger or function, forward-only.
ALTER TABLE outputs
    ALTER COLUMN observation DROP NOT NULL,
    ADD COLUMN connected BOOLEAN,
    ADD COLUMN display_identity JSONB,
    ADD COLUMN display_id UUID REFERENCES displays(id) ON DELETE SET NULL,
    ADD COLUMN display_report JSONB,
    ADD COLUMN display_reported_at DOUBLE PRECISION,
    ADD COLUMN document_change BIGINT CHECK (document_change >= 1),
    ADD COLUMN document_digest TEXT,
    ADD COLUMN document JSONB,
    ADD COLUMN region_change BIGINT CHECK (region_change >= 1),
    ADD COLUMN region_digest TEXT,
    ADD COLUMN picture_change BIGINT CHECK (picture_change >= 1),
    ADD COLUMN picture_digest TEXT,
    ADD COLUMN picture_document JSONB,
    ADD COLUMN picture_report JSONB,
    ADD COLUMN picture_reported_at DOUBLE PRECISION,
    ADD COLUMN created_at DOUBLE PRECISION,
    ADD COLUMN created_by TEXT,
    ADD COLUMN changed_at DOUBLE PRECISION,
    ADD COLUMN changed_by TEXT;

-- A port known only by its reports or documents (no enrollment observation) becomes a row too.
INSERT INTO outputs(player_id, output_id)
SELECT player_id, output_id FROM output_displays
UNION SELECT player_id, output_id FROM output_documents
UNION SELECT player_id, output_id FROM picture_documents
UNION SELECT player_id, output_id FROM picture_reports
ON CONFLICT (player_id, output_id) DO NOTHING;

UPDATE outputs o SET connected = s.connected, display_identity = s.identity, display_id = s.display_id,
                     display_report = s.report, display_reported_at = s.reported_at
FROM output_displays s WHERE s.player_id = o.player_id AND s.output_id = o.output_id;

UPDATE outputs o SET document_change = w.change, document_digest = w.digest, document = w.document,
                     region_change = w.region_change, region_digest = w.region_digest
FROM output_documents w WHERE w.player_id = o.player_id AND w.output_id = o.output_id;

UPDATE outputs o SET picture_change = pd.picture_change, picture_digest = pd.digest,
                     picture_document = pd.document
FROM picture_documents pd WHERE pd.player_id = o.player_id AND pd.output_id = o.output_id;

UPDATE outputs o SET picture_report = pr.report, picture_reported_at = pr.reported_at
FROM picture_reports pr WHERE pr.player_id = o.player_id AND pr.output_id = o.output_id;

UPDATE outputs SET created_at = coalesce(least(display_reported_at, picture_reported_at), EXTRACT(EPOCH FROM now())),
                   created_by = 'unknown',
                   changed_at = coalesce(greatest(display_reported_at, picture_reported_at), EXTRACT(EPOCH FROM now())),
                   changed_by = 'unknown';

ALTER TABLE outputs
    ALTER COLUMN created_at SET NOT NULL,
    ALTER COLUMN created_by SET NOT NULL,
    ALTER COLUMN changed_at SET NOT NULL,
    ALTER COLUMN changed_by SET NOT NULL,
    ADD CONSTRAINT outputs_display_report CHECK (
        (connected IS NULL) = (display_report IS NULL) AND (display_report IS NULL) = (display_reported_at IS NULL)),
    ADD CONSTRAINT outputs_document CHECK (
        (document_change IS NULL) = (document_digest IS NULL) AND (document_change IS NULL) = (document IS NULL)),
    ADD CONSTRAINT outputs_region CHECK ((region_change IS NULL) = (region_digest IS NULL)),
    ADD CONSTRAINT outputs_picture_document CHECK (
        (picture_change IS NULL) = (picture_digest IS NULL) AND (picture_change IS NULL) = (picture_document IS NULL)),
    ADD CONSTRAINT outputs_picture_report CHECK ((picture_report IS NULL) = (picture_reported_at IS NULL));

DROP TABLE output_displays, output_documents, picture_documents, picture_reports;
