"""Display power policy (roadmap 2a; Power and the week, slices T and S2): Night off, holds (All
off, All on, Resume schedule) and the power timetable each HDMI port is sent.

One decider (frame design rule 1): Central decides whether each display should be on; the Pi
switches at the set times on its own clock and reports what it did. Display power is operational
state, kept apart from authored content (R5): nothing here reads a Scene.

Pure domain: no psycopg, no FastAPI, no nodeapi (pyproject's import-linter forbids them); it never
imports central.displays (displays import the timetable, never the other way). The PostgreSQL edge
is central/infra/power_store.py; the operator edge is central/power_routes.py; the Output document
carries the timetable (central/displays/model.py `project_output`).
"""
