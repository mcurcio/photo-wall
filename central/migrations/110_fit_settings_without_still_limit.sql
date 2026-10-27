-- A photo larger than its Frames need always fits (2026-10-26): the photo fit settings no longer
-- hold the Player's still limit (`max_still_edge`). The Player decodes only its Frame's window at
-- its finish size, so no copy is sized for a Player limit. The saved row keeps every other value;
-- `scaled_copy_ratio` reads at its default while it is missing (central/wall_plan/fit_settings.py).
-- Without this, the row would no longer validate and every saved value would read as its default.
-- Plain statements only. Forward-only.
UPDATE photo_fit_settings SET settings = settings - 'max_still_edge';
