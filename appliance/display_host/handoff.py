"""The handoff on the Pi (decision 0034, S4b part 4): which Player run each Output is handed to.

The Display Host hands an Output to the Player when the Output is connected, its Output document
binds a Frame to it (contracts/node_output.py `OutputBinding`) and the app broker says which
Player run is current (the `app_run` ingress op, appliance/kernel/app_facts.py), and withdraws
what no longer matches. Both inputs arrive here and nowhere else; no request to Central decides a
handoff (Players self-sufficient; owner, 2026-10-24).

`wanted_surface` and `next_step` are the rule, pure; `Handoffs` holds the inputs and applies the
rule per Output at the main loop's existing pass (`settle`): no timer of its own.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Protocol
from uuid import UUID, uuid4

from appliance.kernel.app_facts import PLAYER_UID
from contracts.node_display import Surface
from contracts.node_output import OUTPUT_IDS, OutputBinding
from contracts.node_protocol import NodeProcessIdentity, counter

from .domain import DisplayHost, OutputState
from .weston import SurfaceGrant

log = logging.getLogger(__name__)

_RUN_FIELDS = frozenset({"invocation_id", "pid", "start_ticks", "app_epoch"})

REVISION_TTL_MS: Final = 3000   # the shell's revision window (was Central's decision_ms; at most the shell's LEASE_MS)
RETRY_MS: Final = 1000          # a step the backend refused is tried again at most this often, for the same Surface


@dataclass(frozen=True)
class AppRun:
    """The broker's current run: the Player's process (systemd invocation, kernel birth) and the
    broker's app epoch, as its `RunDocument` states them."""
    process: NodeProcessIdentity
    app_epoch: int

    @classmethod
    def of(cls, document: object) -> AppRun:
        """A `RunDocument` (exactly its four fields: a UUID's text, a pid and start ticks >= 1, an
        epoch >= 1); ValueError("app_run") otherwise."""
        try:
            if not isinstance(document, dict) or set(document) != _RUN_FIELDS:
                raise ValueError("app_run")
            invocation = document["invocation_id"]
            if type(invocation) is not str:
                raise ValueError("app_run")
            epoch = document["app_epoch"]
            counter(epoch, 1)
            return cls(NodeProcessIdentity(document["pid"], document["start_ticks"], UUID(invocation)), epoch)
        except (ValueError, TypeError) as error:
            raise ValueError("app_run") from error


def wanted_surface(state: OutputState, binding: OutputBinding | None, run: AppRun | None) -> Surface | None:
    """The Surface the Output is to be handed to: its key, the current run and the bound Frame,
    when the Output is connected, bound and a run is current; None otherwise."""
    if not state.connected or binding is None or run is None:
        return None
    return Surface(state.key, run.process, run.app_epoch, binding.generation,
                   binding.configuration_revision, binding.frame_id)


@dataclass(frozen=True)
class Offer:
    surface: Surface


@dataclass(frozen=True)
class Handoff:
    surface: Surface
    buffer_token: str


@dataclass(frozen=True)
class Revise:
    surface: Surface


@dataclass(frozen=True)
class Withdraw:
    surface: Surface


Step = Offer | Handoff | Revise | Withdraw


def _revision_of(admitted: Surface, wanted: Surface) -> bool:
    """Whether `wanted` is `admitted` with only a higher configuration revision (a Position Save)."""
    same = (admitted.output, admitted.process, admitted.app_epoch, admitted.binding_generation,
            admitted.frame_id)
    return same == (wanted.output, wanted.process, wanted.app_epoch, wanted.binding_generation,
                    wanted.frame_id) and wanted.config_revision > admitted.config_revision


def next_step(state: OutputState, wanted: Surface | None, *, withdrawing: bool) -> Step | None:
    """The one handoff rule, first match wins:
    - not connected: None (the shell's `output` event already reset the Output);
    - the held surface (admitted, else candidate) is not `wanted`:
        Revise(wanted) when admitted and wanted differ only by a higher config_revision, the
        diagnostic is released and no revision is pending; None while that revision is pending or
        the admitted surface's release is still awaited (a Save never withdraws a handoff in
        flight); else Withdraw(held), or None while `withdrawing`;
    - admitted == wanted: None;
    - candidate == wanted and presented == wanted with a buffer token and no release requested:
        Handoff(wanted, token);
    - candidate == wanted otherwise: None (awaiting the compositor's presentation);
    - nothing held and wanted not None: Offer(wanted);
    - else None. Pure."""
    if not state.connected:
        return None
    held = state.admitted if state.admitted is not None else state.candidate
    if held is not None and held != wanted:
        if state.admitted is not None and wanted is not None and _revision_of(state.admitted, wanted):
            if state.diagnostic != "released" or state.revision is not None:
                return None
            return Revise(wanted)
        return None if withdrawing else Withdraw(held)
    if state.admitted is not None:
        return None
    if state.candidate is not None:
        if (state.presented == wanted and state.buffer_token is not None
                and state.diagnostic != "release_requested"):
            return Handoff(wanted, state.buffer_token)
        return None
    if wanted is not None:
        return Offer(wanted)
    return None


class HandoffBackend(Protocol):
    """The shell's handoff ops, as WestonBackend carries them."""
    withdrawals: Mapping[str, object]       # each Output's withdrawal awaiting its `role_removed`
    grants: dict

    def allow(self, grant: SurfaceGrant) -> UUID: ...
    def revise(self, surface: Surface, decision_id: UUID, *, ttl_ms: int) -> None: ...
    def withdraw(self, surface: Surface, decision_id: UUID) -> None: ...


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


class Handoffs:
    """Each Output's binding and the current run, as last told, and the rule applied per Output on
    the main loop (`settle`). `document` is called on the bus thread and `run` on the main loop;
    both only store, under one lock, and never block."""

    def __init__(self, backend: HandoffBackend, *, clock: Callable[[], int] = _monotonic_ms) -> None:
        self._backend = backend
        self._clock = clock
        self._lock = threading.Lock()
        self._bindings: dict[str, OutputBinding | None] = {}
        self._run: AppRun | None = None
        self._refused: dict[str, tuple[Step, str, int]] = {}   # a refused step, its cause, when

    def document(self, output_id: str, binding: OutputBinding | None) -> None:
        """The binding of `output_id`'s newest Output document (None: no Frame bound)."""
        if output_id not in OUTPUT_IDS or not (binding is None or isinstance(binding, OutputBinding)):
            raise ValueError("handoff_document")
        with self._lock:
            self._bindings[output_id] = binding

    def run(self, run: AppRun | None) -> None:
        """The broker's current run (None: no run is current)."""
        if not (run is None or isinstance(run, AppRun)):
            raise ValueError("handoff_run")
        with self._lock:
            self._run = run

    def binding(self, output_id: str) -> OutputBinding | None:
        """The binding last told for `output_id`; None when none is bound or no document came."""
        with self._lock:
            return self._bindings.get(output_id)

    @property
    def current_run(self) -> AppRun | None:
        with self._lock:
            return self._run

    def inputs(self) -> dict:
        """Both inputs as last told, JSON-native, for the root ingress's `outputs` op: `run` (the
        broker's `RunDocument`, None when no run is current or none was told) and `bindings`
        (each Output whose document came: its `OutputBinding` fields, None when unbound). An
        Output left dark for want of an input says which one here, never only in a log."""
        with self._lock:
            run, bindings = self._run, dict(self._bindings)
        return {
            "run": None if run is None else {
                "invocation_id": str(run.process.invocation_id), "pid": run.process.pid,
                "start_ticks": run.process.start_ticks, "app_epoch": run.app_epoch},
            "bindings": {output_id: None if binding is None else {
                "frame_id": binding.frame_id, "generation": binding.generation,
                "configuration_revision": binding.configuration_revision}
                for output_id, binding in sorted(bindings.items())},
        }

    def settle(self, host: DisplayHost) -> None:
        """Main loop, each pass, per Output: `next_step` over its state and the Surface wanted,
        applied. Offer -> the backend's grant, then the host's offer; Handoff -> the host's
        authorized handoff of the presented buffer; Revise -> the backend's revision within
        REVISION_TTL_MS; Withdraw -> the backend's withdrawal. A ValueError or KeyError from a
        stale identity is logged once per cause, and the same step is tried again no sooner than
        RETRY_MS (a changed input decides at once): a run the backend cannot verify costs no
        process check on every pass."""
        with self._lock:
            bindings, run = dict(self._bindings), self._run
        for state in host.states():
            output_id = state.key.output_id
            wanted = wanted_surface(state, bindings.get(output_id), run)
            step = next_step(state, wanted, withdrawing=output_id in self._backend.withdrawals)
            if step is None:
                self._refused.pop(output_id, None)
                continue
            refused = self._refused.get(output_id)
            now = self._clock()
            if refused is not None and refused[0] == step and now - refused[2] < RETRY_MS:
                continue
            try:
                self._apply(host, step, now)
            except (ValueError, KeyError) as error:
                cause = f"{type(error).__name__}: {error}"
                if refused is None or refused[:2] != (step, cause):
                    log.warning("handoff %s on %s refused (%s); decided again at a later pass",
                                type(step).__name__, output_id, cause)
                self._refused[output_id] = (step, cause, now)
            else:
                self._refused.pop(output_id, None)

    def _apply(self, host: DisplayHost, step: Step, now: int) -> None:
        surface = step.surface
        if isinstance(step, Offer):
            self._backend.allow(SurfaceGrant(surface, PLAYER_UID, True))
            try:
                host.offer(surface)
            except Exception:
                self._backend.grants.pop(surface.output.output_id, None)
                raise
        elif isinstance(step, Handoff):
            host.authorize_handoff(surface, now_ms=now, handoff_id=uuid4(), buffer_token=step.buffer_token)
        elif isinstance(step, Revise):
            self._backend.revise(surface, uuid4(), ttl_ms=REVISION_TTL_MS)
        else:
            self._backend.withdraw(surface, uuid4())


__all__ = ["REVISION_TTL_MS", "RETRY_MS", "AppRun", "Handoff", "HandoffBackend", "Handoffs", "Offer",
           "Revise", "Step", "Withdraw", "next_step", "wanted_surface"]
