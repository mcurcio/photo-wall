"""The one source of the Player's session cadence and Central's silence threshold.

The Player's session (player.service, player.central_link) binds these values, and Central serves
SILENT_AFTER_SECONDS and KEEPALIVE_SECONDS to the operator console, so the threshold at which a
Player reads as silent cannot drift from the session it models. Stdlib only.

A Player is live while its session socket is open, and for SILENT_AFTER_SECONDS after it closes
(central/infra/player_contact_store.py): Central writes its contact when the socket opens and when
it closes, never between. Liveness is its own domain, never the readiness report, which is sent
once per change. SILENT_AFTER_SECONDS allows one reconnection after a close: the Player finds a
socket Central closed only on its own side dead by its keepalive (a ping unanswered within
KEEPALIVE_SECONDS, sent every KEEPALIVE_SECONDS), sleeps SESSION_BACKOFF[0], and opens a new
session (locate and hello, each allowed REQUEST_TIMEOUT) whose socket's open writes it connected.
A Pi that vanishes is found by Central's server's own ping (uvicorn's), and its close written
then. A Player that fails twice within 30 s can read as silent while alive.
"""

from __future__ import annotations

from typing import Final

SESSION_BACKOFF: Final = (1, 5, 15, 60)
"""Seconds the Player sleeps between session attempts, indexed by consecutive failures."""

REQUEST_TIMEOUT: Final = 15.0
"""Seconds any one request from the Player to Central may take."""

KEEPALIVE_SECONDS: Final = 10.0
"""Seconds between two pings the Player sends on its session socket, and the time a ping may go
unanswered before the Player closes the socket (player.central_link `WEBSOCKET_OPTIONS`)."""

SILENT_AFTER_SECONDS: Final = (2 * KEEPALIVE_SECONDS + SESSION_BACKOFF[0] + 2 * REQUEST_TIMEOUT)
"""Seconds after a Player's session socket closed before it reads as silent (51 s)."""
