"""Galleria's own constants: its domain, how often it reads, how long it waits. Every name, key and
shape it shares with Photo Wall is in the generated contract.py, never here."""
from __future__ import annotations

from typing import Final

DOMAIN: Final = "galleria"
CONF_ORIGIN: Final = "origin"       # the entry's one setting: Central's origin, as the console is reached
POLL_SECONDS: Final = 5.0           # Central's state and every Pi, read this often (gets only, no consumers)
INSTALL_TIMEOUT: Final = 10.0       # installPlayer answers once, within this
PI_TIMEOUT: Final = 2.0             # one Pi's connect, and each of its gets
CENTRAL_TIMEOUT: Final = 3.0        # each of Central's gets and each state's publish
