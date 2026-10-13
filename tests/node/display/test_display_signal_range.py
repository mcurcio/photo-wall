"""The Pi carries out a Display's signal range (display tuning, decision 0024, design rule 3;
appliance/display_host/signal_range.py and the picture controller's signal step).

The controller runs as in test_display_picture_controls.py (the recording `ddcutil` fake through
the real DdcChannel); the connector and the request are fakes of the signal range's ports, and the
restart is the real `RestartSignal` over the real channel, with a short wait: no restart comes in
a test, so each one ends as a restart that did not come.
"""
from __future__ import annotations

import os
import time

import pytest
from node.display.fake_ddcutil import OUTPUT, Display
from node.display.test_display_picture_controls import Running, until

from appliance.display_host import signal_range
from appliance.display_host.ddc import Urgency
from appliance.display_host.picture import RestartSignal
from appliance.display_host.signal_range import (
    REQUEST_BYTES,
    FileRangeRequests,
    RangeRequest,
    RangeUnknown,
    decode_request,
    encode_request,
    signal_outcome,
)
from contracts.node_output import OUTPUT_IDS
from contracts.node_picture import (
    NEUTRAL_SOFTWARE,
    HardwareResult,
    PictureDocument,
    SignalRange,
    SignalReport,
    SignalResult,
    WriteResult,
)

AUTO, FULL, LIMITED = SignalRange.AUTO, SignalRange.FULL, SignalRange.LIMITED
NO_PROPERTY, UNREADABLE = RangeUnknown.NO_PROPERTY, RangeUnknown.UNREADABLE
WAIT = 0.3          # the test's RESTART_WAIT_SECONDS: no restart comes here


class Connectors:
    """`ConnectorRangeSetter`: each Output's range in force, or why it is not known (absent: no
    range property)."""

    def __init__(self, ranges: dict[str, SignalRange | RangeUnknown], *, takes: bool = True) -> None:
        self.ranges, self.takes = dict(ranges), takes
        self.sets: list[tuple[str, SignalRange]] = []

    def read(self, output_id):
        return self.ranges.get(output_id, NO_PROPERTY)

    def set(self, output_id, wanted):
        self.sets.append((output_id, wanted))
        if self.takes:
            self.ranges[output_id] = wanted
        return self.takes


class Requests:
    """`RangeRequests`: the last request kept, every one made, and what the channel and the display
    looked like at each."""

    def __init__(self, display: Display, last: dict[str, SignalRange] | None = None) -> None:
        self.display = display
        self.kept = RangeRequest(last or {})
        self.made: list[RangeRequest] = []
        self.seen: list[tuple[list, dict]] = []

    def last(self):
        return self.kept

    def request(self, request):
        ports = self.display.channel._ports
        self.seen.append((self.display.writes(), {output_id: ports[output_id]._held for output_id in ports}))
        self.made.append(request)
        self.kept = request


def start(tmp_path, connectors: Connectors, last: dict[str, SignalRange] | None = None) -> tuple[Running, Requests]:
    display = Display(tmp_path)
    requests = Requests(display, last)
    run = Running(display, RestartSignal(connectors, requests, display.channel, wait=WAIT))
    run.connect()
    run.controller.start()
    return run, requests


def document(change: int, signal: SignalRange, hardware: dict[str, int] | None = None,
             output_id: str = OUTPUT) -> PictureDocument:
    return PictureDocument(output_id, change, hardware or {}, NEUTRAL_SOFTWARE, signal_range=signal)


def signal_of(run: Running, change: int, output_id: str = OUTPUT) -> SignalReport:
    until(lambda: (latest := run.reports.latest(output_id)) is not None and latest.signal is not None
          and latest.signal.for_change == change)
    return run.reports.latest(output_id).signal


@pytest.fixture
def running(tmp_path):
    started: list[Running] = []

    def make(connectors: Connectors, last=None, root: str = "pi"):
        (tmp_path / root).mkdir()
        run, requests = start(tmp_path / root, connectors, last)
        started.append(run)
        return run, requests
    yield make
    for run in started:
        run.controller.stop()


# The pure rule ---------------------------------------------------------------------------------

@pytest.mark.parametrize(("wanted", "in_force", "requested", "outcome"), [
    (FULL, NO_PROPERTY, None, SignalResult.UNSUPPORTED),
    (FULL, NO_PROPERTY, FULL, SignalResult.UNSUPPORTED),
    (FULL, UNREADABLE, None, SignalResult.FAILED),
    (AUTO, UNREADABLE, None, SignalResult.FAILED),
    (FULL, FULL, None, SignalResult.APPLIED),
    (FULL, FULL, LIMITED, SignalResult.APPLIED),
    (FULL, AUTO, FULL, SignalResult.FAILED),
    (FULL, AUTO, None, None),
    (FULL, AUTO, LIMITED, None),
    (AUTO, FULL, FULL, None),
])
def test_signal_outcome(wanted, in_force, requested, outcome):
    """Acceptance 1; only a connector with no range property is UNSUPPORTED, one that did not read
    is FAILED. Mutation probes: FAILED when requested != wanted -> (FULL, AUTO, LIMITED) fails;
    UNSUPPORTED for any unknown -> the UNREADABLE rows fail."""
    assert signal_outcome(wanted, in_force, requested) is outcome


# The controller's signal step -------------------------------------------------------------------

def test_full_holds_every_port_then_asks_for_the_restart_before_any_ddc_write(running):
    """Acceptance 2 (the tracer leg on the Pi's side): Full on a connector at Auto takes every
    port's turn at POWER, then writes the request {HDMI-A-1: full}; no DDC/CI write happens before
    it. Mutation probe: the hardware writes before the signal step -> a write precedes the request."""
    run, requests = running(Connectors({OUTPUT: AUTO}))
    run.offered()
    run.controller.document(OUTPUT, document(3, FULL, {"10": 46}))
    until(lambda: requests.made)
    assert requests.made == [RangeRequest({OUTPUT: FULL})]
    writes, held = requests.seen[0]
    assert writes == []
    assert set(held) == set(OUTPUT_IDS) and set(held.values()) == {Urgency.POWER}


def test_after_the_restart_the_range_is_applied_and_the_hardware_follows(running):
    """Acceptance 3: the restarted controller finds the request and the connector both Full ->
    signal (change, full, applied), the hardware written, no new request. Mutation probe: drop the
    APPLIED check -> a second request."""
    run, requests = running(Connectors({OUTPUT: FULL}), last={OUTPUT: FULL})
    run.controller.document(OUTPUT, document(3, FULL, {"10": 46}))
    assert signal_of(run, 3) == SignalReport(3, FULL, SignalResult.APPLIED)
    assert run.reported(3).results == {"10": HardwareResult(WriteResult.TAKEN, 60, 46)}
    assert requests.made == []


def test_a_range_the_connector_kept_after_its_restart_fails_and_is_not_asked_again(running):
    """Acceptance 4 (no restart loop): the connector still Auto after a restart for Full -> FAILED
    with Auto in force, and no new request, now or for the next change. Mutation probe: ignore
    `requested` -> a request."""
    run, requests = running(Connectors({OUTPUT: AUTO}), last={OUTPUT: FULL})
    run.controller.document(OUTPUT, document(3, FULL))
    assert signal_of(run, 3) == SignalReport(3, AUTO, SignalResult.FAILED)
    run.controller.document(OUTPUT, document(4, FULL))
    assert signal_of(run, 4) == SignalReport(4, AUTO, SignalResult.FAILED)
    assert requests.made == []


def test_an_output_without_a_range_is_unsupported_and_never_restarts(running):
    """Acceptance 5: Virtual-1 has no range property -> UNSUPPORTED with Auto in force and never a
    request, so the software e2e tier never restarts Weston. Mutation probe: treat None as AUTO ->
    a request."""
    run, requests = running(Connectors({}))
    run.controller.output("Virtual-1", connected=True, identity=None)
    run.controller.document("Virtual-1", document(2, FULL, output_id="Virtual-1"))
    assert signal_of(run, 2, "Virtual-1") == SignalReport(2, AUTO, SignalResult.UNSUPPORTED)
    time.sleep(WAIT + 0.2)
    assert requests.made == []


def test_a_connector_that_does_not_read_fails_and_never_restarts(running):
    """A sandbox, libdrm or ioctl fault on a real HDMI port is FAILED (Auto reported in force, as
    nothing is known), never "no range setting", and asks no restart. Mutation probe: treat
    UNREADABLE as no property -> UNSUPPORTED."""
    run, requests = running(Connectors({OUTPUT: UNREADABLE}))
    run.controller.document(OUTPUT, document(3, FULL))
    assert signal_of(run, 3) == SignalReport(3, AUTO, SignalResult.FAILED)
    time.sleep(WAIT + 0.2)
    assert requests.made == []


def test_a_restart_that_does_not_come_releases_the_ports_and_fails(running):
    """Acceptance 6: no restart within the wait -> every port's turn released, FAILED reported with
    the range in force, and the change's DDC/CI write then runs. Mutation probe: never release ->
    the write's read waits past its timeout and is no answer."""
    run, requests = running(Connectors({OUTPUT: AUTO}))
    run.offered()
    run.controller.document(OUTPUT, document(5, LIMITED, {"10": 46}))
    assert signal_of(run, 5) == SignalReport(5, AUTO, SignalResult.FAILED)
    assert requests.made == [RangeRequest({OUTPUT: LIMITED})]
    report = run.reported(5)
    assert report.results == {"10": HardwareResult(WriteResult.TAKEN, 60, 46)}
    assert all(port._held is None for port in run.display.channel._ports.values())


def test_a_restart_that_finds_a_port_held_asks_nothing_fails_and_frees_every_port(running):
    """Acceptance 6, the ports not free: a power command holds Virtual-2 (the last port in
    hold_all's order) past the wait -> no request is written, FAILED is reported with the range in
    force, the ports already taken are released, and the change's DDC/CI write runs. Mutation
    probes: request even when hold_all answers None -> a request; drop hold_all's release of the
    ports it took -> HDMI-A-1 stays held and the write is no answer."""
    run, requests = running(Connectors({OUTPUT: AUTO}))
    run.offered()
    held = run.display.channel._port("Virtual-2")
    assert held.acquire(Urgency.POWER, 1.0)
    try:
        run.controller.document(OUTPUT, document(5, LIMITED, {"10": 46}))
        assert signal_of(run, 5) == SignalReport(5, AUTO, SignalResult.FAILED)
        assert run.reported(5).results == {"10": HardwareResult(WriteResult.TAKEN, 60, 46)}
        assert requests.made == []
        ports = run.display.channel._ports
        assert all(ports[output_id]._held is None for output_id in ports if output_id != "Virtual-2")
    finally:
        held.release()


def test_a_restart_asks_every_outputs_range(running):
    """Every Output's wanted range rides the request: another Output's latest document's (HDMI-A-2's
    Full, whatever its connector holds), else its range in force (Virtual-1's Limited); none for an
    Output without one. Mutation probe: never use another Output's document -> HDMI-A-2 rides as Auto."""
    run, requests = running(Connectors({OUTPUT: AUTO, "HDMI-A-2": AUTO, "Virtual-1": LIMITED}),
                            last={"HDMI-A-2": FULL})
    run.connect("HDMI-A-2")
    run.controller.document("HDMI-A-2", document(4, FULL, output_id="HDMI-A-2"))
    assert signal_of(run, 4, "HDMI-A-2") == SignalReport(4, AUTO, SignalResult.FAILED)   # no restart of its own
    run.controller.document(OUTPUT, document(2, FULL))
    until(lambda: requests.made)
    assert requests.made[0] == RangeRequest({OUTPUT: FULL, "HDMI-A-2": FULL, "Virtual-1": LIMITED})


# The request and the root step -----------------------------------------------------------------

def test_the_request_round_trips_and_refuses_anything_else(tmp_path):
    request = RangeRequest({"HDMI-A-2": LIMITED, OUTPUT: FULL})
    assert encode_request(request) == b'{"HDMI-A-1":"full","HDMI-A-2":"limited"}'
    assert decode_request(encode_request(request)) == request
    for raw in (b"[]", b'{"HDMI-A-3":"full"}', b'{"HDMI-A-1":"Full"}', b'{"HDMI-A-1":1}',
                b'{"HDMI-A-1":"full","HDMI-A-1":"auto"}', b"{" + b" " * REQUEST_BYTES + b"}", b"\xff"):
        with pytest.raises(ValueError, match="^range_request$"):
            decode_request(raw)
    requests = FileRangeRequests(tmp_path / "signal-range.json")
    assert requests.last() == RangeRequest({})                  # after a Pi start
    requests.request(request)
    assert requests.last() == request
    assert [path.name for path in tmp_path.iterdir()] == ["signal-range.json"]


def test_the_root_step_blanks_the_tty_first_and_sets_only_what_differs(tmp_path):
    """Acceptance 7: the tty is cleared and its cursor turned off before any set; a connector that
    already holds its range, or has none, is not set."""
    tty = tmp_path / "tty1"
    tty.write_bytes(b"")
    order: list[str] = []

    class Setter(Connectors):
        def set(self, output_id, wanted):
            order.append(f"set {output_id} tty={tty.read_bytes()!r}")
            return super().set(output_id, wanted)

    connectors = Setter({OUTPUT: AUTO, "HDMI-A-2": LIMITED})
    path = tmp_path / "signal-range.json"
    path.write_bytes(encode_request(RangeRequest({OUTPUT: FULL, "HDMI-A-2": LIMITED, "Virtual-1": FULL})))
    signal_range._main(path, connectors, tty)
    assert tty.read_bytes() == b"\033[2J\033[H\033[?25l"
    assert order == [f"set {OUTPUT} tty={tty.read_bytes()!r}"]
    assert connectors.sets == [(OUTPUT, FULL)] and connectors.ranges[OUTPUT] is FULL


def test_the_root_step_says_in_its_journal_what_each_output_read_was_set_and_read_back(tmp_path, caplog):
    """The step's journal (photo-wall-display.service's ExecStartPre=) tells, per output at INFO,
    the range it read, the range it set and what the connector read back, so a bench check of a
    range change reads it there. Mutation probe: drop the read-back log -> fails."""
    tty = tmp_path / "tty1"
    tty.write_bytes(b"")
    connectors = Connectors({OUTPUT: AUTO, "HDMI-A-2": LIMITED})
    refusing = Connectors({OUTPUT: AUTO}, takes=False)
    path = tmp_path / "signal-range.json"
    path.write_bytes(encode_request(RangeRequest({OUTPUT: FULL, "HDMI-A-2": LIMITED})))
    with caplog.at_level("INFO", logger=signal_range.__name__):
        signal_range._main(path, connectors, tty)
    said = [record.getMessage() for record in caplog.records if record.levelname == "INFO"]
    assert said == [f"signal range: {OUTPUT} read auto, set full, read back full",
                    "signal range: HDMI-A-2 read limited, wants limited: nothing to set"]
    caplog.clear()
    with caplog.at_level("INFO", logger=signal_range.__name__):
        signal_range._main(path, refusing, tty)
    said = [(record.levelname, record.getMessage()) for record in caplog.records]
    assert ("INFO", f"signal range: {OUTPUT} read auto, set full, read back auto") in said
    assert ("WARNING", f"signal range: {OUTPUT} did not take full") in said


@pytest.mark.parametrize("case", ["missing", "malformed", "oversized", "symlink", "fifo", "refused", "raises",
                                  "no tty"])
def test_the_root_step_survives_anything_and_sets_nothing_it_should_not(tmp_path, case):
    """Acceptance 7: a malformed, oversized or symlinked request, a missing card, a refused set or
    a missing tty never raise. Mutation probe: let decode_request's ValueError escape -> raises."""
    tty = tmp_path / "tty1"
    tty.write_bytes(b"")
    path = tmp_path / "signal-range.json"
    connectors = Connectors({OUTPUT: AUTO}, takes=case != "refused")
    good = encode_request(RangeRequest({OUTPUT: FULL}))
    if case == "malformed":
        path.write_bytes(b'{"HDMI-A-1":"full"')
    elif case == "oversized":
        path.write_bytes(good + b" " * REQUEST_BYTES)
    elif case == "symlink":
        (tmp_path / "elsewhere").write_bytes(good)
        path.symlink_to(tmp_path / "elsewhere")
    elif case == "fifo":
        os.mkfifo(path)
    elif case in ("refused", "no tty"):
        path.write_bytes(good)
    elif case == "raises":
        path.write_bytes(good)

        def broken(output_id, wanted):
            raise OSError("no card")
        connectors.set = broken
    if case == "no tty":
        tty = tmp_path / "absent"
    signal_range._main(path, connectors, tty)
    if case in ("missing", "malformed", "oversized", "symlink", "fifo"):
        assert connectors.sets == [] and connectors.ranges[OUTPUT] is AUTO
    if case == "refused":
        assert connectors.sets == [(OUTPUT, FULL)] and connectors.ranges[OUTPUT] is AUTO
    if case == "no tty":
        assert connectors.ranges[OUTPUT] is FULL                # the range is still set


def test_main_exits_zero_whatever_happens(monkeypatch, tmp_path):
    def boom(*args):
        raise RuntimeError("anything")
    monkeypatch.setattr(signal_range, "_main", boom)
    with pytest.raises(SystemExit) as exited:
        signal_range.main()
    assert exited.value.code == 0


def test_the_drm_reader_tells_no_range_property_from_a_connector_that_did_not_read(tmp_path):
    """No connector directory: no range property. A connector whose connector_id or card node does
    not read (no libdrm, a sandbox): unreadable, and nothing set."""
    ranges = signal_range.DrmConnectorRanges(tmp_path, tmp_path / "dri")
    assert ranges.read(OUTPUT) is NO_PROPERTY and ranges.read("Virtual-1") is NO_PROPERTY
    (tmp_path / "card1-HDMI-A-1").mkdir()
    assert ranges.read(OUTPUT) is UNREADABLE
    (tmp_path / "card1-HDMI-A-1" / "connector_id").write_text("33\n")
    assert ranges.read(OUTPUT) is UNREADABLE and ranges.set(OUTPUT, FULL) is False
