"""The display component on its Node's bus (roadmap 1b; run ledger .claude/runs/display-1b.md,
slices F0 and D1; decision 0017 C11, E3b design §7.3).

The display controller declares the `display` store line (contracts.node_link.STORE_LINES: fleet
pipe, 3 streams, 1.5 MiB; the Node store is at 17 of 17 streams, so 1b adds no line and no
stream). Its three streams:

- `KV_desired_display`, the desired bucket Central writes: one Output document per Output
  (`contracts.node_output.output_document_key`). The table leaves the rest of the line to the
  record buffer; decision 0017 C11's later "Output layout" document joins this table as its own key
  per Output (`layout-<output_id>`), which a release that lists it applies with no new stream.
- `KV_state_display`, the state bucket the controller writes: one Output report per Output
  (`output_report_key`) beside `birth` and `outbox`.
- `RECORD_display`, the circular record buffer: `display.record.power` (one `PowerAttempt` per
  attempt) and the session's own `display.record.call`. The display component registers no method.

The slice is built at import, so a key table that does not fit the line fails here, on every
build, before anything ships.
"""
from __future__ import annotations

from typing import Final

from contracts.node_link import NODE_BUS_URL, STORE_LINES
from contracts.node_output import (
    OUTPUT_DOCUMENT_BYTES,
    OUTPUT_IDS,
    OUTPUT_REPORT_BYTES,
    SCHEMA_MAJOR,
    OutputReport,
    PowerAttempt,
    encode_output_report,
    encode_power_attempt,
    output_document_key,
    output_report_key,
)
from nodeapi.buffers import KeyTable, Slice, desired_bucket, event_buffer, state_bucket
from nodeapi.node import NodeSession, Release

DISPLAY_COMPONENT: Final = "display"
DISPLAY_VERSION: Final = "1.0.0"                   # the display component's release version (birth)
POWER_RECORD: Final = "record.power"               # emit subject of a PowerAttempt, under the line
DISPLAY_STATE: Final = KeyTable({output_report_key(output): OUTPUT_REPORT_BYTES for output in OUTPUT_IDS})
DISPLAY_DESIRED: Final = KeyTable({output_document_key(output): OUTPUT_DOCUMENT_BYTES for output in OUTPUT_IDS})


def _display_slice() -> Slice:
    """The record buffer takes every byte of the display line the two buckets leave."""
    state = state_bucket(DISPLAY_COMPONENT, DISPLAY_STATE)
    desired = desired_bucket(DISPLAY_COMPONENT, DISPLAY_DESIRED)
    records = event_buffer(DISPLAY_COMPONENT, "record",
                           STORE_LINES[DISPLAY_COMPONENT].max_bytes - state.max_bytes - desired.max_bytes)
    return Slice(DISPLAY_COMPONENT, (records, state, desired))


DISPLAY_SLICE: Final[Slice] = _display_slice()


def display_session(release_digest: str, *, url: str = NODE_BUS_URL) -> NodeSession:
    """The display component's session, not started: it declares the display line with
    DISPLAY_SLICE, writes birth (Release(DISPLAY_VERSION, digest=release_digest,
    schema_majors={"display": SCHEMA_MAJOR})) at every attach, registers no method, and exposes the
    desired bucket as `session.desired`. Its start() returns at once and it connects forever, so
    the controller never waits on the bus."""
    return NodeSession(DISPLAY_COMPONENT, DISPLAY_SLICE,
                       Release(DISPLAY_VERSION, release_digest, {DISPLAY_COMPONENT: SCHEMA_MAJOR}), url=url)


class BusReportSink:
    """`output_power.ReportSink` on the display session: an Output's report is its state key, a
    power attempt one `display.record.power` event. Both only queue on the session, so neither
    blocks the Weston dispatch loop."""

    def __init__(self, session: NodeSession) -> None:
        self._session = session

    def put_report(self, report: OutputReport) -> None:
        self._session.state.put(output_report_key(report.output_id), encode_output_report(report))

    def emit_attempt(self, attempt: PowerAttempt) -> None:
        self._session.events.emit(POWER_RECORD, encode_power_attempt(attempt), schema_major=SCHEMA_MAJOR)
