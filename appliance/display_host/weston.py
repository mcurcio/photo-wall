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
from typing import Callable, Protocol
from uuid import UUID, uuid4

from contracts.display_turn import DisplayTurn
from contracts.node_frame import PictureOperations, encode_picture_operations, parse_picture_change
from contracts.node_output import Power
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
        self.picture_events: PictureEvents | None = None   # set by the runner (roadmap 1c)

    def _receive(self) -> dict:
        raw, _ancillary, flags, _address = self.channel.recvmsg(MAX_PACKET)
        if not raw or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            raise ConnectionError("display_channel_lost")
        value = json.loads(raw, object_pairs_hook=_pairs)
        if type(value) is not dict or type(value.get("event")) is not str:
            raise ValueError("display_message_invalid")
        return value

    def _note_power(self, value: dict) -> bool:
        """Keep the compositor's output power from an `output_power` event, or the black curtain's
        state from a `black` event; whether it was one of them."""
        if value.get("event") == "black":
            if type(value.get("on")) is not bool:
                raise ValueError("display_message_invalid")
            self._black[value["output_id"]] = value["on"]
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
            if not self._note_power(value):
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
        Player draws its pictures turned to match). False for an Output the shell does not have
        (render redesign, run ledger .claude/runs/render-planes/s1-page.md, bead B2)."""
        raise NotImplementedError("render-planes S1 bead B2")

    def picture(self, operations: PictureOperations, *, timeout: float, brightness: str) -> bool:
        """Hand one Output's picture operations and base page brightness line to the shell (control
        op `picture`; frame protocol v4 forwards them to the Player and the overlay client); whether
        the shell took them. Called from the picture controller's workers."""
        try:
            with self._channel_lock:
                if self.host is None:
                    return False
                key = self.host.state(operations.output_id).key
                self._request("picture", key, timeout=timeout,
                              operations=encode_picture_operations(operations), brightness=brightness)
                return True
        except (OSError, ValueError, KeyError, TimeoutError):
            return False

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
        if self._note_power(value):
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
            if value["connected"]:
                self._powered[key.output_id] = Power(value.get("power", Power.ON.value))
                self._black[key.output_id] = value.get("black") is True   # the shell says
            else:
                self._powered.pop(key.output_id, None)
                self._black.pop(key.output_id, None)
            self.grants.pop(key.output_id, None)
            self.revisions.pop(key.output_id, None)
            self.withdrawals.pop(key.output_id, None)
            self.trials.pop(key.output_id, None)
            if value["connected"] and self.picture_events is not None:
                self.picture_events.shell_output(key.output_id)
            return key
        if event == "overlay_presented":
            key = _output(value["output"])
            state = self.host.state(key.output_id)
            if state.key == key and state.admitted is not None:
                return OverlayPresentation(
                    key, value["serial"], value["buffer_id"], value["frame_tag"]
                )
            return None
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
