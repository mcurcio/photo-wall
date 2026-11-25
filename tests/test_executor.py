from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

from contracts.continuation import CONTINUATION_PREFIX
from contracts.frame_lighting import FrameLighting, Paper
from contracts.models import (
    Calibration,
    CarriedPhoto,
    Commit,
    Continuation,
    Layer,
    LostGrant,
    OutputBinding,
    Plan,
    PlayerConfiguration,
    PositionBox,
    Revocation,
    UnfilledSpan,
    Variant,
)
from contracts.node_app_link import FrameBlack, FrameBlackCause
from contracts.photo_crop import CENTRED
from contracts.time import ManualClock
from player.cache import Cache
from player.executor import AuthorityError, Executor
from player.rendering import PresentationResult, RecordingRenderer
from player.show_clock import ShowClock


def media(data: bytes = b"picture", video: bool = False) -> Variant:
    return Variant(asset_id="asset-" + hashlib.sha256(data).hexdigest()[:16],
                   recipe_id=("f" if video else "e") * 64,
                   sha256=hashlib.sha256(data).hexdigest(), size=len(data),
                   media_type="video/mp4" if video else "image/png", width=1920, height=1080,
                   duration=120 if video else None)


def test_external_revocation_is_idempotent_and_cannot_cross_plan_revision(tmp_path):
    rig = Rig(tmp_path)
    rig.play(layer())
    revoked = Revocation(plan_id=rig.plan.plan_id, revision=1, authority_epoch=1,
                         sequence=8, assignment_ids=("picture",))
    assert rig.executor.accept_revocation(revoked)
    rig.executor.prepare_imminent()
    assert not rig.executor.accept_revocation(revoked)
    assert rig.executor.readiness().prepared == ("picture",)
    rig.offer(layer(), revision=2)
    with pytest.raises(AuthorityError, match="revocation authority"):
        rig.executor.accept_revocation(revoked.model_copy(update={"sequence": 9}))


def test_native_presentation_ack_can_arrive_later_without_invalidating_grant(tmp_path):
    class AsyncRenderer(RecordingRenderer):
        acknowledged = False

        def present(self, composition):
            if composition.layers and not self.acknowledged:
                return PresentationResult("pending")
            return super().present(composition)

    rig = Rig(tmp_path)
    renderer = AsyncRenderer()
    rig.renderer = rig.executor.renderer = renderer
    assert rig.play(layer()) == ()
    assert rig.executor.readiness().prepared == ("picture",)
    renderer.acknowledged = True
    assert rig.executor.tick()[0].status == "presented"
    assert renderer.outputs["hdmi1"].layers[0].layer.assignment_id == "picture"


def test_native_missing_presentation_ack_expires_into_reported_failure(tmp_path):
    class StuckRenderer(RecordingRenderer):
        def present(self, composition):
            if composition.layers:
                return PresentationResult("pending")
            return super().present(composition)

    rig = Rig(tmp_path)
    rig.renderer = rig.executor.renderer = StuckRenderer()
    rig.play(layer())
    rig.advance(.6)
    observations = rig.executor.tick()
    assert any(o.status == "failed" and o.detail == "decode" for o in observations)
    assert not rig.executor.readiness().prepared
    assert rig.renderer.outputs["hdmi1"].fallback


def test_native_ack_observes_actual_sample_and_draw_time(tmp_path):
    rig = Rig(tmp_path)

    class ActualRenderer(RecordingRenderer):
        def present(self, composition):
            if not composition.layers:
                return super().present(composition)
            drawn = replace(composition, layers=tuple(
                replace(local, position=.066) for local in composition.layers))
            return PresentationResult("presented", composition=drawn,
                                      presented_at=rig.clock.monotonic() - .02)

    rig.renderer = rig.executor.renderer = ActualRenderer()
    rig.offer(layer(video=True))
    rig.secure()
    rig.executor.prepare_imminent()
    rig.commit("picture")
    rig.advance(.1)
    observation = rig.executor.tick()[0]
    assert observation.status == "presented"
    assert observation.position == .066
    assert observation.observed_at == pytest.approx(100.08)


def test_retained_fallback_waits_for_native_draw_without_overwriting_with_black(tmp_path):
    class DeferredFallback(RecordingRenderer):
        acknowledged = False

        def present(self, composition):
            if composition.fallback and composition.layers and not self.acknowledged:
                return PresentationResult("pending")
            return super().present(composition)

    rig = Rig(tmp_path)
    rig.renderer = rig.executor.renderer = DeferredFallback()
    rig.play(layer(end=101, after_end="keep_this_photo"))
    rig.advance(1.01)
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].layers
    rig.renderer.acknowledged = True
    rig.executor.tick()
    shown = rig.renderer.outputs["hdmi1"]
    assert shown.fallback and shown.layers[0].layer.assignment_id == "picture"


@pytest.mark.parametrize(("display_mm", "kept"), [(None, True), ((950, 540), False)],
                         ids=["same-display", "new-display-size"])
def test_a_kept_still_is_dropped_when_its_displays_size_changes(tmp_path, display_mm, kept):
    """A kept still was composed for the Frame's physical size (its lights and mat, from the
    Display's size): the same binding with a new `display_mm` drops it, an unchanged one keeps
    it. Mutation probe: drop the display_mm comparison in `accept_configuration` -> the kept
    still survives a new Display size."""
    rig = Rig(tmp_path)
    rig.play(layer(end=101, after_end="keep_this_photo"))
    rig.advance(1.01)
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].layers
    moved = binding(display_mm=display_mm, configuration_revision=2)
    rig.executor.accept_configuration(rig.configuration.model_copy(update={
        "configuration_revision": 2, "bindings": (moved, binding("hdmi2", "frame2"))}))
    rig.executor.tick()
    shown = rig.renderer.outputs["hdmi1"]
    assert shown.fallback and bool(shown.layers) is kept


@pytest.mark.parametrize(("first_draw_after_start", "kept"), [(1.9, False), (2, True)])
def test_kept_still_takes_effect_from_its_first_draw_at_or_after_its_fade_in(
        tmp_path, first_draw_after_start, kept):
    """A "keep_this_photo" layer with fade-in 2 is kept from its first draw at or after start + 2
    (contracts/on_screen.py `kept_from`), never from a draw before it."""
    rig = Rig(tmp_path)
    rig.play(layer(end=102.5, fade_in=2, after_end="keep_this_photo"))
    rig.advance(first_draw_after_start)
    rig.executor.tick()
    rig.advance(3 - first_draw_after_start)
    rig.executor.tick()
    shown = rig.renderer.outputs["hdmi1"]
    assert shown.fallback
    assert [local.layer.assignment_id for local in shown.layers] == (["picture"] if kept else [])


@pytest.mark.parametrize("fault", ["future", "binding", "layer", "calibration", "fallback", "path",
                                   "position", "alpha"])
def test_native_ack_rejects_stale_or_different_authority(tmp_path, fault):
    rig = Rig(tmp_path)

    class InvalidRenderer(RecordingRenderer):
        def present(self, composition):
            if not composition.layers:
                return super().present(composition)
            drawn = composition
            completed = rig.clock.monotonic()
            if fault == "future":
                completed += .1
            elif fault == "binding":
                drawn = replace(drawn, binding=drawn.binding.model_copy(update={"generation": 2}))
            elif fault == "calibration":
                drawn = replace(drawn, binding=drawn.binding.model_copy(
                    update={"calibration": Calibration(fit=PositionBox(left=0.25, top=0.25, width=0.5, height=0.5))}))
            elif fault == "fallback":
                drawn = replace(drawn, fallback=not drawn.fallback)
            elif fault == "path":
                drawn = replace(drawn, layers=(replace(drawn.layers[0], path=Path("/elsewhere")),))
            elif fault == "position":
                drawn = replace(drawn, layers=(replace(drawn.layers[0], position=-1.0),))
            elif fault == "alpha":
                drawn = replace(drawn, layers=(replace(drawn.layers[0], alpha=1.5),))
            else:
                local = drawn.layers[0]
                drawn = replace(drawn, layers=(replace(local, layer=local.layer.model_copy(
                    update={"priority": 999})),))
            return PresentationResult("presented", composition=drawn, presented_at=completed)

    rig.renderer = rig.executor.renderer = InvalidRenderer()
    observations = rig.play(layer())
    assert not any(o.status == "presented" for o in observations)
    assert any(o.status == "failed" for o in observations)


def test_each_request_answered_pending_gets_its_own_draw_grace(tmp_path):
    """The draw grace runs per request (`OutputComposition.request`): a photo that follows one still
    pending at its cut is waited on from its own first "pending", not its predecessor's, so a
    renderer that takes 0.3 s to draw each never fails the second."""
    rig = Rig(tmp_path)
    delays = {"first": 1.0, "second": .3}
    seen: dict[tuple, float] = {}

    class SlowRenderer(RecordingRenderer):
        def present(self, composition):
            if not composition.layers:
                return super().present(composition)
            first_seen = seen.setdefault(composition.request, rig.clock.monotonic())
            delay = max(delays[local.layer.assignment_id] for local in composition.layers)
            if rig.clock.monotonic() - first_seen < delay:
                return PresentationResult("pending")
            return super().present(composition)

    rig.renderer = rig.executor.renderer = SlowRenderer()
    first = layer("first", data=b"first", end=100.4)
    second = layer("second", data=b"second", start=100.4)
    observations = list(rig.play(first, second, payloads={"first": b"first", "second": b"second"}))
    for _ in range(10):
        rig.advance(.1)
        rig.executor.prepare_imminent()
        observations += rig.executor.tick()
    assert [o.status for o in observations if o.assignment_id == "second"][-1] == "presented"
    assert not any(o.status == "failed" for o in observations)


def test_a_still_photo_drawn_long_ago_still_reads_as_presented(tmp_path):
    """Render only what reaches the screen: a still photo is never redrawn, so the renderer's
    "presented" is a state; a draw ten minutes old that still stands is presented, with the
    time it was drawn."""
    rig = Rig(tmp_path)
    age = 600.0

    class RenewingRenderer(RecordingRenderer):
        def present(self, composition):
            if not composition.layers:
                return super().present(composition)
            return PresentationResult("presented", composition=composition,
                                      presented_at=rig.clock.monotonic() - age)

    rig.renderer = rig.executor.renderer = RenewingRenderer()
    observations = rig.play(layer())
    assert any(o.status == "presented" for o in observations)


def served(data: bytes = b"picture") -> dict:
    """Central's answer with `data`: its `Repr-Digest` and `Content-Length`."""
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def cached(variant: Variant) -> str:
    """The cache's pin name for a variant (its key, hashed)."""
    return hashlib.sha256(variant.key.encode()).hexdigest()


LIGHTING = FrameLighting(mat_depth_mm=0, lights=(), paper=Paper(whiteness=.9, ink_density=2))


def binding(output: str = "hdmi1", frame: str = "frame1", **kwargs) -> OutputBinding:
    return OutputBinding(output_id=output, frame_id=frame, generation=1,
                         **{"lighting": LIGHTING, "display_mm": None, **kwargs})


def layer(key: str = "picture", *, output: str = "hdmi1", frame: str = "frame1",
          data: bytes = b"picture", video: bool = False, **kwargs) -> Layer:
    # A Run's first turn (decision 0032): it awaits Central's grant, as these tests drive it.
    fields = dict(assignment_id=key, run_id="run", output_id=output, frame_id=frame,
                  binding_generation=1, start=100, end=200, media_origin=100,
                  variant=media(data, video), crop=CENTRED, awaits_grant=True)
    fields.update(kwargs)
    if fields.get("presentation") == "black":
        fields["crop"] = None
    return Layer(**fields)


class Rig:
    def __init__(self, directory: Path, *, capacity=4, cache_bytes=4096,
                 bindings: tuple[OutputBinding, ...] | None = None, epoch=1):
        self.clock = ManualClock(100)     # its wall reading stands for Central's time
        self.show_clock = ShowClock(self.clock.monotonic)
        self.sync_clock()
        self.cache = Cache(directory / "cache", cache_bytes)
        self.renderer = RecordingRenderer(capacity)
        self.executor = Executor("player1", self.cache, self.renderer, self.clock,
                                 self.show_clock)
        self.configuration = PlayerConfiguration(
            player_id="player1", authority_epoch=epoch, configuration_revision=1,
            bindings=bindings or (binding(), binding("hdmi2", "frame2")),
            enabled_outputs=tuple(b.output_id for b in bindings) if bindings else ("hdmi1", "hdmi2"),
        )
        self.executor.accept_configuration(self.configuration)
        self.plan: Plan | None = None

    def offer(self, *layers: Layer, revision=1, **kwargs) -> Plan:
        fields = dict(plan_id="stable", revision=revision, player_id="player1",
                      authority_epoch=self.configuration.authority_epoch,
                      issued_at=self.clock.utc(), valid_from=self.clock.utc(), valid_until=400,
                      bindings=self.configuration.bindings, layers=layers)
        fields.update(kwargs)
        self.plan = Plan(**fields)
        self.executor.accept_plan(self.plan)
        return self.plan

    def secure(self, key="picture", data=b"picture") -> None:
        assert self.executor.acquire(key, [data], **served(data))

    def commit(self, *keys: str) -> Commit:
        assert self.plan
        command = Commit(plan_id=self.plan.plan_id, revision=self.plan.revision,
                         authority_epoch=self.plan.authority_epoch, assignment_ids=keys,
                         readiness_sequence=self.executor.readiness().sequence,
                         committed_at=self.clock.utc())
        self.executor.accept_commit(command)
        return command

    def play(self, *layers: Layer, payloads: dict[str, bytes] | None = None):
        self.offer(*layers)
        for item in layers:
            if item.variant:
                self.secure(item.assignment_id, (payloads or {}).get(item.assignment_id, b"picture"))
        self.executor.prepare_imminent()
        self.commit(*(item.assignment_id for item in layers))
        return self.executor.tick()

    def sync_clock(self) -> None:
        """One instant round trip to Central."""
        monotonic = self.clock.monotonic()
        self.show_clock.sample(server_time=self.clock.utc(), sent_monotonic=monotonic,
                               received_monotonic=monotonic)

    def advance(self, amount: float, *, refresh=True) -> None:
        self.clock.advance(amount)
        if refresh:
            self.sync_clock()


def test_two_outputs_secured_is_not_prepared_committed_or_observed(tmp_path):
    rig = Rig(tmp_path)
    first, second = layer(), layer("second", output="hdmi2", frame="frame2", data=b"second")
    rig.offer(first, second)
    rig.secure()
    rig.secure("second", b"second")
    readiness = rig.executor.readiness()
    assert readiness.secured == ("picture", "second")
    assert readiness.prepared == ()
    assert rig.cache.stats()["pinned_bytes"] == len(b"picturesecond")
    assert rig.renderer.outputs == {}
    with pytest.raises(AuthorityError):
        rig.commit("picture", "second")
    rig.executor.prepare_imminent()
    readiness2 = rig.executor.readiness()
    assert readiness2.sequence > readiness.sequence
    assert readiness2.prepared == ("picture", "second")
    rig.commit("picture", "second")
    assert rig.renderer.outputs == {}
    observations = rig.executor.tick()
    assert {o.assignment_id for o in observations if o.status == "presented"} == {"picture", "second"}
    assert rig.renderer.outputs["hdmi1"].layers[0].layer == first
    assert rig.renderer.outputs["hdmi2"].layers[0].layer == second
    assert rig.renderer.capacity(tuple(rig.renderer.outputs.values())).qualified is False


def test_foreign_player_stale_epoch_and_changed_plan_identity_rejected(tmp_path):
    rig = Rig(tmp_path)
    offered = rig.offer(layer())
    with pytest.raises(AuthorityError):
        rig.executor.accept_configuration(rig.configuration.model_copy(update={"player_id": "player2"}))
    with pytest.raises(AuthorityError):
        rig.executor.accept_plan(offered.model_copy(update={"player_id": "player2", "revision": 2}))
    with pytest.raises(AuthorityError):
        rig.executor.accept_plan(offered.model_copy(update={"plan_id": "other", "revision": 2}))
    with pytest.raises(AuthorityError):
        rig.executor.accept_plan(offered.model_copy(update={"authority_epoch": 2, "revision": 2}))
    assert rig.executor.accept_plan(offered) is False
    assert rig.executor.accept_configuration(rig.configuration) is False


def test_future_gate_only_prerolls_five_seconds_ahead(tmp_path):
    rig = Rig(tmp_path)
    rig.offer(layer(start=110, media_origin=110))
    rig.secure()
    rig.executor.prepare_imminent()
    assert rig.executor.readiness().prepared == ()
    rig.advance(5)
    rig.executor.prepare_imminent()
    assert rig.executor.readiness().prepared == ("picture",)
    rig.commit("picture")
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].fallback
    rig.advance(5)
    assert any(o.status == "presented" for o in rig.executor.tick())


def test_async_prepare_and_whole_player_capacity_are_independent(tmp_path):
    rig = Rig(tmp_path, capacity=1)
    rig.offer(layer(), layer("second", output="hdmi2", frame="frame2"))
    rig.secure()
    rig.secure("second")
    rig.renderer.pending.add("picture")
    rig.executor.prepare_imminent()
    readiness = rig.executor.readiness()
    assert not readiness.capacity_ok
    assert readiness.prepared == ()
    assert set(readiness.secured) == {"picture", "second"}
    assert {f.code for f in readiness.failures} == {"capacity"}
    rig.renderer.pending.clear()
    rig.renderer.limit = 4
    rig.executor.prepare_imminent()
    assert rig.executor.readiness().prepared == ("picture", "second")
    rig.commit("picture", "second")


def test_stale_revision_and_duplicate_commit_cannot_restore_lost_readiness(tmp_path):
    rig = Rig(tmp_path)
    offered = rig.offer(layer())
    rig.secure()
    rig.executor.prepare_imminent()
    commit = rig.commit("picture")
    assert rig.executor.accept_commit(commit) is False
    rig.renderer.prepare_failures.add("picture")
    rig.executor.prepare_imminent()
    with pytest.raises(AuthorityError):
        rig.executor.accept_commit(commit)
    assert rig.executor.readiness().failures[0].code == "decode"
    rig.offer(layer(), revision=2)
    with pytest.raises(AuthorityError):
        rig.executor.accept_plan(offered)
    with pytest.raises(AuthorityError):
        rig.executor.accept_commit(commit)


def test_secured_cycle_identity_cannot_be_rerolled_but_priority_can_change(tmp_path):
    rig = Rig(tmp_path)
    original = layer()
    rig.offer(original)
    rig.secure()
    with pytest.raises(AuthorityError, match="immutable"):
        rig.offer(layer(data=b"different"), revision=2)
    rig.offer(original.model_copy(update={"priority": 50, "root_order": 4}), revision=2)
    assert rig.executor.readiness().secured == ("picture",)


@pytest.mark.parametrize("failure_phase", ["prepare", "present"])
def test_recoverable_replacement_failure_retains_valid_still(tmp_path, failure_phase):
    rig = Rig(tmp_path)
    old = layer(after_end="keep_this_photo")
    rig.play(old)
    replacement = layer("replacement", data=b"new", start=102)
    rig.advance(2)
    rig.offer(old, replacement, revision=2)
    rig.secure("replacement", b"new")
    if failure_phase == "prepare":
        rig.renderer.prepare_failures.add("replacement")
    else:
        rig.renderer.presentation_failures.add("replacement")
    rig.executor.prepare_imminent()
    rig.commit("picture")
    if failure_phase == "present":
        rig.commit("replacement")
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].layers[0].layer.assignment_id == "picture"
    assert not rig.renderer.outputs["hdmi1"].fallback


def test_cover_pause_and_reveal_seeks_to_current_position_32(tmp_path):
    rig = Rig(tmp_path)
    video = layer("video", video=True)
    overlay = layer("cover", presentation="black", variant=None, start=112, end=132, priority=10)
    rig.offer(video, overlay)
    rig.secure("video")
    rig.executor.prepare_imminent()
    rig.commit("video")
    rig.executor.tick()
    rig.advance(12)
    rig.executor.prepare_imminent()
    rig.commit("cover")
    observations = rig.executor.tick()
    assert [o.assignment_id for o in observations if o.status == "presented"] == ["cover"]
    rig.advance(20)
    observations = rig.executor.tick()
    assert [(o.assignment_id, o.position) for o in observations if o.status == "presented"] == [
        ("video", 32)
    ]
    video_preparations = [p for p in rig.renderer.preparations if p.layer.assignment_id == "video"]
    assert video_preparations[-1].position == 32


def test_original_fade_interval_survives_rolling_plan_and_black_is_not_transparency(tmp_path):
    rig = Rig(tmp_path)
    background = layer()
    cover = layer("cover", presentation="black", variant=None, priority=10, fade_in=10, end=120)
    rig.play(background, cover)
    composition = rig.renderer.outputs["hdmi1"]
    assert composition.layers[-1].layer.presentation == "black"
    assert composition.layers[-1].alpha == 0
    rig.advance(5)
    rig.offer(background, cover, revision=2)
    rig.executor.prepare_imminent()
    rig.commit("picture", "cover")
    observations = rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].layers[-1].alpha == .5
    rig.advance(5)
    observations += rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].layers[-1].alpha == 1
    # Seen from its first visible draw, reported once (`Executor._report`).
    assert [o.assignment_id for o in observations if o.status == "presented"] == ["cover"]


def test_overlay_and_video_expire_to_retained_still_in_warm_outage(tmp_path):
    rig = Rig(tmp_path)
    old = layer(after_end="keep_this_photo", end=101)
    rig.play(old)
    rig.executor.maintain_cache()
    rig.advance(1)
    video = layer("video", video=True, start=101, end=106, media_origin=101)
    overlay = layer("overlay", start=101, end=104, data=b"overlay", priority=10)
    rig.offer(video, overlay, revision=2, valid_until=106)
    rig.secure("video")
    rig.secure("overlay", b"overlay")
    rig.executor.prepare_imminent()
    rig.commit("video", "overlay")
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].layers[-1].layer.assignment_id == "overlay"
    rig.advance(100, refresh=False)
    observations = rig.executor.tick()
    composition = rig.renderer.outputs["hdmi1"]
    assert composition.fallback
    assert [local.layer.assignment_id for local in composition.layers] == ["picture"]
    assert all(o.status == "fallback" for o in observations)
    rig.executor.maintain_cache()
    assert rig.cache.stats()["pinned_bytes"] == len(b"picture")


def test_no_successful_still_means_black_beyond_horizon(tmp_path):
    rig = Rig(tmp_path)
    rig.play(layer(video=True, end=105))
    rig.advance(6)
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].fallback
    assert rig.renderer.outputs["hdmi1"].layers == ()


def loosen(rig: Rig, bound: float) -> None:
    """Central answered once, slowly: the ShowClock holds Central's time to within `bound`."""
    rig.show_clock._anchor = replace(rig.show_clock._anchor, bound=bound)


def test_a_loose_clock_starts_a_granted_layer_and_never_reports_clock(tmp_path):
    """Show time is a quality, never a gate (decision 0032): a ShowClock anchored to within 50 s
    still starts and presents a granted layer, reports no `clock` failure (its bound reported)
    and accepts a Commit. Mutation: re-add `uncertainty > 0.1` to the start condition in `tick`
    -> nothing is drawn."""
    rig = Rig(tmp_path)
    rig.offer(layer())
    rig.secure()
    rig.executor.prepare_imminent()
    loosen(rig, 50.0)
    readiness = rig.executor.readiness()
    assert readiness.failures == () and readiness.prepared == ("picture",)
    assert readiness.clock_uncertainty == pytest.approx(50.0)
    rig.commit("picture")
    observations = rig.executor.tick()
    assert [local.layer.assignment_id for local in rig.renderer.outputs["hdmi1"].layers] == ["picture"]
    assert [o.status for o in observations] == ["presented"]
    rig.advance(30, refresh=False)
    rig.executor.tick()
    assert [local.layer.assignment_id for local in rig.renderer.outputs["hdmi1"].layers] == ["picture"]


def test_a_commit_is_refused_on_capacity_alone(tmp_path):
    """Mutation probe: drop the gate in `accept_commit` -> a grant taken without capacity stands."""
    rig = Rig(tmp_path)
    rig.offer(layer())
    rig.secure()
    rig.executor.prepare_imminent()
    readiness = rig.executor.readiness()
    rig.executor._capacity_ok = False
    with pytest.raises(AuthorityError, match="^commit capacity gate failed$"):
        rig.executor.accept_commit(Commit(plan_id=rig.plan.plan_id, revision=1, authority_epoch=1,
                                          readiness_sequence=readiness.sequence, assignment_ids=("picture",),
                                          committed_at=rig.clock.utc()))


def test_cache_pressure_and_corruption_revoke_precommit_readiness(tmp_path):
    rig = Rig(tmp_path, cache_bytes=7)
    rig.offer(layer(), layer("new", data=b"new"))
    rig.secure()
    assert not rig.executor.acquire("new", [b"new"], **served(b"new"))
    assert rig.cache.stats()["pinned_bytes"] == 7
    rig.executor.prepare_imminent()
    assert {f.code for f in rig.executor.failures} == {"capacity"}
    path = rig.cache.path_for(media())
    assert path
    path.write_bytes(b"damaged")
    rig.executor.verify_secured()
    assert "picture" not in rig.executor.readiness().secured
    assert {f.code for f in rig.executor.failures} == {"capacity", "integrity"}
    with pytest.raises(AuthorityError):
        rig.commit("picture")


def test_download_errors_are_immutable_sanitized_failure_records(tmp_path):
    rig = Rig(tmp_path)
    rig.offer(layer())

    def broken():
        yield b"pi"
        raise RuntimeError("private upstream details never cross protocol")

    assert rig.executor.acquire("picture", broken(), **served()) is False
    failures = rig.executor.failures
    assert isinstance(failures, tuple)
    assert failures[0].code == "download"
    assert "private" not in str(failures)
    with pytest.raises(Exception):
        failures[0].code = "changed"


def test_cancellation_during_blocked_stream_neither_blocks_tick_nor_resurrects(tmp_path):
    rig = Rig(tmp_path)
    offered = rig.offer(layer())
    entered, resume = Event(), Event()

    def chunks():
        yield b"pi"
        entered.set()
        assert resume.wait(3)
        yield b"cture"

    with ThreadPoolExecutor(max_workers=2) as pool:
        acquisition = pool.submit(rig.executor.acquire, "picture", chunks(), **served())
        assert entered.wait(3)
        control = pool.submit(lambda: (rig.executor.cancel(["picture"]), rig.executor.tick()))
        control.result(timeout=1)
        assert rig.executor.accept_plan(offered) is False
        resume.set()
        assert acquisition.result(timeout=3) is False
    rig.executor.maintain_cache()
    assert rig.cache.stats()["pinned_bytes"] == 0
    assert rig.executor.readiness().secured == ()
    rig.offer(layer(), revision=2)
    assert rig.executor.readiness().secured == ()


def test_rebinding_and_disabled_configuration_remove_old_retained_authority(tmp_path):
    rig = Rig(tmp_path)
    rig.play(layer(after_end="keep_this_photo"))
    replacement = binding().model_copy(update={"generation": 2, "configuration_revision": 2})
    config = rig.configuration.model_copy(update={
        "configuration_revision": 2, "bindings": (replacement,), "enabled_outputs": (),
    })
    rig.executor.accept_configuration(config)
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].layers == ()
    assert rig.renderer.outputs["hdmi2"].fallback
    with pytest.raises(AuthorityError):
        rig.executor.accept_configuration(rig.configuration.model_copy(update={"configuration_revision": 3}))
    with pytest.raises(AuthorityError):
        rig.executor.accept_plan(rig.plan.model_copy(update={"revision": 2}))


def test_restart_restores_no_authority_or_pins_and_rejects_old_epoch(tmp_path):
    rig = Rig(tmp_path)
    rig.play(layer(after_end="keep_this_photo"))
    rig.executor.maintain_cache()
    rig.cache.close()
    cache = Cache(tmp_path / "cache", 4096)
    restarted_renderer = RecordingRenderer()
    restarted = Executor("player1", cache, restarted_renderer, rig.clock, rig.show_clock)
    restarted.maintain_cache()
    assert cache.stats()["pinned_bytes"] == 0
    assert restarted.tick() == ()
    assert restarted_renderer.outputs == {}
    new = rig.configuration.model_copy(update={"authority_epoch": 2})
    restarted.accept_configuration(new)
    with pytest.raises(AuthorityError):
        restarted.accept_plan(rig.plan)
    with pytest.raises(AuthorityError):
        restarted.accept_commit(rig.commit("picture"))
    restarted.tick()
    assert restarted_renderer.outputs["hdmi1"].fallback
    assert restarted_renderer.outputs["hdmi1"].layers == ()


def test_new_authority_epoch_rejects_old_commits_and_clears_old_output(tmp_path):
    rig = Rig(tmp_path)
    rig.offer(layer(after_end="keep_this_photo"))
    rig.secure()
    rig.executor.prepare_imminent()
    old_commit = rig.commit("picture")
    rig.executor.tick()
    rig.executor.accept_configuration(rig.configuration.model_copy(update={"authority_epoch": 2}))
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].layers == ()
    with pytest.raises(AuthorityError):
        rig.executor.accept_commit(old_commit)


def test_rendering_methods_do_not_touch_cache_or_hash_files(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    rig.offer(layer(after_end="keep_this_photo"))
    rig.secure()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("cache operation on rendering path")

    for name in ("path_for", "pin", "release", "secure", "stats"):
        monkeypatch.setattr(rig.cache, name, forbidden)
    rig.executor.prepare_imminent()
    rig.commit("picture")
    rig.executor.tick()
    rig.advance(101)
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].fallback


def test_revoked_commit_cannot_replay_after_preparation_recovers(tmp_path):
    rig = Rig(tmp_path)
    rig.offer(layer(start=103, media_origin=103))
    rig.secure()
    rig.executor.prepare_imminent()
    old = rig.commit("picture")
    rig.renderer.prepare_failures.add("picture")
    rig.executor.prepare_imminent()
    rig.renderer.prepare_failures.clear()
    rig.advance(1)
    rig.executor.prepare_imminent()
    with pytest.raises(AuthorityError, match="revoked"):
        rig.executor.accept_commit(old)
    rig.advance(2)
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].fallback
    rig.commit("picture")
    assert any(o.status == "presented" for o in rig.executor.tick())


def test_a_grant_dropped_locally_is_reported_lost_in_every_report_until_regranted(tmp_path):
    rig = Rig(tmp_path)
    rig.offer(layer(start=103, media_origin=103))
    rig.secure()
    rig.executor.prepare_imminent()
    granted = rig.commit("picture")
    assert rig.executor.readiness().lost_grants == ()
    rig.renderer.prepare_failures.add("picture")
    rig.executor.prepare_imminent()
    rig.renderer.prepare_failures.clear()
    rig.advance(1)
    rig.executor.prepare_imminent()
    lost = (LostGrant(assignment_id="picture", readiness_sequence=granted.readiness_sequence),)
    assert rig.executor.readiness().lost_grants == lost
    assert rig.executor.readiness().lost_grants == lost
    regranted = rig.commit("picture")
    assert rig.executor.readiness().lost_grants == ()
    # Central re-sends the grant the Player holds, or the earlier one without its re-granted
    # member: neither adds anything, and neither is refused.
    assert rig.executor.accept_commit(regranted) is False
    assert rig.executor.accept_commit(granted) is False


def test_a_refused_grant_is_reported_lost_with_its_own_sequence(tmp_path):
    rig = Rig(tmp_path)
    rig.offer(layer(start=103, media_origin=103))
    rig.secure()
    rig.executor.prepare_imminent()
    ready = rig.executor.readiness()
    for _ in range(128):              # more changes than the Player's report window holds
        rig.executor._readiness_state = None    # (readiness is numbered once per change)
        rig.executor.readiness()
    late = Commit(plan_id="stable", revision=1, authority_epoch=1, assignment_ids=("picture",),
                  readiness_sequence=ready.sequence, committed_at=rig.clock.utc())
    with pytest.raises(AuthorityError, match="revoked or unknown"):
        rig.executor.accept_commit(late)
    assert rig.executor.readiness().lost_grants == (
        LostGrant(assignment_id="picture", readiness_sequence=ready.sequence),)
    rig.commit("picture")
    assert rig.executor.readiness().lost_grants == ()


def test_a_grant_revoked_by_central_or_replaced_by_a_new_revision_is_never_lost(tmp_path):
    """Only a grant the Player dropped is lost: one Central revoked, or one a new plan revision
    replaced, needs a fresh grant, never a re-grant of the old one."""
    rig = Rig(tmp_path)
    rig.offer(layer(start=103, media_origin=103))
    rig.secure()
    rig.executor.prepare_imminent()
    rig.commit("picture")
    rig.executor.invalidate(["picture"])
    rig.executor.prepare_imminent()
    revoked = rig.executor.readiness()
    assert (revoked.prepared, revoked.lost_grants) == (("picture",), ())
    rig.commit("picture")
    rig.offer(layer(start=103, media_origin=103), revision=2)
    rig.executor.prepare_imminent()
    replaced = rig.executor.readiness()
    assert (replaced.prepared, replaced.lost_grants) == (("picture",), ())


def test_nonterminal_invalidation_preserves_bytes_requires_fresh_grant(tmp_path):
    rig = Rig(tmp_path)
    rig.offer(layer())
    rig.secure()
    rig.executor.prepare_imminent()
    old = rig.commit("picture")
    rig.executor.invalidate(["picture"])
    assert rig.executor.readiness().secured == ("picture",)
    assert rig.executor.readiness().prepared == ()
    rig.executor.prepare_imminent()
    with pytest.raises(AuthorityError, match="revoked"):
        rig.executor.accept_commit(old)
    rig.commit("picture")
    rig.executor.tick()
    assert not rig.renderer.outputs["hdmi1"].fallback


def test_missed_start_rejects_delayed_old_grant_accepts_current_late_join(tmp_path):
    rig = Rig(tmp_path)
    rig.offer(layer(start=103, media_origin=103))
    rig.secure()
    rig.executor.prepare_imminent()
    ready = rig.executor.readiness()
    old = Commit(plan_id="stable", revision=1, authority_epoch=1, assignment_ids=("picture",),
                 readiness_sequence=ready.sequence, committed_at=100)
    rig.advance(10)
    with pytest.raises(AuthorityError, match="late-join"):
        rig.executor.accept_commit(old)
    rig.commit("picture")
    observation = next(o for o in rig.executor.tick() if o.status == "presented")
    assert observation.position == 7


def test_rolling_revision_preserves_original_execution_order_and_lease(tmp_path):
    rig = Rig(tmp_path)
    picture = layer(end=110)
    black = layer("black", presentation="black", variant=None, priority=10, end=110)
    rig.offer(picture, black, valid_until=110)
    rig.secure()
    rig.executor.prepare_imminent()
    rig.commit("picture", "black")
    observations = rig.executor.tick()
    assert [o.assignment_id for o in observations if o.status == "presented"] == ["black"]
    rig.advance(1)
    changed_black = black.model_copy(update={"priority": -10})
    rig.offer(picture, changed_black, revision=2, valid_until=400)
    # A new revision is no change of what the Frame shows: nothing is reported again.
    assert not [o for o in rig.executor.tick() if o.status == "presented"]
    assert rig.renderer.outputs["hdmi1"].layers[-1].layer.priority == 10
    rig.advance(10)
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].fallback
    assert rig.renderer.outputs["hdmi1"].layers == ()


def test_a_rolling_revision_before_start_keeps_the_granted_start(tmp_path):
    """A Commit is Central's grant to start that content at its time. A newer revision that
    keeps the content arrives with no Commit of its own (Central grants one only after the
    Player reports readiness for it), so voiding the grant would leave the Frame on fallback
    at the start until the fresh Commit lands. The grant starts on time, in the order Central
    committed, and the fresh Commit renews it."""
    rig = Rig(tmp_path)
    upcoming = layer("next", start=103, media_origin=103, data=b"next", priority=10)
    rig.offer(upcoming)
    rig.secure("next", b"next")
    rig.executor.prepare_imminent()
    rig.commit("next")
    rig.advance(2.9)
    rig.offer(upcoming.model_copy(update={"priority": -10}), revision=2)
    rig.executor.prepare_imminent()
    rig.advance(.2)
    observations = rig.executor.tick()
    assert [o.assignment_id for o in observations if o.status == "presented"] == ["next"]
    assert not rig.renderer.outputs["hdmi1"].fallback
    assert rig.renderer.outputs["hdmi1"].layers[-1].layer.priority == 10
    rig.executor.prepare_imminent()
    rig.commit("next")
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].layers[-1].layer.priority == -10


def test_a_carried_grant_ends_as_a_commit_does(tmp_path):
    """The carried grant is the Commit itself, not a new authority: Central's revocation ends it
    exactly as it ends a Commit of the current revision."""
    rig = Rig(tmp_path)
    upcoming = layer("next", start=103, media_origin=103, data=b"next")
    rig.offer(upcoming)
    rig.secure("next", b"next")
    rig.executor.prepare_imminent()
    rig.commit("next")
    rig.advance(1)
    rig.offer(upcoming, revision=2)
    rig.executor.invalidate(["next"])
    rig.advance(2.1)
    rig.executor.prepare_imminent()
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].fallback


def test_omitted_secured_assignment_keeps_lease_and_immutable_identity(tmp_path):
    rig = Rig(tmp_path)
    rig.offer(layer(end=110), valid_until=110)
    rig.secure()
    rig.offer(revision=2)
    rig.executor.maintain_cache()
    assert rig.executor.readiness().secured == ()
    assert rig.cache.stats()["pinned_bytes"] == len(b"picture")
    with pytest.raises(AuthorityError, match="immutable"):
        rig.offer(layer(data=b"other", end=110), revision=3)
    rig.advance(11)
    rig.executor.maintain_cache()
    assert rig.cache.stats()["pinned_bytes"] == 0


@pytest.mark.parametrize("failure", ["pending", "prepare", "present"])
def test_unavailable_retained_fallback_cannot_leave_expired_video_visible(tmp_path, failure):
    rig = Rig(tmp_path)
    rig.play(layer(after_end="keep_this_photo", end=101))
    rig.executor.maintain_cache()
    rig.advance(1)
    rig.offer(layer("video", video=True, start=101, end=103), revision=2)
    rig.secure("video")
    rig.executor.prepare_imminent()
    rig.commit("video")
    rig.executor.tick()
    if failure == "pending":
        rig.renderer.pending.add("picture")
    elif failure == "prepare":
        rig.renderer.prepare_failures.add("picture")
    else:
        rig.renderer.presentation_failures.add("picture")
    rig.advance(3)
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].fallback
    assert rig.renderer.outputs["hdmi1"].layers == ()


def test_retention_created_during_worker_pass_keeps_original_pin(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    video = layer("video", video=True, end=102)
    dummy = layer("dummy", output="hdmi2", frame="frame2", data=b"dummy", after_end="keep_this_photo")
    picture = layer(start=101, end=102, priority=10, after_end="keep_this_photo")
    rig.play(video, dummy, picture, payloads={"dummy": b"dummy"})
    entered, resume = Event(), Event()
    original_pin = rig.cache.pin

    def paused_pin(variant, owner):
        if variant == media(b"dummy"):
            entered.set()
            assert resume.wait(3)
        return original_pin(variant, owner)

    monkeypatch.setattr(rig.cache, "pin", paused_pin)
    with ThreadPoolExecutor(max_workers=1) as pool:
        maintenance = pool.submit(rig.executor.maintain_cache)
        assert entered.wait(3)
        rig.advance(1)
        rig.executor.tick()
        rig.advance(2)
        resume.set()
        maintenance.result(timeout=3)
    assert any(cached(media()) in keys for keys in rig.cache._pins.values())
    monkeypatch.setattr(rig.cache, "pin", original_pin)
    rig.executor.maintain_cache()
    assert any(owner.startswith("pwretain:") and cached(media()) in keys
               for owner, keys in rig.cache._pins.items())


def test_expired_sequential_decode_resources_are_released(tmp_path):
    rig = Rig(tmp_path)
    clips = [layer(f"v{index}", video=True, start=100 + 10 * index,
                   end=110 + 10 * index, media_origin=100 + 10 * index) for index in range(5)]
    rig.offer(*clips)
    for item in clips:
        rig.secure(item.assignment_id)
    for index in range(5):
        rig.executor.prepare_imminent()
        assert rig.executor.readiness().capacity_ok
        rig.commit(f"v{index}")
        rig.executor.tick()
        rig.executor.maintain_cache()
        assert len(rig.renderer.resident) <= 2
        rig.advance(10)
    rig.executor.tick()
    assert not rig.renderer.resident


def test_capacity_loss_at_intended_start_skips_previously_committed_work(tmp_path):
    rig = Rig(tmp_path)
    rig.offer(layer(start=103))
    rig.secure()
    rig.executor.prepare_imminent()
    rig.commit("picture")
    rig.renderer.limit = 0
    rig.advance(3)
    observations = rig.executor.tick()
    assert any(o.status == "failed" and o.detail == "capacity" for o in observations)
    assert rig.renderer.outputs["hdmi1"].fallback
    rig.renderer.limit = 4
    rig.executor.prepare_imminent()
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].fallback
    rig.commit("picture")
    rig.executor.tick()
    assert not rig.renderer.outputs["hdmi1"].fallback


def test_executor_never_writes_an_authority_journal(tmp_path):
    rig = Rig(tmp_path)
    rig.offer(layer())
    assert rig.executor.acquire("picture", [b"picture"], **served())
    rig.offer(layer(), revision=2)
    assert {path.name for path in tmp_path.iterdir()} == {"cache"}
    assert not list(tmp_path.glob(".executor-*"))


def test_plan_omission_revokes_execution_but_preserves_secured_lease(tmp_path):
    rig = Rig(tmp_path)
    rig.play(layer())
    rig.offer(revision=2)
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].fallback
    assert rig.renderer.outputs["hdmi1"].layers == ()
    rig.executor.maintain_cache()
    assert rig.cache.stats()["pinned_bytes"] == len(b"picture")
    rig.advance(101)
    rig.executor.maintain_cache()
    assert rig.cache.stats()["pinned_bytes"] == 0


def test_removed_overlay_reveals_current_video_position_before_original_expiry(tmp_path):
    rig = Rig(tmp_path)
    video = layer("video", video=True)
    overlay = layer("cover", presentation="black", variant=None, start=112, end=150, priority=10)
    rig.offer(video, overlay)
    rig.secure("video")
    rig.executor.prepare_imminent()
    rig.commit("video")
    rig.executor.tick()
    rig.advance(12)
    rig.executor.prepare_imminent()
    rig.commit("cover")
    rig.executor.tick()
    rig.advance(20)
    rig.offer(video, revision=2)
    observations = rig.executor.tick()
    assert [(o.assignment_id, o.position) for o in observations if o.status == "presented"] == [("video", 32)]
    assert [p for p in rig.renderer.preparations if p.layer.assignment_id == "video"][-1].position == 32


def test_first_commissioning_without_plan_renders_empty_operational_fallback(tmp_path):
    rig = Rig(tmp_path)
    rig.configuration = rig.configuration.model_copy(update={
        "configuration_revision": 2, "enabled_outputs": (),
    })
    rig.executor.accept_configuration(rig.configuration)
    assert rig.plan is None
    assert rig.executor.tick() == ()
    assert rig.renderer.outputs["hdmi1"].fallback
    assert rig.renderer.outputs["hdmi1"].layers == ()
    with pytest.raises(AuthorityError, match="no current plan"):
        rig.executor.readiness()


def test_a_photo_dissolving_in_is_drawn_over_the_one_it_replaces_whatever_the_plan_order(tmp_path):
    """The executor stacks by contracts `stack_order` itself (priority, root order, admission
    order, then start), not by the order the plan lists its layers: two photos of one Run that
    overlap (a cross-fade) arrive with the later one listed first, and the later one is drawn on
    top, at half strength halfway through its fade-in.

    Mutation probe: sort by priority only (the old key) -> the earlier photo is drawn on top."""
    rig = Rig(tmp_path)
    current = layer("current", start=100, end=110)
    following = layer("following", data=b"following", start=104, end=120, media_origin=104, fade_in=3)
    rig.offer(following, current)
    rig.secure("current")
    rig.secure("following", b"following")
    rig.executor.prepare_imminent()
    rig.commit("current", "following")
    rig.advance(5.5)
    rig.executor.prepare_imminent()
    rig.executor.tick()
    drawn = rig.renderer.outputs["hdmi1"].layers
    assert [local.layer.assignment_id for local in drawn] == ["current", "following"]
    assert drawn[-1].alpha == pytest.approx(.5)


def test_a_cover_starting_before_its_runs_next_layer_is_drawn_above_it(tmp_path):
    """Decision 0036 (slice 3): a Skip's cover (an action layer) is drawn above a regular layer of
    its Run that starts after it, and an action layer's flag is content identity: a revision that
    drops it from a secured layer is refused. Mutation probes: leave `action` out of
    `stack_order`; leave it out of `_content_identity`."""
    rig = Rig(tmp_path)
    cover = layer("cover", start=100, end=120, action=True)
    following = layer("following", data=b"following", start=104, end=140, media_origin=104)
    rig.offer(following, cover)
    rig.secure("cover")
    rig.secure("following", b"following")
    rig.executor.prepare_imminent()
    rig.commit("cover", "following")
    rig.advance(6)
    rig.executor.prepare_imminent()
    rig.executor.tick()
    drawn = rig.renderer.outputs["hdmi1"].layers
    assert [local.layer.assignment_id for local in drawn][-1] == "cover"
    with pytest.raises(AuthorityError, match="immutable"):
        rig.offer(following, cover.model_copy(update={"action": False}), revision=2)


def black(rig):
    return {report.output_id: report.cause for report in rig.executor.frames_black()}


def test_frames_black_no_current_plan(tmp_path):
    rig = Rig(tmp_path)
    assert black(rig) == {"hdmi1": FrameBlackCause.NO_PLAN, "hdmi2": FrameBlackCause.NO_PLAN}


def test_frames_black_an_expired_plan(tmp_path):
    rig = Rig(tmp_path)
    rig.offer(layer(end=120), valid_until=120)
    rig.clock.advance(30)
    rig.sync_clock()
    assert black(rig)["hdmi1"] is FrameBlackCause.NO_PLAN


def test_frames_black_only_for_enabled_bindings_and_sorted(tmp_path):
    rig = Rig(tmp_path)
    assert [report.output_id for report in rig.executor.frames_black()] == ["hdmi1", "hdmi2"]
    rig.executor.accept_configuration(rig.configuration.model_copy(update={
        "configuration_revision": 2, "enabled_outputs": ("hdmi2",)}))
    assert [report.output_id for report in rig.executor.frames_black()] == ["hdmi2"]


def test_frames_black_a_due_photo_walks_its_causes(tmp_path):
    rig = Rig(tmp_path)
    rig.offer(layer())
    assert black(rig)["hdmi1"] is FrameBlackCause.NO_PHOTO
    rig.secure()
    assert black(rig)["hdmi1"] is FrameBlackCause.CANNOT_DRAW    # secured, not prepared
    rig.executor.prepare_imminent()
    assert black(rig)["hdmi1"] is FrameBlackCause.NOT_STARTED
    rig.commit("picture")
    assert black(rig)["hdmi1"] is FrameBlackCause.CANNOT_DRAW    # granted, nothing drawn yet
    rig.executor.tick()
    assert "hdmi1" not in black(rig)


def test_frames_black_a_deferred_layer_is_ready_to_start(tmp_path):
    """A layer deferred by the decoder budget is prepared as far as a grant goes. Mutation probe:
    read only `prepared` -> it is called undrawable."""
    rig = Rig(tmp_path)
    rig.offer(layer())
    rig.secure()
    rig.executor._assignments["picture"].deferred = True
    assert black(rig)["hdmi1"] is FrameBlackCause.NOT_STARTED


def test_frames_black_a_layer_executing_under_its_lease_is_started(tmp_path):
    """Its grant has gone, its execution lease has not, and nothing is drawn: a draw failure, not
    a wait for Central. Mutation probe: read only the grant."""
    rig = Rig(tmp_path)
    rig.play(layer())
    executing = rig.executor._assignments["picture"]
    executing.granted_layer = None
    rig.executor._current.clear()
    assert black(rig)["hdmi1"] is FrameBlackCause.CANNOT_DRAW


def test_frames_black_a_layer_and_a_span_not_yet_begun_are_silent(tmp_path):
    """Mutation probe: drop the start of `_in_force` -> a future photo blacks the Frame now."""
    rig = Rig(tmp_path)
    rig.offer(layer(start=300, end=350), unfilled=(UnfilledSpan(output_id="hdmi2", start=300, end=350),))
    assert black(rig) == {}


def test_frames_black_a_failed_download_and_a_failed_decode(tmp_path):
    rig = Rig(tmp_path)
    rig.offer(layer())
    assert not rig.executor.acquire("picture", [b"wrong"], sha256="0" * 64, size=5)
    assert black(rig)["hdmi1"] is FrameBlackCause.NO_PHOTO
    rig.secure()
    rig.renderer.prepare_failures.add("picture")
    rig.executor.prepare_imminent()
    assert black(rig)["hdmi1"] is FrameBlackCause.CANNOT_DRAW


def test_frames_black_is_no_time_only_before_any_answer_never_for_a_loose_anchor(tmp_path):
    """Mutation: pass `uncertainty <= 0.1` as `clock_ok` -> a loose anchor reads as NO_TIME."""
    rig = Rig(tmp_path)
    rig.offer(layer())
    loosen(rig, 50.0)
    assert black(rig)["hdmi1"] is FrameBlackCause.NO_PHOTO
    rig.show_clock._anchor = None
    assert black(rig)["hdmi1"] is FrameBlackCause.NO_TIME


def test_frames_black_a_clock_never_anchored_counts_every_layer(tmp_path):
    rig = Rig(tmp_path)
    rig.offer(layer(start=300, end=350))
    rig.offer(layer(start=300, end=350), revision=2, unfilled=(UnfilledSpan(output_id="hdmi2", start=300, end=350),))
    rig.show_clock._anchor = None
    assert black(rig) == {"hdmi1": FrameBlackCause.NO_TIME, "hdmi2": FrameBlackCause.NO_CONTENT}


def test_frames_black_a_black_layer_kept_photo_and_pattern_draw_no_card(tmp_path):
    rig = Rig(tmp_path)
    rig.play(layer("dark", presentation="black", variant=None))
    assert "hdmi1" not in black(rig)

    rig = Rig(tmp_path / "kept")
    rig.play(layer(end=140, after_end="keep_this_photo"))
    rig.clock.advance(50)
    rig.sync_clock()
    rig.executor.tick()
    assert "hdmi1" not in black(rig)

    rig = Rig(tmp_path / "pattern")
    rig.offer(layer())
    rig.renderer.patterns.add("hdmi1")
    assert "hdmi1" not in black(rig)


def test_frames_black_an_unfilled_span_is_no_content_only_while_it_lasts(tmp_path):
    rig = Rig(tmp_path)
    rig.offer(unfilled=(UnfilledSpan(output_id="hdmi1", start=100, end=150),))
    assert black(rig)["hdmi1"] is FrameBlackCause.NO_CONTENT and "hdmi2" not in black(rig)
    rig.clock.advance(60)
    rig.sync_clock()
    assert "hdmi1" not in black(rig)


def test_frames_black_with_no_configuration_is_no_report(tmp_path):
    rig = Rig(tmp_path)
    fresh = Executor("other", rig.cache, rig.renderer, rig.clock, rig.show_clock)
    assert fresh.frames_black() == ()
    assert isinstance(rig.executor.frames_black()[0], FrameBlack)


# Decision 0032: grants only for a change, continuations past the plan, and a rejoin with no black.

def test_a_later_turn_starts_at_its_instant_with_no_commit_and_a_first_turn_waits(tmp_path):
    """B9: a layer that awaits no grant starts at its instant once secured and prepared; one that
    awaits a grant waits for its Commit. Mutation probe: treat `awaits_grant` as always False."""
    rig = Rig(tmp_path)
    later = layer("later", awaits_grant=False)
    first = layer("first", output="hdmi2", frame="frame2", data=b"first")
    rig.offer(later, first)
    rig.secure("later")
    rig.secure("first", b"first")
    rig.executor.prepare_imminent()
    rig.executor.tick()
    assert [local.layer.assignment_id for local in rig.renderer.outputs["hdmi1"].layers] == [
        "later"]
    assert not rig.renderer.outputs["hdmi2"].layers
    rig.commit("first")
    rig.executor.tick()
    assert [local.layer.assignment_id for local in rig.renderer.outputs["hdmi2"].layers] == [
        "first"]


def _continued_plan(rig, *, valid_until=130.0, until=None):
    """Two photos at 10 s turns to 120, then the Run's continuation from 120 listing both."""
    photos = (layer("a", data=b"a", start=100, end=110, awaits_grant=False),
              layer("b", data=b"b", start=110, end=120, awaits_grant=False))
    continuation = Continuation(output_id="hdmi1", run_id="run", photos=("a", "b"), start=120,
                                seconds=10, until=until)
    rig.offer(*photos, valid_until=valid_until, continuations=(continuation,))
    return photos


def test_past_its_plan_a_frame_cycles_the_continuations_held_photos_with_no_card(tmp_path):
    """B10: past `valid_until` the executor plays continuation slots of the photos it holds, by
    their own spans, and states no black Frame. Mutation probe: cut continuation slots at
    `valid_until`."""
    rig = Rig(tmp_path)
    _continued_plan(rig)
    rig.secure("a", b"a")
    rig.secure("b", b"b")
    drawn = []
    for _ in range(100):
        rig.executor.prepare_imminent()
        rig.executor.tick()
        assert rig.executor.frames_black() == ()
        top = rig.renderer.outputs["hdmi1"].layers[-1].layer
        drawn.append((rig.clock.utc(), top.variant.asset_id, top.assignment_id))
        rig.advance(.5)
    past = [(at, asset) for at, asset, key in drawn if at >= 130]
    assert past and all(key.startswith(CONTINUATION_PREFIX) for at, _, key in drawn if at >= 120)
    turns = {at: asset for at, asset, _ in drawn if at in (120.0, 130.0, 140.0, 145.0)}
    assert turns == {120.0: media(b"a").asset_id, 130.0: media(b"b").asset_id,
                     140.0: media(b"a").asset_id, 145.0: media(b"a").asset_id}
    readiness = rig.executor.readiness()
    assert not any(key.startswith(CONTINUATION_PREFIX)
                   for key in (*readiness.secured, *readiness.prepared))


def test_continuing_since_is_set_at_the_first_slot_drawn_and_cleared_by_a_planned_layer(tmp_path):
    """C4: an Output states since when it plays Central's continuation (the Player line's fact):
    from the first continuation slot it draws, unchanged across later slots, cleared once a planned
    layer is drawn again; None on a Frame drawing nothing. Mutation probe: never clear it."""
    rig = Rig(tmp_path)
    _continued_plan(rig)
    rig.secure("a", b"a")
    rig.secure("b", b"b")
    frame, other = binding(), binding("hdmi2", "frame2")
    since = []
    for _ in range(50):                               # 100 to 125, past the layers' end at 120
        rig.executor.prepare_imminent()
        rig.executor.tick()
        since.append((rig.clock.utc(), rig.executor.continuing_since(frame)))
        rig.advance(.5)
    assert all(value is None for at, value in since if at < 120)
    assert all(value == pytest.approx(120) for at, value in since if at >= 120)
    assert rig.executor.continuing_since(other) is None
    planned = layer("c", data=b"c", start=125, end=200, awaits_grant=False)
    rig.offer(planned, revision=2)
    rig.secure("c", b"c")
    rig.executor.prepare_imminent()
    rig.executor.tick()
    assert [local.layer.assignment_id for local in rig.renderer.outputs["hdmi1"].layers] == ["c"]
    assert rig.executor.continuing_since(frame) is None


def test_a_continuation_skips_a_photo_the_player_does_not_hold(tmp_path):
    rig = Rig(tmp_path)
    _continued_plan(rig)
    rig.secure("a", b"a")
    rig.advance(25)
    for _ in range(3):
        rig.executor.prepare_imminent()
        rig.executor.tick()
        assert rig.renderer.outputs["hdmi1"].layers[-1].layer.variant == media(b"a")
        rig.advance(10)


def test_a_new_plan_whose_photo_is_not_secured_leaves_the_old_photo_drawn_and_carried(tmp_path):
    """B11: rejoining needs no event. The old photo stays drawn while the new plan's current
    layer cannot start, and is reported carried; once the new layer can start, it is drawn.
    Mutation probe: omit `carried`."""
    rig = Rig(tmp_path)
    old = layer("old", data=b"old", end=150, awaits_grant=False)
    rig.offer(old)
    rig.secure("old", b"old")
    rig.executor.prepare_imminent()
    rig.executor.tick()
    rig.advance(60)                                   # past the old layer's end
    new = layer("new", data=b"new", start=160, end=200, awaits_grant=False)
    rig.offer(new, revision=2)
    for _ in range(3):
        rig.executor.prepare_imminent()
        rig.executor.tick()
        shown = rig.renderer.outputs["hdmi1"]
        assert [local.layer.assignment_id for local in shown.layers] == ["old"]
        assert rig.executor.frames_black() == ()
        rig.advance(.5)
    assert rig.executor.readiness().carried == (
        CarriedPhoto(output_id="hdmi1", asset_id=media(b"old").asset_id),)
    rig.secure("new", b"new")
    rig.executor.prepare_imminent()
    rig.executor.tick()
    assert [local.layer.assignment_id for local in rig.renderer.outputs["hdmi1"].layers] == ["new"]
    assert rig.executor.readiness().carried == ()


def test_a_new_plan_with_nothing_on_the_frame_takes_the_after_state(tmp_path):
    rig = Rig(tmp_path)
    rig.play(layer())
    rig.offer(revision=2)
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].fallback


def test_re_delivering_the_held_plan_past_its_validity_is_a_no_op(tmp_path):
    """B13: the Player keeps its plan through an outage; hearing it again is no error. Mutation
    probe: restore the expiry check first."""
    rig = Rig(tmp_path)
    plan = rig.offer(layer(end=150), valid_until=150)
    rig.advance(100)
    assert rig.executor.accept_plan(plan) is False
    with pytest.raises(AuthorityError, match="expired plan"):
        rig.offer(layer(end=150), revision=2, valid_until=150, issued_at=100, valid_from=100)


def _slots(rig, output="hdmi1"):
    return [a for a in rig.executor._assignments.values()
            if a.source is not None and a.layer.output_id == output]


def test_continuation_slots_that_ended_are_dropped_through_a_long_outage(tmp_path):
    """Decision 0032: through a day-long outage the executor holds the plan's layers and the slots
    of its prepare lead, never every slot it ever made (each tick and prepare pass scans them).
    Mutation probe: never drop an ended slot."""
    rig = Rig(tmp_path)
    photos = _continued_plan(rig)
    rig.secure("a", b"a")
    rig.secure("b", b"b")
    for _ in range(800):                              # 2000 s: 200 turns past the plan's end
        rig.executor.prepare_imminent()
        rig.executor.tick()
        rig.advance(2.5)
    # The plan's two layers, the slots over the 5 s prepare lead at 10 s turns, one still drawn.
    assert len(rig.executor._assignments) <= len(photos) + 3
    assert rig.renderer.outputs["hdmi1"].layers[-1].layer.assignment_id.startswith(
        CONTINUATION_PREFIX)


def test_a_slot_keeps_its_photo_when_a_download_lands_during_its_turn(tmp_path):
    """Decision 0032: slot k shows the (k mod n)-th held photo, and n grows as downloads land while
    Central is slow; a slot already made never gets a second assignment with another photo, so a
    turn never swaps its photo midway. Mutation probe: dedupe slots on their generated id alone."""
    rig = Rig(tmp_path)
    _continued_plan(rig)
    rig.secure("a", b"a")
    rig.advance(27)                                   # 127: slot 1 (130 to 140) is within the lead
    rig.executor.prepare_imminent()
    rig.executor.tick()
    rig.secure("b", b"b")                             # now slot 1 would map to "b"
    rig.advance(1)
    rig.executor.prepare_imminent()
    assert len([a for a in _slots(rig) if a.layer.start == 130]) == 1
    for _ in range(18):                               # 128 to 137: through slot 1's turn
        rig.executor.prepare_imminent()
        rig.executor.tick()
        if rig.clock.utc() >= 130:
            shown = rig.renderer.outputs["hdmi1"].layers
            assert [local.layer.variant for local in shown] == [media(b"a")]
        rig.advance(.5)


def test_a_continuation_with_no_listed_photo_held_states_no_photo(tmp_path):
    """Decision 0032: the card shows when the Frame goes black while content is wanted; a
    continuation covering now whose listed photos are not held (a wiped cache, downloads not done)
    is NO_PHOTO, never a silent black. Once one is held the statement goes. Mutation probe: leave
    a continuation with no slot out of `frames_black`."""
    rig = Rig(tmp_path)
    _continued_plan(rig)
    rig.advance(25)                                   # 125: past the layers, the continuation's
    rig.executor.prepare_imminent()
    rig.executor.tick()
    assert rig.executor.frames_black() == (FrameBlack("hdmi1", FrameBlackCause.NO_PHOTO),)
    rig.secure("a", b"a")
    rig.executor.prepare_imminent()
    rig.executor.tick()
    assert rig.executor.frames_black() == ()


def test_a_cancelled_or_invalidated_source_stops_its_continuation_slots(tmp_path):
    """Decision 0032: Central cancelling a listed photo removes the slots made from it, and
    invalidating one stops new slots drawing it; the Frame cycles the photos it still may.
    Mutation probe: ignore revocations when choosing the held photos."""
    rig = Rig(tmp_path)
    _continued_plan(rig)
    rig.secure("a", b"a")
    rig.secure("b", b"b")
    rig.advance(22)
    rig.executor.prepare_imminent()
    rig.executor.cancel(["a"])
    assert not [slot for slot in _slots(rig) if slot.source == "a"]
    rig.executor.invalidate(["b"])
    assert not _slots(rig)
    for _ in range(10):
        rig.executor.prepare_imminent()
        rig.executor.tick()
        assert not _slots(rig)
        rig.advance(2.5)


def test_a_kept_still_no_layer_of_the_held_plan_places_is_reported_carried(tmp_path):
    """Decision 0032: a photo the Frame keeps (its after-state) that the held plan does not place
    travels in readiness as carried, also while a later layer draws over it, so Central keeps it
    off every other Frame. Mutation probe: leave kept photos out of `carried`."""
    rig = Rig(tmp_path)
    rig.play(layer(end=101, after_end="keep_this_photo"))
    rig.advance(2)
    rig.executor.tick()
    over = layer("over", data=b"over", start=105, end=300, awaits_grant=False,
                 after_end="leave_as_is")
    rig.offer(over, revision=2)
    rig.secure("over", b"over")
    rig.advance(3)
    rig.executor.prepare_imminent()
    rig.executor.tick()
    assert [local.layer.assignment_id for local in rig.renderer.outputs["hdmi1"].layers] == ["over"]
    assert rig.executor.readiness().carried == (
        CarriedPhoto(output_id="hdmi1", asset_id=media().asset_id),)


def test_a_photo_held_for_the_rejoin_keeps_its_decoder(tmp_path):
    """Decision 0032: the photo a superseded composition still draws while the Frame rejoins (the
    new plan's layer waits for its grant) stays resident with no layer of the held plan naming it.
    Mutation probe: leave the held photo out of the prepare pass's pictures."""
    rig = Rig(tmp_path, capacity=1)
    old = layer("old", data=b"old", end=300, awaits_grant=False)
    rig.offer(old)
    rig.secure("old", b"old")
    rig.executor.prepare_imminent()
    rig.executor.tick()
    rig.advance(60)                                   # 160: the old layer still drawn
    rig.executor.tick()
    new = layer("new", data=b"new", start=155, end=200)          # awaits its grant
    rig.offer(new, revision=2)
    rig.secure("new", b"new")
    rig.executor.prepare_imminent()
    rig.executor.tick()
    assert [local.layer.assignment_id for local in rig.renderer.outputs["hdmi1"].layers] == ["old"]
    assert "old" in rig.executor._wanted


def test_a_grant_arriving_late_within_the_clocks_bound_is_no_missed_start(tmp_path):
    """Slice A: a grant committed before its start and heard after it is a missed start only past
    the clock's own bound: with Central's time held to within 5 s, one heard 1 s late still starts.
    Mutation probe: judge the missed start by a fixed 0.1 s."""
    rig = Rig(tmp_path)
    rig.offer(layer(start=103, media_origin=103))
    rig.secure()
    rig.executor.prepare_imminent()
    ready = rig.executor.readiness()
    granted = Commit(plan_id="stable", revision=1, authority_epoch=1, assignment_ids=("picture",),
                     readiness_sequence=ready.sequence, committed_at=102)
    rig.advance(4, refresh=False)                    # 104: a second past the start
    loosen(rig, 5.0)
    assert rig.executor.accept_commit(granted)


def test_a_layer_is_wanted_past_its_end_only_while_a_continuation_naming_it_runs():
    """Decision 0032, `may_still_play`: a plan layer's bytes are wanted within its span and the
    plan's validity, or while a continuation of the plan names it and has not reached `until`.
    Mutation probe: ignore `until`."""
    from player.executor import may_still_play
    listed = layer("listed", start=100, end=150)
    plan = Plan(plan_id="stable", revision=1, player_id="player1", authority_epoch=1,
                issued_at=100, valid_from=100, valid_until=160, bindings=(binding(),),
                layers=(listed,), continuations=(Continuation(
                    output_id="hdmi1", run_id="run", photos=("listed",), start=150, seconds=10,
                    until=200),))
    assert may_still_play(plan, listed, 140)
    assert may_still_play(plan, listed, 190)
    assert not may_still_play(plan, listed, 200)
    alone = plan.model_copy(update={"continuations": ()})
    assert not may_still_play(alone, listed, 150)


def test_a_still_photo_is_reported_once_and_each_change_of_its_state_once(tmp_path):
    """2026-11-16: one still photo, unchanged for an hour, was reported presented on every tick (a
    row in Central every half second). A report is a change of an assignment's state, numbered
    once: presented when it starts, then the fallback when it ends, each once however many ticks
    stand between. Mutation probe: drop the `reported` check in `Executor._report`."""
    rig = Rig(tmp_path)
    reports = list(rig.play(layer()))
    for _ in range(300):                       # ten seconds of 33 ms ticks
        rig.advance(.033)
        reports += rig.executor.tick()
    assert [(report.assignment_id, report.status) for report in reports] == [
        ("picture", "presented")]
    rig.advance(200 - rig.clock.utc())         # the photo's end
    for _ in range(30):
        rig.advance(.033)
        reports += rig.executor.tick()
    assert [(report.status, report.detail) for report in reports] == [
        ("presented", "none"), ("fallback", "expired")]
    numbers = [report.sequence for report in reports]
    assert numbers == sorted(set(numbers))


def test_readiness_is_numbered_once_per_change(tmp_path):
    """Readiness is level state, numbered when it changes: asked again while nothing changed it
    keeps its number (the Player sends it once, Central writes it once); a change (a photo
    prepared, the plan's next revision) takes a new number. Mutation probe: number every ask ->
    the second ask's sequence differs."""
    rig = Rig(tmp_path)
    rig.offer(layer())
    rig.secure()
    first = rig.executor.readiness()
    rig.advance(.5)
    assert rig.executor.readiness().sequence == first.sequence
    rig.executor.prepare_imminent()
    prepared = rig.executor.readiness()
    assert prepared.prepared == ("picture",) and prepared.sequence > first.sequence
    rig.advance(.5)
    assert rig.executor.readiness().sequence == prepared.sequence
    rig.offer(layer(), revision=2)
    assert rig.executor.readiness().sequence > prepared.sequence


def test_readiness_is_numbered_again_when_the_clock_bound_central_holds_drifts_off(tmp_path):
    """Central grows the reported clock bound by DRIFT_RATE from its receipt (decision 0032); while
    the Player's own bound stays tight the two part, and once they differ by more than
    CLOCK_BOUND_SLACK the Player reports again, so a guard never stays wide for want of a report.
    Mutation probe: leave the bound out of the change -> the same number after an hour."""
    from contracts.time import DRIFT_RATE
    from player.executor import CLOCK_BOUND_SLACK
    rig = Rig(tmp_path)
    rig.offer(valid_until=10_000)       # nothing else changes: no layer starts or ends
    first = rig.executor.readiness()
    rig.advance(CLOCK_BOUND_SLACK / DRIFT_RATE / 2)
    assert rig.executor.readiness().sequence == first.sequence
    rig.advance(CLOCK_BOUND_SLACK / DRIFT_RATE)
    assert rig.executor.readiness().sequence > first.sequence


@pytest.mark.parametrize("granted", [True, False])
def test_a_cut_layer_fades_out_at_its_cut(tmp_path, granted):
    """Decision 0036 (slice 3): a revision bringing Central's cut onto a layer already running
    (granted, or starting at its own instant) ends it at the cut, its fade-out ending there.
    Mutation probes: keep the grant uncut in `accept_plan`; store the layer as offered rather
    than `drawn`."""
    rig = Rig(tmp_path)
    shown = layer(fade_out=0, awaits_grant=granted)
    rig.offer(shown)
    rig.secure()
    rig.executor.prepare_imminent()
    if granted:
        rig.commit("picture")
    rig.advance(10)
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].layers[0].alpha == 1
    rig.offer(shown.model_copy(update={"cut_at": 120.0, "fade_out": 4.0}), revision=2)
    rig.advance(8)       # 118: half-way through the cut's fade
    rig.executor.prepare_imminent()
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].layers[0].alpha == pytest.approx(0.5)
    rig.advance(3)       # 121: past the cut
    rig.executor.tick()
    assert all(local.layer.assignment_id != "picture" for local in rig.renderer.outputs["hdmi1"].layers)


def test_a_revision_moving_a_cut_later_is_refused(tmp_path):
    """Decision 0036: a cut is set once and only ever brought earlier. Mutation probe: drop the
    `cut_is_final` check."""
    rig = Rig(tmp_path)
    cut = layer(cut_at=150.0, fade_out=2.0)
    rig.offer(cut)
    rig.secure()
    with pytest.raises(AuthorityError, match="cut_is_final"):
        rig.offer(cut.model_copy(update={"cut_at": 160.0}), revision=2)
    with pytest.raises(AuthorityError, match="cut_is_final"):
        rig.offer(cut.model_copy(update={"cut_at": None, "fade_out": 0.0}), revision=3)
    rig.offer(cut.model_copy(update={"cut_at": 140.0}), revision=4)
