"""Roadmap 1x (expose what already exists): each Scene setting the console now writes is
stored by Central and reaches the plan a Player is sent.

The Scene bodies are exactly what the console's `buildSave` writes for each setting
(central/console/src/authoring.js; tests/test_console_flow.py pins that shape), saved through
the operator route, started through the activation route, and read back from the stored Scene
and from the Player's offered plan:

- #41 fade between photos: each layer's `fade_in` and `fade_out` (half each);
- #62, #63 how it ends: after Finish, the outro's layer, opaque black or a fading photo;
- #64 keep the last photo up: each layer's `retain_on_expiry`;
- #65 keep these Frames together: another Scene on the same Frame is refused.

The end of a Scene is proved on what the Frame shows: Central's real plans run through the real
Player executor (player/executor.py, which owns the kept photo), and the tests read the Frame's
composition after the Scene ends. An ending (black or fading) is never undone by the kept
photo; a kept photo with nothing after it does not fade to black and snap back.

The browser half (the console sets each value, saves, reloads and shows it) is
tests/browser/test_scene_flow_browser.py.
"""

import hashlib

from fastapi.testclient import TestClient
from test_coordination import publish_fixture_catalog, setup_players
from test_registry import ADMIN

from central.app import create_app
from contracts.models import Commit
from contracts.time import ManualClock, TimeMapping
from player.cache import Cache
from player.executor import Executor
from player.rendering import RecordingRenderer

AUTH = {"Authorization": "Bearer " + ADMIN}
FRAME = "frame-0"


def _scene(scene_id, **settings):
    """A live Scene as the console saves it, with delivery 1x's settings applied the way
    `buildSave` writes them."""
    fade = settings.get("fade", 0)
    keep_last = settings.get("keep_last", True)
    contribution = {"target": f"frame:{FRAME}", "role": FRAME, "kind": "media",
                    "source_refs": ["library:1"]}
    body = {"scene_id": scene_id, "revision": 1, "cycle_seconds": 20, "loop": True,
            "contributions": [{**contribution,
                               **({"fade_in_seconds": fade / 2, "fade_out_seconds": fade / 2}
                                  if fade else {}),
                               **({"retain_on_expiry": True} if keep_last else {})}]}
    ending, seconds = settings.get("ending", ("none", 0))
    if ending != "none":
        body["outro_seconds"] = seconds
        # "Fades out": the last photo returns over half the fade, then fades out over the rest.
        body["outro_contributions"] = [
            {"target": f"frame:{FRAME}", "role": FRAME, "kind": "black"} if ending == "black"
            else {**contribution,
                  **({"fade_in_seconds": fade / 2} if fade else {}),
                  "fade_out_seconds": seconds - fade / 2}]
    if settings.get("keep_together"):
        body["protect_frames"] = True
    return body


# The fixture photo's bytes: the Player checks every byte it shows against its digest.
PHOTO = b"photo-wall"


def _rig(registry):
    player = setup_players(registry, count=1)[0]
    publish_fixture_catalog(registry, digest=hashlib.sha256(PHOTO).hexdigest())
    app = create_app(registry.db, registry.clock, ADMIN, run_scheduler=False)
    return player, app, app.state.coordinator


def _save_and_start(client, body):
    saved = client.put(f"/v1/operator/scenes/{body['scene_id']}", json=body, headers=AUTH)
    assert saved.status_code == 200, saved.text
    started = client.post("/v1/operator/activations", headers=AUTH,
                          json={"scene_id": body["scene_id"], "activation_id": "act-" + body["scene_id"]})
    assert started.status_code == 200, started.text
    return started.json()


def _stored(coordinator, scene_id):
    return coordinator.runtime.read().export_state()["scenes"][scene_id]


def _layers(coordinator, player):
    coordinator.advance()
    plan = coordinator.delivery(player["player_id"], player["authority_epoch"])["plan"]
    assert plan is not None
    return plan.layers


def test_fade_and_keep_last_photo_reach_the_players_plan(registry):
    player, app, coordinator = _rig(registry)
    with TestClient(app) as client:
        _save_and_start(client, _scene("faded", fade=3, keep_last=True))
        stored = _stored(coordinator, "faded")["contributions"][0]
        assert (stored["fade_in_seconds"], stored["fade_out_seconds"], stored["retain_on_expiry"]) == (
            1.5, 1.5, True)
        layers = _layers(coordinator, player)
        assert layers and all((layer.fade_in, layer.fade_out, layer.retain_on_expiry) == (1.5, 1.5, True)
                              for layer in layers)


def test_keep_last_photo_off_reaches_the_players_plan(registry):
    player, app, coordinator = _rig(registry)
    with TestClient(app) as client:
        _save_and_start(client, _scene("plain", keep_last=False))
        assert _stored(coordinator, "plain")["contributions"][0]["retain_on_expiry"] is False
        layers = _layers(coordinator, player)
        assert layers and not any(layer.retain_on_expiry for layer in layers)
        assert all((layer.fade_in, layer.fade_out) == (0, 0) for layer in layers)


def _finish_and_reach_outro(client, coordinator, registry, admission):
    finished = client.post(f"/v1/operator/runs/{admission['run_id']}/finish", headers=AUTH)
    assert finished.status_code == 200, finished.text
    registry.clock.advance(20)  # the current cycle ends; the outro starts
    coordinator.advance()


def test_black_ending_reaches_the_players_plan(registry):
    player, app, coordinator = _rig(registry)
    with TestClient(app) as client:
        admission = _save_and_start(client, _scene("black-end", ending=("black", 4)))
        stored = _stored(coordinator, "black-end")
        assert stored["outro_seconds"] == 4
        assert stored["outro_contributions"][0]["kind"] == "black"
        _finish_and_reach_outro(client, coordinator, registry, admission)
        outro = [layer for layer in _layers(coordinator, player) if layer.presentation == "black"]
        assert len(outro) == 1
        assert (outro[0].end - outro[0].start, outro[0].opacity) == (4, 1)


def test_fade_out_ending_reaches_the_players_plan(registry):
    player, app, coordinator = _rig(registry)
    with TestClient(app) as client:
        admission = _save_and_start(client, _scene("fade-end", ending=("fade", 5)))
        stored = _stored(coordinator, "fade-end")
        assert (stored["outro_seconds"], stored["outro_contributions"][0]["fade_out_seconds"]) == (5, 5)
        _finish_and_reach_outro(client, coordinator, registry, admission)
        outro = [layer for layer in _layers(coordinator, player)
                 if layer.end - layer.start == 5 and layer.fade_out == 5]
        assert len(outro) == 1 and outro[0].presentation == "media"


def test_keep_frames_together_turns_another_scene_away(registry):
    _player, app, coordinator = _rig(registry)
    with TestClient(app) as client:
        _save_and_start(client, _scene("together", keep_together=True))
        assert _stored(coordinator, "together")["protect_frames"] is True
        other = _scene("intruder")
        assert client.put("/v1/operator/scenes/intruder", json=other, headers=AUTH).status_code == 200
        refused = client.post("/v1/operator/activations", headers=AUTH,
                              json={"scene_id": "intruder", "activation_id": "act-intruder",
                                    "priority": 10})
        assert refused.json()["reason"] == "protected_frames", refused.text


# --- What the Frame shows when the Scene ends: Central's plans run on the Player executor.


class _Frame:
    """The Pi feeding FRAME, played from Central's deliveries: each sync takes the current
    configuration and plan, fetches and prepares what is imminent, commits it, and draws."""

    def __init__(self, registry, coordinator, player, directory):
        self.registry, self.coordinator, self.player = registry, coordinator, player
        self.clock = ManualClock(registry.clock.utc())
        self.mapping = TimeMapping(self.clock)
        self.mapping.establish(.01)
        self.renderer = RecordingRenderer(4)
        self.executor = Executor(player["player_id"], Cache(directory / "cache", 1 << 20),
                                 self.renderer, self.clock, self.mapping)
        self.output = None

    def sync(self):
        self.coordinator.advance()
        delivery = self.coordinator.delivery(self.player["player_id"], self.player["authority_epoch"])
        self.executor.accept_configuration(delivery["configuration"])
        self.output = delivery["configuration"].bindings[0].output_id
        plan = delivery["plan"]
        if plan is not None:
            self.executor.accept_plan(plan)
            for layer in plan.layers:
                if layer.variant is not None:
                    self.executor.acquire(layer.assignment_id, [PHOTO])
            self.executor.prepare_imminent()
            readiness = self.executor.readiness()
            if readiness.prepared:
                self.executor.accept_commit(Commit(
                    plan_id=plan.plan_id, revision=plan.revision, authority_epoch=plan.authority_epoch,
                    assignment_ids=readiness.prepared, readiness_sequence=readiness.sequence,
                    committed_at=self.clock.utc()))
        self.executor.tick()
        return self.renderer.outputs[self.output]

    def advance(self, seconds):
        """Both clocks move on together, then the Frame syncs and draws."""
        self.registry.clock.advance(seconds)
        self.clock.advance(seconds)
        self.mapping.establish(.01)
        return self.sync()


def _shown(composition):
    """What the Frame draws: each layer as (black or photo, strength), and whether it is the
    fallback (nothing planned)."""
    return composition.fallback, [
        ("black" if local.layer.presentation == "black" else "photo", round(local.alpha, 2))
        for local in composition.layers]


def _play_one_cycle(client, frame, body):
    """`body` scheduled for exactly one 20 s cycle, starting now (a Program, so its end and
    ending are planned ahead), then 10 s on: mid-cycle, the photo shows at full strength."""
    saved = client.put(f"/v1/operator/scenes/{body['scene_id']}", json=body, headers=AUTH)
    assert saved.status_code == 200, saved.text
    now = frame.registry.clock.utc()
    program = {"program_id": "p-" + body["scene_id"], "scene_id": body["scene_id"],
               "starts_at": now, "ends_at": now + 20}
    scheduled = client.put(f"/v1/operator/programs/{program['program_id']}", json=program, headers=AUTH)
    assert scheduled.status_code == 200, scheduled.text
    frame.sync()
    assert _shown(frame.advance(10)) == (False, [("photo", 1.0)])
    return frame


def test_after_a_black_ending_the_kept_photo_does_not_come_back(registry, tmp_path):
    player, app, coordinator = _rig(registry)
    frame = _Frame(registry, coordinator, player, tmp_path)
    with TestClient(app) as client:
        _play_one_cycle(client, frame, _scene("black-end", ending=("black", 4), keep_last=True))
        assert _shown(frame.advance(11)) == (False, [("black", 1.0)])  # the ending
        # After the ending nothing plays: the Frame stays black, not the kept photo again.
        assert _shown(frame.advance(5)) == (True, [])


def test_after_a_fading_ending_the_kept_photo_does_not_come_back(registry, tmp_path):
    player, app, coordinator = _rig(registry)
    frame = _Frame(registry, coordinator, player, tmp_path)
    with TestClient(app) as client:
        _play_one_cycle(client, frame, _scene("fade-end", ending=("fade", 4), fade=2, keep_last=True))
        # The ending's photo returns over half the fade (1 s), then fades out over the rest.
        assert _shown(frame.advance(10.5)) == (False, [("photo", 0.5)])
        assert _shown(frame.advance(2)) == (False, [("photo", 0.5)])
        assert _shown(frame.advance(2.5)) == (True, [])


def test_a_kept_photo_with_nothing_after_it_does_not_fade_to_black_and_snap_back(registry, tmp_path):
    player, app, coordinator = _rig(registry)
    frame = _Frame(registry, coordinator, player, tmp_path)
    with TestClient(app) as client:
        _play_one_cycle(client, frame, _scene("stops", fade=3, keep_last=True))
        # 0.5 s before the last cycle ends, inside its 1.5 s fade-out: nothing follows, so the
        # kept photo stays at full strength...
        assert _shown(frame.advance(9.5)) == (False, [("photo", 1.0)])
        # ...and after the Scene it stays up, the same photo, with no dip to black between.
        assert _shown(frame.advance(1)) == (True, [("photo", 1.0)])


def test_without_keep_the_last_photo_fades_out_to_black(registry, tmp_path):
    """The hold is only for a kept photo: a Scene that keeps nothing fades its last photo out."""
    player, app, coordinator = _rig(registry)
    frame = _Frame(registry, coordinator, player, tmp_path)
    with TestClient(app) as client:
        _play_one_cycle(client, frame, _scene("plain", fade=3, keep_last=False))
        assert _shown(frame.advance(9.5)) == (False, [("photo", 0.33)])
        assert _shown(frame.advance(1)) == (True, [])
