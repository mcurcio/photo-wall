"""Configuration snapshots (owner, 2026-10-13: "configuration snapshots" that keep "a history of what
has been done in the past"; scope "Everything per Display+Frame"; use "View and restore").

Pure: the read contract over the two owners' histories (a Display's picture history,
central/displays/picture_history.py; a Frame's Position history, central/fleet/position_history.py),
the rule that composes one Frame's snapshot at a minute from them, the page bounds and the retention
every history writer trims to. It never writes and never decides current state. The PostgreSQL
edge is central/infra/tuning_history_store.py (reads) and each owner's store (writes); the operator
edge is central/tuning_history_routes.py. No psycopg, no FastAPI, no nodeapi (pyproject's
import-linter).
"""
