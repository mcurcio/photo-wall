"""The display controller reports each Output's EDID identity and modes on the display line
(roadmap 1b, slice D1; run ledger .claude/runs/display-1b.md).

EDIDs are built here byte by byte (VESA E-EDID, CTA-861), so each case states the bytes a monitor
sends; the spike's test monitor (XYM, product 5475, name "MNN", serial 0, blank serial text,
1920x1080@60) is the first case. The reporter runs on the real `OutputReporter` with the real
`parse_edid`, `read_edid` over a sysfs-shaped directory, and `BusReportSink` on a real (unstarted)
display session, so what lands in the session's state bucket is what the bus would carry.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from appliance.display_host.bus import DISPLAY_SLICE, BusReportSink, display_session
from appliance.display_host.domain import OutputState
from appliance.display_host.edid import parse_edid, read_edid
from appliance.display_host.runner import OutputReporter
from contracts.node_output import (
    DisplayIdentity,
    DisplayMode,
    OutputReport,
    decode_output_report,
    output_report_key,
)
from contracts.node_protocol import OutputKey

BOOT, INCARNATION = uuid4(), uuid4()


def _letters(maker: str) -> bytes:
    packed = 0
    for letter in maker:
        packed = packed << 5 | (ord(letter) - ord("A") + 1)
    return packed.to_bytes(2, "big")


def _text(tag: int, text: str) -> bytes:
    body = text.encode("ascii")
    body = body + b"\n" + b" " * (12 - len(body)) if len(body) < 13 else body
    return bytes((0, 0, 0, tag, 0)) + body


def _timing(width: int, height: int, refresh_hz: int, *, interlaced: bool = False) -> bytes:
    blank_h, blank_v = 280, 45                         # CTA 1080p60: 2200 x 1125 total
    clock = (width + blank_h) * (height + blank_v) * refresh_hz // 10_000
    d = bytearray(18)
    d[0:2] = clock.to_bytes(2, "little")
    d[2], d[3], d[4] = width & 0xFF, blank_h & 0xFF, (width >> 8) << 4 | blank_h >> 8
    d[5], d[6], d[7] = height & 0xFF, blank_v & 0xFF, (height >> 8) << 4 | blank_v >> 8
    d[17] = 0x80 if interlaced else 0x18
    return bytes(d)


def _checksummed(block: bytearray) -> bytes:
    block[127] = (-sum(block[:127])) % 256
    return bytes(block)


def edid(maker: str = "XYM", product: int = 5475, *, name: str | None = "MNN", serial_number: int = 0,
         serial_text: str | None = " ", timings: tuple[bytes, ...] = (), vics: tuple[int, ...] = ()) -> bytes:
    """A base block (and a CTA extension when `vics`) stating the given identity and timings."""
    base = bytearray(128)
    base[0:8] = bytes((0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00))
    base[8:10] = _letters(maker)
    base[10:12] = product.to_bytes(2, "little")
    base[12:16] = serial_number.to_bytes(4, "little")
    base[18], base[19] = 1, 3                          # EDID 1.3
    descriptors = [*timings]
    if name is not None:
        descriptors.append(_text(0xFC, name))
    if serial_text is not None:
        descriptors.append(_text(0xFF, serial_text))
    descriptors += [bytes((0, 0, 0, 0x10)) + bytes(14)] * (4 - len(descriptors))
    for index, descriptor in enumerate(descriptors):
        base[54 + 18 * index:72 + 18 * index] = descriptor
    base[126] = 1 if vics else 0
    raw = _checksummed(base)
    if vics:
        cta = bytearray(128)
        cta[0], cta[1] = 0x02, 3
        cta[4] = 2 << 5 | len(vics)
        cta[5:5 + len(vics)] = bytes(vics)
        cta[2] = 5 + len(vics)
        raw += _checksummed(cta)
    return raw


SPIKE = edid(timings=(_timing(1920, 1080, 60),))
SPIKE_IDENTITY = DisplayIdentity("XYM", 5475, "MNN", None)
P1080 = DisplayMode(1920, 1080, 60000, preferred=True)


# parse_edid: what the bytes say.

def test_the_spike_monitor_has_no_usable_serial_and_prefers_1080p60():
    assert parse_edid(SPIKE) == (SPIKE_IDENTITY, (P1080,))


def test_a_numeric_serial_is_its_decimal():
    identity, _ = parse_edid(edid(serial_number=0x01020304, serial_text=None))
    assert identity == DisplayIdentity("XYM", 5475, "MNN", "16909060")


def test_the_serial_text_wins_over_the_number():
    identity, _ = parse_edid(edid(serial_number=1234, serial_text="ABC-0042"))
    assert identity is not None and identity.serial == "ABC-0042"


def test_a_bad_checksum_has_no_identity():
    broken = bytearray(SPIKE)
    broken[20] ^= 0x01
    assert parse_edid(bytes(broken)) == (None, ())
    assert parse_edid(SPIKE[:127]) == (None, ())
    assert parse_edid(b"") == (None, ())


def test_cta_video_descriptors_follow_the_preferred_mode_once_each():
    _, modes = parse_edid(edid(timings=(_timing(1920, 1080, 60),), vics=(16, 4, 97, 5, 200)))
    assert modes == (P1080, DisplayMode(1280, 720, 60000), DisplayMode(3840, 2160, 60000))


def test_an_interlaced_first_timing_is_neither_a_mode_nor_preferred():
    _, modes = parse_edid(edid(timings=(_timing(1920, 540, 60, interlaced=True),), vics=(4,)))
    assert modes == (DisplayMode(1280, 720, 60000),)


# read_edid: the connector's sysfs file.

def test_read_edid_reads_the_named_connector_only(tmp_path: Path):
    (tmp_path / "card1-HDMI-A-1").mkdir()
    (tmp_path / "card1-HDMI-A-1" / "edid").write_bytes(SPIKE)
    (tmp_path / "card1-HDMI-A-2").mkdir()
    (tmp_path / "card1-HDMI-A-2" / "edid").write_bytes(b"")     # nothing plugged in
    assert read_edid("HDMI-A-1", root=tmp_path) == SPIKE
    assert read_edid("HDMI-A-2", root=tmp_path) is None
    assert read_edid("Virtual-1", root=tmp_path) is None


# The reporter: one Output report per Output, put on the display line only when it changed.

class Sysfs:
    """A connector's EDID as the kernel would show it; None = no EDID (nothing plugged in)."""

    def __init__(self) -> None:
        self.edids: dict[str, bytes | None] = {}

    def read(self, output_id: str) -> bytes | None:
        return self.edids.get(output_id)


def state(output_id: str, generation: int, *, connected: bool = True, mode: int = 1) -> OutputState:
    return OutputState(OutputKey(BOOT, INCARNATION, output_id, generation, mode), connected=connected)


class Reporting:
    def __init__(self) -> None:
        self.session = display_session(DISPLAY_SLICE.digest)
        self.sysfs = Sysfs()
        self.sink = Recording(BusReportSink(self.session))
        self.reporter = OutputReporter(self.sink, read=self.sysfs.read)

    @property
    def puts(self) -> list[OutputReport]:
        return self.sink.puts

    def on_line(self, output_id: str) -> OutputReport | None:
        raw = self.session.state.get(output_report_key(output_id))
        return None if raw is None else decode_output_report(raw)


class Recording:
    """The real bus sink, counting its puts."""

    def __init__(self, sink: BusReportSink) -> None:
        self.sink, self.puts = sink, []

    def put_report(self, report: OutputReport) -> None:
        self.puts.append(report)
        self.sink.put_report(report)

    def emit_attempt(self, attempt) -> None:
        self.sink.emit_attempt(attempt)


def test_a_connected_output_reports_its_identity_and_modes_on_the_display_line():
    run = Reporting()
    run.sysfs.edids["HDMI-A-1"] = SPIKE
    run.reporter.observe((state("HDMI-A-1", 1),))
    report = run.on_line("HDMI-A-1")
    assert report == OutputReport("HDMI-A-1", True, SPIKE_IDENTITY, (P1080,), (), None, None, None, None)


def test_a_disconnect_reports_no_identity_and_a_reconnect_the_same_one():
    run = Reporting()
    run.sysfs.edids["HDMI-A-1"] = SPIKE
    run.reporter.observe((state("HDMI-A-1", 1),))
    run.sysfs.edids["HDMI-A-1"] = None
    run.reporter.observe((state("HDMI-A-1", 2, connected=False),))
    gone = run.on_line("HDMI-A-1")
    assert gone is not None and (gone.connected, gone.identity, gone.modes) == (False, None, ())
    run.sysfs.edids["HDMI-A-1"] = SPIKE
    run.reporter.observe((state("HDMI-A-1", 3),))
    back = run.on_line("HDMI-A-1")
    assert back is not None and (back.connected, back.identity) == (True, SPIKE_IDENTITY)
    assert len(run.puts) == 3


def test_an_unchanged_output_puts_nothing():
    run = Reporting()
    run.sysfs.edids["HDMI-A-1"] = SPIKE
    run.reporter.observe((state("HDMI-A-1", 1),))
    run.reporter.observe((state("HDMI-A-1", 1),))                # the same compositor view
    run.reporter.observe((state("HDMI-A-1", 1, mode=2),))        # a new mode, the same display
    assert len(run.puts) == 1


def test_each_output_reports_alone_and_an_unknown_output_is_not_reported():
    run = Reporting()
    run.sysfs.edids["HDMI-A-2"] = edid(serial_number=7, serial_text=None, timings=(_timing(1920, 1080, 60),))
    run.reporter.observe((state("HDMI-A-1", 1, connected=False), state("HDMI-A-2", 1), state("DSI-1", 1)))
    assert [report.output_id for report in run.puts] == ["HDMI-A-1", "HDMI-A-2"]
    second = run.on_line("HDMI-A-2")
    assert second is not None and second.identity == DisplayIdentity("XYM", 5475, "MNN", "7")


def test_the_display_session_declares_the_display_line_with_its_desired_view():
    session = display_session(DISPLAY_SLICE.digest)
    assert session.desired is not None
