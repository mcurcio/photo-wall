-- The Display's size (frame box run, .claude/runs/frame-box/page.md, bead fb-2): its picture area
-- as built, in whole millimetres, as its EDID base block states it (bytes 21 and 22, centimetres
-- times 10; appliance/display_host/edid.py `parse_edid`). Central keeps the size of the latest
-- sighting that states one (central/infra/display_store.py `_display_for`); NULL until a Pi on the
-- new release reports it, or when the display states none (a projector). No CHECK, trigger or
-- function: the rule of what a size may be is app code (contracts/node_output.py `OutputReport`).
--
-- The display line's schema major goes 9 to 10 (`OutputReport.size_mm`, always present on the
-- wire): every stored Output report gains `size_mm` null, so each one decodes under schema 10.
-- No backwards-compatible decoder.
ALTER TABLE displays ADD COLUMN width_mm INTEGER NULL, ADD COLUMN height_mm INTEGER NULL;

UPDATE output_displays SET report = report || '{"size_mm": null}'::jsonb;
