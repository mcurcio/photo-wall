-- The Frame region document (issue #124; debug overlay design revision 2): Central sends each
-- Output its bound Frame's rectangle in whole pixels on its own key (contracts/node_output.py
-- `region_document_key`), beside the Output document, with its own change number, so a saved
-- Position or a new mode never raises the Output document's `change`. The number and the digest it
-- was raised for live on the Output document's row (central/infra/display_store.py
-- `output_documents`, which upserts that row first); NULL until the first region is worked out.
-- No trigger or function: the rule of when the number rises is app code (`model.region_digest`).
--
-- The display line's schema major goes 10 to 11 (`OutputReport.current_mode`, always present on
-- the wire): every stored Output report gains `current_mode` null, so each one decodes under
-- schema 11 (migration 103's precedent). No backwards-compatible decoder.
ALTER TABLE output_documents ADD COLUMN region_change INTEGER CHECK (region_change >= 1),
    ADD COLUMN region_digest TEXT;

UPDATE output_displays SET report = report || '{"current_mode": null}'::jsonb;
