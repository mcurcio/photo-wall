"""Base-only bridge to the pinned native Weston shell.

The socket is private to the base display UID. App requests must never be passed
through this transport. AppEffectBroker/Registry authorize ``SurfaceGrant`` after
verifying the exact live process/service identity; the shell independently checks
Wayland peer PID/UID and /proc start time at each observed presentation.
"""

from __future__ import annotations

import json
import logging
import select
import socket
import struct
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Callable, Literal, Protocol
from uuid import UUID, uuid4

from contracts.display_turn import DisplayTurn, wayland_transform
from contracts.node_frame import parse_picture_change
from contracts.node_output import (
    MAX_SCREEN_SIDE,
    CentralCard,
    FrameRegion,
    Power,
    encode_central_card,
    encode_frame_region,
)
from contracts.node_protocol import NodeProcessIdentity, OutputKey, SurfaceFact

from .domain import DisplayHost, Surface

MAX_PACKET = 16384

log = logging.getLogger(__name__)


class PictureEvents(Protocol):
    """What the shell tells the picture controller (picture.PictureController), from `dispatch`."""

    def presented(self, output_id: str, picture_change: int | None) -> None: ...   # never blocks

    def shell_output(self, output_id: str) -> None: ...   # the shell (re)created the Output; never blocks


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("display_duplicate_field")
        result[key] = value
    return result


def _output(value: dict) -> OutputKey:
    return OutputKey(
        UUID(value["kernel_boot_id"]),
        UUID(value["display_host_incarnation"]),
        value["output_id"],
        value["connection_generation"],
        value["mode_generation"],
    )


def _surface(value: dict) -> Surface:
    process = value["process"]
    return Surface(
        _output(value["output"]),
        NodeProcessIdentity(process["pid"], process["start_ticks"], UUID(process["invocation_id"])),
        value["app_epoch"],
        value["binding_generation"],
        value["config_revision"],
        value["frame_id"],
    )


@dataclass(frozen=True)
class SurfaceGrant:
    """Trusted base decision, never an app-self-reported surface claim.

    ``starting_new`` authorizes synthetic candidate pixels beneath the diagnostic
    during a cold start/authorized switch. It is not Runtime show commitment.
    """

    surface: Surface
    uid: int
    starting_new: bool

    def __post_init__(self) -> None:
        self.surface.fact("invalidated")
        if type(self.uid) is not int or not 1 <= self.uid < 2**32:
            raise ValueError("surface_uid_invalid")
        if self.starting_new is not True:
            raise ValueError("candidate_display_not_authorized")


@dataclass(frozen=True)
class CompositorPresentation:
    fact: SurfaceFact
    grant_id: UUID
    frame_tag: str
    observed_monotonic_ms: int


@dataclass(frozen=True)
class RoleRemoval:
    surface: Surface
    decision_id: UUID


@dataclass(frozen=True)
class DiagnosticPresentation:
    output: OutputKey
    serial: int


@dataclass(frozen=True)
class OverlayPresentation:
    output: OutputKey
    serial: int
    buffer_id: str
    frame_tag: str


@dataclass(frozen=True)
class DiagnosticRelease:
    surface: Surface
    handoff_id: UUID


@dataclass(frozen=True)
class HoldFact:
    """The shell's `hold` event (issue #123): whether the released Output is held, and by what.
    `picture_plane`: a mapped sub-surface of the app's with a buffer and a window alpha above 0;
    `display_drawing`: Display's own pattern, target (the `picture` op's `drawing`) or a live
    trial. None: neither, so the holding slate covers the Output (the grant kept)."""

    output_id: str
    holding: bool
    evidence: Literal["picture_plane", "display_drawing"] | None


class WestonBackend:
    """Serialized controller; call ``dispatch`` only outside a domain mutation.

    One lock serializes the channel: the display controller's main loop dispatches and requests
    on it, and the power controller's worker asks for output power (``output_power``,
    ``power_methods.OutputPowerControl``) from its own thread.

    The caller owns a bounded authenticated ingress and event outbox. Transport
    failure makes the output unknown; it cannot acknowledge a diagnostic. The
    shell's own local timer independently covers the app on lease/process loss.
    """

    def __init__(
        self,
        channel: socket.socket,
        *,
        display_uid: int,
        compositor_pid: int,
        verify_process: Callable[[SurfaceGrant], bool],
    ):
        if channel.type & socket.SOCK_SEQPACKET != socket.SOCK_SEQPACKET:
            raise ValueError("display_seqpacket_required")
        if not hasattr(socket, "SO_PEERCRED"):
            raise RuntimeError("linux_display_backend_required")
        pid, uid, _gid = struct.unpack(
            "3i", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        if (pid, uid) != (compositor_pid, display_uid):
            raise ValueError("display_peer_identity")
        self.channel = channel
        self.channel.settimeout(2)
        self.verify_process = verify_process
        self.pending: deque[dict] = deque(maxlen=256)
        self.grants: dict[str, tuple[SurfaceGrant, UUID]] = {}
        self.withdrawals = {}
        self.trials: dict = {}
        self.revisions: dict[str, tuple[SurfaceGrant, UUID]] = {}
        self.host: DisplayHost | None = None
        self.boot_id: UUID | None = None
        self.incarnation_id: UUID | None = None
        self._channel_lock = threading.RLock()
        self._powered: dict[str, Power] = {}   # the compositor's own output power, per Output
        self._black: dict[str, bool] = {}      # whether the shell's black curtain is up, per Output
        self._turned: dict[str, int] = {}      # the transform the shell's last `turn` event said, per Output
        self._modes: dict[str, tuple[int, int]] = {}   # the mode Weston runs, per connected Output
        self.picture_events: PictureEvents | None = None   # set by the runner (roadmap 1c)
        # Told each Output id the shell (re)creates (a Weston restart or hot-plug), from `dispatch`;
        # each never blocks. The runner's region forwarder re-sends the Frame region (issue #124).
        self.output_listeners: list[Callable[[str], None]] = []

    def _receive(self) -> dict:
        raw, _ancillary, flags, _address = self.channel.recvmsg(MAX_PACKET)
        if not raw or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            raise ConnectionError("display_channel_lost")
        value = json.loads(raw, object_pairs_hook=_pairs)
        if type(value) is not dict or type(value.get("event")) is not str:
            raise ValueError("display_message_invalid")
        return value

    def _note_output_state(self, value: dict) -> bool:
        """Keep the compositor's output power from an `output_power` event, the black curtain's
        state from a `black` event or the screen's transform from a `turn` event (each the answer
        to the controller's own op); whether it was one of them."""
        if value.get("event") == "black":
            if type(value.get("on")) is not bool:
                raise ValueError("display_message_invalid")
            self._black[value["output_id"]] = value["on"]
            return True
        if value.get("event") == "turn":
            if type(value.get("transform")) is not int:
                raise ValueError("display_message_invalid")
            self._turned[value["output_id"]] = value["transform"]
            return True
        if value.get("event") != "output_power":
            return False
        self._powered[value["output_id"]] = Power(value["power"])
        return True

    def _enqueue(self, value: dict) -> None:
        if len(self.pending) == self.pending.maxlen:
            raise ConnectionError("display_event_gap")
        self.pending.append(value)

    def initialize(self) -> DisplayHost:
        hello = self._receive()
        if hello.get("event") != "hello" or hello.get("protocol") != 1:
            raise ValueError("display_protocol_incompatible")
        self.boot_id = UUID(hello["boot_id"])
        self.incarnation_id = UUID(hello["incarnation_id"])
        self.host = DisplayHost(
            boot_id=self.boot_id, incarnation_id=self.incarnation_id, backend=self
        )
        return self.host

    def _request(self, operation: str, key: OutputKey, *, timeout: float = 2, **values: object) -> None:
        with self._channel_lock:
            self._request_locked(operation, key, timeout, values)

    def _request_locked(self, operation: str, key: OutputKey, timeout: float, values: dict) -> None:
        request_id = str(uuid4())
        payload = {
            "op": operation,
            "request_id": request_id,
            "output_id": key.output_id,
            "output": asdict(key),
            **values,
        }
        wire = json.dumps(payload, default=str, separators=(",", ":")).encode()
        if len(wire) > MAX_PACKET or self.channel.send(wire) != len(wire):
            raise ConnectionError("display_request_failed")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self._receive()
            if value.get("event") == "response" and value.get("request_id") == request_id:
                if value.get("accepted") is not True:
                    raise ValueError("display_request_rejected")
                return
            if not self._note_output_state(value):
                self._enqueue(value)
        raise TimeoutError("display_response_timeout")

    def output_power(self, output_id: str, power: Power, *, timeout: float) -> bool:
        """Ask the shell to turn `output_id`'s signal off or on (DPMS; the Output stays); whether
        the compositor reports it done. Called from the power controller's worker thread."""
        try:
            with self._channel_lock:
                if self.host is None or output_id not in self._powered:
                    return False
                key = self.host.state(output_id).key
                self._request("output_power", key, timeout=timeout, power=power.value)
                return self._powered.get(output_id) is power
        except (OSError, ValueError, KeyError, TimeoutError):
            return False

    def output_powered(self, output_id: str) -> Power | None:
        """The compositor's own output power for `output_id`; None while it has no such Output."""
        with self._channel_lock:
            return self._powered.get(output_id)

    def black(self, output_id: str, on: bool, *, timeout: float) -> bool:
        """Ask the shell to put its opaque black curtain over every view on `output_id` (`on`) or
        take it down (control op `black`); whether the shell's `black` event confirms it. Called
        from the power controller's worker thread (output_power.BlackPicture)."""
        try:
            with self._channel_lock:
                if self.host is None or output_id not in self._black:
                    return False
                key = self.host.state(output_id).key
                self._request("black", key, timeout=timeout, on=on)
                return self._black.get(output_id) is on
        except (OSError, ValueError, KeyError, TimeoutError):
            return False

    def black_shown(self, output_id: str) -> bool | None:
        """Whether the shell last said `output_id`'s black curtain is up; None while it has no such
        Output (a new Output has none)."""
        with self._channel_lock:
            return self._black.get(output_id)

    def turn(self, output_id: str, turn: DisplayTurn, *, timeout: float) -> bool:
        """Ask the shell to turn `output_id`'s screen as it hangs (control op `turn`, its
        `transform` contracts/display_turn.py `wayland_transform(turn)`); whether the shell's `turn`
        event confirms that transform. Weston then reports the Output turned to every client (the
        Player draws its pictures turned to match), and the shell treats the change as any change
        of the Output's mode (the app's grant is invalidated, a new Output key follows). False for
        an Output the shell does not have, a refusal or a timeout (render redesign, run ledger
        .claude/runs/render-planes/s1-page.md, bead B2). ValueError for a turn that is not one of
        TURNS (the caller's error, never sent)."""
        transform = wayland_transform(turn)
        try:
            with self._channel_lock:
                if self.host is None or output_id not in self._powered:
                    return False
                key = self.host.state(output_id).key
                self._turned.pop(output_id, None)
                self._request("turn", key, timeout=timeout, transform=transform)
                return self._turned.get(output_id) == transform
        except (OSError, ValueError, KeyError, TimeoutError):
            return False

    def picture(self, output_id: str, operations: str, brightness: str, *, drawing: bool,
                timeout: float = 2) -> bool:
        """Hand one Output's encoded picture operations (contracts/node_frame.py
        `encode_picture_operations`) and base page brightness line to the shell (control op
        `picture`; frame protocol v4 forwards them to the Player and the overlay client), with
        `drawing`: whether Display itself draws on the Output (a test pattern or calibration
        target), which holds it with no picture plane (issue #123); whether the shell took them.
        Called from the picture controller's workers."""
        try:
            with self._channel_lock:
                if self.host is None or type(drawing) is not bool:
                    return False
                key = self.host.state(output_id).key
                self._request("picture", key, timeout=timeout,
                              operations=operations, brightness=brightness, drawing=drawing)
                return True
        except (OSError, ValueError, KeyError, TimeoutError):
            return False

    def region(self, output_id: str, region: FrameRegion | None, *, timeout: float = 2) -> bool:
        """Hand one Output's Frame region (issue #124) to the shell (control op `region`: its
        canonical JSON, contracts/node_output.py `encode_frame_region`, or "" for none), which keeps
        it and forwards it verbatim to the overlay client; whether the shell took it. False for an
        Output the shell does not have (yet), a refusal or a timeout."""
        text = "" if region is None else encode_frame_region(region).decode()
        try:
            with self._channel_lock:
                if self.host is None:
                    return False
                key = self.host.state(output_id).key
                self._request("region", key, timeout=timeout, region=text)
                return True
        except (OSError, ValueError, KeyError, TimeoutError):
            return False

    def central_card(self, output_id: str, card: CentralCard | None, *, timeout: float = 2) -> bool:
        """Hand one Output's Central card to the shell (control op `central_card`: its canonical
        JSON, contracts/node_output.py `encode_central_card`, or "" for none), which keeps it and
        forwards it verbatim to the overlay client (pw_diagnostic_manager_v1 v6); whether the shell
        took it. False for an Output the shell does not have (yet), a refusal or a timeout."""
        text = "" if card is None else encode_central_card(card).decode()
        try:
            with self._channel_lock:
                if self.host is None:
                    return False
                key = self.host.state(output_id).key
                self._request("central_card", key, timeout=timeout, card=text)
                return True
        except (OSError, ValueError, KeyError, TimeoutError):
            return False

    def current_mode(self, output_id: str) -> tuple[int, int] | None:
        """The mode Weston runs on `output_id` (width, height), as the shell's last `output` event
        said; None while the Output is not connected or Weston states none."""
        with self._channel_lock:
            return self._modes.get(output_id)

    def allow(self, grant: SurfaceGrant) -> UUID:
        """Called by protected lifecycle/configuration ingress, never by Player IPC."""
        if not self.verify_process(grant):
            raise ValueError("display_process_not_verified")
        if grant.surface.output.output_id in self.grants:
            raise ValueError("display_authorized_withdrawal_required")
        grant_id = uuid4()
        self.grants[grant.surface.output.output_id] = (grant, grant_id)
        return grant_id

    def candidate(self, surface: Surface) -> None:
        grant, grant_id = self.grants[surface.output.output_id]
        if grant.surface != surface or not self.verify_process(grant):
            raise ValueError("display_grant_stale")
        self._request(
            "candidate",
            surface.output,
            identity=asdict(surface),
            grant_id=str(grant_id),
            uid=grant.uid,
        )

    def revise(self, surface: Surface, decision_id: UUID, *, expires_boottime_ms: int) -> None:
        from appliance.kernel.clock import boottime_ms

        state = self.host.state(surface.output.output_id)
        active, _ = self.grants[surface.output.output_id]
        grant = SurfaceGrant(surface, active.uid, True)
        if state.revision == surface:
            return
        if not self.verify_process(grant):
            raise ValueError("revision_process_stale")
        self.host.stage_revision(surface)
        grant_id = uuid4()
        try:
            self._request(
                "revision",
                surface.output,
                identity=asdict(surface),
                grant_id=str(grant_id),
                decision_id=str(decision_id),
                ttl_ms=max(1, min(5000, expires_boottime_ms - boottime_ms())),
            )
        except Exception:
            self.host.revision_expired(surface)
            raise
        self.revisions[surface.output.output_id] = (grant, grant_id)

    def withdraw(self, surface: Surface, decision_id: UUID) -> None:
        grant, _ = self.grants.get(surface.output.output_id, (None, None))
        if grant is None or grant.surface != surface:
            raise ValueError("withdrawal_surface_changed")
        self.withdrawals[surface.output.output_id] = (surface, decision_id)
        try:
            self._request("withdraw", surface.output, identity=asdict(surface),
                          decision_id=str(decision_id))
        except BaseException:
            self.withdrawals.pop(surface.output.output_id, None)
            raise

    def trial(self, key: OutputKey, raw: str | None) -> None:
        from dataclasses import replace

        from contracts.node_calibration import encode_trial, parse_trial

        if raw is None:
            if key.output_id in self.trials:
                self._request("trial_end", key)
                self.trials.pop(key.output_id, None)
            return
        candidate = parse_trial(raw)
        if candidate.baseline != self.host.state(key.output_id).admitted:
            raise ValueError("trial_not_current_admitted")
        prior = self.trials.get(key.output_id)
        if prior is not None and (prior.trial_id, prior.generation) == (
            candidate.trial_id,
            candidate.generation,
        ):
            hard = min(prior.hard_expires_boottime_ms, candidate.hard_expires_boottime_ms)
            candidate = replace(
                candidate,
                hard_expires_boottime_ms=hard,
                expires_boottime_ms=min(hard, candidate.expires_boottime_ms),
            )
        self._request("trial", key, candidate=encode_trial(candidate))
        self.trials[key.output_id] = candidate

    def diagnostic(self, key: OutputKey, reason: str) -> None:
        self._request("diagnostic", key)
        self.grants.pop(key.output_id, None)

    def release_diagnostic(self, surface: Surface, buffer_token: str, handoff_id: UUID) -> None:
        grant, grant_id = self.grants[surface.output.output_id]
        if grant.surface != surface or not self.verify_process(grant):
            raise ValueError("display_grant_stale")
        self._request(
            "handoff",
            surface.output,
            grant_id=str(grant_id),
            buffer_id=buffer_token,
            handoff_id=str(handoff_id),
        )

    def dispatch(
        self,
    ) -> (
        CompositorPresentation
        | DiagnosticPresentation
        | DiagnosticRelease
        | HoldFact
        | RoleRemoval
        | SurfaceFact
        | OutputKey
        | None
    ):
        """Consume an independently delivered native event; never synthesize one. None when
        another thread's request already took the event the caller saw arrive."""
        if self.host is None:
            raise RuntimeError("display_not_initialized")
        with self._channel_lock:
            if self.pending:
                return self._dispatch(self.pending.popleft())
            if not select.select([self.channel], [], [], 0)[0]:
                return None
            return self._dispatch(self._receive())

    def _picture_presented(self, output_id: str, text: object) -> None:
        """A v4 `presented` event's picture change to the picture controller (null: a v3 `tag`)."""
        if self.picture_events is None or text is None:
            return
        try:
            change = parse_picture_change(text) if isinstance(text, str) else None
        except ValueError:
            change = None
        if change is None and text != "0":
            log.warning("presented event with an invalid picture change on %s", output_id)
            return
        self.picture_events.presented(output_id, change)

    def _dispatch(self, value: dict):
        event = value["event"]
        if self._note_output_state(value):
            return None
        if event == "role_removed":
            surface, decision_id = _surface(value["identity"]), UUID(value["decision_id"])
            if self.withdrawals.get(surface.output.output_id) != (surface, decision_id):
                return None
            self.withdrawals.pop(surface.output.output_id, None)
            self.trials.pop(surface.output.output_id, None)
            return RoleRemoval(surface, decision_id)
        if event == "output":
            key = _output(value["output"])
            self.host.observe_output(key, connected=value["connected"])
            mode = value.get("mode")
            if mode is not None and not (type(mode) is list and len(mode) == 2
                                         and all(type(side) is int for side in mode)):
                raise ValueError("display_message_invalid")
            if value["connected"]:
                self._powered[key.output_id] = Power(value.get("power", Power.ON.value))
                self._black[key.output_id] = value.get("black") is True   # the shell says
                if mode is None or not all(1 <= side <= MAX_SCREEN_SIDE for side in mode):
                    self._modes.pop(key.output_id, None)   # none, or none the report can carry
                else:
                    self._modes[key.output_id] = (mode[0], mode[1])
            else:
                self._powered.pop(key.output_id, None)
                self._black.pop(key.output_id, None)
                self._turned.pop(key.output_id, None)
                self._modes.pop(key.output_id, None)
            self.grants.pop(key.output_id, None)
            self.revisions.pop(key.output_id, None)
            self.withdrawals.pop(key.output_id, None)
            self.trials.pop(key.output_id, None)
            if value["connected"] and self.picture_events is not None:
                self.picture_events.shell_output(key.output_id)
            if value["connected"]:
                for listener in self.output_listeners:
                    listener(key.output_id)
            return key
        if event == "overlay_presented":
            key = _output(value["output"])
            state = self.host.state(key.output_id)
            if state.key == key and state.admitted is not None:
                return OverlayPresentation(
                    key, value["serial"], value["buffer_id"], value["frame_tag"]
                )
            return None
        if event == "hold":
            key = _output(value["output"])
            holding, evidence = value["holding"], value["evidence"]
            if type(holding) is not bool or evidence not in (
                    (None,) if not holding else ("picture_plane", "display_drawing")):
                raise ValueError("display_message_invalid")
            if self.host.state(key.output_id).key != key:
                return None   # a hold of an Output key since replaced
            self.host.hold(key.output_id, holding=holding)
            return HoldFact(key.output_id, holding, evidence)
        if event == "diagnostic_presented":
            key = _output(value["output"])
            state = self.host.state(key.output_id)
            if state.key == key and state.diagnostic == "requested":
                self.host.diagnostic_presented(key)
                return DiagnosticPresentation(key, value["serial"])
            return None
        if event in ("revision_adopted", "revision_expired"):
            surface = _surface(value["identity"])
            pending = self.revisions.get(surface.output.output_id)
            if (
                pending is None
                or pending[0].surface != surface
                or UUID(value["grant_id"]) != pending[1]
            ):
                return None
            if event == "revision_adopted":
                self.host.revision_adopted(surface)
                self.grants[surface.output.output_id] = pending
            else:
                self.host.revision_expired(surface)
            self.revisions.pop(surface.output.output_id, None)
            return None
        if event not in ("presented", "handoff_presented", "invalidated"):
            raise ValueError("display_unexpected_event")
        surface = _surface(value["identity"])
        grant, grant_id = self.grants.get(surface.output.output_id, (None, None))
        if grant is None or grant.surface != surface:
            return None  # stale prior grant; never renew its successor
        if event == "invalidated":
            fact = surface.fact("invalidated")
            self.host.backend_invalidated(surface.output, reason=value["reason"])
            self.grants.pop(surface.output.output_id, None)
            return fact
        if not self.verify_process(grant):
            self.host.process_exited(surface.process)
            return None
        if event == "handoff_presented":
            handoff_id = UUID(value["handoff_id"])
            self.host.diagnostic_released(surface, handoff_id=handoff_id)
            return DiagnosticRelease(surface, handoff_id)
        if UUID(value["grant_id"]) != grant_id:
            return None
        from contracts.node_protocol import counter, token

        token(value["frame_tag"], 96)
        counter(value["observed_monotonic_ms"])
        fact = self.host.presented(
            surface, buffer_token=value["buffer_id"], now_ms=value["observed_monotonic_ms"]
        )
        self._picture_presented(surface.output.output_id, value.get("picture_change"))
        return CompositorPresentation(
            fact, grant_id, value["frame_tag"], value["observed_monotonic_ms"]
        )
