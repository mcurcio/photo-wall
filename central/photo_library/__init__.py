"""The photo library context (roadmap 4, run ledger .claude/runs/photo-4.md): Immich connections
and their health, Photo source queries, and the pool each query becomes.

Pure domain: no I/O, no Immich client, no database (import-linter "The photo library domain is
pure"). Central's store (central/infra/photo_library_store.py) persists it; the media worker
(media/library_checks.py) is the only code that talks to Immich and reads a key; the Planner sees
only `PoolMember`s, never an Immich id, URL or key.
"""
