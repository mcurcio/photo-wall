"""The one source of the Player's reporting cadence and Central's silence threshold.

The Player's session loop (player.service, player.central_link) binds these values, and Central
serves SILENT_AFTER_SECONDS to the operator console, so the threshold at which a Player reads as
silent cannot drift from the loop it models. Stdlib only.

SILENT_AFTER_SECONDS allows one failed control cycle against a Central that answers the retry
promptly: the failing cycle issues poll_state and then the readiness POST, each allowed
REQUEST_TIMEOUT; after a session of 30 s or longer the Player sleeps SESSION_BACKOFF[0] before the
retry; the retry's first accepted report lands about one REPORT_INTERVAL later. A Central that
answers near its timeout, or a Player that fails twice within 30 s, can read as silent while alive
(docs/operator-console-ux-pass2.md section 2).
"""

from __future__ import annotations

from typing import Final

REPORT_INTERVAL: Final = 0.5
"""Seconds between the starts of two readiness reports in one live session."""

SESSION_BACKOFF: Final = (1, 5, 15, 60)
"""Seconds the Player sleeps between session attempts, indexed by consecutive failures."""

REQUEST_TIMEOUT: Final = 15.0
"""Seconds any one request from the Player to Central may take."""

SILENT_AFTER_SECONDS: Final = 2 * REQUEST_TIMEOUT + SESSION_BACKOFF[0] + REPORT_INTERVAL
"""Seconds after its last accepted report before a Player reads as silent (31.5 s)."""
