"""A Frame's lighting, owned in one place (pure): whether it fits the house (every named room light
exists and its reference is the kind its reporter needs, its paper profile is known), the one
lighting a new Frame starts with, the trial envelope's size limit, and which Frames name a room light
or a paper profile. contracts/frame_lighting.py holds the shape; this package holds the rules.

Every writer of a Frame's lighting (Position's trial begin and edit, a Restore from history, a new
Frame) calls the one port, `ports.FrameLightingCheck`, inside its own transaction; its
implementation (central/infra/frame_lighting_store.py) reads the house under the room-lights lock, so
a room light or paper profile cannot be removed between the check and the write.

Pure: no psycopg, FastAPI, nodeapi or central.infra (pyproject's import-linter). It reads room lights'
vocabulary (central.room_light.model); room lights never import it.
"""
