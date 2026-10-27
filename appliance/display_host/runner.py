"""Linux base DisplayHost controller with a root-only broker ingress.

This entry point does not accept app self-reported authority or expose a network
listener. Central transport and calibration Save remain separate owners. The
bounded local event feed is observation only and acknowledges no Runtime commit.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import selectors
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Final, Generic, Protocol, TypeVar
from uuid import UUID

from appliance.feed import Feed, answer_feed_read
from appliance.feed_socket import FEED_READERS, FEEDS_GROUP, FeedListener
from appliance.kernel.display_paths import (
    CONTROLLER_STATE,
    DISPLAY_UNIT,
    RUNTIME,
    SIGNAL_RANGE_REQUEST,
)
from appliance.kernel.unix_credentials import receive_credential_packet
from contracts.node_output import (
    OUTPUT_IDS,
    CentralCard,
    FrameRegion,
    OutputReport,
    Power,
    PowerAttempt,
    decode_output_document,
    decode_region_document,
    output_document_key,
    region_document_key,
)
from contracts.node_picture import decode_picture_document, picture_document_key
from contracts.node_protocol import OutputKey
from nodeapi.documents import Document

from .bus import DISPLAY_SLICE, BusImpededReports, BusPictureSink, BusReportSink, display_session
from .central_card import CentralCards
from .ddc import DdcChannel
from .domain import OutputState
from .edid import EdidReading, parse_edid, read_edid
from .output_power import OutputPowerController, PiClock, ReportSink
from .picture import (
    DdcPictureAdapter,
    FileAppliedPictures,
    PictureController,
    PictureReports,
    PictureShell,
    RestartSignal,
)
from .picture_list import FileOfferedCache, PictureListAsker
from .power_methods import DdcCiAdapter, HdmiCecAdapter, SignalOffAdapter, run_tool
from .screen_turn import ScreenTurns
from .signal_range import DrmConnectorRanges, FileRangeRequests
from .weston import MAX_PACKET, SurfaceGrant, WestonBackend, _pairs, _surface

log = logging.getLogger(__name__)

# The display feed for node readers (root, pw-health): the `events` op only, over the kernel
# feed socket. The root-only ingress (`<runtime>/ingress.sock`) keeps every operation.
FEED_SOCKET = Path("/run/photo-wall-display-feed/feed.sock")


def _unit(unit: str) -> dict[str, str]:
    result = subprocess.run(
        ["/usr/bin/systemctl", "show", unit, "--property=MainPID,InvocationID,ControlGroup"],
        check=True,
        capture_output=True,
        text=True,
        timeout=2,
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def verify_process(grant: SurfaceGrant) -> bool:
    """Bracket kernel identity with the base-owned service manager invocation."""
    try:
        before = _unit("photo-wall-node-player.service")
        process = grant.surface.process
        if (
            int(before["MainPID"]) != process.pid
            or UUID(before["InvocationID"]) != process.invocation_id
        ):
            return False

        def kernel_sample():
            proc = Path(f"/proc/{process.pid}")
            stat = (proc / "stat").read_text()
            ticks = int(stat.rpartition(") ")[2].split()[19])
            uid_line = next(
                row for row in (proc / "status").read_text().splitlines() if row.startswith("Uid:")
            )
            return (
                ticks,
                tuple(int(v) for v in uid_line.split()[1:]),
                tuple((proc / "cgroup").read_text().splitlines()),
            )

        sample = kernel_sample()
        after = _unit("photo-wall-node-player.service")
        return (
            sample[0] == process.start_ticks
            and sample[1] == (grant.uid,) * 4
            and f"0::{before['ControlGroup']}" in sample[2]
            and after == before
            and kernel_sample() == sample
        )
    except (OSError, ValueError, KeyError, IndexError, StopIteration, subprocess.SubprocessError):
        return False


def _output_document(state, power: OutputPowerController | None = None) -> dict:
    """One `OutputState` as the feed snapshot shows it (JSON-native values only), with `holding`
    (the shell's hold, issue #123) and `power`: the power in force on the Output as the power
    controller's report says it (`in_force.power`), None without a report or a power in force."""
    admitted = state.admitted
    report = None if power is None else power.report(state.key.output_id)
    in_force = None if report is None or report.in_force is None else report.in_force.power
    return {
        "output_id": state.key.output_id,
        "connected": state.connected,
        "admitted": None
        if admitted is None
        else {
            "pid": admitted.process.pid,
            "start_ticks": admitted.process.start_ticks,
            "invocation_id": str(admitted.process.invocation_id),
            "app_epoch": admitted.app_epoch,
            "frame_id": admitted.frame_id,
        },
        "diagnostic": state.diagnostic,
        "fault": state.fault,
        "holding": state.holding,
        "power": None if in_force is None else in_force.value,
    }


def feed_listener(
    controller: Controller, path: Path = FEED_SOCKET, *, owner_uid: int | None = None,
    group: int = FEEDS_GROUP, **seams
) -> FeedListener:
    """The display feed socket: owned by the controller's uid (pw-display), group
    pw-node-feeds, read by root and pw-health only (`seams`: `peer` and `kind`, for tests)."""
    return FeedListener(
        path,
        owner_uid=os.getuid() if owner_uid is None else owner_uid,
        group=group,
        readers=FEED_READERS,
        answer=controller.feed_events,
        max_reply=MAX_PACKET,
        **seams,
    )


class OutputReporter:
    """Each Output the compositor knows, as an Output report on the display line (roadmap 1b, D1).

    The EDID is read again only when the compositor's view of the Output changes (its key: every
    hotplug raises the connection generation, or its connection), and a report is put only when it
    differs from the Output's last, so an unchanged Output costs nothing on the bus. An Output id
    outside the wire's OUTPUT_IDS has no key on the line and is not reported. Power fields stay
    empty here: the power controller (slice D2) fills them. `mode` gives the mode Weston runs on a
    connected Output (WestonBackend.current_mode; issue #124), its `current_mode`; a new mode is a
    new Output key, so it is read again then."""

    def __init__(self, sink: ReportSink, *, read: Callable[[str], bytes | None] = read_edid,
                 mode: Callable[[str], tuple[int, int] | None] = lambda output_id: None) -> None:
        self._sink = sink
        self._read = read
        self._mode = mode
        self._seen: dict[str, tuple[OutputKey, bool]] = {}
        self._reports: dict[str, OutputReport] = {}

    def observe(self, states: Iterable[OutputState]) -> None:
        for state in states:
            output_id = state.key.output_id
            seen = (state.key, state.connected)
            if output_id not in OUTPUT_IDS or self._seen.get(output_id) == seen:
                continue
            self._seen[output_id] = seen
            raw = self._read(output_id) if state.connected else None
            reading = parse_edid(raw) if raw is not None else EdidReading(None, (), None)
            report = OutputReport(output_id, state.connected, reading.identity, reading.modes, answers=(),
                                  method=None, for_change=None, result=None, in_force=None,
                                  size_mm=reading.size_mm,
                                  current_mode=self._mode(output_id) if state.connected else None)
            if self._reports.get(output_id) != report:
                self._reports[output_id] = report
                self._sink.put_report(report)


class PowerObservations:
    """`ReportSink` for the OutputReporter that hands what it saw (connection, EDID identity,
    modes) to the power controller, so the power controller is the one writer of each Output's
    report and the report carries both halves (roadmap 1b, D2); and the connection and identity to
    the picture controller (decision 0022: an identity whose list is not held is asked, another
    identity carries out the picture again); and the connection to the screen turns (render
    redesign S3: the Output appearing connected asks the shell for its turn again)."""

    def __init__(self, power: OutputPowerController, picture: PictureController, turns: ScreenTurns) -> None:
        self._power = power
        self._picture = picture
        self._turns = turns

    def put_report(self, report: OutputReport) -> None:
        self._power.output(report.output_id, connected=report.connected, identity=report.identity,
                           modes=report.modes, size_mm=report.size_mm,
                           current_mode=None if report.current_mode is None else tuple(report.current_mode))
        self._picture.output(report.output_id, connected=report.connected, identity=report.identity)
        self._turns.output(report.output_id, connected=report.connected)

    def emit_attempt(self, attempt: PowerAttempt) -> None:
        raise TypeError("the OutputReporter makes no power attempt")


SHELL_OP_SECONDS: Final = 2.0  # one forwarded op's answer from the shell (`region`, `central_card`)
REGION_SECONDS: Final = SHELL_OP_SECONDS

_Wanted = TypeVar("_Wanted")


class ShellForwarder(Generic[_Wanted]):
    """Carries each Output's newest value of one kept shell op to the shell on its own worker
    thread, so neither the bus's callbacks, the controllers' workers nor the dispatch loop wait on
    the shell. The shell keeps the value per Output and forwards it verbatim to the overlay client.

    Newest wins per Output. The shell (re)creating an Output (a Weston restart or hot-plug;
    WestonBackend.output_listeners) sends that Output's value again, as the picture is. A send the
    shell refuses is not retried: the shell refuses one only for an Output it does not have yet or
    under an Output key since replaced, and every new key arrives with the shell's `output` event,
    which sends it again. Subclasses name the op (`_send`)."""

    _name = "shell-op"

    def __init__(self, *, timeout: float = SHELL_OP_SECONDS) -> None:
        self._timeout = timeout
        self._changed = threading.Condition()
        self._wanted: dict[str, _Wanted | None] = {}
        self._due: dict[str, None] = {}              # Outputs whose value is to be sent, in order
        self._stopping = False
        self._thread: threading.Thread | None = None

    def _send(self, output_id: str, value: _Wanted | None, timeout: float) -> bool:
        raise NotImplementedError

    def start(self) -> None:
        with self._changed:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name=f"shell-{self._name}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._changed:
            self._stopping = True
            self._changed.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)

    def document(self, output_id: str, value: _Wanted | None) -> None:
        """`output_id`'s newest value (None: none). Never blocks."""
        with self._changed:
            if output_id in self._wanted and self._wanted[output_id] == value:
                return
            self._wanted[output_id] = value
            self._due[output_id] = None
            self._changed.notify_all()

    def shell_output(self, output_id: str) -> None:
        """The shell (re)created `output_id`: its value, if any came, is sent again. Never blocks."""
        with self._changed:
            if output_id in self._wanted:
                self._due[output_id] = None
                self._changed.notify_all()

    def _run(self) -> None:
        while True:
            with self._changed:
                while not self._due and not self._stopping:
                    self._changed.wait()
                if self._stopping:
                    return
                output_id = next(iter(self._due))
                del self._due[output_id]
                value = self._wanted[output_id]
            if not self._send(output_id, value, self._timeout):
                log.info("%s for %s not taken by the shell; sent again when it announces the Output",
                         self._name, output_id)


class RegionShell(Protocol):
    """The shell's `region` op, as WestonBackend carries it."""

    def region(self, output_id: str, region: FrameRegion | None, *, timeout: float) -> bool: ...


class ShellRegions(ShellForwarder[FrameRegion]):
    """Each Output's Frame region (issue #124) to the shell's `region` op. An Output whose region
    never reaches the shell keeps the debug overlay on the whole Display, the safe side of the
    rule."""

    _name = "region"

    def __init__(self, shell: RegionShell, *, timeout: float = REGION_SECONDS) -> None:
        super().__init__(timeout=timeout)
        self._shell = shell

    def _send(self, output_id: str, value: FrameRegion | None, timeout: float) -> bool:
        return self._shell.region(output_id, value, timeout=timeout)


class CardShell(Protocol):
    """The shell's `central_card` op, as WestonBackend carries it."""

    def central_card(self, output_id: str, card: CentralCard | None, *, timeout: float) -> bool: ...


class ShellCards(ShellForwarder[CentralCard]):
    """Each Output's Central card (central_card.CentralCards composes it) to the shell's
    `central_card` op. The shell drops an Output's card itself when it invalidates the Output's
    grant (a Player crash, hang or mode change), so a card never outlives its run there even
    before the controller has heard of the end."""

    _name = "central_card"

    def __init__(self, shell: CardShell, *, timeout: float = SHELL_OP_SECONDS) -> None:
        super().__init__(timeout=timeout)
        self._shell = shell

    def _send(self, output_id: str, value: CentralCard | None, timeout: float) -> bool:
        return self._shell.central_card(output_id, value, timeout=timeout)


class PowerToCards:
    """`ReportSink` around the power controller's sink that also tells Central's cards whether the
    power in force leaves the display on (an Output turned off by Night off or Resting shows no
    card). Never blocks."""

    def __init__(self, sink: ReportSink, cards: CentralCards) -> None:
        self._sink, self._cards = sink, cards

    def put_report(self, report: OutputReport) -> None:
        self._sink.put_report(report)
        in_force = report.in_force
        self._cards.power(report.output_id, in_force is None or in_force.power is not Power.OFF)

    def emit_attempt(self, attempt: PowerAttempt) -> None:
        self._sink.emit_attempt(attempt)


class DrawingToCards:
    """`picture.PictureShell` around the shell that also tells Central's cards whether Display draws
    a test pattern or calibration target on the Output (the op's `drawing`, once the shell took it)."""

    def __init__(self, shell: PictureShell, cards: CentralCards) -> None:
        self._shell, self._cards = shell, cards

    def picture(self, output_id: str, operations: str, brightness: str, *, drawing: bool,
                timeout: float) -> bool:
        taken = self._shell.picture(output_id, operations, brightness, drawing=drawing, timeout=timeout)
        if taken:
            self._cards.drawing(output_id, drawing)
        return taken


def admitted_run(backend: WestonBackend, state: OutputState) -> tuple[str | None, str | None]:
    """The admitted Player run on the Output as Central's card rule reads it: (its shell grant id,
    its Frame id), or (None, None) while none shows its picture: nothing admitted, its handoff not
    complete, or the holding slate up (no hold evidence)."""
    admitted = state.admitted
    granted = backend.grants.get(state.key.output_id)
    if (admitted is None or granted is None or granted[0].surface != admitted
            or state.diagnostic != "released" or not state.holding):
        return None, None
    return str(granted[1]), admitted.frame_id


def tell_cards(cards: CentralCards, backend: WestonBackend) -> None:
    """The main loop's facts for Central's cards, after each change it handled (a shell event, an
    ingress op, a trial): each Output's admitted run and whether a live trial is drawn on it."""
    host = backend.host
    if host is None:
        return
    for state in host.states():
        grant, frame = admitted_run(backend, state)
        cards.admitted(state.key.output_id, grant, frame, trial=state.key.output_id in backend.trials)


def document_feed(power: OutputPowerController, picture: PictureController,
                  turns: ScreenTurns, regions: ShellRegions) -> Callable[[str, Document], None]:
    """The desired view's callback, routed by key: an Output document (`output-…`), decoded, to the
    power controller and to the screen turns (its `turn`, render redesign S3); a picture document (`picture-…`, decision 0022), decoded, to the picture
    controller (each acts on its own change number); a region document (`region-…`, issue #124),
    decoded, its region to the shell's regions. A document that does not decode, sits under
    another Output's key, or under a key of none of these kinds, is logged and ignored; a refused
    region document gives its Output no region (the debug overlay on the whole Display), logged
    once per change, and touches neither power nor the turn."""
    output_keys = {output_document_key(output_id) for output_id in OUTPUT_IDS}
    picture_keys = {picture_document_key(output_id) for output_id in OUTPUT_IDS}
    region_keys = {region_document_key(output_id): output_id for output_id in OUTPUT_IDS}
    refused: dict[str, bytes] = {}       # region key -> the refused document last logged

    def on_region(key: str, document: Document) -> None:
        try:
            wanted = decode_region_document(document.value)
            if region_document_key(wanted.output_id) != key:
                raise ValueError("region_document_key")
        except ValueError as error:
            if refused.get(key) != document.value:
                refused[key] = document.value
                log.warning("region document under %s refused (%s); the debug overlay uses the whole Display",
                            key, error)
            regions.document(region_keys[key], None)
            return
        refused.pop(key, None)
        regions.document(wanted.output_id, wanted.region)

    def on_document(key: str, document: Document) -> None:
        if key in region_keys:
            on_region(key, document)
            return
        try:
            if key in output_keys:
                decoded = decode_output_document(document.value)
                if output_document_key(decoded.output_id) != key:
                    raise ValueError("output_document_key")
                power.document(decoded.output_id, decoded)
                turns.document(decoded.output_id, decoded)
            elif key in picture_keys:
                wanted = decode_picture_document(document.value)
                if picture_document_key(wanted.output_id) != key:
                    raise ValueError("picture_document_key")
                picture.document(wanted.output_id, wanted)
            else:
                raise ValueError("display_document_key")
        except ValueError as error:
            log.warning("display document under %s ignored: %s", key, error)

    return on_document


class ShellBlackPicture:
    """output_power.BlackPicture over the shell's `black` op (WestonBackend)."""

    def __init__(self, backend: WestonBackend) -> None:
        self._backend = backend

    def show(self, output_id: str, black: bool, *, timeout: float) -> bool:
        return self._backend.black(output_id, black, timeout=timeout)

    def shown(self, output_id: str) -> bool | None:
        return self._backend.black_shown(output_id)


def power_controller(backend: WestonBackend, sink: ReportSink,
                     ddc: DdcChannel) -> tuple[OutputPowerController, HdmiCecAdapter]:
    """The three methods on this Pi's tools and compositor, the shell's black picture, and the
    controller over them."""
    cec = HdmiCecAdapter(run_tool)
    adapters = (cec, DdcCiAdapter(ddc), SignalOffAdapter(backend))
    return OutputPowerController({adapter.method: adapter for adapter in adapters}, sink, PiClock(),
                                 ShellBlackPicture(backend)), cec


def picture_controller(backend: WestonBackend, sink: PictureReports, ddc: DdcChannel,
                       applied: Path = CONTROLLER_STATE, *, shell: PictureShell | None = None) -> PictureController:
    """The picture controller on the port's DDC/CI channel (the one the power controller uses, at
    picture's turn), asking each display's list there (PictureListAsker), and the shell,
    remembering each display's list (FileOfferedCache) and applied picture change in `applied` (the
    controller's preserved runtime directory); the signal range read from the connector, asked for
    in the same directory (display_paths.SIGNAL_RANGE_REQUEST) behind every port's turn on the same
    channel; it hears the shell's `presented` and `output` events through the backend. `shell`: the
    picture op's carrier when not the backend itself (DrawingToCards around it)."""
    signal = RestartSignal(DrmConnectorRanges(), FileRangeRequests(applied / SIGNAL_RANGE_REQUEST.name), ddc)
    picture = PictureController(DdcPictureAdapter(ddc), PictureListAsker(ddc), FileOfferedCache(applied),
                                backend if shell is None else shell, sink, FileAppliedPictures(applied), time.monotonic, signal)
    backend.picture_events = picture
    return picture


class Controller:
    def __init__(self, backend: WestonBackend, *, reports: OutputReporter | None = None,
                 power: OutputPowerController | None = None):
        self.backend = backend
        self._power = power   # each Output's power in force, for the feed snapshot
        self.host = backend.initialize()
        self.feed = Feed(256)
        self.service = None
        self.reports = reports
        if reports is not None:
            reports.observe(self.host.states())

    def observe(self) -> None:
        observed = self.backend.dispatch()
        if self.reports is not None:
            self.reports.observe(self.host.states())
        if observed is None:
            return
        if self.service is not None:
            self.service.observe(observed)
        self.feed.append(
            type(observed).__name__, asdict(observed) if is_dataclass(observed) else observed
        )

    def revise(self, decision) -> None:
        self.backend.revise(
            decision.surface, decision.decision_id, expires_boottime_ms=decision.expires_boottime_ms
        )

    def outputs_snapshot(self) -> list[dict]:
        """Every Output the host knows (bounded by its `max_outputs`) with its admission, so a
        reader that lapped the ring or restarted learns the Output set from any one read."""
        return [_output_document(state, self._power) for state in self.host.states()]

    def feed_events(self, value: dict) -> dict:
        """The feed socket's one operation: `events` (an absent `op` reads as `events`)."""
        if value.get("op", "events") != "events":
            raise ValueError("feed_read_request")
        return self.receive({**value, "op": "events"})

    def receive(self, value: dict) -> dict:
        operation = value.get("op")
        if operation == "events":
            return {
                **answer_feed_read(self.feed, value),
                "boot_id": self.host.boot_id,
                "incarnation_id": self.host.incarnation_id,
                "outputs": self.outputs_snapshot(),
            }
        if operation == "outputs":
            return {"outputs": [asdict(state) for state in self.host.states()]}
        if operation == "candidate":
            surface = _surface(value["surface"])
            grant = SurfaceGrant(surface, value["uid"], value.get("starting_new"))
            grant_id = self.backend.allow(grant)
            try:
                self.host.offer(surface)
            except Exception:
                self.backend.grants.pop(surface.output.output_id, None)
                raise
            return {"grant_id": grant_id}
        if operation == "withdraw":
            output = value["output_id"]
            return {"state": asdict(self.host.authorized_withdrawal(output))}
        if operation == "handoff":
            import time

            surface = _surface(value["surface"])
            state = self.host.authorize_handoff(surface, now_ms=time.monotonic_ns() // 1_000_000)
            return {"handoff_id": state.handoff_id}
        raise ValueError("display_operation_unsupported")


class _stopping:
    """Stops the controllers', the screen turns', the regions' and the cards' workers and the report
    reader, and gives back the CEC addresses on the way out."""

    def __init__(self, power: OutputPowerController, picture: PictureController, turns: ScreenTurns,
                 regions: ShellRegions, cards: ShellCards, impeded: BusImpededReports,
                 cec: HdmiCecAdapter) -> None:
        self._power, self._picture, self._turns, self._regions, self._cec = power, picture, turns, regions, cec
        self._cards, self._impeded = cards, impeded

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> None:
        self._power.stop()
        self._picture.stop()
        self._turns.stop()
        self._regions.stop()
        self._cards.stop()
        self._impeded.stop()
        self._cec.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=RUNTIME)
    parser.add_argument(
        "--config", type=Path, help="base-owned systemd credential JSON; optional runtime path"
    )
    args = parser.parse_args()
    config = {}
    if args.config is not None:
        config = json.loads(args.config.read_bytes(), object_pairs_hook=_pairs)
        if type(config) is not dict or set(config) - {"runtime", "central", "serial", "offer_id"}:
            raise ValueError("display_config_fields")
        if "runtime" in config:
            args.runtime = Path(config["runtime"])
            if not args.runtime.is_absolute():
                raise ValueError("display_runtime_absolute")
    os.umask(0o077)
    # systemd stops the unit with SIGTERM: exit through the finally below (CEC `--clear`).
    signal.signal(signal.SIGTERM, lambda _signal, _frame: sys.exit(0))
    peer = _unit(DISPLAY_UNIT)
    # The display line's session: always, with or without the retiring Central config. It connects
    # on its own thread forever, so the compositor loop never waits on the bus.
    session = display_session(DISPLAY_SLICE.digest)
    session.start()
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as channel:
        channel.connect(str(args.runtime / "control.sock"))
        backend = WestonBackend(
            channel,
            display_uid=os.getuid(),
            compositor_pid=int(peer["MainPID"]),
            verify_process=verify_process,
        )
        # One DDC/CI channel per Pi (one command at a time per port, power first), shared by power
        # and picture; each controller writes its own report key.
        ddc = DdcChannel(run_tool)
        # Central's card (the Frame shows Central's error when it blacks the Frame): the Player's
        # `impeded` read on the bus by Display's own reader, composed with the controller's facts,
        # to the shell's `central_card` op. Never through the health judge.
        impeded = BusImpededReports()
        shell_cards = ShellCards(backend)   # the shell's `central_card` op (WestonBackend.central_card)
        cards = CentralCards(impeded, shell_cards.document)
        impeded.on_change(cards.reported)
        backend.output_listeners.append(shell_cards.shell_output)
        power, cec = power_controller(backend, PowerToCards(BusReportSink(session), cards), ddc)
        picture = picture_controller(backend, BusPictureSink(session), ddc, shell=DrawingToCards(backend, cards))
        turns = ScreenTurns(backend)        # the shell's `turn` op (WestonBackend.turn)
        regions = ShellRegions(backend)     # the shell's `region` op (WestonBackend.region)
        backend.output_listeners.append(regions.shell_output)
        power.start()
        picture.start()
        turns.start()
        regions.start()
        shell_cards.start()
        impeded.start()
        feed = document_feed(power, picture, turns, regions)
        session.desired.on_change(feed)
        for output_id in OUTPUT_IDS:     # what arrived before the callback, once more (idempotent)
            for key in (output_document_key(output_id), picture_document_key(output_id),
                        region_document_key(output_id)):
                if (present := session.desired.get(key)) is not None:
                    feed(key, present)
        controller = Controller(backend, reports=OutputReporter(PowerObservations(power, picture, turns),
                                                                mode=backend.current_mode),
                                power=power)
        if config:
            from .service import DisplayService

            controller.service = DisplayService(
                controller,
                central=config["central"],
                serial=config["serial"],
                offer_id=UUID(config["offer_id"]),
                runtime=args.runtime,
            )
        with (
            _stopping(power, picture, turns, regions, shell_cards, impeded, cec),
            feed_listener(controller) as feeds,
            socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as ingress,
        ):
            path = args.runtime / "ingress.sock"
            path.unlink(missing_ok=True)
            ingress.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            ingress.bind(str(path))
            os.chmod(path, 0o600)
            ingress.listen(4)
            with selectors.DefaultSelector() as selector:
                selector.register(channel, selectors.EVENT_READ, "backend")
                selector.register(ingress, selectors.EVENT_READ, "ingress")
                selector.register(feeds.listener, selectors.EVENT_READ, "feed")
                while True:
                    while backend.pending:
                        controller.observe()
                    if controller.service is not None:
                        controller.service.tick()
                    tell_cards(cards, backend)   # what this pass changed (no change: nothing sent)
                    for key, _mask in selector.select(timeout=0.1):
                        if key.data == "backend":
                            controller.observe()
                            continue
                        if key.data == "feed":
                            feeds.serve()  # bounded: READS_PER_TURN short reads at most
                            continue
                        connection, _ = ingress.accept()
                        with connection:
                            connection.settimeout(1)
                            _pid, uid, _gid = struct.unpack(
                                "3i",
                                connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12),
                            )
                            if uid != 0:
                                continue
                            try:
                                credentials, raw = receive_credential_packet(
                                    connection, maximum=MAX_PACKET
                                )
                                if credentials[1] != 0:
                                    raise ValueError("display_ingress_peer")
                                value = json.loads(raw, object_pairs_hook=_pairs)
                                if type(value) is not dict:
                                    raise ValueError("display_ingress_type")
                                result = {"accepted": True, **controller.receive(value)}
                            except (ValueError, KeyError, TypeError, OSError) as exc:
                                result = {"accepted": False, "reason": type(exc).__name__}
                            wire = json.dumps(result, default=str, separators=(",", ":")).encode()
                            if len(wire) > MAX_PACKET:
                                wire = b'{"accepted":false,"reason":"response_bound"}'
                            connection.sendall(wire)


if __name__ == "__main__":
    main()
