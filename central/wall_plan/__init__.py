"""The wall plan (roadmap 5a; run ledger .claude/runs/photo-5a.md): pure rules for putting a photo
pool's photos on Frames. No I/O, no clock, no database, no Immich (pyproject.toml: "The wall plan
is pure").

- `key_content`: the vocabulary of what a photo holds (faces, an interest grid), measured once per
  photo by the media worker (media/key_content.py) and read by the Planner.
- `fit`: the photo fit rule: whether a photo fills a Frame with its key content kept, and the crop.
- `selection`: a Scene's selection settings (order, no-repeat days, crop limit).
- `order`: each Scene's line of photos, in its order, across passes.
- `assignment`: the wall assignment: every Frame's photos over the plan horizon, computed together.
- `ports`: what the wall plan's stores answer (shown history, key content).

central/planner.py composes them; the wall plan never imports the Planner or Runtime.
"""
