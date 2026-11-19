"""The app unit's stop timeout: systemd's TimeoutStopSec for the Player's unit (process_linux).

The broker never stops the app itself (issue #125, decision 0034 S4c: the release switch's owned
stop went with the online path); PID1 stops it with its Weston or at shutdown, within this bound.
"""
from __future__ import annotations

from typing import Final

STOP_TIMEOUT_SECONDS: Final = 30
