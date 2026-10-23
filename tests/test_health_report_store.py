"""A recorded health line to the Frame page's status (issue #126): the health judge's verdict, as
Central's link store records it from the Pi's `health` line, is read back by
central/infra/health_report_store.py and becomes the bound Frame's `dark` in the inventory. Only the
entry of the Output the Frame is bound to counts; a value that does not decode, or a removed key,
gives none. The same path from a real Pi's bus is the node-pid1 `dark` leg's."""
from __future__ import annotations

import asyncio

from test_registry import enroll, frame

from central.content_catalog.catalog import device_id_for_serial
from central.infra.health_report_store import (
    HEALTH_REPORT_SUBJECT,
    HEALTH_STATE_STREAM,
    health_reports_in,
)
from central.infra.node_link_store import PgLinkStores
from contracts.node_faults import FAULTS, catalogue_digest
from contracts.node_health import HealthReport, OutputHealth, encode_health_report
from contracts.node_link import Pipe
from nodeapi.buffers import KV_OPERATION_HEADER
from nodeapi.epoch import Token
from nodeapi.pull import Batch, Read

SERIAL = "10000000d4a3e126"
SINCE = "2026-10-20T08:10:00Z"


def _commit(registry, value: bytes, seq: int, headers: dict | None = None) -> None:
    """One health-line record, committed by the real link store as Central's NodeLink does."""
    read = Read(Token("epoch", seq), HEALTH_REPORT_SUBJECT, headers or {}, value)
    asyncio.run(PgLinkStores(registry.db).store(SERIAL, Pipe.FLEET).commit(
        HEALTH_STATE_STREAM, Batch((read,), Token("epoch", seq), False)))


def _verdict(sequence: int, *outputs: OutputHealth) -> bytes:
    return encode_health_report(HealthReport(catalogue_digest(), outputs))


def _dark(registry, frame_id: str):
    return next(f for f in registry.inventory().frames if f.id == frame_id).dark


def test_a_recorded_verdict_makes_only_the_bound_outputs_frame_dark(registry):
    device = device_id_for_serial(SERIAL)
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO devices(device_id, serial, first_seen, last_seen) VALUES (%s, %s, 1, 1)",
                     (device, SERIAL))
    identity = enroll(registry, device_id=device, count=2)[0]
    frame(registry, "hearth")
    frame(registry, "spare")
    registry.bind("hearth", identity["player_id"], "HDMI-A-2", expected_generation=0)
    assert _dark(registry, "hearth") is None            # no verdict yet

    # The other Output is dark; the Frame's own is lit: the Frame is not dark.
    _commit(registry, _verdict(1, OutputHealth("HDMI-A-1", True, "app_absent", SINCE),
                               OutputHealth("HDMI-A-2", False, None, None)), 1)
    assert _dark(registry, "hearth") is None
    assert _dark(registry, "spare") is None

    # Its own Output goes dark: the newest record is read, worded by the catalogue.
    _commit(registry, _verdict(2, OutputHealth("HDMI-A-1", False, None, None),
                               OutputHealth("HDMI-A-2", True, "app_descriptor_limit", SINCE)), 2)
    dark = _dark(registry, "hearth")
    assert (dark.code, dark.line, dark.since) == (
        "app_descriptor_limit", FAULTS["app_descriptor_limit"].household_line, SINCE)
    assert _dark(registry, "spare") is None
    with registry.db.transaction() as conn:
        assert set(health_reports_in(conn)) == {identity["player_id"]}

    # A newer value that does not decode reads as no verdict: never the older one.
    _commit(registry, b"not a verdict", 3)
    assert _dark(registry, "hearth") is None
    with registry.db.transaction() as conn:
        assert health_reports_in(conn) == {}

    # A dark verdict again, then the key removed: no verdict.
    _commit(registry, _verdict(0, OutputHealth("HDMI-A-2", True, "app_absent", SINCE)), 4)
    assert _dark(registry, "hearth").code == "app_absent"
    _commit(registry, b"", 5, headers={KV_OPERATION_HEADER: "DEL"})
    assert _dark(registry, "hearth") is None
