"""Authenticated timed Player execution, with explicit worker/rendering boundaries."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock

from contracts.models import (
    Commit,
    Failure,
    Layer,
    Observation,
    OutputBinding,
    Plan,
    PlayerConfiguration,
    Readiness,
    Revocation,
)
from contracts.time import Clock, TimeMapping
from player.cache import Cache, CacheCapacityError, CacheError
from player.rendering import LocalLayer, OutputComposition, PresentationResult, Renderer


class AuthorityError(ValueError):
    """A message cannot establish current execution authority."""


@dataclass
class _Assignment:
    layer: Layer
    owner: str
    secured: bool = False
    path: Path | None = None
    prepared: bool = False
    committed: bool = False
    started: bool = False
    acquiring: bool = False
    failure: str | None = None
    execution_layer: Layer | None = None
    execution_until: float = 0
    offered_until: float = 0
    readiness_generation: int = 0


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
    )


def _binding_identity(binding: OutputBinding) -> tuple:
    return binding.output_id, binding.frame_id, binding.generation


def _alpha(layer: Layer, now: float) -> float:
    fade_in = min(1.0, max(0.0, (now - layer.start) / layer.fade_in)) if layer.fade_in else 1
    fade_out = min(1.0, max(0.0, (layer.end - now) / layer.fade_out)) if layer.fade_out else 1
    return layer.opacity * min(fade_in, fade_out)


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
        clock: Clock,
        mapping: TimeMapping,
        state_path: Path,
        prepare_lead: float = 5,
    ):
        if not math.isfinite(prepare_lead) or prepare_lead <= 0:
            raise ValueError("prepare_lead must be finite and positive")
        self.player_id = player_id
        self.cache, self.renderer = cache, renderer
        self.clock, self.mapping = clock, mapping
        self.state_path = Path(state_path)
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
        self._last_epoch = 0
        self._last_plan_id: str | None = None
        self._last_revision = 0
        self._last_revocation_sequence = 0
        self._last_configuration_revision = 0
        self._frame_generations: dict[str, int] = {}
        self._anchor: tuple[float, float] | None = None
        self._capacity_ok = False
        self._durable_ok = True
        self._renderer_releases: set[str] = set()
        self._renderer_residents: set[str] = set()
        self._retired_outputs: dict[str, OutputBinding] = {}
        self._load_reconciliation()

    def _load_reconciliation(self) -> None:
        if not self.state_path.exists():
            return
        if self.state_path.is_symlink():
            raise ValueError("execution state cannot be a symlink")
        # A corrupt authority journal fails closed; silently discarding it could
        # permit the same retired process epoch to execute after a cold restart.
        data = json.loads(self.state_path.read_text())
        if data.get("version") != 1 or data.get("player_id") != self.player_id:
            raise ValueError("execution state identity/version mismatch")
        epoch = data["authority_epoch"]
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            raise ValueError("invalid persisted authority epoch")
        self._last_epoch = epoch
        self._pin_intents = dict(data["pin_intents"])
        for owner, digest in self._pin_intents.items():
            if (not isinstance(owner, str) or len(owner) > 512
                    or not owner.startswith((f"pwexec:{self.player_id}:", f"pwretain:{self.player_id}:"))
                    or not isinstance(digest, str) or len(digest) != 64
                    or any(character not in "abcdef0123456789" for character in digest)):
                raise ValueError("invalid persisted pin ownership")
        self._frame_generations = dict(data.get("frame_generations", {}))
        # Other persisted fields support central reconciliation, never replay.

    def _persist(self) -> None:
        data = {
            "version": 1,
            "player_id": self.player_id,
            "authority_epoch": self._last_epoch,
            "configuration_revision": self._last_configuration_revision,
            "plan_id": self._last_plan_id,
            "plan_revision": self._last_revision,
            "revocation_sequence": self._last_revocation_sequence,
            "plan": self._plan.model_dump(mode="json") if self._plan else None,
            "cancelled": sorted(self._cancelled),
            "pin_intents": self._pin_intents,
            "frame_generations": self._frame_generations,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".executor-", dir=self.state_path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(data, stream, separators=(",", ":"), sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
            directory = os.open(self.state_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            self._durable_ok = False
            raise
        finally:
            Path(temporary).unlink(missing_ok=True)
        self._durable_ok = True

    def _now(self) -> float:
        if self.mapping.healthy():
            self._anchor = self.clock.utc(), self.clock.monotonic()
        if self._anchor is not None:
            utc, mono = self._anchor
            return utc + self.clock.monotonic() - mono
        return self.clock.utc()

    def _binding(self, output_id: str) -> OutputBinding | None:
        if self._config:
            return next((b for b in self._config.bindings if b.output_id == output_id), None)
        return None

    def _current_authority(self, assignment: _Assignment) -> bool:
        binding = self._binding(assignment.layer.output_id)
        return bool(
            self._durable_ok and binding and assignment.layer.assignment_id not in self._cancelled
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
                        or binding.profile != retained.binding.profile):
                    del self._retained[output_id]
            for output_id, composition in list(self._current.items()):
                binding = self._binding(output_id)
                if not binding or _binding_identity(binding) != _binding_identity(composition.binding):
                    self._retired_outputs[output_id] = composition.binding
                    del self._current[output_id]
            self._capacity_ok = False
            self._persist()
            return True

    def accept_plan(self, plan: Plan) -> bool:
        with self._lock:
            if (not self._config or plan.player_id != self.player_id
                    or plan.authority_epoch != self._last_epoch):
                raise AuthorityError("plan authority mismatch")
            now = self._now()
            if plan.valid_until <= now or plan.issued_at > now + self.mapping.max_uncertainty:
                raise AuthorityError("expired or future-issued plan")
            if self._last_plan_id is not None and plan.plan_id != self._last_plan_id:
                raise AuthorityError("plan identity must remain stable within its epoch")
            if plan == self._plan:
                return False
            if plan.revision <= self._last_revision:
                raise AuthorityError("stale plan revision")
            if tuple(plan.bindings) != tuple(self._config.bindings):
                raise AuthorityError("plan configuration mismatch")
            for layer in plan.layers:
                old = self._assignments.get(layer.assignment_id) or self._inactive.get(layer.assignment_id)
                if old and (old.secured or old.acquiring) and (
                    _content_identity(layer) != _content_identity(old.layer)
                ):
                    raise AuthorityError("secured assignment content is immutable")
            next_assignments: dict[str, _Assignment] = {}
            for layer in plan.layers:
                key = layer.assignment_id
                if key in self._cancelled:
                    continue
                old = self._assignments.get(key) or self._inactive.pop(key, None)
                if old and _content_identity(layer) == _content_identity(old.layer):
                    old.layer = layer
                    # Revision-specific authorization must be renewed by central.
                    old.committed = False
                    old.offered_until = max(old.offered_until, plan.valid_until)
                    next_assignments[key] = old
                else:
                    next_assignments[key] = _Assignment(
                        layer, f"pwexec:{self.player_id}:{self._last_epoch}:{key}",
                        secured=layer.presentation == "black",
                        offered_until=plan.valid_until,
                    )
            for key, assignment in self._assignments.items():
                if key not in next_assignments:
                    # A complete newer manifest supersedes execution, independently
                    # of the conservative lease protecting possibly secured bytes.
                    self._lose_preparation(assignment)
                    self._invalidated.add(key)
                    if assignment.secured or assignment.acquiring:
                        self._inactive[key] = assignment
            self._renderer_releases.update(set(self._assignments) - set(next_assignments))
            self._assignments = next_assignments
            self._plan = plan
            self._last_plan_id, self._last_revision = plan.plan_id, plan.revision
            self._last_revocation_sequence = 0
            self._capacity_ok = False
            self._reported.clear()
            self._persist()
            return True

    def acquire(self, assignment_id: str, chunks: Iterable[bytes]) -> bool:
        """Worker-only acquisition; streaming never owns the execution lock."""
        with self._cache_work:
            return self._acquire(assignment_id, chunks)

    def _acquire(self, assignment_id: str, chunks: Iterable[bytes]) -> bool:
        with self._lock:
            assignment = self._assignments.get(assignment_id)
            if not assignment or not self._current_authority(assignment):
                raise AuthorityError("unknown or cancelled assignment")
            if assignment.acquiring:
                raise AuthorityError("assignment acquisition is already active")
            if assignment.layer.variant is None:
                return True
            assignment.acquiring = True
            self._pin_intents[assignment.owner] = assignment.layer.variant.sha256
            self._persist()
        try:
            path = self.cache.secure(assignment.layer.variant, chunks, assignment.owner)
        except Exception as error:
            with self._lock:
                assignment.acquiring = False
                if self._assignments.get(assignment_id) is assignment:
                    assignment.secured = False
                    self._lose_preparation(assignment)
                    assignment.failure = "capacity" if isinstance(error, CacheCapacityError) else "download"
                    self._persist()
            return False
        with self._lock:
            assignment.acquiring = False
            assignment.path = path
            assignment.secured = True
            if (self._assignments.get(assignment_id) is not assignment
                    or not self._current_authority(assignment)
                    or not self._plan or self._now() >= min(
                        assignment.layer.end, self._plan.valid_until
                    )):
                # Pin remains journaled for the worker's deferred reconciliation.
                return False
            assignment.failure = None
            self._persist()
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
        with self._lock:
            self._persist()

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
            # Until fresh configuration arrives, all recovered owners are uncertain.
            if self._config is None:
                return
            retained = tuple(self._retained.values())
        for item in retained:
            assert item.local.layer.variant is not None
            try:
                self.cache.pin(item.local.layer.variant.sha256, item.owner)
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
        with self._lock:
            self._persist()

    def _desired_owners(self) -> set[str]:
        now = self._now()
        desired = {a.owner for a in self._assignments.values() if a.acquiring or (
            a.secured and self._current_authority(a) and self._plan
            and now < min(a.layer.end, self._plan.valid_until)
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
                if self._valid_current(local, composition, now) and local.layer.variant:
                    desired.add(f"pwexec:{self.player_id}:{self._last_epoch}:"
                                f"{local.layer.assignment_id}")
        return desired

    def _local(self, assignment: _Assignment, now: float) -> LocalLayer:
        return LocalLayer(assignment.layer, assignment.path, assignment.layer.position(now),
                          _alpha(assignment.layer, now))

    def _compositions(self, items: Iterable[_Assignment], now: float) -> tuple[OutputComposition, ...]:
        grouped: dict[str, list[LocalLayer]] = {}
        for assignment in items:
            grouped.setdefault(assignment.layer.output_id, []).append(self._local(assignment, now))
        result = []
        for output_id, layers in grouped.items():
            binding = self._binding(output_id)
            if binding:
                layers.sort(key=lambda local: (
                    local.layer.priority, local.layer.root_order, local.layer.admission_order
                ))
                result.append(OutputComposition(binding, binding.effective_calibration(now),
                                                tuple(layers)))
        return tuple(result)

    def _imminent(self, now: float) -> list[_Assignment]:
        if not self._plan:
            return []
        return [a for a in self._assignments.values() if self._current_authority(a)
                and now < min(a.layer.end, self._plan.valid_until)
                and max(a.layer.start, self._plan.valid_from) <= now + self.prepare_lead]

    def _clock_gate(self) -> bool:
        healthy = self.mapping.healthy()
        if not healthy:
            for assignment in self._assignments.values():
                assignment.committed = False
                assignment.readiness_generation += 1
        return healthy

    @staticmethod
    def _lose_preparation(assignment: _Assignment) -> None:
        assignment.prepared = assignment.committed = False
        assignment.execution_layer = None
        assignment.readiness_generation += 1

    def prepare_imminent(self) -> None:
        """Renderer-thread preroll. No filesystem or network access occurs here."""
        with self._lock:
            self._drain_renderer_releases()
            now = self._now()
            imminent = self._imminent(now)
            self._release_unused_renderer(now)
            for assignment in imminent:
                if not assignment.secured:
                    continue
                result = self.renderer.prepare(self._local(assignment, max(now, assignment.layer.start)))
                self._renderer_residents.add(assignment.layer.assignment_id)
                if result.status != "prepared" and (assignment.prepared or assignment.committed):
                    self._lose_preparation(assignment)
                assignment.prepared = result.status == "prepared"
                if result.status == "failed":
                    assignment.failure = result.code if result.code != "none" else "decode"
                    if not assignment.started:
                        assignment.committed = False
                elif result.status == "pending":
                    assignment.failure = None
                    if not assignment.started:
                        assignment.committed = False
                else:
                    assignment.failure = None
            self._capacity_ok = self.renderer.capacity(self._compositions(imminent, now)).available
            if not self._capacity_ok:
                for assignment in imminent:
                    if not assignment.started:
                        self._lose_preparation(assignment)
            self._clock_gate()

    @property
    def failures(self) -> tuple[Failure, ...]:
        with self._lock:
            return tuple(Failure(assignment_id=key, code=assignment.failure)
                         for key, assignment in sorted(self._assignments.items())
                         if assignment.failure is not None)

    def readiness(self) -> Readiness:
        with self._lock:
            if not self._plan:
                raise AuthorityError("no current plan")
            now = self._now()
            clock_ok = self._clock_gate()
            imminent = self._imminent(now)
            current = [a for a in self._assignments.values() if self._current_authority(a)
                       and now < min(a.layer.end, self._plan.valid_until)]
            failures = {f.assignment_id: f for f in self.failures}
            for assignment in imminent:
                key = assignment.layer.assignment_id
                if not clock_ok:
                    failures[key] = Failure(assignment_id=key, code="clock")
                elif not self._capacity_ok:
                    failures[key] = Failure(assignment_id=key, code="capacity")
            self._sequence += 1
            # Wire values remain finite; unhealthy mapping is also explicitly coded.
            uncertainty = self.mapping.uncertainty
            if not math.isfinite(uncertainty):
                uncertainty = 86400.0
            readiness = Readiness(
                plan_id=self._plan.plan_id, revision=self._plan.revision,
                authority_epoch=self._last_epoch, sequence=self._sequence,
                secured=tuple(sorted(a.layer.assignment_id for a in current if a.secured)),
                prepared=tuple(sorted(a.layer.assignment_id for a in imminent
                                      if a.prepared and a.secured
                                      and a.layer.assignment_id not in failures)),
                capacity_ok=self._capacity_ok, clock_uncertainty=uncertainty,
                observed_at=now, failures=tuple(failures[key] for key in sorted(failures)),
            )
            self._reported[self._sequence] = {
                key: self._assignments[key].readiness_generation for key in readiness.prepared
            }
            for sequence in sorted(self._reported)[:-128]:
                del self._reported[sequence]
            return readiness

    def accept_commit(self, commit: Commit) -> bool:
        with self._lock:
            if (not self._plan or commit.authority_epoch != self._last_epoch
                    or commit.plan_id != self._plan.plan_id or commit.revision != self._plan.revision):
                raise AuthorityError("commit authority mismatch")
            now = self._now()
            if (not self._clock_gate() or not self._capacity_ok
                    or commit.committed_at > now + self.mapping.max_uncertainty
                    or commit.committed_at < self._plan.issued_at):
                raise AuthorityError("commit clock, time or capacity gate failed")
            assignments = []
            reported = self._reported.get(commit.readiness_sequence, {})
            for key in commit.assignment_ids:
                assignment = self._assignments.get(key)
                if (not assignment or not self._current_authority(assignment)
                        or not assignment.secured or not assignment.prepared or assignment.failure
                        or now >= min(assignment.layer.end, self._plan.valid_until)
                        or max(assignment.layer.start, self._plan.valid_from) > now + self.prepare_lead):
                    raise AuthorityError("commit assignment is not ready")
                if reported.get(key) != assignment.readiness_generation:
                    raise AuthorityError("commit references revoked or unknown readiness")
                intended_start = max(assignment.layer.start, self._plan.valid_from)
                if (assignment.execution_layer is None
                        and now > intended_start + self.mapping.max_uncertainty
                        and commit.committed_at < intended_start):
                    raise AuthorityError("missed start requires fresh late-join authorization")
                assignments.append(assignment)
            changed = any(not a.committed for a in assignments)
            for assignment in assignments:
                assignment.committed = True
                self._invalidated.discard(assignment.layer.assignment_id)
            self._persist()
            return changed

    def cancel(self, assignment_ids: Iterable[str]) -> None:
        with self._lock:
            for key in assignment_ids:
                self._cancelled.add(key)
                self._assignments.pop(key, None)
                self._inactive.pop(key, None)
                self._renderer_releases.add(key)
            self._persist()

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
                    self._invalidated.add(key)
            self._persist()

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
            self._persist()
            return True

    def _drain_renderer_releases(self) -> None:
        protected = {local.layer.assignment_id for composition in self._current.values()
                     for local in composition.layers}
        protected.update(item.local.layer.assignment_id for item in self._retained.values())
        for key in tuple(self._renderer_releases - protected):
            self.renderer.release(key)
            self._renderer_residents.discard(key)
            self._renderer_releases.discard(key)

    def _release_unused_renderer(self, now: float) -> None:
        needed = {a.layer.assignment_id for a in self._imminent(now) if a.secured}
        needed.update(local.layer.assignment_id for composition in self._current.values()
                      for local in composition.layers)
        needed.update(item.local.layer.assignment_id for item in self._retained.values())
        for key in self._renderer_residents - needed:
            self.renderer.release(key)
        self._renderer_residents.intersection_update(needed)

    @staticmethod
    def _visible(layers: tuple[LocalLayer, ...]) -> tuple[LocalLayer, ...]:
        visible = []
        for local in reversed(layers):
            if local.alpha > 0:
                visible.append(local)
            if local.alpha >= 1:
                break
        return tuple(reversed(visible))

    def _valid_current(self, local: LocalLayer, composition: OutputComposition, now: float) -> bool:
        binding = self._binding(composition.binding.output_id)
        return bool(
            self._durable_ok and self._plan and binding
            and self._config and binding.output_id in self._config.enabled_outputs
            and local.layer.assignment_id not in self._cancelled
            and local.layer.assignment_id not in self._invalidated
            and _binding_identity(binding) == _binding_identity(composition.binding)
            and local.layer.start <= now < min(
                local.layer.end, self._current_leases.get(binding.output_id, -math.inf)
            )
        )

    def _fallback(self, binding: OutputBinding, now: float) -> OutputComposition:
        retained = self._retained.get(binding.output_id)
        layers = (retained.local,) if retained else ()
        return OutputComposition(binding, binding.effective_calibration(now), layers, fallback=True)

    def _show_fallback(self, composition: OutputComposition) -> bool:
        """Only observed fallback replaces state; unavailable retained media yields black."""
        if composition.layers and any(
            self.renderer.prepare(local).status != "prepared" for local in composition.layers
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
        key = (tuple((local.layer, local.path) for local in composition.layers),
               composition.binding, composition.calibration, composition.fallback)
        output_id = composition.binding.output_id
        pending = self._pending_presentations.get(output_id)
        if pending is None or pending[0] != key:
            pending = (key, self.clock.monotonic())
            self._pending_presentations[output_id] = pending
        return self.clock.monotonic() - pending[1] < .5

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
        age = self.clock.monotonic() - completed
        if (not 0 <= age <= .5 or drawn.binding != candidate.binding
                or drawn.calibration != candidate.calibration or drawn.fallback != candidate.fallback
                or len(drawn.layers) != len(candidate.layers)):
            return None
        for actual, requested in zip(drawn.layers, candidate.layers, strict=True):
            if (actual.layer != requested.layer or actual.path != requested.path
                    or not math.isfinite(actual.position) or actual.position < 0
                    or not math.isfinite(actual.alpha) or not 0 <= actual.alpha <= 1):
                return None
        return drawn, self._now() - age

    def _observation(self, local: LocalLayer, now: float, status: str,
                     detail: str = "none") -> Observation:
        assert self._plan is not None
        return Observation(
            plan_id=self._plan.plan_id, revision=self._plan.revision,
            authority_epoch=self._last_epoch, assignment_id=local.layer.assignment_id,
            observed_at=now, status=status, position=local.position, detail=detail,
        )

    def tick(self) -> tuple[Observation, ...]:
        """Renderer thread: current-state execution and calibration/fallback updates."""
        with self._lock:
            self._drain_renderer_releases()
            now = self._now()
            self._clock_gate()
            if not self._config:
                return ()
            for output_id, binding in tuple(self._retired_outputs.items()):
                if self._show_fallback(OutputComposition(
                    binding, binding.effective_calibration(now), fallback=True
                )):
                    del self._retired_outputs[output_id]
            active = []
            for assignment in self._assignments.values():
                if (not assignment.secured or not assignment.prepared or assignment.failure
                        or not self._current_authority(assignment)):
                    continue
                if (assignment.committed and self._plan and self.mapping.healthy()
                        and max(assignment.layer.start, self._plan.valid_from) <= now
                        < min(assignment.layer.end, self._plan.valid_until)):
                    assignment.execution_layer = assignment.layer
                    assignment.execution_until = self._plan.valid_until
                executing = assignment.execution_layer
                if (executing is not None and executing.start <= now
                        < min(executing.end, assignment.execution_until)):
                    # Revisions renew offers, not old execution leases. Until a
                    # fresh commit, use the original authorized ordering/effects.
                    active.append(replace(assignment, layer=executing))
            candidates = {c.binding.output_id: c for c in self._compositions(active, now)}
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
                        local.layer.assignment_id for local in self._visible(previous.layers)
                    } if previous and not previous.fallback else set()
                    for local in self._visible(candidate.layers):
                        if local.layer.assignment_id not in formerly_visible:
                            # A covered pipeline may have paused. Seek to logical now
                            # before reveal instead of using its historical preroll.
                            prepared = self.renderer.prepare(local)
                            self._renderer_residents.add(local.layer.assignment_id)
                            if prepared.status != "prepared":
                                assignment = self._assignments[local.layer.assignment_id]
                                self._lose_preparation(assignment)
                                assignment.failure = "decode" if prepared.status == "failed" else None
                                failed = True
                                if prepared.status == "failed":
                                    observations.append(self._observation(local, now, "failed", "decode"))
                    if not failed:
                        capacity = self.renderer.capacity(tuple(candidates.values()))
                        if not capacity.available:
                            self._capacity_ok = False
                            failed = True
                            for local in candidate.layers:
                                assignment = self._assignments[local.layer.assignment_id]
                                self._lose_preparation(assignment)
                                assignment.failure = "capacity"
                                observations.append(self._observation(local, now, "failed", "capacity"))
                        else:
                            presented = self.renderer.present(candidate)
                            acknowledged = self._acknowledgment(candidate, presented)
                            if acknowledged:
                                drawn, observed_at = acknowledged
                                self._pending_presentations.pop(binding.output_id, None)
                                self._current[binding.output_id] = drawn
                                self._current_leases[binding.output_id] = min(
                                    self._assignments[local.layer.assignment_id].execution_until
                                    for local in candidate.layers
                                )
                                for local in self._visible(drawn.layers):
                                    assignment = self._assignments[local.layer.assignment_id]
                                    assignment.started = True
                                    observations.append(self._observation(local, observed_at, "presented"))
                                    if (local.alpha >= 1 and local.layer.retain_on_expiry
                                            and local.layer.variant
                                            and local.layer.variant.duration is None):
                                        identity = (f"{binding.output_id}:{binding.frame_id}:"
                                                    f"{binding.generation}:{local.layer.assignment_id}")
                                        owner = (f"pwretain:{self.player_id}:{self._last_epoch}:"
                                                 f"{hashlib.sha256(identity.encode()).hexdigest()}")
                                        self._pin_intents[owner] = local.layer.variant.sha256
                                        self._retained[binding.output_id] = _Retained(local, binding, owner)
                                continue
                            if presented.status == "pending":
                                # Native GL presentation acknowledges on a later main-loop turn.
                                # Do not invalidate its grant or overwrite its queued frame with
                                # fallback before that acknowledgment can occur.
                                if self._awaiting_draw(candidate):
                                    continue
                            failed = True
                            for local in self._visible(candidate.layers):
                                assignment = self._assignments[local.layer.assignment_id]
                                self._lose_preparation(assignment)
                                assignment.failure = "decode"
                                observations.append(self._observation(local, now, "failed", "decode"))
                # Retain only a still-valid previous composition. Expired overlays or
                # videos cannot persist because a replacement failed or went missing.
                if previous and not previous.fallback and all(
                    self._valid_current(local, previous, now) for local in previous.layers
                ):
                    continuation = OutputComposition(
                        binding, binding.effective_calibration(now), tuple(
                            LocalLayer(local.layer, local.path, local.layer.position(now),
                                       _alpha(local.layer, now)) for local in previous.layers
                        )
                    )
                    acknowledgment = self._acknowledgment(
                        continuation, self.renderer.present(continuation))
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
                            observations.append(self._observation(
                                self._local(assignment, now), now, "fallback",
                                "decode" if failed else "expired",
                            ))
            self._release_unused_renderer(now)
            self._persist()
            return tuple(observations)
