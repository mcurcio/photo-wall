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
from appliance.display_host.edid import EdidReading, parse_edid, read_edid
from appliance.display_host.output_power import OutputPowerController
from appliance.display_host.runner import OutputReporter, PowerObservations
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


def _timing(width: int, height: int, refresh_hz: int, *, interlaced: bool = False,
            size_mm: tuple[int, int] = (0, 0)) -> bytes:
    blank_h, blank_v = 280, 45                         # CTA 1080p60: 2200 x 1125 total
    clock = (width + blank_h) * (height + blank_v) * refresh_hz // 10_000
    d = bytearray(18)
    d[0:2] = clock.to_bytes(2, "little")
    d[2], d[3], d[4] = width & 0xFF, blank_h & 0xFF, (width >> 8) << 4 | blank_h >> 8
    d[5], d[6], d[7] = height & 0xFF, blank_v & 0xFF, (height >> 8) << 4 | blank_v >> 8
    d[12], d[13], d[14] = size_mm[0] & 0xFF, size_mm[1] & 0xFF, (size_mm[0] >> 8) << 4 | size_mm[1] >> 8
    d[17] = 0x80 if interlaced else 0x18
    return bytes(d)


def _checksummed(block: bytearray) -> bytes:
    block[127] = (-sum(block[:127])) % 256
    return bytes(block)


def edid(maker: str = "XYM", product: int = 5475, *, name: str | None = "MNN", serial_number: int = 0,
         serial_text: str | None = " ", timings: tuple[bytes, ...] = (), vics: tuple[int, ...] = (),
         size_cm: tuple[int, int] = (0, 0)) -> bytes:
    """A base block (and a CTA extension when `vics`) stating the given identity, timings and
    picture size (bytes 21 and 22, whole centimetres; 0 states none)."""
    base = bytearray(128)
    base[0:8] = bytes((0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00))
    base[8:10] = _letters(maker)
    base[10:12] = product.to_bytes(2, "little")
    base[12:16] = serial_number.to_bytes(4, "little")
    base[18], base[19] = 1, 3                          # EDID 1.3
    base[21], base[22] = size_cm
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
# The Samsung QN43Q8F as read on the production Pi (2026-10-19): its base block states 95 x 54 cm
# (43 in across, right); its preferred detailed timing states 1872 x 1053 mm (bytes 12-14 = 80, 29,
# 116; 85 in, wrong). The base block's is the size.
QN43Q8F_TIMING = _timing(3840, 2160, 60, size_mm=(1872, 1053))
QN43Q8F = edid("SAM", 29749, name="QN43Q8F", timings=(QN43Q8F_TIMING,), size_cm=(95, 54))


# parse_edid: what the bytes say.

def test_the_spike_monitor_has_no_usable_serial_and_prefers_1080p60():
    assert parse_edid(SPIKE) == EdidReading(SPIKE_IDENTITY, (P1080,), None)


def test_the_size_is_the_base_blocks_centimetres_never_the_detailed_timings_millimetres():
    reading = parse_edid(QN43Q8F)
    assert reading.size_mm == (950, 540)
    assert reading.identity == DisplayIdentity("SAM", 29749, "QN43Q8F", None)
    assert [(mode.width, mode.height, mode.preferred) for mode in reading.modes] == [(3840, 2160, True)]


def test_a_base_block_stating_either_side_zero_states_no_size():
    """A projector, or an aspect-ratio encoding (VESA E-EDID 1.4 3.6.2): no size."""
    for size_cm in ((95, 0), (0, 54), (0, 0)):
        reading = parse_edid(edid(timings=(QN43Q8F_TIMING,), size_cm=size_cm))
        assert reading.size_mm is None and reading.modes   # the timing's millimetres are never the size


def test_a_numeric_serial_is_its_decimal():
    identity = parse_edid(edid(serial_number=0x01020304, serial_text=None)).identity
    assert identity == DisplayIdentity("XYM", 5475, "MNN", "16909060")


def test_the_serial_text_wins_over_the_number():
    identity = parse_edid(edid(serial_number=1234, serial_text="ABC-0042")).identity
    assert identity is not None and identity.serial == "ABC-0042"


def test_a_bad_checksum_has_no_identity():
    broken = bytearray(QN43Q8F)
    broken[20] ^= 0x01
    nothing = EdidReading(None, (), None)
    assert parse_edid(bytes(broken)) == nothing
    assert parse_edid(QN43Q8F[:127]) == nothing
    assert parse_edid(b"") == nothing


def test_cta_video_descriptors_follow_the_preferred_mode_once_each():
    modes = parse_edid(edid(timings=(_timing(1920, 1080, 60),), vics=(16, 4, 97, 5, 200))).modes
    assert modes == (P1080, DisplayMode(1280, 720, 60000), DisplayMode(3840, 2160, 60000))


def test_an_interlaced_first_timing_is_neither_a_mode_nor_preferred():
    modes = parse_edid(edid(timings=(_timing(1920, 540, 60, interlaced=True),), vics=(4,))).modes
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
    assert report == OutputReport("HDMI-A-1", True, SPIKE_IDENTITY, (P1080,), (), None, None, None, None, None)


def test_a_connected_output_reports_its_displays_size_on_the_display_line():
    run = Reporting()
    run.sysfs.edids["HDMI-A-1"] = QN43Q8F
    run.reporter.observe((state("HDMI-A-1", 1),))
    report = run.on_line("HDMI-A-1")
    assert report is not None and report.size_mm == (950, 540)


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


def test_a_picture_report_is_its_own_state_key_beside_the_output_report():
    """Decision 0022: picture leaves the Output report; each Output's picture report is the
    `picture-<output>` state key, and its document the `picture-<output>` desired key, each with
    its own size cap."""
    from appliance.display_host.bus import DISPLAY_DESIRED, DISPLAY_STATE, BusPictureSink
    from contracts.node_output import OUTPUT_IDS
    from contracts.node_picture import (
        PICTURE_DOCUMENT_BYTES,
        PICTURE_REPORT_BYTES,
        PictureReport,
        decode_picture_report,
        picture_document_key,
        picture_report_key,
    )

    for output_id in OUTPUT_IDS:
        assert DISPLAY_STATE.sizes[picture_report_key(output_id)] == PICTURE_REPORT_BYTES
        assert DISPLAY_DESIRED.sizes[picture_document_key(output_id)] == PICTURE_DOCUMENT_BYTES
        assert output_report_key(output_id) in DISPLAY_STATE.sizes
    session = display_session(DISPLAY_SLICE.digest)
    report = PictureReport("HDMI-A-2", SPIKE_IDENTITY, (), 4, {}, 4)
    BusPictureSink(session).put_picture(report)
    assert decode_picture_report(session.state.get(picture_report_key("HDMI-A-2"))) == report
    assert session.state.get(output_report_key("HDMI-A-2")) is None


def test_the_power_controller_the_one_writer_of_the_report_carries_the_size_on():
    """In production the reporter's sink is PowerObservations, and the power controller writes the
    Output's report: what the EDID said about the size reaches the line through it."""
    run = Reporting()
    power = OutputPowerController({}, run.sink, clock=None, black=None)   # never started: no look runs
    reporter = OutputReporter(PowerObservations(power, Ignored(), Ignored()), read=run.sysfs.read)
    run.sysfs.edids["HDMI-A-1"] = QN43Q8F
    reporter.observe((state("HDMI-A-1", 1),))
    report = run.on_line("HDMI-A-1")
    assert report is not None and report.size_mm == (950, 540)


def test_the_power_controller_carries_the_mode_weston_runs_on_the_line():
    """Issue #124: the Output report states the mode Weston runs (`current_mode`, as built, never
    turned), the one size Central reads for a Frame's screen; a new mode re-puts the report, and an
    Output whose mode is not known states none."""
    run = Reporting()
    power = OutputPowerController({}, run.sink, clock=None, black=None)   # never started: no look runs
    power.output("HDMI-A-1", connected=True, identity=SPIKE_IDENTITY, modes=(P1080,), current_mode=(1920, 1080))
    report = run.on_line("HDMI-A-1")
    assert report is not None and report.current_mode == (1920, 1080)
    power.output("HDMI-A-1", connected=True, identity=SPIKE_IDENTITY, modes=(P1080,), current_mode=(1280, 720))
    report = run.on_line("HDMI-A-1")
    assert report is not None and report.current_mode == (1280, 720)
    power.output("HDMI-A-2", connected=False, identity=None, modes=())
    report = run.on_line("HDMI-A-2")
    assert report is not None and report.current_mode is None


class Ignored:
    """The picture controller and the screen turns: not what this test is about."""

    def output(self, *_args, **_kwargs) -> None:
        return None
