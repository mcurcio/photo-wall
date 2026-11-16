"""The Player's outbox of observations for Central (the transactional outbox, acknowledged by
Central's answer): every report the executor makes (`player/executor.py` `_report`, a state change
numbered once) waits here until Central answers it, and is never dropped for want of room.

Each assignment holds at most one pending report, its newest: a later change replaces one not yet
sent (what Central needs is where the assignment is now), so the outbox is bounded by the
assignments the plan holds however long Central is slow or away. A report Central did not answer
goes back to the head with its number unchanged, unless a newer one for its assignment arrived
meanwhile. A report Central answered stale (409: it offered a newer revision of the plan meanwhile)
is never made again by the executor, so it waits here until the Player holds a newer revision and
is then sent under it, its number unchanged (`renewed`); never before, so a 409 is not a busy loop.
Thread-safe: the main loop puts, the session's sender takes and returns.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

from contracts.models import Observation


class ReportOutbox:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: OrderedDict[str, Observation] = OrderedDict()
        # Reports Central answered stale, each until the Player holds a newer revision.
        self._stale: dict[str, Observation] = {}

    def put(self, report: Observation) -> None:
        """Queue `report`, replacing a pending one for its assignment."""
        with self._lock:
            self._stale.pop(report.assignment_id, None)
            self._pending.pop(report.assignment_id, None)
            self._pending[report.assignment_id] = report

    def take(self) -> Observation | None:
        """The oldest pending report, removed; None when there is none."""
        with self._lock:
            return self._pending.popitem(last=False)[1] if self._pending else None

    def again(self, report: Observation) -> None:
        """Return an unanswered `report` to the head, unless a newer one replaced it."""
        with self._lock:
            if report.assignment_id not in self._pending:
                self._pending[report.assignment_id] = report
                self._pending.move_to_end(report.assignment_id, last=False)

    def stale(self, report: Observation) -> None:
        """Keep `report`, answered stale, until a newer revision renews it, unless a newer one
        for its assignment replaced it."""
        with self._lock:
            if report.assignment_id not in self._pending:
                self._stale[report.assignment_id] = report

    def renewed(self, plan_id: str, revision: int) -> None:
        """The Player holds `revision` of plan `plan_id`: each stale report under an earlier
        revision of it goes back to the head, stamped with `revision`, its number unchanged."""
        with self._lock:
            for key, report in tuple(self._stale.items()):
                if report.plan_id != plan_id:
                    del self._stale[key]        # another plan's: no longer Central's to judge
                elif report.revision < revision:
                    del self._stale[key]
                    self._pending[key] = report.model_copy(update={"revision": revision})
                    self._pending.move_to_end(key, last=False)

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()
            self._stale.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._pending) + len(self._stale)
