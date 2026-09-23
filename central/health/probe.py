"""The pod probe behind `/livez` and `/readyz` (design §9 decision 5).

Ready means this process is up AND can reach the database. It never checks cache contents,
origins or convergence: a pod that can serve from the DB-backed catalog is ready even when a
download is failing or the cache is cold.
"""

from __future__ import annotations

from collections.abc import Callable


class PodProbe:
    """`/livez` and `/readyz`: process + DB, never cache/origins."""

    def __init__(self, database_reachable: Callable[[], bool]) -> None:
        self._database_reachable = database_reachable

    def live(self) -> bool:
        """Always True: the process answered. Needs no database."""
        return True

    def ready(self) -> bool:
        """True iff the database is reachable; any exception from the check is False."""
        try:
            return self._database_reachable() is True
        except Exception:
            return False
