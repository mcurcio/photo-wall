"""Actual Player NativeRenderer/Executor client for disposable compositor evidence.

The fixture has no Scene or plan. A configuration file supplies only the explicit
bound disabled Output. Trial metadata must travel through the real GDK commit.
"""
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/frozen-player" if Path("/frozen-player/player/native.py").exists() and os.environ.get("PHOTO_WALL_CURRENT_PLAYER") != "1" else "/repo")
import contracts.node_frame
import player.native
import player.wayland_frames
from contracts.models import Commit, Layer, Plan, PlayerConfiguration
from contracts.time import TimeMapping
from player.cache import Cache
from player.executor import Executor
from player.native import NativeOutput, NativeRenderer

for module in (player.native, player.wayland_frames, contracts.node_frame):
    source = Path(module.__file__)
    print("FROZEN_IMPORT", source, hashlib.sha256(source.read_bytes()).hexdigest(), flush=True)
print("FROZEN_CLIENT", hashlib.sha256(Path("/usr/lib/photo-wall/frame-client/libphoto-wall-frame-client.so").read_bytes()).hexdigest(), flush=True)

renderer = NativeRenderer((NativeOutput("headless", "photo-wall-headless", 640, 480),))
renderer.set_unbound_outputs((), "fixture-player")
class FixtureClock:
    started = time.monotonic()
    def utc(self):
        return 1000 + time.monotonic() - self.started
    def monotonic(self):
        return time.monotonic()
clock = FixtureClock()
mapping = TimeMapping(clock)
mapping.establish(0.01)
executor = Executor("fixture-player", Cache(Path("/tmp/gtk-cache"), 4096), renderer, clock, mapping)
configured = False
last_configuration = None
last = None
media_plan = None
media_committed = False
media_observed = None
started = time.monotonic()

def tick():
    global configured, last, last_configuration, media_plan, media_committed, media_observed
    path = Path("/tmp/gtk-configuration.json")
    raw = path.read_text() if path.exists() else None
    if raw and raw != last_configuration:
        config = PlayerConfiguration.model_validate_json(raw)
        last_configuration = raw
        executor.player_id = config.player_id
        executor.accept_configuration(config)
        configured = True
    if configured:
        if Path("/tmp/fixture-media-authorized").exists():
            from native_display_media import image_fixture

            config = PlayerConfiguration.model_validate_json(last_configuration)
            binding = config.bindings[0] if config.bindings else None
            if binding and binding.calibration.gain == 0.5 and media_plan is None:
                # Explicit fixture authority: genuine readiness/Commit path, no
                # assertion that this local fixture is Central Run certification.
                config = config.model_copy(update={"enabled_outputs": (binding.output_id,)})
                executor.accept_configuration(config.model_copy(update={
                    "configuration_revision": config.configuration_revision + 1}))
                payload, variant = image_fixture()
                layer = Layer(assignment_id="fixture-media", run_id="fixture-run",
                    output_id=binding.output_id, frame_id=binding.frame_id,
                    binding_generation=binding.generation, start=clock.utc(), end=1100,
                    media_origin=clock.utc(), variant=variant)
                media_plan = Plan(plan_id="fixture-plan", revision=1, player_id=config.player_id,
                    authority_epoch=1, issued_at=clock.utc(), valid_from=1000, valid_until=1100,
                    bindings=(binding,), layers=(layer,))
                executor.accept_plan(media_plan)
                assert executor.acquire("fixture-media", [payload])
            if media_plan is not None and not media_committed:
                executor.prepare_imminent()
                readiness = executor.readiness()
                if "fixture-media" in readiness.prepared and readiness.capacity_ok:
                    executor.accept_commit(Commit(plan_id=media_plan.plan_id, revision=1,
                        authority_epoch=1, readiness_sequence=readiness.sequence,
                        assignment_ids=("fixture-media",), committed_at=clock.utc()))
                    media_committed = True
                    print("GTK_FIXTURE_MEDIA_COMMIT", flush=True)
        observations = executor.tick()
        observed = [(o.status, o.detail) for o in observations]
        if media_plan is not None and observed and observed != media_observed:
            print("GTK_MEDIA_OBSERVATIONS", observed, flush=True)
            media_observed = observed
        surface = renderer._surfaces["headless"]
        ack = surface.acknowledged
        applied = ack.applied_calibration if ack else None
        state = applied.model_dump(mode="json") if applied else None
        if state != last:
            print("GTK_APPLIED", json.dumps(state), flush=True)
            last = state
        if surface.failure:
            print("GTK_FAILURE", surface.failure, flush=True)
    if time.monotonic() - started > (350 if os.environ.get("PHOTO_WALL_BROWSER_PROBE") == "1" else 70):
        renderer.Gtk.main_quit()
        return False
    return True

renderer.GLib.timeout_add(50, tick)
renderer.Gtk.main()
renderer.close()
