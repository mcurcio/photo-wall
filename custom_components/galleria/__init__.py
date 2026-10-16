"""Galleria for Home Assistant: one config entry per Central (`{origin}`), with devices Central,
Installation, Player (per Pi) and Frame, speaking NATS only (owner, 2026-10-16: "we should build a
HACS plugin into the repo so that home assistant can install photo-wall and users get a native
experience"; the integration's name: "the HACS integration should be called "galleria" instead of
photo-wall, because that is going to be the future name of this project").

It reaches Central's hub through Central's door (contracts/central_api.py, as generated into
`contract.py`) and each Pi through its read-only door (contracts/node_link.py NODE_DOOR_PORT). It
never imports a package of this repository at runtime: every name, key and shape it uses comes from
its generated copy, `contract.py` (written by scripts/galleria_contract.py; a check test fails when it
drifts).
"""
from __future__ import annotations

from typing import Final

DOMAIN: Final = "galleria"
