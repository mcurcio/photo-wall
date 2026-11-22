"""A person's Restart app between the bus and the broker's loop (decision 0036, "Restart app").

The bus method is answered on the session's thread, at once; every effect (the journal, systemctl)
is the loop's (`broker_runner.BrokerLoop`). Two small objects stand between them, neither of which
touches the bus or a process:

- `RestartAsks`: the one slot a request waits in. It answers each ask (`accepted`, `in_flight`,
  `duplicate`: contracts/node_apps.py) from what it holds alone, so two requests while one restart
  is in flight are one restart, and a request already served is never served again.
- `RestartSteps`: states each step of the journal's restart once, in order, to a sink (the bus
  line: appliance/apps/bus.py).

Both are seeded from the journal's record, so a broker that starts again within a boot neither
serves an old request twice nor states its steps twice. Stdlib only.
"""
from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import Final, Literal

from appliance.apps.broker import Restart

AskOutcome = Literal["accepted", "in_flight", "duplicate"]   # contracts/node_apps.py AppRestartOutcome
# What a restart at each journal step has stated, in order (`stopping` states nothing: Central
# wrote "requested" itself). A start that raised happened after the stop was seen done.
_STATED: Final[dict[str, tuple[str, ...]]] = {
    "stopping": (),
    "app_stopped": ("app_stopped",),
    "app_started": ("app_stopped", "app_started"),
    "not_started": ("app_stopped", "not_started"),
    "not_stopped": ("not_stopped",),
}


class RestartAsks:
    """The slot between the bus method and the loop. `ask` (the session's thread) answers a request
    and holds an accepted one for `take` (the loop's thread); `settle` (the loop) ends the restart
    in flight. At most one request is held or in flight at a time."""

    def __init__(self, newest: Restart | None = None) -> None:
        """`newest`: the journal's restart when the broker started (None: none was asked this boot)."""
        self._lock = Lock()
        self._newest = None if newest is None else newest.request_id
        self._in_flight = newest is not None and newest.in_flight
        self._waiting: str | None = None

    def ask(self, request_id: str) -> AskOutcome:
        """`in_flight` while a restart is in flight (this request's or another's); `duplicate` for
        the newest request once it was served; else the request is held for the loop: `accepted`."""
        with self._lock:
            if self._in_flight:
                return "in_flight"
            if request_id == self._newest:
                return "duplicate"
            self._newest, self._in_flight, self._waiting = request_id, True, request_id
            return "accepted"

    def take(self) -> str | None:
        """The accepted request the loop has not taken yet, once; None otherwise."""
        with self._lock:
            waiting, self._waiting = self._waiting, None
            return waiting

    def settle(self, request_id: str) -> None:
        """The restart `request_id` asked for is over (started, or failed): the next request is a
        new restart. Nothing for any other request, or while one waits for the loop."""
        with self._lock:
            if request_id == self._newest and self._waiting is None:
                self._in_flight = False


class RestartSteps:
    """Each step of the journal's restart, stated once to `state(request_id, step)`, in order."""

    def __init__(self, state: Callable[[str, str], None], said: Restart | None = None) -> None:
        """`said`: the journal's restart when the broker started, taken as already stated."""
        self._state = state
        self._said = said

    def tell(self, restart: Restart | None) -> None:
        """State what `restart` has reached and this object has not yet said of that request."""
        if restart is None or restart == self._said:
            return
        said = self._said
        done = _STATED[said.step] if said is not None and said.request_id == restart.request_id else ()
        for step in _STATED[restart.step]:
            if step not in done:
                self._state(restart.request_id, step)
        self._said = restart


__all__ = ["AskOutcome", "RestartAsks", "RestartSteps"]
