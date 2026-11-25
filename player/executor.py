"""Authenticated timed Player execution, with explicit worker/rendering boundaries."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Hashable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock

from contracts.continuation import continuation_layers, continuation_source
from contracts.models import (
    PREPARE_SECONDS,
    CarriedPhoto,
    Commit,
    Failure,
    Layer,
    LostGrant,
    Observation,
    OutputBinding,
    Plan,
    PlayerConfiguration,
    Readiness,
    Revocation,
    stack_order,
)
from contracts.node_app_link import FrameBlack
from contracts.on_screen import kept_from
from contracts.time import DRIFT_RATE, Monotonic
from player.cache import Cache, CacheCapacityError, CacheError
from player.frame_black import DueLayer, frame_black
from player.look import alone, handed_over, held, look_at
from player.rendering import (
    LocalLayer,
    OutputComposition,
    PresentationResult,
    Renderer,
)
from player.show_clock import ShowClock
from player.visibility import beneath_shown, shed, shown_within, visible_now

DRAW_GRACE = 0.5
# Seconds by which the clock bound Central holds for this Player (its last reported bound grown by
# DRIFT_RATE since, central/coordination.py `_held_photos`) may part from the Player's own before
# readiness is numbered again (`Executor.readiness`): under the 0.1 s floor Central's guards take,
# and at DRIFT_RATE about one report in eight minutes for a Player whose bound stays tight.
CLOCK_BOUND_SLACK = 0.05
"""Seconds a request the renderer answers "pending" is waited on before its layers fail ("decode").
It must exceed the longest a renderer holds a commit in flight (player/pacing.py `OVERDUE`) plus
one planning tick (player/service.py `PLANNING_TICK_MS`), or a held request on a silent Output is
given up before it can be committed ([plane renderer](../docs/module-plane-renderer.md), "One
commit in flight"; held by tests/test_plane_renderer.py)."""


class AuthorityError(ValueError):
    """A message cannot establish current execution authority."""


class AcquisitionStopped(Exception):
    """The Player stopped an acquisition before its last byte: its session ended, or the job's
    authority moved on (a newer plan revision, a revocation). Raised by the bytes' producer. The
    Player's own stop is never the content's failure, so `acquire` records no readiness failure
    for it: the assignment is simply not secured, and the next job fetches it again."""


@dataclass
class _Assignment:
    # The layer as drawn: as offered, with Central's cut applied (`Layer.drawn`), so every rule
    # reads one end; `offered` keeps it as offered (its content identity and its cut).
    layer: Layer
    owner: str
    secured: bool = False
    path: Path | None = None
    prepared: bool = False
    # Covered by an opaque layer above it from now through the prepare lead (player/visibility.py):
    # it holds no decoder, and counts as ready for its reveal, prepared within the lead before it.
    deferred: bool = False
    committed: bool = False
    started: bool = False
    acquiring: bool = False
    failure: str | None = None
    execution_layer: Layer | None = None
    execution_until: float = 0
    offered_until: float = 0
    readiness_generation: int = 0
    # The readiness sequence of the latest start grant (Commit) seen for this revision, accepted
    # or refused; reported as lost while it is prepared and not committed.
    granted_sequence: int | None = None
    # What the latest accepted Commit authorized: this layer (Central's ordering at that
    # revision) between `granted_from` and `granted_until`. A rolling revision that keeps the
    # content keeps the grant until a fresh Commit renews it; every loss of the Commit ends it.
    granted_layer: Layer | None = None
    granted_from: float = 0
    granted_until: float = 0
    # Decision 0032: a continuation slot (contracts/continuation.py) names the plan layer whose
    # secured bytes it draws; None for a plan layer. A slot is never reported to Central.
    source: str | None = None
    # What the latest observation of it said (`_report`): Central hears a change once, never a
    # level state again on every tick.
    reported: tuple | None = None
    # The layer as Central offered it, its cut included (None: `layer` is as offered).
    offered: Layer | None = None

    @property
    def as_offered(self) -> Layer:
        return self.offered or self.layer


@dataclass(frozen=True)
class _Retained:
    local: LocalLayer
    binding: OutputBinding
    owner: str


def _content_identity(layer: Layer) -> tuple:
    """The execution's immutable media/cycle identity; ordering may be revised."""
    return (
        layer.run_id, layer.output_id, layer.frame_id, layer.binding_generation,
        layer.variant, layer.presentation, layer.start, layer.end, layer.media_origin,
        layer.action,
    )


def cut_is_final(before: Layer, after: Layer) -> bool:
    """Decision 0036: Central's cut (`Layer.cut_at`) is set once and only ever brought earlier;
    a revision that removes it or moves it later is refused."""
    return before.cut_at is None or (after.cut_at is not None and after.cut_at <= before.cut_at)


def _cut(layer: Layer | None, offered: Layer) -> Layer | None:
    """`layer` (a grant or an execution of `offered`'s content) under `offered`'s cut: a cut
    only brings an end earlier, never adds authority."""
    if layer is None or offered.cut_at is None or offered.cut_at >= layer.end:
        return layer
    return layer.model_copy(update={"cut_at": offered.cut_at, "fade_out": offered.fade_out}).drawn()


CUT_WAIT_STEP = 2.0
"""Seconds by which the one wait at a cut (`Executor._wait_at_cuts`) moves a cut layer's end past
`now` and its fade-out each time: the fade begins at most this long after the layer beneath is
ready. Moved again once less than half of it is left, so a still picture is not redrawn every
tick, which needs a tick (or a prepare pass) more often than every `CUT_WAIT_STEP / 2`."""


def _waited(layer: Layer | None, offered: Layer, cut: float | None) -> Layer | None:
    """`layer` (the layer as drawn, a grant or an execution of `offered`'s content) ending at
    `cut` with `offered`'s fade-out instead of where it ended (None: at `offered`'s own end): the
    one wait (decision 0036, S3) moves a cut later than Central's and never past the layer's end,
    and never adds authority a layer did not have."""
    if layer is None:
        return None
    whole = layer.model_copy(update={"end": offered.end, "cut_at": None, "fade_out": offered.fade_out})
    if cut is None or cut >= offered.end:
        return whole
    return whole.model_copy(update={"cut_at": cut}).drawn()


def _binding_identity(binding: OutputBinding) -> tuple:
    return binding.output_id, binding.frame_id, binding.generation


def may_still_play(plan: Plan, layer: Layer, now: float) -> bool:
    """Whether `layer` of `plan` may still be drawn at or after `now` (decision 0032): within its
    own span and the plan's validity, or as the source of a continuation of the plan that has not
    ended. The one test of how long a plan layer's bytes are wanted (the executor's pins, the
    service's downloads)."""
    if now < min(layer.drawn_end, plan.valid_until):
        return True
    return any(layer.assignment_id in continuation.photos
               and (continuation.until is None or continuation.until > now)
               for continuation in plan.continuations)


TIMEBASE_TOLERANCE = 0.001
"""Seconds the ShowClock's timebase may move before compositions carry the new one (`_timebase`):
a re-anchor that small is no change any frame can show."""


class Executor:
    """One process's execution state; constructor never restores playback authority.

    Only an adapter authenticated to the central origin may call acceptance methods.
    ``acquire``, ``verify_secured`` and ``maintain_cache`` run on workers. Renderer
    methods (``prepare_imminent``/``tick``) run on its owning thread. The executor
    lock is never held while hashing files or advancing a supplied byte iterator.
    """

    def __init__(
        self,
        player_id: str,
        cache: Cache,
        renderer: Renderer,
        clock: Monotonic,
        show_clock: ShowClock,
        prepare_lead: float = PREPARE_SECONDS,
    ):
        if not math.isfinite(prepare_lead) or prepare_lead <= 0:
            raise ValueError("prepare_lead must be finite and positive")
        self.player_id = player_id
        self.cache, self.renderer = cache, renderer
        self.clock, self.show_clock = clock, show_clock
        self.prepare_lead = prepare_lead
        self._lock = RLock()
        self._cache_work = RLock()
        self._config: PlayerConfiguration | None = None
        self._plan: Plan | None = None
        self._assignments: dict[str, _Assignment] = {}
        self._inactive: dict[str, _Assignment] = {}
        self._cancelled: set[str] = set()
        self._invalidated: set[str] = set()
        self._retained: dict[str, _Retained] = {}
        self._established_retention: set[str] = set()
        self._current: dict[str, OutputComposition] = {}
        self._current_leases: dict[str, float] = {}
        self._pending_presentations: dict[str, tuple[tuple, float]] = {}
        self._pin_intents: dict[str, str] = {}
        self._sequence = 0
        self._reported: dict[int, dict[str, int]] = {}
        # The latest readiness numbered (`readiness`): what it said, and its bound as of when (this
        # machine's monotonic clock), from which the bound Central holds is grown.
        self._readiness: Readiness | None = None
        self._readiness_state: tuple | None = None
        self._readiness_at = 0.0
        # The number of the latest observation (`_report`), never reused while this process runs;
        # each process enrolls into a new authority epoch, so (epoch, number) names one report.
        self._report_sequence = 0
        self._last_epoch = 0
        self._last_plan_id: str | None = None
        self._last_revision = 0
        self._last_revocation_sequence = 0
        self._last_configuration_revision = 0
        self._frame_generations: dict[str, int] = {}
        self._handed: float | None = None       # the timebase compositions carry (`_timebase`)
        self._capacity_ok = False
        self._renderer_releases: set[str] = set()
        self._renderer_residents: set[str] = set()
        # The last prepare pass's decoder budget: what to keep resident, what to give up.
        self._wanted: set[str] = set()
        self._unwanted: set[str] = set()
        self._retired_outputs: dict[str, OutputBinding] = {}
        # Decision 0032: assignments a newer plan superseded while on screen; their composition
        # keeps drawing until that Output draws a layer of the new plan (`_rejoin_hold`).
        self._superseded: set[str] = set()
        # Decision 0032: per Output, the show time it began drawing only continuation slots of the
        # held plan (`continuing_since`); kept by tick (`_note_continuing`).
        self._continuing: dict[str, float] = {}
        # Decision 0032: plan layers Central invalidated at the held revision (`invalidate`); no
        # continuation slot draws one until a Commit renews it or a new revision comes.
        self._revoked: set[str] = set()

    def _timebase(self) -> float:
        """Plan time less this machine's monotonic time, so a renderer reads a composition's
        layers at its own frame times (player/look.py): the ShowClock's, handed over unchanged
        until it moves by more than TIMEBASE_TOLERANCE, so the renderer compares it by equality
        and a re-anchor no frame can show never redraws a still picture."""
        timebase = self.show_clock.timebase()
        if self._handed is None or abs(timebase - self._handed) > TIMEBASE_TOLERANCE:
            self._handed = timebase
        return self._handed

    def _now(self) -> float:
        return self.show_clock.now()

    def _binding(self, output_id: str) -> OutputBinding | None:
        if self._config:
            return next((b for b in self._config.bindings if b.output_id == output_id), None)
        return None

    def _current_authority(self, assignment: _Assignment) -> bool:
        binding = self._binding(assignment.layer.output_id)
        return bool(
            binding and assignment.layer.assignment_id not in self._cancelled
            and self._config and binding.output_id in self._config.enabled_outputs
            and (binding.frame_id, binding.generation) == (
                assignment.layer.frame_id, assignment.layer.binding_generation
            )
        )

    def accept_configuration(self, configuration: PlayerConfiguration) -> bool:
        with self._lock:
            if configuration.player_id != self.player_id:
                raise AuthorityError("configuration belongs to another Player")
            epoch = configuration.authority_epoch
            if epoch < self._last_epoch or (self._config is None and epoch <= self._last_epoch):
                raise AuthorityError("cold restart requires a fresh authority epoch")
            if self._config and epoch == self._last_epoch:
                if configuration == self._config:
                    return False
                if configuration.configuration_revision <= self._last_configuration_revision:
                    raise AuthorityError("stale configuration revision")
            for binding in configuration.bindings:
                if binding.generation < self._frame_generations.get(binding.frame_id, 0):
                    raise AuthorityError("retired Frame generation")
                old = self._binding(binding.output_id)
                if (old and epoch == self._last_epoch and binding.frame_id == old.frame_id
                        and binding.generation == old.generation
                        and binding.configuration_revision < old.configuration_revision):
                    raise AuthorityError("stale Output configuration revision")
            if epoch > self._last_epoch:
                if self._config:
                    self._retired_outputs.update({b.output_id: b for b in self._config.bindings})
                self._renderer_releases.update(self._assignments)
                self._assignments.clear()
                self._inactive.clear()
                self._retained.clear()
                self._established_retention.clear()
                self._current.clear()
                self._current_leases.clear()
                self._cancelled.clear()
                self._invalidated.clear()
                self._plan = None
                self._last_plan_id = None
                self._last_revision = 0
                self._sequence = 0
                self._reported.clear()
            old_bindings = {b.output_id: b for b in self._config.bindings} if self._config else {}
            self._config = configuration
            self._last_epoch = epoch
            self._last_configuration_revision = configuration.configuration_revision
            for binding in configuration.bindings:
                self._frame_generations[binding.frame_id] = binding.generation
            for key, assignment in list(self._assignments.items()):
                binding = self._binding(assignment.layer.output_id)
                if not self._current_authority(assignment):
                    self._assignments.pop(key)
                    self._renderer_releases.add(key)
                elif binding != old_bindings.get(assignment.layer.output_id):
                    self._lose_preparation(assignment)
                    self._invalidated.add(key)
            for output_id, retained in list(self._retained.items()):
                binding = self._binding(output_id)
                if (not binding or _binding_identity(binding) != _binding_identity(retained.binding)
                        or binding.display_mm != retained.binding.display_mm):
                    del self._retained[output_id]
            for output_id, composition in list(self._current.items()):
                binding = self._binding(output_id)
                if not binding or _binding_identity(binding) != _binding_identity(composition.binding):
                    self._retired_outputs[output_id] = composition.binding
                    del self._current[output_id]
            self._capacity_ok = False
            return True

    def accept_plan(self, plan: Plan) -> bool:
        with self._lock:
            if (not self._config or plan.player_id != self.player_id
                    or plan.authority_epoch != self._last_epoch):
                raise AuthorityError("plan authority mismatch")
            # Re-delivery of the held plan is a no-op, past its validity too (decision 0032).
            if plan == self._plan:
                return False
            now = self._now()
            if plan.valid_until <= now:
                raise AuthorityError("expired plan")
            if self._last_plan_id is not None and plan.plan_id != self._last_plan_id:
                raise AuthorityError("plan identity must remain stable within its epoch")
            if plan.revision <= self._last_revision:
                raise AuthorityError("stale plan revision")
            if tuple(plan.bindings) != tuple(self._config.bindings):
                raise AuthorityError("plan configuration mismatch")
            for layer in plan.layers:
                old = self._assignments.get(layer.assignment_id) or self._inactive.get(layer.assignment_id)
                if old and (old.secured or old.acquiring) and (
                    _content_identity(layer) != _content_identity(old.as_offered)
                ):
                    raise AuthorityError("secured assignment content is immutable")
                if (old and _content_identity(layer) == _content_identity(old.as_offered)
                        and not cut_is_final(old.as_offered, layer)):
                    raise AuthorityError("cut_is_final")
            next_assignments: dict[str, _Assignment] = {}
            for layer in plan.layers:
                key = layer.assignment_id
                if key in self._cancelled:
                    continue
                old = self._assignments.get(key) or self._inactive.pop(key, None)
                if old and _content_identity(layer) == _content_identity(old.as_offered):
                    old.layer, old.offered = layer.drawn(), layer
                    # Decision 0036: a cut applies to what already runs, its grant included.
                    old.granted_layer = _cut(old.granted_layer, layer)
                    old.execution_layer = _cut(old.execution_layer, layer)
                    if old.execution_layer is not None:
                        old.execution_until = min(old.execution_until, old.execution_layer.end)
                    # Revision-specific authorization must be renewed by central. Until it
                    # is, the grant already accepted still starts this content at its time:
                    # the newer revision arrives before its own Commit can.
                    old.committed = False
                    old.granted_sequence = None
                    old.offered_until = max(old.offered_until, plan.valid_until)
                    next_assignments[key] = old
                else:
                    next_assignments[key] = _Assignment(
                        layer.drawn(), f"pwexec:{self.player_id}:{self._last_epoch}:{key}",
                        secured=layer.presentation == "black",
                        offered_until=plan.valid_until, offered=layer,
                    )
            drawn = {local.layer.assignment_id for composition in self._current.values()
                     if not composition.fallback for local in composition.layers}
            for key, assignment in self._assignments.items():
                if key not in next_assignments:
                    # A complete newer manifest supersedes execution, independently
                    # of the conservative lease protecting possibly secured bytes. One on screen
                    # is not invalidated: its composition keeps drawing until its Output draws a
                    # layer of the new plan (decision 0032, `_rejoin_hold`).
                    self._lose_preparation(assignment)
                    if key in drawn:
                        self._superseded.add(key)
                    else:
                        self._invalidated.add(key)
                    if assignment.source is None and (assignment.secured or assignment.acquiring):
                        self._inactive[key] = assignment
            self._renderer_releases.update(set(self._assignments) - set(next_assignments))
            self._assignments = next_assignments
            self._plan = plan
            self._revoked.clear()
            self._last_plan_id, self._last_revision = plan.plan_id, plan.revision
            self._last_revocation_sequence = 0
            self._capacity_ok = False
            self._reported.clear()
            return True

    def acquire(self, assignment_id: str, chunks: Iterable[bytes], *, sha256: str,
                size: int) -> bool:
        """Worker-only acquisition of the bytes Central served for the assignment's variant,
        `size` bytes whose SHA-256 is `sha256` (its `Content-Length` and `Repr-Digest`);
        streaming never owns the execution lock. `chunks` raising AcquisitionStopped is the
        Player's own stop: not secured, and no readiness failure recorded."""
        with self._cache_work:
            return self._acquire(assignment_id, chunks, sha256=sha256, size=size)

    def resolve_cached(self, assignment_id: str) -> bool:
        """Secure already-present exact bytes without starting a network request."""
        with self._cache_work:
            with self._lock:
                assignment = self._assignments.get(assignment_id)
                if not assignment or not self._current_authority(assignment):
                    return False
                variant = assignment.layer.variant
                if variant is None:
                    return True
                owner = assignment.owner
            try:
                path = self.cache.path_for(variant)
                if path is None:
                    return False
                self.cache.pin(variant, owner)
            except (CacheError, OSError):
                return False
            return self._accept_acquired_path(assignment_id, assignment, path)

    def _acquire(self, assignment_id: str, chunks: Iterable[bytes], *, sha256: str,
                 size: int) -> bool:
        with self._lock:
            assignment = self._assignments.get(assignment_id)
            if not assignment or not self._current_authority(assignment):
                raise AuthorityError("unknown or cancelled assignment")
            if assignment.acquiring:
                raise AuthorityError("assignment acquisition is already active")
            if assignment.layer.variant is None:
                return True
            assignment.acquiring = True
            self._pin_intents[assignment.owner] = assignment.layer.variant.key
        try:
            path = self.cache.secure(assignment.layer.variant, chunks, assignment.owner,
                                     sha256=sha256, size=size)
        except Exception as error:
            with self._lock:
                assignment.acquiring = False
                if self._assignments.get(assignment_id) is assignment:
                    assignment.secured = False
                    self._lose_preparation(assignment)
                    if not isinstance(error, AcquisitionStopped):
                        assignment.failure = ("capacity" if isinstance(error, CacheCapacityError)
                                              else "download")
            return False
        return self._accept_acquired_path(assignment_id, assignment, path)

    def _accept_acquired_path(
        self, assignment_id: str, assignment: _Assignment, path: Path
    ) -> bool:
        with self._lock:
            assignment.acquiring = False
            assignment.path = path
            assignment.secured = True
            if (
                self._assignments.get(assignment_id) is not assignment
                or not self._current_authority(assignment)
                or not self._plan
                or not may_still_play(self._plan, assignment.layer, self._now())
            ):
                return False
            assignment.failure = None
            self._pin_intents[assignment.owner] = assignment.layer.variant.key
            return True

    def verify_secured(self) -> None:
        """Worker-only exact-byte verification; revoke readiness on local damage."""
        with self._cache_work:
            self._verify_secured()

    def _verify_secured(self) -> None:
        with self._lock:
            items = tuple(self._assignments.items())
        for key, assignment in items:
            if not assignment.secured or assignment.layer.variant is None:
                continue
            try:
                path = self.cache.path_for(assignment.layer.variant)
            except (CacheError, OSError):
                path = None
            with self._lock:
                if self._assignments.get(key) is not assignment:
                    continue
                if path is None:
                    assignment.path = None
                    assignment.secured = False
                    self._lose_preparation(assignment)
                    assignment.failure = "integrity"
                    assignment.execution_layer = None
                    self._invalidated.add(key)
                    self._retained = {
                        output: retained for output, retained in self._retained.items()
                        if retained.local.layer.variant != assignment.layer.variant
                    }
                    self._capacity_ok = False
                else:
                    assignment.path = path
        with self._lock:
            retained_items = tuple(self._retained.items())
        for output_id, retained in retained_items:
            try:
                path = self.cache.path_for(retained.local.layer.variant)
            except (CacheError, OSError):
                path = None
            if path is None:
                with self._lock:
                    if self._retained.get(output_id) is retained:
                        del self._retained[output_id]
                    self._current.pop(output_id, None)
    def maintain_cache(self) -> None:
        """Worker: pin retained pictures before releasing superseded ownership.

        Snapshot/race checks make this safe beside control messages and rendering.
        A stale snapshot may keep an extra pin until the next maintenance pass; it
        can never release the original assignment before a retained pin exists.
        """
        with self._cache_work:
            self._maintain_cache()

    def _maintain_cache(self) -> None:
        with self._lock:
            if self._config is None:
                return
            retained = tuple(self._retained.values())
        for item in retained:
            assert item.local.layer.variant is not None
            try:
                self.cache.pin(item.local.layer.variant, item.owner)
                with self._lock:
                    self._established_retention.add(item.owner)
            except (CacheError, OSError):
                with self._lock:
                    if self._retained.get(item.binding.output_id) is item:
                        del self._retained[item.binding.output_id]
        with self._lock:
            releases = set(self._pin_intents) - self._desired_owners()
        for owner in releases:
            # No executor lock while the cache might wait behind a stream.
            self.cache.release(owner)
            with self._lock:
                self._established_retention.discard(owner)
                # Assignment owners are epoch-scoped and never revived after cancel.
                if owner not in self._desired_owners():
                    self._pin_intents.pop(owner, None)
    def _desired_owners(self) -> set[str]:
        now = self._now()
        desired = {a.owner for a in self._assignments.values() if a.acquiring or (
            a.secured and self._current_authority(a) and self._plan
            and (now < a.layer.end if a.source is not None
                 else may_still_play(self._plan, a.layer, now))
        )} | {item.owner for item in self._retained.values()}
        for key, assignment in tuple(self._inactive.items()):
            if assignment.acquiring or (assignment.secured and now < min(
                assignment.layer.end, assignment.offered_until
            )):
                desired.add(assignment.owner)
            else:
                del self._inactive[key]
        # A render tick can add retention after this worker's pin snapshot and
        # before a lease expires. Its original owner stays protected until a later
        # worker pass has actually established the dedicated retained-picture pin.
        for item in self._retained.values():
            if item.owner not in self._established_retention:
                desired.add(f"pwexec:{self.player_id}:{self._last_epoch}:"
                            f"{item.local.layer.assignment_id}")
        for composition in self._current.values():
            for local in composition.layers:
                if local.layer.variant and (local.layer.assignment_id in self._superseded
                                            or self._valid_current(local, composition, now)):
                    desired.add(f"pwexec:{self.player_id}:{self._last_epoch}:"
                                f"{local.layer.assignment_id}")
        return desired

    def _local(self, assignment: _Assignment, now: float) -> LocalLayer:
        return LocalLayer(assignment.layer, assignment.path, assignment.layer.position(now),
                          assignment.layer.alpha(now))

    def _apply_after_end(self, binding: OutputBinding, local: LocalLayer, now: float) -> None:
        """A drawn layer, covered or not, sets what its Frame keeps for when nothing plays
        (`Layer.after_end`, planned by Central): nothing, from its first draw; or this photo,
        from its first draw at or after `kept_from` (contracts/on_screen.py), drawn as the seam
        rule looks it (`_fallback`)."""
        layer = local.layer
        if layer.after_end == "keep_nothing":
            self._retained.pop(binding.output_id, None)
            return
        if layer.after_end == "leave_as_is" or now < kept_from(layer):
            return
        identity = (f"{binding.output_id}:{binding.frame_id}:"
                    f"{binding.generation}:{layer.assignment_id}")
        owner = (f"pwretain:{self.player_id}:{self._last_epoch}:"
                 f"{hashlib.sha256(identity.encode()).hexdigest()}")
        self._pin_intents[owner] = layer.variant.key
        self._retained[binding.output_id] = _Retained(local, binding, owner)

    def _compositions(self, items: Iterable[_Assignment], now: float,
                      timebase: float) -> tuple[OutputComposition, ...]:
        return self._group((self._local(assignment, now) for assignment in items), timebase, now)

    def _group(self, pictures: Iterable[LocalLayer], timebase: float | None = None,
               now: float | None = None) -> tuple[OutputComposition, ...]:
        """`pictures` as one composition per bound Output, bottom to top. Drawn over time from
        `now` on (with `timebase`), each carries what its Frame shows (`_shown`) and draws next
        (`_following`), and its layers' alphas are its look at `now`
        (player/look.py `look_at`, the seam rule): what the executor reads as seen is what the
        renderer draws."""
        grouped: dict[str, list[LocalLayer]] = {}
        for local in pictures:
            grouped.setdefault(local.layer.output_id, []).append(local)
        result = []
        for output_id, layers in grouped.items():
            binding = self._binding(output_id)
            if binding:
                # Bottom to top by the one stacking order: a later photo of a Run is drawn over
                # the one it dissolves in over.
                layers.sort(key=lambda local: stack_order(local.layer))
                composition = OutputComposition(binding, tuple(layers), timebase=timebase)
                if now is not None and timebase is not None:
                    composition = look_at(replace(
                        composition, following=self._following(output_id, layers, now),
                        shown=self._shown(binding)), now)
                result.append(composition)
        return tuple(result)

    def _following(self, output_id: str, drawn: Iterable[LocalLayer], now: float) -> tuple[Layer, ...]:
        """The layers `output_id`'s Frame holds that are not `drawn`, have not ended at `now` and
        can be drawn: of the current plan (none failed or superseded; a plan's layers lie within
        its validity, contracts/models.py `Plan`), secured, and granted when they await a grant
        (decision 0032). What the Frame draws next, for player/look.py's seam rule; a layer that
        may never draw is not one, so the picture before it fades out as planned."""
        drawing = {local.layer.assignment_id for local in drawn}
        return tuple(assignment.granted_layer or assignment.layer for key, assignment in self._assignments.items()
                     if assignment.layer.output_id == output_id and key not in drawing
                     and key not in self._superseded and assignment.failure is None
                     and self._current_authority(assignment) and now < assignment.layer.end
                     and assignment.secured
                     and (not assignment.layer.awaits_grant or assignment.granted_layer is not None))

    def _shown(self, binding: OutputBinding) -> Hashable | None:
        """The picture `binding`'s Frame shows alone at full: player/look.py `alone` of what its
        Output last acknowledged drawing for this binding (`_current`); None before one."""
        current = self._current.get(binding.output_id)
        if current is None or _binding_identity(current.binding) != _binding_identity(binding):
            return None
        return alone(current.layers)

    def _window(self, assignment: _Assignment) -> tuple[float, float]:
        """When `assignment` may be drawn: a plan layer within the plan's validity; a
        continuation slot by its own start and end, never cut by the plan's validity."""
        layer = assignment.layer
        if assignment.source is not None or self._plan is None:
            return layer.start, layer.end
        return max(layer.start, self._plan.valid_from), min(layer.end, self._plan.valid_until)

    def _ready_beneath(self, cut: _Assignment, now: float) -> bool:
        """Whether what `cut`'s Frame shows beneath it at `now` can be drawn: the topmost layer
        below it there in force now (a continuation slot included) is secured, prepared (or ready
        for its reveal, `deferred`), not failed, and may start or runs. True with nothing beneath:
        the Frame then takes its after-state, as at any end."""
        order = stack_order(self._drawn_layer(cut))
        beneath = [a for key, a in self._assignments.items()
                   if a is not cut and a.layer.output_id == cut.layer.output_id
                   and key not in self._superseded and key not in self._invalidated
                   and self._current_authority(a) and self._assignment_in_force(a, now)
                   and a.layer.start <= now < a.layer.end and stack_order(self._drawn_layer(a)) < order]
        if not beneath:
            return True
        top = max(beneath, key=lambda a: stack_order(self._drawn_layer(a)))
        return (top.secured and (top.prepared or top.deferred) and top.failure is None
                and (self._may_start(top, now) or self._executing(top, now)))

    def _wait_at_cuts(self, now: float) -> None:
        """Decision 0036 (S3), the Player's one wait: a layer on screen that Central cut (Back to
        normal, a takeover's end, Carry on) is drawn on past its cut while what shows beneath it
        is not ready (never downloaded, or its place given up), and then fades, so a Frame never
        goes black at a cut. Each pass moves its end to `CUT_WAIT_STEP` past `now` and its
        fade-out, never past the layer's own end; once the layer beneath is ready its end stays,
        and it fades from there. Central's cut, as offered, is unchanged (`cut_is_final`)."""
        for assignment in self._assignments.values():
            offered = assignment.offered
            if (offered is None or offered.cut_at is None or offered.cut_at >= offered.end
                    or not assignment.started or now >= offered.end):
                continue
            drawn_end = assignment.layer.end
            if now < drawn_end - offered.fade_out - CUT_WAIT_STEP / 2 or self._ready_beneath(assignment, now):
                continue
            cut = now + offered.fade_out + CUT_WAIT_STEP
            cut = None if cut >= offered.end else cut
            assignment.layer = _waited(assignment.layer, offered, cut)
            assignment.granted_layer = _waited(assignment.granted_layer, offered, cut)
            assignment.execution_layer = _waited(assignment.execution_layer, offered, cut)
            if assignment.execution_layer is not None:
                assignment.execution_until = assignment.execution_layer.end

    def _imminent(self, now: float) -> list[_Assignment]:
        if not self._plan:
            return []
        return [a for a in self._assignments.values() if self._current_authority(a)
                and now < (window := self._window(a))[1]
                and window[0] <= now + self.prepare_lead]

    def _continue(self, now: float) -> None:
        """Decision 0032: the held plan's continuation slots over [now, now + prepare lead), each
        drawing its source's secured bytes (never downloaded on its own); slots that ended and
        are not drawn are dropped. contracts/continuation.py is the one generator."""
        plan = self._plan
        drawn = {local.layer.assignment_id for composition in self._current.values()
                 for local in composition.layers}
        for key, assignment in tuple(self._assignments.items()):
            if assignment.source is not None and assignment.layer.end <= now and key not in drawn:
                del self._assignments[key]
                self._renderer_releases.add(key)
        if plan is None:
            return
        # A slot, once made, keeps the photo it took: the held set grows as downloads land (and
        # shrinks as one fails), which moves the k mod n mapping, so a slot already made is never
        # made again with another photo. Within one plan, slots on an Output never share a start.
        made = {(a.layer.output_id, a.layer.start) for a in self._assignments.values()
                if a.source is not None}
        for continuation in plan.continuations:
            if continuation.until is not None and continuation.until <= now:
                continue
            held = {photo for photo in continuation.photos
                    if (source := self._assignments.get(photo)) is not None and source.secured
                    and source.path is not None and source.source is None
                    and photo not in self._revoked and self._current_authority(source)}
            for layer in continuation_layers(plan, continuation, held=held, start=now,
                                             end=self._continued_until(continuation.output_id, now)):
                key = layer.assignment_id
                if (key in self._assignments or key in self._cancelled
                        or (layer.output_id, layer.start) in made):
                    continue
                found = continuation_source(plan, key, at=layer.start)
                if found is None:
                    continue
                source = self._assignments[found[2].assignment_id]
                self._assignments[key] = _Assignment(
                    layer, f"pwexec:{self.player_id}:{self._last_epoch}:{key}", secured=True,
                    path=source.path, source=source.layer.assignment_id)

    def _continued_until(self, output_id: str, now: float) -> float:
        """How far ahead slots are made on `output_id`: the prepare lead, and past the end of every
        layer playing there whose fade-out starts within it, so what its Frame draws next is held
        before that fade-out starts (player/look.py's seam rule reads it); a slot made early keeps
        its photo and is prepared only within the prepare lead (`_imminent`)."""
        playing = [assignment.layer.end for assignment in self._assignments.values()
                   if assignment.layer.output_id == output_id and assignment.layer.start <= now
                   and assignment.layer.end - assignment.layer.fade_out <= now + self.prepare_lead
                   and self._current_authority(assignment)]
        return max([now + self.prepare_lead, *(math.nextafter(end, math.inf) for end in playing)])

    def _may_start(self, assignment: _Assignment, now: float) -> bool:
        """Decision 0032, a grant only for a change: a layer that awaits a grant starts while its
        grant covers `now`; any other (a continuation slot included) at its own instant."""
        if assignment.layer.awaits_grant:
            return self._grant_covers(assignment, now)
        return assignment.layer.start <= now < assignment.layer.end

    @staticmethod
    def _end_grant(assignment: _Assignment) -> None:
        """The Commit is lost: neither this revision's nor a carried one starts the content."""
        assignment.committed = False
        assignment.granted_layer = None

    @classmethod
    def _lose_preparation(cls, assignment: _Assignment) -> None:
        assignment.prepared = False
        assignment.deferred = False
        cls._end_grant(assignment)
        assignment.execution_layer = None
        assignment.readiness_generation += 1

    @staticmethod
    def _frames(items: Iterable[_Assignment]) -> dict[str, list[_Assignment]]:
        frames: dict[str, list[_Assignment]] = {}
        for assignment in items:
            frames.setdefault(assignment.layer.output_id, []).append(assignment)
        return frames

    @staticmethod
    def _drawn_layer(assignment: _Assignment) -> Layer:
        """The layer as tick draws it: as its grant authorized (a revision awaiting its grant keeps
        the granted order and fades), else as offered."""
        return assignment.granted_layer or assignment.execution_layer or assignment.layer

    @classmethod
    def _covering(cls, items: Iterable[_Assignment]) -> list[Layer]:
        """The layers that will be drawn and so may cover others: secured, and not failed (one
        given up for slots still stands above what it covered, itself not drawn)."""
        return [cls._drawn_layer(a) for a in items
                if a.secured and a.failure in (None, "capacity")]

    def _horizon(self, now: float) -> float:
        if not self._plan:
            return now
        return max([self._plan.valid_until, *(a.layer.end for a in self._assignments.values()
                                              if a.source is not None)])

    def prepare_imminent(self) -> None:
        """Renderer-thread preroll. No filesystem or network access occurs here.

        Every imminent layer keeps its decoder while slots are free. When they run short, covered
        pictures give theirs up first (player/visibility.py: covered from now through the prepare
        lead): a Frame's kept photo, then the bottom-most covered layer, which stays ready for its
        reveal and is prepared again within the lead before it shows. Only then does a Frame give
        up its bottom-most seen layer (failure `capacity`), never the whole screen."""
        with self._lock:
            now = self._now()
            self._continue(now)
            self._wait_at_cuts(now)
            self._drain_renderer_releases()
            imminent = self._imminent(now)
            until, horizon = now + self.prepare_lead, self._horizon(now)
            frames = self._frames(imminent)
            seen: dict[str, list[_Assignment]] = {}
            covered: list[_Assignment] = []
            for output_id, items in frames.items():
                shown = shown_within((self._drawn_layer(a) for a in items), self._covering(items),
                                     now, until, horizon=horizon)
                seen[output_id] = [a for a in items if a.layer.assignment_id in shown]
                covered += [a for a in items if a.layer.assignment_id not in shown]
            kept_seen = {output_id for output_id in self._retained if beneath_shown(
                self._covering(frames.get(output_id, ())), now, until, horizon=horizon)}
            pictures = {a.layer.assignment_id: self._local(a, max(now, a.layer.start))
                        for a in imminent}
            for output_id, retained in self._retained.items():
                pictures.setdefault(retained.local.layer.assignment_id, retained.local)
            # What a superseded composition still draws stays resident (`_rejoin_hold`).
            holding = {local.layer.assignment_id: (local, composition.binding)
                       for composition in self._current.values() for local in composition.layers
                       if local.layer.assignment_id in self._superseded}
            for key, (local, _) in holding.items():
                pictures.setdefault(key, local)
            needed = {a.layer.assignment_id for items in seen.values() for a in items}
            needed.update(holding)
            needed.update(self._retained[o].local.layer.assignment_id for o in kept_seen)
            spare = [retained.local.layer.assignment_id for output_id, retained
                     in self._retained.items() if output_id not in kept_seen]
            spare += [a.layer.assignment_id for a in
                      sorted(covered, key=lambda a: stack_order(self._drawn_layer(a)))]
            spare = [key for key in dict.fromkeys(spare) if key not in needed]
            released: list[str] = []
            given_up: list[_Assignment] = []
            while True:
                unwanted = {*released, *(a.layer.assignment_id for a in given_up)}
                self._wanted, self._unwanted = set(pictures) - unwanted, unwanted
                self._release_unused_renderer(now, budgeting=True)
                self._capacity_ok = self.renderer.capacity(
                    self._group(pictures[key] for key in self._wanted)).available
                if self._capacity_ok:
                    break
                if len(released) < len(spare):
                    released.append(spare[len(released)])
                    continue
                victim = shed({output_id: [self._drawn_layer(a) for a in items]
                               for output_id, items in seen.items()})
                if victim is None:
                    break
                given = next(a for a in seen[victim.output_id]
                             if a.layer.assignment_id == victim.assignment_id)
                seen[victim.output_id].remove(given)
                given_up.append(given)
            for assignment in given_up:
                self._lose_preparation(assignment)
                assignment.failure = "capacity"
            for assignment in covered:
                if assignment.secured and assignment.layer.assignment_id in released:
                    assignment.prepared, assignment.deferred = False, True
                    if assignment.failure == "capacity":
                        assignment.failure = None
            for output_id, retained in self._retained.items():
                if retained.local.layer.assignment_id not in released:
                    self.renderer.prepare(retained.local, self._binding(output_id) or retained.binding)
                    self._renderer_residents.add(retained.local.layer.assignment_id)
            for key, (local, binding) in holding.items():
                self.renderer.prepare(local, binding)
                self._renderer_residents.add(key)
            for assignment in (*(a for items in seen.values() for a in items),
                               *(a for a in covered if a.layer.assignment_id not in released)):
                if not assignment.secured:
                    continue
                result = self.renderer.prepare(pictures[assignment.layer.assignment_id],
                                               self._binding(assignment.layer.output_id))
                self._renderer_residents.add(assignment.layer.assignment_id)
                if result.status == "pending" and assignment.deferred:
                    # Its decode for the reveal is in flight: still ready, its grant kept.
                    assignment.failure = None
                    continue
                assignment.deferred = False
                if result.status != "prepared" and (assignment.prepared or assignment.committed):
                    self._lose_preparation(assignment)
                assignment.prepared = result.status == "prepared"
                if result.status == "failed":
                    assignment.failure = result.code if result.code != "none" else "decode"
                    if not assignment.started:
                        self._end_grant(assignment)
                elif result.status == "pending":
                    assignment.failure = None
                    if not assignment.started:
                        self._end_grant(assignment)
                else:
                    assignment.failure = None
            if not self._capacity_ok:
                for assignment in imminent:
                    if not assignment.started:
                        self._lose_preparation(assignment)

    def showing(self, binding: OutputBinding) -> bool:
        """Whether `binding`'s Frame has a picture on screen: the composition its Output last
        acknowledged drawing is this binding's (Frame and generation) and some layer of it can be
        seen (a retained picture included); black, nothing drawn yet or another binding's is not."""
        with self._lock:
            current = self._current.get(binding.output_id)
            return (current is not None
                    and (current.binding.frame_id, current.binding.generation) == (binding.frame_id, binding.generation)
                    and bool(visible_now(current.layers)))

    def continuing_since(self, binding: OutputBinding) -> float | None:
        """Show time at which this Output began drawing continuation slots of the held plan without
        a planned layer between; None while it draws a planned layer, a superseded one, or nothing."""
        with self._lock:
            current = self._current.get(binding.output_id)
            if current is None or _binding_identity(current.binding) != _binding_identity(binding):
                return None
            return self._continuing.get(binding.output_id)

    def _note_continuing(self, now: float) -> None:
        """Tick's one writer of `_continuing`: an Output whose drawn composition shows only
        continuation slots of the held plan keeps the instant it first did (`now` then); any other
        visible layer (a planned one, a superseded one, a kept photo) or nothing visible clears it."""
        def slot(assignment_id: str) -> bool:
            assignment = self._assignments.get(assignment_id)
            return (assignment is not None and assignment.source is not None
                    and assignment_id not in self._superseded)

        for output_id in tuple(self._continuing):
            if output_id not in self._current:
                del self._continuing[output_id]
        for output_id, composition in self._current.items():
            visible = () if composition.fallback else visible_now(composition.layers)
            if visible and all(slot(local.layer.assignment_id) for local in visible):
                self._continuing.setdefault(output_id, now)
            else:
                self._continuing.pop(output_id, None)

    def frames_black(self) -> tuple[FrameBlack, ...]:
        """Renderer thread: each enabled Output that is black while content is wanted, and why
        (player/frame_black.py), sorted by Output. Nothing here draws or decides what is drawn."""
        with self._lock:
            if not self._config:
                return ()
            now, plan = self._now(), self._plan
            # Decision 0032: a continuation that covers now is plan too.
            has_plan = plan is not None and (plan.valid_until > now or any(
                c.start - c.lead <= now and (c.until is None or now < c.until)
                for c in plan.continuations))
            clock_ok = self.show_clock.anchored
            found = []
            for binding in self._config.bindings:
                if binding.output_id not in self._config.enabled_outputs:
                    continue
                due = [self._due_layer(a, now) for a in self._assignments.values()
                       if a.layer.output_id == binding.output_id
                       and self._assignment_in_force(a, now)]
                if not due and plan is not None:
                    due = self._continuation_unheld(plan, binding.output_id, now)
                cause = frame_black(
                    showing=self.showing(binding),
                    own_drawing=self.renderer.patterned(binding.output_id),
                    renderer_ok=self.renderer.can_draw,
                    has_plan=has_plan, clock_ok=clock_ok, due=due,
                    unfilled=plan is not None and any(
                        span.output_id == binding.output_id and self._in_force(span.start, span.end, now)
                        for span in plan.unfilled))
                if cause is not None:
                    found.append(FrameBlack(binding.output_id, cause))
            return tuple(sorted(found, key=lambda black: black.output_id))

    @staticmethod
    def _continuation_unheld(plan: Plan, output_id: str, now: float) -> list[DueLayer]:
        """Decision 0032: a continuation covering `now` on `output_id` with no slot in force wants a
        photo it does not hold (none of its listed photos is secured: a wiped cache, downloads not
        done), stated as one unsecured media layer due (`NO_PHOTO`), never a silent black."""
        layers = {layer.assignment_id: layer for layer in plan.layers}
        return [DueLayer("media", stack_order(layers[c.photos[0]]), secured=False, prepared=False,
                         granted=False, failure=None)
                for c in plan.continuations if c.output_id == output_id
                and c.start - c.lead <= now and (c.until is None or now < c.until)]

    def _in_force(self, start: float, end: float, now: float) -> bool:
        """Whether something planned over [start, end) is in force at `now`, within the plan. Until
        the ShowClock is anchored there is no `now` to tell by, so all of it counts."""
        if not self.show_clock.anchored:
            return True
        return (self._plan is not None
                and max(start, self._plan.valid_from) <= now < min(end, self._plan.valid_until))

    def _assignment_in_force(self, assignment: _Assignment, now: float) -> bool:
        """`_in_force` for an assignment: a continuation slot by its own span alone."""
        if not self.show_clock.anchored:
            return True
        start, end = self._window(assignment)
        return self._plan is not None and start <= now < end

    def _due_layer(self, assignment: _Assignment, now: float) -> DueLayer:
        return DueLayer(assignment.layer.presentation, stack_order(assignment.layer),
                        secured=assignment.secured,
                        prepared=assignment.prepared or assignment.deferred,
                        granted=self._may_start(assignment, now) or self._executing(assignment, now),
                        failure=assignment.failure)

    @staticmethod
    def _grant_covers(assignment: _Assignment, now: float) -> bool:
        """Whether the Commit accepted for `assignment` authorizes it at `now`."""
        granted = assignment.granted_layer
        return (granted is not None and max(granted.start, assignment.granted_from) <= now
                < min(granted.end, assignment.granted_until))

    @staticmethod
    def _executing(assignment: _Assignment, now: float) -> bool:
        """Whether `assignment`'s execution lease, the grant it started under, runs at `now`."""
        executing = assignment.execution_layer
        return (executing is not None
                and executing.start <= now < min(executing.end, assignment.execution_until))

    @property
    def failures(self) -> tuple[Failure, ...]:
        with self._lock:
            return tuple(Failure(assignment_id=key, code=assignment.failure)
                         for key, assignment in sorted(self._assignments.items())
                         if assignment.failure is not None and assignment.source is None)

    def secured_ids(self) -> frozenset[str]:
        """The current-authority assignments whose bytes are secured, regardless of the clock:
        what downloads and Readiness both read, so downloads never wait for Central's time."""
        with self._lock:
            return frozenset(key for key, assignment in self._assignments.items()
                             if assignment.secured and assignment.source is None
                             and self._current_authority(assignment))

    def readiness(self) -> Readiness | None:
        """Readiness for the current plan; None while the ShowClock is not anchored, the one
        decider of "no readiness yet" (a Player without Central's time claims nothing).

        Level state, numbered once per change (2026-11-16: numbered on every ask, it was posted
        and written every half second): asked again while what it says is unchanged it is the
        same report, its number and time kept, so the Player sends it once and Central writes it
        once. A change is any field but its number, its time and its clock bound, which grows
        with time; the bound counts as changed when the one Central holds (this report's, grown
        by DRIFT_RATE) parts from the Player's own by more than CLOCK_BOUND_SLACK."""
        with self._lock:
            if not self._plan:
                raise AuthorityError("no current plan")
            if not self.show_clock.anchored:
                return None
            now = self._now()
            # A continuation slot is the Player's own: never reported (Central knows its source).
            imminent = [a for a in self._imminent(now) if a.source is None]
            failures = {f.assignment_id: f for f in self.failures}
            if not self._capacity_ok:
                for assignment in imminent:
                    key = assignment.layer.assignment_id
                    failures[key] = Failure(assignment_id=key, code="capacity")
            # Wire values remain finite. The bound is a quality Central reads, never a failure.
            uncertainty = self.show_clock.uncertainty
            if not math.isfinite(uncertainty):
                uncertainty = 86400.0
            prepared = tuple(sorted(a.layer.assignment_id for a in imminent
                                    if (a.prepared or a.deferred) and a.secured
                                    and a.layer.assignment_id not in failures))
            # Level state: every report says which grants this Player saw and no longer holds.
            lost = tuple(LostGrant(assignment_id=key,
                                   readiness_sequence=self._assignments[key].granted_sequence)
                         for key in prepared if not self._assignments[key].committed
                         and self._assignments[key].granted_sequence is not None)
            generations = {key: self._assignments[key].readiness_generation for key in prepared}
            state = (self._plan.plan_id, self._plan.revision, self._last_epoch,
                     tuple(sorted(self.secured_ids())), prepared, self._capacity_ok,
                     tuple(failures[key] for key in sorted(failures)), lost, self._carried(),
                     tuple(sorted(generations.items())))
            monotonic = self.clock.monotonic()
            previous = self._readiness
            if (previous is not None and state == self._readiness_state
                    and abs(previous.clock_uncertainty
                            + DRIFT_RATE * (monotonic - self._readiness_at)
                            - uncertainty) <= CLOCK_BOUND_SLACK):
                return previous
            self._sequence += 1
            readiness = Readiness(
                plan_id=self._plan.plan_id, revision=self._plan.revision,
                authority_epoch=self._last_epoch, sequence=self._sequence,
                secured=state[3], prepared=prepared, capacity_ok=self._capacity_ok,
                clock_uncertainty=uncertainty, observed_at=now, failures=state[6],
                lost_grants=lost, carried=state[8],
            )
            self._readiness, self._readiness_state, self._readiness_at = (
                readiness, state, monotonic)
            self._reported[self._sequence] = generations
            for sequence in sorted(self._reported)[:-128]:
                del self._reported[sequence]
            return readiness

    def _carried(self) -> tuple[CarriedPhoto, ...]:
        """Decision 0032: each photo drawn (visible in what an Output last acknowledged) or kept
        whose assignment is neither a layer nor a continuation slot of the held plan."""
        def placed(assignment_id: str) -> bool:
            return assignment_id in self._assignments
        found: dict[tuple[str, str], None] = {}
        for output_id, composition in self._current.items():
            for local in visible_now(composition.layers):
                if local.layer.variant and not placed(local.layer.assignment_id):
                    found[(output_id, local.layer.variant.asset_id)] = None
        for output_id, retained in self._retained.items():
            if retained.local.layer.variant and not placed(retained.local.layer.assignment_id):
                found[(output_id, retained.local.layer.variant.asset_id)] = None
        return tuple(CarriedPhoto(output_id=output_id, asset_id=asset_id)
                     for output_id, asset_id in sorted(found))

    def accept_commit(self, commit: Commit) -> bool:
        with self._lock:
            if (not self._plan or commit.authority_epoch != self._last_epoch
                    or commit.plan_id != self._plan.plan_id or commit.revision != self._plan.revision):
                raise AuthorityError("commit authority mismatch")
            # Seen, whether accepted or refused below: a grant not held afterwards is lost.
            for key in commit.assignment_ids:
                if key in self._assignments:
                    granted = self._assignments[key].granted_sequence
                    self._assignments[key].granted_sequence = max(
                        commit.readiness_sequence, granted or 0)
            # A grant already held adds nothing: a Commit re-projected without a re-granted
            # member names only assignments this Player already holds.
            if commit.assignment_ids and all(
                    key in self._assignments and self._assignments[key].committed
                    for key in commit.assignment_ids):
                return False
            now = self._now()
            if not self._capacity_ok:
                raise AuthorityError("commit capacity gate failed")
            assignments = []
            reported = self._reported.get(commit.readiness_sequence, {})
            for key in commit.assignment_ids:
                assignment = self._assignments.get(key)
                if (not assignment or not self._current_authority(assignment)
                        or not assignment.secured or not (assignment.prepared or assignment.deferred)
                        or assignment.failure
                        or now >= min(assignment.layer.end, self._plan.valid_until)
                        or max(assignment.layer.start, self._plan.valid_from) > now + self.prepare_lead):
                    raise AuthorityError("commit assignment is not ready")
                if reported.get(key) != assignment.readiness_generation:
                    raise AuthorityError("commit references revoked or unknown readiness")
                intended_start = max(assignment.layer.start, self._plan.valid_from)
                if (assignment.execution_layer is None
                        and now > intended_start + self.show_clock.uncertainty
                        and commit.committed_at < intended_start):
                    raise AuthorityError("missed start requires fresh late-join authorization")
                assignments.append(assignment)
            changed = any(not a.committed for a in assignments)
            for assignment in assignments:
                assignment.committed = True
                assignment.granted_layer = assignment.layer
                assignment.granted_from = self._plan.valid_from
                assignment.granted_until = self._plan.valid_until
                self._invalidated.discard(assignment.layer.assignment_id)
                self._revoked.discard(assignment.layer.assignment_id)
            return changed

    def cancel(self, assignment_ids: Iterable[str]) -> None:
        with self._lock:
            keys = set(assignment_ids)
            for key in keys:
                self._cancelled.add(key)
                self._assignments.pop(key, None)
                self._inactive.pop(key, None)
                self._renderer_releases.add(key)
            self._drop_slots_of(keys)

    def _drop_slots_of(self, sources: set[str]) -> None:
        """Decision 0032: the continuation slots made from `sources` go with them (a cancelled or
        invalidated photo is no longer held, so `_continue` makes no new one)."""
        for key, assignment in tuple(self._assignments.items()):
            if assignment.source in sources:
                del self._assignments[key]
                self._renderer_releases.add(key)

    def release(self, assignment_ids: Iterable[str]) -> None:
        self.cancel(assignment_ids)

    def invalidate(self, assignment_ids: Iterable[str]) -> None:
        """Revoke readiness/authorization without cancelling the secured identity."""
        with self._lock:
            for key in assignment_ids:
                assignment = self._assignments.get(key)
                if assignment:
                    self._lose_preparation(assignment)
                    assignment.execution_layer = None
                    # Revoked by Central, not lost: nothing to re-grant.
                    assignment.granted_sequence = None
                    self._invalidated.add(key)
                    self._revoked.add(key)
                    self._drop_slots_of({key})

    def accept_revocation(self, revocation: Revocation) -> bool:
        """Authenticated, revision-bound and idempotent external invalidation."""
        with self._lock:
            if (not self._plan or revocation.authority_epoch != self._last_epoch
                or revocation.plan_id != self._plan.plan_id or revocation.revision != self._plan.revision):
                raise AuthorityError("revocation authority mismatch")
            if revocation.sequence <= self._last_revocation_sequence:
                return False
            if not set(revocation.assignment_ids) <= {a.assignment_id for a in self._plan.layers}:
                raise AuthorityError("unknown revoked assignment")
            if revocation.mode == "cancel":
                self.cancel(revocation.assignment_ids)
            else:
                self.invalidate(revocation.assignment_ids)
            self._last_revocation_sequence = revocation.sequence
            return True

    def _drain_renderer_releases(self) -> None:
        protected = {local.layer.assignment_id for composition in self._current.values()
                     for local in composition.layers}
        protected.update(item.local.layer.assignment_id for item in self._retained.values())
        for key in tuple(self._renderer_releases - protected):
            self.renderer.release(key)
            self._renderer_residents.discard(key)
            self._renderer_releases.discard(key)

    def _release_unused_renderer(self, now: float, *, budgeting: bool = False) -> None:
        """Budgeting (a prepare pass): keep exactly what the pass wants. Between passes keep what
        it kept, every kept photo and what is drawn (a reveal prepared it), less what it gave up."""
        drawn = {local.layer.assignment_id for composition in self._current.values()
                 for local in composition.layers}
        kept = {item.local.layer.assignment_id for item in self._retained.values()}
        if budgeting:
            needed = set(self._wanted)
        else:
            needed = self._wanted & ({a.layer.assignment_id for a in self._imminent(now)} | kept)
            needed |= kept | drawn
            needed -= self._unwanted - drawn
        for key in self._renderer_residents - needed:
            self.renderer.release(key)
        self._renderer_residents.intersection_update(needed)

    def _drawable(self, composition: OutputComposition) -> OutputComposition:
        """`composition` less its deferred layers that are covered now."""
        visible = {local.layer.assignment_id for local in visible_now(composition.layers)}
        return replace(composition, layers=tuple(
            local for local in composition.layers
            if local.layer.assignment_id in visible
            or not self._assignments[local.layer.assignment_id].deferred))

    def _valid_current(self, local: LocalLayer, composition: OutputComposition, now: float) -> bool:
        binding = self._binding(composition.binding.output_id)
        return bool(
            self._plan and binding
            and self._config and binding.output_id in self._config.enabled_outputs
            and local.layer.assignment_id not in self._cancelled
            and local.layer.assignment_id not in self._invalidated
            and _binding_identity(binding) == _binding_identity(composition.binding)
            and local.layer.start <= now < min(
                local.layer.end, self._current_leases.get(binding.output_id, -math.inf)
            )
        )

    def _fallback(self, binding: OutputBinding, now: float) -> OutputComposition:
        """What `binding`'s Frame shows when nothing plays: the photo it keeps, looked at `now` by
        the seam rule (player/look.py `look_at`: a kept photo with nothing after it draws itself
        next, so from `kept_from` on it is at its full strength), or nothing."""
        retained = self._retained.get(binding.output_id)
        kept = OutputComposition(binding, (retained.local,) if retained else (), fallback=True)
        return look_at(kept, now) if retained else kept

    def _show_fallback(self, composition: OutputComposition) -> bool:
        """Only observed fallback replaces state; unavailable retained media yields black."""
        if composition.layers and any(
            self.renderer.prepare(local, composition.binding).status != "prepared"
            for local in composition.layers
        ):
            composition = replace(composition, layers=())
        self._renderer_residents.update(local.layer.assignment_id for local in composition.layers)
        result = self.renderer.present(composition)
        if result.status == "pending" and self._awaiting_draw(composition):
            return False
        if result.status != "presented" and composition.layers:
            composition = replace(composition, layers=())
            result = self.renderer.present(composition)
        acknowledged = self._acknowledgment(composition, result)
        if acknowledged:
            self._pending_presentations.pop(composition.binding.output_id, None)
            self._current[composition.binding.output_id] = acknowledged[0]
            return True
        return False

    def _awaiting_draw(self, composition: OutputComposition) -> bool:
        key = composition.request
        output_id = composition.binding.output_id
        pending = self._pending_presentations.get(output_id)
        if pending is None or pending[0] != key:
            pending = (key, self.clock.monotonic())
            self._pending_presentations[output_id] = pending
        return self.clock.monotonic() - pending[1] < DRAW_GRACE

    def _acknowledgment(self, candidate: OutputComposition,
                        result: PresentationResult) -> tuple[OutputComposition, float] | None:
        """Validate draw authority and report the actual drawn snapshot and time."""
        if result.status != "presented":
            return None
        if result.composition is None and result.presented_at is None:
            return candidate, self._now()  # Immediate recording implementation.
        drawn, completed = result.composition, result.presented_at
        if drawn is None or completed is None or not math.isfinite(completed):
            return None
        # A renderer's "presented" is a state (the draw still stands, however long ago it was
        # drawn: a still photo is never redrawn); only a draw time in the future is refused.
        age = self.clock.monotonic() - completed
        if not 0 <= age or drawn.request != candidate.request:
            return None
        for actual in drawn.layers:
            if (not math.isfinite(actual.position) or actual.position < 0
                    or not math.isfinite(actual.alpha) or not 0 <= actual.alpha <= 1):
                return None
        if (result.applied_calibration is not None and result.applied_calibration.trial_id is None
                and result.applied_calibration.calibration != candidate.calibration):
            return None
        if not hasattr(self, "_applied_output_calibration"):
            self._applied_output_calibration = {}
        self._applied_output_calibration[drawn.binding.output_id] = result.applied_calibration
        return drawn, self._now() - age

    def _report(self, reports: list[Observation], local: LocalLayer, now: float, status: str,
                detail: str = "none") -> None:
        """The one home of what the Player tells Central about an assignment: an observation is a
        change of its state (status, detail, the calibration drawn), appended to `reports` once and
        numbered once, so a still photo drawn on every tick is reported presented when it starts,
        never again, and a report sent again after a slow or failed answer keeps its number
        (Central writes each (epoch, number) once). Position is no part of the state: it moves on
        every tick of a video."""
        assert self._plan is not None
        assignment = self._assignments[local.layer.assignment_id]
        applied = (getattr(self, "_applied_output_calibration", {}).get(local.layer.output_id)
                   if status in ("presented", "fallback") else None)
        state = (self._last_epoch, self._plan.plan_id, status, detail, applied)
        if assignment.reported == state:
            return
        assignment.reported = state
        self._report_sequence += 1
        reports.append(Observation(
            plan_id=self._plan.plan_id, revision=self._plan.revision,
            authority_epoch=self._last_epoch, assignment_id=local.layer.assignment_id,
            sequence=self._report_sequence, observed_at=now, status=status,
            position=local.position, detail=detail, applied_calibration=applied,
        ))

    def tick(self) -> tuple[Observation, ...]:
        """Renderer thread: current-state execution and calibration/fallback updates."""
        with self._lock:
            self._drain_renderer_releases()
            # What each Output shows is decided at `now`; how it looks, at each frame the
            # renderer draws, through `timebase` (player/look.py).
            timebase = self._timebase()
            now = timebase + self.clock.monotonic()
            if not self._config:
                return ()
            self._wait_at_cuts(now)
            for output_id, binding in tuple(self._retired_outputs.items()):
                if self._show_fallback(OutputComposition(binding, fallback=True)):
                    del self._retired_outputs[output_id]
            active = []
            for assignment in self._assignments.values():
                if (not assignment.secured or not (assignment.prepared or assignment.deferred)
                        or assignment.failure or not self._current_authority(assignment)):
                    continue
                # Decision 0032: a layer that awaits a grant starts under it, any other at its own
                # instant; either runs to its end, never leased to the plan's validity.
                if self._may_start(assignment, now):
                    started = assignment.granted_layer or assignment.layer
                    assignment.execution_layer = started
                    assignment.execution_until = started.end
                if self._executing(assignment, now):
                    # Revisions renew offers, not old execution leases. Until a
                    # fresh commit, use the original authorized ordering/effects.
                    active.append(replace(assignment, layer=assignment.execution_layer))
            # Every playing layer, a covered one included; what is drawn leaves out a deferred one
            # while it is covered (it holds no decoder).
            playing = {c.binding.output_id: c for c in self._compositions(active, now, timebase)}
            candidates = {output_id: self._hand_over(self._drawable(composition), now, timebase)
                          for output_id, composition in playing.items()}
            observations = []
            for binding in self._config.bindings:
                if binding.output_id not in self._config.enabled_outputs:
                    self._retained.pop(binding.output_id, None)
                    fallback = self._fallback(binding, now)
                    self._show_fallback(fallback)
                    continue
                previous = self._current.get(binding.output_id)
                candidate = candidates.get(binding.output_id)
                failed = False
                if candidate:
                    formerly_visible = {
                        local.layer.assignment_id for local in visible_now(previous.layers)
                    } if previous and not previous.fallback else set()
                    waiting: set[str] = set()
                    # The held layers of a hand-over (`_hand_over`) are drawn, never the held plan's.
                    handing = {local.layer.assignment_id for local in candidate.layers
                               if local.layer.assignment_id not in self._assignments}
                    for local in visible_now(candidate.layers):
                        if local.layer.assignment_id not in formerly_visible | handing:
                            # A covered pipeline may have paused. Seek to logical now
                            # before reveal instead of using its historical preroll.
                            prepared = self.renderer.prepare(local, candidate.binding)
                            self._renderer_residents.add(local.layer.assignment_id)
                            assignment = self._assignments[local.layer.assignment_id]
                            if prepared.status == "prepared":
                                assignment.prepared, assignment.deferred = True, False
                            elif prepared.status == "pending" and (
                                    assignment.deferred or assignment.started):
                                # Revealed while its decode is in flight: it keeps its grant
                                # and is drawn once ready; the rest of the Frame draws now.
                                waiting.add(local.layer.assignment_id)
                            else:
                                self._lose_preparation(assignment)
                                assignment.failure = "decode" if prepared.status == "failed" else None
                                failed = True
                                if prepared.status == "failed":
                                    self._report(observations, local, now, "failed", "decode")
                    if waiting:
                        layers = tuple(local for local in candidate.layers
                                       if local.layer.assignment_id not in waiting)
                        # Held layers alone are no composition of the new plan: with every one of
                        # its layers waiting, what the Frame holds keeps drawing (`_rejoin_hold`).
                        if all(local.layer.assignment_id in handing for local in layers):
                            layers = ()
                        candidate = replace(candidate, layers=layers)
                        candidates[binding.output_id] = candidate
                    given_up: set[str] = set()
                    if not failed and candidate.layers:
                        fits = self.renderer.capacity(tuple(candidates.values())).available
                        while not fits and (victim := shed(
                                {binding.output_id: [local.layer for local in candidate.layers]}
                        )) is not None:
                            # Slots short: this screen gives up its bottom-most layer, not itself.
                            local = next(local for local in candidate.layers
                                         if local.layer.assignment_id == victim.assignment_id)
                            candidate = replace(candidate, layers=tuple(
                                item for item in candidate.layers if item is not local))
                            candidates[binding.output_id] = candidate
                            given_up.add(victim.assignment_id)
                            self.renderer.release(victim.assignment_id)
                            self._renderer_residents.discard(victim.assignment_id)
                            if victim.assignment_id in handing:
                                fits = self.renderer.capacity(tuple(candidates.values())).available
                                continue                # a held picture is given up first, unreported
                            assignment = self._assignments[victim.assignment_id]
                            self._lose_preparation(assignment)
                            assignment.failure = "capacity"
                            self._report(observations, local, now, "failed", "capacity")
                            fits = self.renderer.capacity(tuple(candidates.values())).available
                        if not fits:
                            self._capacity_ok = False
                            failed = True
                            for local in candidate.layers:
                                if local.layer.assignment_id in handing:
                                    continue
                                assignment = self._assignments[local.layer.assignment_id]
                                self._lose_preparation(assignment)
                                assignment.failure = "capacity"
                                self._report(observations, local, now, "failed", "capacity")
                        else:
                            presented = self.renderer.present(candidate)
                            acknowledged = self._acknowledgment(candidate, presented)
                            if acknowledged:
                                drawn, observed_at = acknowledged
                                self._pending_presentations.pop(binding.output_id, None)
                                self._current[binding.output_id] = drawn
                                self._current_leases[binding.output_id] = min(
                                    self._assignments[local.layer.assignment_id].execution_until
                                    for local in candidate.layers if local.layer.assignment_id not in handing
                                )
                                for local in visible_now(drawn.layers):
                                    if local.layer.assignment_id in handing:
                                        continue
                                    assignment = self._assignments[local.layer.assignment_id]
                                    assignment.started = True
                                    self._report(observations, local, observed_at, "presented")
                                # Every playing layer, covered ones included (a Scene beneath keeps
                                # its photo while a Scene on top shows), bottom to top, so the
                                # topmost layer's after-state holds.
                                for local in playing[binding.output_id].layers:
                                    if local.layer.assignment_id not in given_up:
                                        self._apply_after_end(binding, local, now)
                                continue
                            if presented.status == "pending":
                                # Native GL presentation acknowledges on a later main-loop turn.
                                # Do not invalidate its grant or overwrite its queued frame with
                                # fallback before that acknowledgment can occur.
                                if self._awaiting_draw(candidate):
                                    continue
                            failed = True
                            for local in visible_now(candidate.layers):
                                if local.layer.assignment_id in handing:
                                    continue
                                assignment = self._assignments[local.layer.assignment_id]
                                self._lose_preparation(assignment)
                                assignment.failure = "decode"
                                self._report(observations, local, now, "failed", "decode")
                # Retain only a still-valid previous composition. Expired overlays or
                # videos cannot persist because a replacement failed or went missing.
                # A superseded one only while the new plan wants this Output (`_rejoin_hold`).
                if previous and not previous.fallback and all(
                    self._valid_current(local, previous, now) for local in previous.layers
                ) and (not any(local.layer.assignment_id in self._superseded
                               for local in previous.layers)
                       or self._wants_output(binding.output_id, now)):
                    (continuation,) = self._group((
                        LocalLayer(local.layer, local.path, local.layer.position(now),
                                   local.layer.alpha(now)) for local in previous.layers
                    ), timebase, now)
                    acknowledgment = self._acknowledgment(
                        continuation, self.renderer.present(continuation))
                    if acknowledgment:
                        self._current[binding.output_id] = acknowledgment[0]
                    continue
                hold = self._rejoin_hold(previous, binding, now, timebase)
                if hold is not None:
                    acknowledgment = self._acknowledgment(hold, self.renderer.present(hold))
                    if acknowledgment:
                        self._current[binding.output_id] = acknowledgment[0]
                    continue
                fallback = self._fallback(binding, now)
                if not self._show_fallback(fallback):
                    continue
                if self._plan:
                    # Each affected assignment is correlated; empty unbound fallback
                    # has no invented assignment identity.
                    for assignment in self._assignments.values():
                        if (assignment.layer.output_id == binding.output_id
                                and now >= assignment.layer.start):
                            self._report(observations, self._local(assignment, now), now,
                                         "fallback", "decode" if failed else "expired")
            self._superseded.intersection_update(
                local.layer.assignment_id for composition in self._current.values()
                for local in composition.layers)
            # Presented is the state of being seen on its Frame: one covered or no longer drawn has
            # left it, so its next draw is a change and reported again (`_report`).
            seen = {local.layer.assignment_id for composition in self._current.values()
                    if not composition.fallback for local in visible_now(composition.layers)}
            for key, assignment in self._assignments.items():
                if (assignment.reported is not None and assignment.reported[2] == "presented"
                        and key not in seen):
                    assignment.reported = None
            self._note_continuing(now)
            self._release_unused_renderer(now)
            return tuple(observations)

    def _wants_output(self, output_id: str, now: float) -> bool:
        """Whether the held plan has a layer or continuation slot on `output_id` in force now or
        starting within the prepare lead."""
        return any(a.layer.output_id == output_id and self._current_authority(a)
                   and (window := self._window(a))[0] <= now + self.prepare_lead
                   and now < window[1] for a in self._assignments.values())

    def _hand_over(self, incoming: OutputComposition, now: float, timebase: float) -> OutputComposition:
        """Decision 0032, rejoining shows no black: `incoming`, the held plan's composition on its
        Output, over what the composition it superseded still draws there (player/look.py `held`,
        then `handed_over`: one more composition the seam rule looks), until the new layers hide
        it; `incoming` alone with nothing superseded drawn. A held layer is drawn and kept resident
        while drawn (`_superseded`), never prepared, started or reported as the held plan's; once
        the hand-over is over it leaves the composition and its picture is released with it."""
        previous = self._current.get(incoming.binding.output_id)
        if (previous is None or previous.fallback
                or _binding_identity(previous.binding) != _binding_identity(incoming.binding)):
            return incoming
        # Only what the held composition still shows at `now` (a layer faded out, or covered, is gone).
        kept = tuple(LocalLayer(local.layer, local.path, local.layer.position(now), local.alpha)
                     for local in visible_now(look_at(held(previous, now), now).layers)
                     if local.layer.assignment_id in self._superseded
                     and local.layer.assignment_id not in self._cancelled)
        return handed_over(incoming, kept, now) if kept else incoming

    def _rejoin_hold(self, previous: OutputComposition | None, binding: OutputBinding,
                     now: float, timebase: float) -> OutputComposition | None:
        """Decision 0032, rejoining needs no event: a composition a newer plan superseded keeps
        drawing past its layers' ends (player/look.py `held`), looked by the seam rule like every
        composition (`_group`), so holding it never changes its picture, while the new plan has a
        layer or continuation slot on this Output in force now or starting within the prepare
        lead that is not drawn yet (secured, prepared, due and granted if it awaits one). With
        nothing of the new plan there, the Frame takes its after-state (None)."""
        if (previous is None or previous.fallback or not previous.layers
                or _binding_identity(binding) != _binding_identity(previous.binding)):
            return None
        keys = [local.layer.assignment_id for local in previous.layers]
        if not any(key in self._superseded for key in keys) or any(
                key in self._cancelled for key in keys):
            return None
        if not self._wants_output(binding.output_id, now):
            return None
        (hold,) = self._group((
            LocalLayer(local.layer, local.path, local.layer.position(now), local.alpha)
            for local in held(previous, now).layers), timebase, now)
        return hold
