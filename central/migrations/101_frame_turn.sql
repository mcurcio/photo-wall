-- The Display turn (render redesign S2, run ledger .claude/runs/render-planes/s2-page.md): how a
-- Frame's screen hangs, 0, 90, 180 or 270 degrees clockwise (contracts/display_turn.py). It belongs
-- to the spot on the wall (owner, gate Q3, 2026-10-17), so it is the Frame's, kept through a Pi swap
-- or a port change; Central sends it in the Output document of the Output the Frame is bound to
-- (an unbound Output's is 0). The rule of what a turn may be is app code (contracts/display_turn.py
-- `is_turn`); no CHECK, trigger or stored procedure.
--
-- Upgrade data: every Frame hangs as built (0). A Position whose own rotation is a quarter turn
-- keeps drawing by it until migration 102 (S5) turns every Position into a rectangle and moves that
-- quarter turn here. No Frame's generation moves, so every ready Frame stays ready.
ALTER TABLE frames ADD COLUMN turn SMALLINT NOT NULL DEFAULT 0;

-- Every stored Output document decodes under the display line's schema major 9 (no backwards-
-- compatible decoder): each gains the turn its Output has now, 0 (every Frame hangs as built). Its
-- digest stays the pre-101 one, so the document source's next read rewrites it under the next
-- `change` and the Pi, on schema 9, is sent it.
UPDATE output_documents SET document = document || '{"turn": 0}'::jsonb;
