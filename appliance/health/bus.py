"""The health judge on its Node's bus (issue #126; contracts/node_health.py).

The judge declares the `health` store line (contracts.node_link.STORE_LINES: fleet pipe): its state
bucket holds `birth`, `outbox` and HEALTH_REPORT_KEY, the judge's newest `HealthReport`; a circular
record buffer takes the rest of the line (the session's own `health.record.call`; the judge
registers no method). Central records every value it reads (node_link_records) and reads the newest
for the Frame page's status; Home Assistant may read it through the Pi's read-only door.

The session connects forever on its own thread and attaches at every connect, so after a bus restart
(which starts empty) the line, `birth` and the last report are back with no help from Central. The
judge never waits on the bus: `BusHealthSink.put` only hands the report to the session's queue.

The slice is built at import, so a key table that does not fit the line fails here, on every build.
"""
from __future__ import annotations

from typing import Final

from contracts.node_health import (
    HEALTH_COMPONENT,
    HEALTH_LINE,
    HEALTH_REPORT_BYTES,
    HEALTH_REPORT_KEY,
    HEALTH_SCHEMA_MAJOR,
    HealthReport,
    encode_health_report,
)
from contracts.node_link import NODE_BUS_URL, STORE_LINES
from nodeapi.buffers import KeyTable, Slice, event_buffer, state_bucket
from nodeapi.node import NodeSession, Release

HEALTH_VERSION: Final = "1.0.0"                   # the health component's release version (birth)
HEALTH_STATE: Final = KeyTable({HEALTH_REPORT_KEY: HEALTH_REPORT_BYTES})


def _health_slice() -> Slice:
    """The record buffer takes every byte of the health line the state bucket leaves."""
    state = state_bucket(HEALTH_LINE, HEALTH_STATE)
    records = event_buffer(HEALTH_LINE, "record", STORE_LINES[HEALTH_LINE].max_bytes - state.max_bytes)
    return Slice(HEALTH_LINE, (records, state))


HEALTH_SLICE: Final[Slice] = _health_slice()


def health_session(release_digest: str, *, url: str = NODE_BUS_URL) -> NodeSession:
    """The health component's session, not started: it declares the health line with HEALTH_SLICE,
    writes birth (Release(HEALTH_VERSION, digest=release_digest,
    schema_majors={"health": HEALTH_SCHEMA_MAJOR})) at every attach and registers no method. Its
    start() returns at once and it connects forever, so the judge never waits on the bus."""
    return NodeSession(HEALTH_COMPONENT, HEALTH_SLICE,
                       Release(HEALTH_VERSION, release_digest, {HEALTH_LINE: HEALTH_SCHEMA_MAJOR}), url=url)


class BusHealthSink:
    """Where the judge puts each new verdict: the session's HEALTH_REPORT_KEY. `put` holds the
    encoded report in the session (which publishes it while attached and re-puts it at every
    attach) and returns at once: it never blocks or waits for the bus."""

    def __init__(self, session: NodeSession) -> None:
        self._session = session

    def put(self, report: HealthReport) -> None:
        """ValueError("health_report_too_large") when the report does not fit its key."""
        self._session.state.put(HEALTH_REPORT_KEY, encode_health_report(report))


__all__ = ["HEALTH_SLICE", "HEALTH_STATE", "HEALTH_VERSION", "BusHealthSink", "health_session"]
