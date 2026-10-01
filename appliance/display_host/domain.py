"""Per-Output surface authority, independent of application preparation.

Backend requests are never presentation evidence. Only the qualified backend calls
``presented``/``diagnostic_presented``; an application frame token cannot call them.
The native Weston adapter is separate; this domain makes no claim about physical pixels.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol
from uuid import UUID, uuid4

from contracts.node_display import Surface
from contracts.node_protocol import NodeProcessIdentity, OutputKey, SurfaceFact


@dataclass(frozen=True)
class OutputState:
    key: OutputKey
    connected: bool = True
    candidate: Surface | None = None
    revision: Surface | None = None
    presented: Surface | None = None
    admitted: Surface | None = None
    buffer_token: str | None = None
    lease_expires_ms: int = 0
    diagnostic: str = "requested"
    handoff_id: UUID | None = None
    fault: str | None = None


class DisplayBackend(Protocol):
    def diagnostic(self, key: OutputKey, reason: str) -> None:
        """Request base diagnostic; acknowledgment arrives independently."""
        ...

    def candidate(self, surface: Surface) -> None:
        """Prepare a constrained candidate; keep the operational surface intact."""
        ...

    def release_diagnostic(self, surface: Surface, buffer_token: str, handoff_id: UUID) -> None:
        """Request the authorized handoff; do not report physical pixels."""
        ...


class DisplayHost:
    def __init__(
        self,
        *,
        boot_id: UUID,
        incarnation_id: UUID,
        backend: DisplayBackend,
        lease_ms: int = 5000,
        max_outputs: int = 16,
    ):
        if type(lease_ms) is not int or not 1 <= lease_ms <= 60000:
            raise ValueError("surface_lease_invalid")
        if type(max_outputs) is not int or not 1 <= max_outputs <= 64:
            raise ValueError("output_bound_invalid")
        self.boot_id = boot_id
        self.incarnation_id = incarnation_id
        self.backend = backend
        self.lease_ms = lease_ms
        self.max_outputs = max_outputs
        self._outputs: dict[str, OutputState] = {}

    def states(self) -> tuple[OutputState, ...]:
        return tuple(self._outputs.values())

    def state(self, output_id: str) -> OutputState:
        return self._outputs[output_id]

    def observe_output(self, key: OutputKey, *, connected: bool) -> OutputState:
        """Adopt an identity created by the trusted backend, never by the app."""
        if (
            key.kernel_boot_id != self.boot_id
            or key.display_host_incarnation != self.incarnation_id
            or type(connected) is not bool
        ):
            raise ValueError("backend_output_identity")
        previous = self._outputs.get(key.output_id)
        if previous is None and len(self._outputs) >= self.max_outputs:
            raise ValueError("output_capacity")
        if previous is not None and (
            key.connection_generation <= previous.key.connection_generation
            or key.mode_generation <= previous.key.mode_generation
        ):
            if key == previous.key and connected == previous.connected:
                return previous
            raise ValueError("backend_output_generation")
        state = OutputState(
            key,
            connected=connected,
            diagnostic="requested" if connected else "unknown",
            fault="app_absent" if connected else "output_disconnected",
        )
        self._outputs[key.output_id] = state
        return state

    def backend_invalidated(self, key: OutputKey, *, reason: str) -> OutputState:
        """Backend already covered this Output; do not issue a duplicate command."""
        old = self.state(key.output_id)
        if old.key != key:
            raise ValueError("backend_output_stale")
        state = OutputState(key, connected=old.connected, diagnostic="requested", fault=reason)
        self._outputs[key.output_id] = state
        return state

    def connect(self, output_id: str) -> OutputState:
        previous = self._outputs.get(output_id)
        if previous is None and len(self._outputs) >= self.max_outputs:
            raise ValueError("output_capacity")
        generation = previous.key.connection_generation + 1 if previous else 1
        mode = previous.key.mode_generation + 1 if previous else 1
        key = OutputKey(self.boot_id, self.incarnation_id, output_id, generation, mode)
        self._outputs[output_id] = OutputState(key)
        return self._diagnostic(output_id, "app_absent")

    def mode_changed(self, output_id: str) -> OutputState:
        old = self.state(output_id)
        if not old.connected:
            raise ValueError("output_disconnected")
        key = replace(
            old.key,
            connection_generation=old.key.connection_generation + 1,
            mode_generation=old.key.mode_generation + 1,
        )
        self._outputs[output_id] = OutputState(key)
        return self._diagnostic(output_id, "mode_changed")

    def disconnect(self, output_id: str) -> OutputState:
        old = self.state(output_id)
        key = replace(
            old.key,
            connection_generation=old.key.connection_generation + 1,
            mode_generation=old.key.mode_generation + 1,
        )
        state = OutputState(key, connected=False, diagnostic="unknown", fault="output_disconnected")
        self._outputs[output_id] = state
        return state

    def _current(self, surface: Surface) -> OutputState:
        state = self.state(surface.output.output_id)
        if not state.connected or state.key != surface.output:
            raise ValueError("surface_output_stale")
        return state

    def offer(self, surface: Surface) -> OutputState:
        state = self._current(surface)
        if state.admitted is not None and state.admitted != surface:
            raise ValueError("authorized_withdrawal_required")
        # Validate the common identity fields with the canonical shared contract.
        surface.fact("invalidated")
        self.backend.candidate(surface)
        # Preparing candidates cannot withdraw an admitted operational surface.
        state = replace(state, candidate=surface)
        if state.admitted is None and state.presented != surface:
            state = replace(state, presented=None, buffer_token=None, lease_expires_ms=0)
        self._outputs[state.key.output_id] = state
        return state

    def stage_revision(self, surface: Surface) -> None:
        state = self._current(surface)
        old = state.admitted
        if (
            old is None
            or state.diagnostic != "released"
            or (old.output, old.process, old.app_epoch, old.binding_generation, old.frame_id)
            != (surface.output, surface.process, surface.app_epoch, surface.binding_generation, surface.frame_id)
            or surface.config_revision <= old.config_revision
        ):
            raise ValueError("revision_not_same_admitted_surface")
        self._outputs[state.key.output_id] = replace(state, revision=surface)

    def revision_adopted(self, surface: Surface) -> None:
        state = self._current(surface)
        if state.revision != surface or state.admitted is None:
            raise ValueError("revision_not_authorized")
        self._outputs[state.key.output_id] = replace(
            state,
            candidate=surface,
            admitted=surface,
            revision=None,
            presented=None,
            buffer_token=None,
        )

    def revision_expired(self, surface: Surface) -> None:
        state = self._current(surface)
        if state.revision == surface:
            self._outputs[state.key.output_id] = replace(state, revision=None)

    def presented(self, surface: Surface, *, buffer_token: str, now_ms: int) -> SurfaceFact:
        state = self._current(surface)
        if (
            surface not in (state.candidate, state.admitted)
            or type(now_ms) is not int
            or now_ms < 0
        ):
            raise ValueError("presentation_not_current")
        fact = surface.fact("presented_to_compositor", buffer_token)
        state = replace(
            state,
            presented=surface,
            buffer_token=buffer_token,
            lease_expires_ms=now_ms + self.lease_ms,
        )
        self._outputs[state.key.output_id] = state
        return fact

    def authorize_handoff(
        self, surface: Surface, *, now_ms: int, handoff_id: UUID | None = None,
        buffer_token: str | None = None
    ) -> OutputState:
        """Authorize a fresh handoff, independently of first-frame evidence.

        Its UUID correlates backend acknowledgment even if opaque buffer IDs are
        reused across withdrawal and re-admission of the same surface tuple.
        """
        state = self._current(surface)
        if (
            state.candidate != surface
            or state.presented != surface
            or state.buffer_token is None
            or type(now_ms) is not int
            or not 0 <= now_ms < state.lease_expires_ms
        ):
            raise ValueError("handoff_without_current_presentation")
        handoff_id = handoff_id or uuid4()
        if type(handoff_id) is not UUID:
            raise ValueError("handoff_identity_invalid")
        self.backend.release_diagnostic(surface, buffer_token or state.buffer_token, handoff_id)
        state = replace(
            state, admitted=surface, diagnostic="release_requested", handoff_id=handoff_id
        )
        self._outputs[state.key.output_id] = state
        return state

    def diagnostic_released(self, surface: Surface, *, handoff_id: UUID) -> OutputState:
        state = self._current(surface)
        if (
            state.admitted != surface
            or state.diagnostic != "release_requested"
            or type(handoff_id) is not UUID
            or state.handoff_id != handoff_id
        ):
            raise ValueError("diagnostic_release_stale")
        state = replace(state, diagnostic="released")
        self._outputs[state.key.output_id] = state
        return state

    def diagnostic_presented(self, key: OutputKey) -> OutputState:
        state = self.state(key.output_id)
        if state.key != key or not state.connected or state.diagnostic != "requested":
            raise ValueError("diagnostic_presentation_stale")
        state = replace(state, diagnostic="presented_to_compositor")
        self._outputs[key.output_id] = state
        return state

    def _diagnostic(self, output_id: str, reason: str) -> OutputState:
        state = self.state(output_id)
        state = replace(
            state,
            candidate=None,
            revision=None,
            presented=None,
            admitted=None,
            buffer_token=None,
            lease_expires_ms=0,
            diagnostic="requested",
            handoff_id=None,
            fault=reason,
        )
        self._outputs[output_id] = state
        try:
            self.backend.diagnostic(state.key, reason)
        except Exception:
            state = replace(state, diagnostic="unknown", fault="display_backend_unavailable")
            self._outputs[output_id] = state
        return state

    def authorized_withdrawal(self, output_id: str) -> OutputState:
        """Lifecycle has authorized withdrawal; staging must never invoke this."""
        return self._diagnostic(output_id, "authorized_withdrawal")

    def process_exited(self, process: NodeProcessIdentity) -> tuple[SurfaceFact, ...]:
        facts = []
        for output_id, state in tuple(self._outputs.items()):
            affected = [
                s
                for s in (state.admitted, state.presented, state.candidate)
                if s is not None and s.process == process
            ]
            if not affected:
                continue
            facts.extend(s.fact("invalidated") for s in dict.fromkeys(affected))
            if state.admitted is not None and state.admitted.process != process:
                # A failed background candidate must not cover healthy playback.
                self._outputs[output_id] = replace(state, candidate=None, presented=state.admitted)
            else:
                self._diagnostic(output_id, "app_exited")
        return tuple(facts)

    def expire(self, *, now_ms: int) -> tuple[SurfaceFact, ...]:
        if type(now_ms) is not int or now_ms < 0:
            raise ValueError("clock_invalid")
        facts = []
        for output_id, state in tuple(self._outputs.items()):
            if state.presented is not None and now_ms >= state.lease_expires_ms:
                facts.append(state.presented.fact("invalidated"))
                self._diagnostic(output_id, "surface_lease_expired")
        return tuple(facts)
