from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

from contracts.models import (
    Calibration,
    Commit,
    FrameProfile,
    Layer,
    OutputBinding,
    Plan,
    PlayerConfiguration,
    Revocation,
    Variant,
)
from contracts.time import ManualClock, TimeMapping
from player.cache import Cache
from player.executor import AuthorityError, Executor
from player.rendering import PresentationResult, RecordingRenderer


def media(data: bytes = b"picture", video: bool = False) -> Variant:
    return Variant(sha256=hashlib.sha256(data).hexdigest(), size=len(data),
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
    rig.play(layer(end=101, retain_on_expiry=True))
    rig.advance(1.01)
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].layers
    rig.renderer.acknowledged = True
    rig.executor.tick()
    shown = rig.renderer.outputs["hdmi1"]
    assert shown.fallback and shown.layers[0].layer.assignment_id == "picture"


@pytest.mark.parametrize("fault", ["old", "future", "binding", "layer", "calibration"])
def test_native_ack_rejects_stale_or_different_authority(tmp_path, fault):
    rig = Rig(tmp_path)

    class InvalidRenderer(RecordingRenderer):
        def present(self, composition):
            if not composition.layers:
                return super().present(composition)
            drawn = composition
            completed = rig.clock.monotonic()
            if fault == "old":
                completed -= .6
            elif fault == "future":
                completed += .1
            elif fault == "binding":
                drawn = replace(drawn, binding=drawn.binding.model_copy(update={"generation": 2}))
            elif fault == "calibration":
                drawn = replace(drawn, calibration=Calibration(gain=.5))
            else:
                local = drawn.layers[0]
                drawn = replace(drawn, layers=(replace(local, layer=local.layer.model_copy(
                    update={"priority": 999})),))
            return PresentationResult("presented", composition=drawn, presented_at=completed)

    rig.renderer = rig.executor.renderer = InvalidRenderer()
    observations = rig.play(layer())
    assert not any(o.status == "presented" for o in observations)
    assert any(o.status == "failed" for o in observations)


def binding(output: str = "hdmi1", frame: str = "frame1", **kwargs) -> OutputBinding:
    return OutputBinding(output_id=output, frame_id=frame, generation=1,
                         profile=FrameProfile(width_px=1920, height_px=1080, diagonal_inches=32),
                         **kwargs)


def layer(key: str = "picture", *, output: str = "hdmi1", frame: str = "frame1",
          data: bytes = b"picture", video: bool = False, **kwargs) -> Layer:
    fields = dict(assignment_id=key, run_id="run", output_id=output, frame_id=frame,
                  binding_generation=1, start=100, end=200, media_origin=100,
                  variant=media(data, video))
    fields.update(kwargs)
    return Layer(**fields)


class Rig:
    def __init__(self, directory: Path, *, capacity=4, cache_bytes=4096,
                 bindings: tuple[OutputBinding, ...] | None = None, epoch=1):
        self.clock = ManualClock(100)
        self.mapping = TimeMapping(self.clock)
        self.mapping.establish(.01)
        self.cache = Cache(directory / "cache", cache_bytes)
        self.renderer = RecordingRenderer(capacity)
        self.executor = Executor("player1", self.cache, self.renderer, self.clock,
                                 self.mapping)
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
        assert self.executor.acquire(key, [data])

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

    def advance(self, amount: float, *, refresh=True) -> None:
        self.clock.advance(amount)
        if refresh:
            self.mapping.establish(.01)


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
    old = layer(retain_on_expiry=True)
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
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].layers[-1].alpha == .5
    rig.advance(5)
    observations = rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].layers[-1].alpha == 1
    assert [o.assignment_id for o in observations if o.status == "presented"] == ["cover"]


def test_overlay_and_video_expire_to_retained_still_in_warm_outage(tmp_path):
    rig = Rig(tmp_path)
    old = layer(retain_on_expiry=True, end=101)
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


def test_preview_expires_locally_without_configuration_message(tmp_path):
    preview = binding(preview=Calibration(gain=.4), preview_expires=105)
    rig = Rig(tmp_path, bindings=(preview,))
    rig.play(layer())
    assert rig.renderer.outputs["hdmi1"].calibration.gain == .4
    rig.advance(6, refresh=False)
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].calibration.gain == 1


def test_clock_step_and_stale_sample_refuse_unexecuted_commit_retain_visible(tmp_path):
    rig = Rig(tmp_path)
    rig.play(layer(retain_on_expiry=True))
    future = layer("future", start=103, data=b"future")
    rig.offer(layer(retain_on_expiry=True), future, revision=2)
    rig.secure("future", b"future")
    rig.executor.prepare_imminent()
    rig.commit("future")
    rig.clock.step_utc(20)
    readiness = rig.executor.readiness()
    assert {f.code for f in readiness.failures} == {"clock"}
    rig.clock.advance(3)
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].layers[0].layer.assignment_id == "picture"
    with pytest.raises(AuthorityError):
        rig.commit("future")
    rig.mapping.establish(.01)
    rig.executor.prepare_imminent()
    rig.commit("future")
    rig.clock.advance(31)
    with pytest.raises(AuthorityError):
        rig.commit("future")


def test_cache_pressure_and_corruption_revoke_precommit_readiness(tmp_path):
    rig = Rig(tmp_path, cache_bytes=7)
    rig.offer(layer(), layer("new", data=b"new"))
    rig.secure()
    assert not rig.executor.acquire("new", [b"new"])
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

    assert rig.executor.acquire("picture", broken()) is False
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
        acquisition = pool.submit(rig.executor.acquire, "picture", chunks())
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
    rig.play(layer(retain_on_expiry=True))
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
    rig.play(layer(retain_on_expiry=True))
    rig.executor.maintain_cache()
    rig.cache.close()
    cache = Cache(tmp_path / "cache", 4096)
    restarted_renderer = RecordingRenderer()
    restarted = Executor("player1", cache, restarted_renderer, rig.clock, rig.mapping)
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
    rig.offer(layer(retain_on_expiry=True))
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
    rig.offer(layer(retain_on_expiry=True))
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
    rig.executor.tick()
    rig.advance(1)
    changed_black = black.model_copy(update={"priority": -10})
    rig.offer(picture, changed_black, revision=2, valid_until=400)
    observations = rig.executor.tick()
    assert [o.assignment_id for o in observations if o.status == "presented"] == ["black"]
    assert rig.renderer.outputs["hdmi1"].layers[-1].layer.priority == 10
    rig.advance(10)
    rig.executor.tick()
    assert rig.renderer.outputs["hdmi1"].fallback
    assert rig.renderer.outputs["hdmi1"].layers == ()


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
    rig.play(layer(retain_on_expiry=True, end=101))
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
    dummy = layer("dummy", output="hdmi2", frame="frame2", data=b"dummy", retain_on_expiry=True)
    picture = layer(start=101, end=102, priority=10, retain_on_expiry=True)
    rig.play(video, dummy, picture, payloads={"dummy": b"dummy"})
    entered, resume = Event(), Event()
    original_pin = rig.cache.pin

    def paused_pin(digest, owner):
        if digest == media(b"dummy").sha256:
            entered.set()
            assert resume.wait(3)
        return original_pin(digest, owner)

    monkeypatch.setattr(rig.cache, "pin", paused_pin)
    with ThreadPoolExecutor(max_workers=1) as pool:
        maintenance = pool.submit(rig.executor.maintain_cache)
        assert entered.wait(3)
        rig.advance(1)
        rig.executor.tick()
        rig.advance(2)
        resume.set()
        maintenance.result(timeout=3)
    assert any(media().sha256 in digests for digests in rig.cache._pins.values())
    monkeypatch.setattr(rig.cache, "pin", original_pin)
    rig.executor.maintain_cache()
    assert any(owner.startswith("pwretain:") and media().sha256 in digests
               for owner, digests in rig.cache._pins.items())


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
    assert rig.executor.acquire("picture", [b"picture"])
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
