"""Roadmap 1x (expose what already exists): each Scene setting the console now writes is
stored by Central and reaches the plan a Player is sent.

The Scene bodies are exactly what the console's `buildSave` writes for each setting
(central/console/src/authoring.js; tests/test_console_flow.py pins that shape), saved through
the operator route, started through the activation route, and read back from the stored Scene
and from the Player's offered plan:

- #41 fade between photos: each layer's `fade_in` and `fade_out` (half each);
- #62, #63 how it ends: after Finish, the outro's layer, opaque black or a fading photo, which
  keeps nothing after it (`after_end`);
- #64 keep the last photo up: each layer keeps its photo after it (`after_end`);
- #65 keep these Frames together: another Scene on the same Frame is refused.

The end of a Scene is proved on what the Frame shows: Central's real plans run through the real
Player executor (player/executor.py, which applies each layer's after-state), with readiness and
commits through Central, and the tests read the Frame's composition as the Scene ends, Central
included and lost. An ending (black or fading) is never undone by the kept photo; a kept photo
with nothing after it does not fade to black and snap back (docs/execution-contract.md, "What a
Frame keeps").

The browser half (the console sets each value, saves, reloads and shows it) is
tests/browser/test_scene_flow_browser.py.
"""

import hashlib

from fastapi.testclient import TestClient
from test_coordination import publish_fixture_catalog, setup_players
from test_registry import ADMIN

from central.app import create_app
from contracts.time import ManualClock, TimeMapping
from player.cache import Cache
from player.executor import AuthorityError, Executor
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
                               **({"after_end": "keep_this_photo"} if keep_last else {})}]}
    ending, seconds = settings.get("ending", ("none", 0))
    if ending != "none":
        body["outro_seconds"] = seconds
        # "Fades out": the last photo returns over half the fade, then fades out over the rest.
        body["outro_contributions"] = [
            {"target": f"frame:{FRAME}", "role": FRAME, "kind": "black", "after_end": "keep_nothing"}
            if ending == "black"
            else {**contribution,
                  **({"fade_in_seconds": fade / 2} if fade else {}),
                  "fade_out_seconds": seconds - fade / 2, "after_end": "keep_nothing"}]
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
        assert (stored["fade_in_seconds"], stored["fade_out_seconds"], stored["after_end"]) == (
            1.5, 1.5, "keep_this_photo")
        layers = _layers(coordinator, player)
        assert layers and all(
            (layer.fade_in, layer.fade_out, layer.after_end) == (1.5, 1.5, "keep_this_photo")
            for layer in layers)


def test_keep_last_photo_off_reaches_the_players_plan(registry):
    player, app, coordinator = _rig(registry)
    with TestClient(app) as client:
        _save_and_start(client, _scene("plain", keep_last=False))
        assert _stored(coordinator, "plain")["contributions"][0]["after_end"] == "leave_as_is"
        layers = _layers(coordinator, player)
        assert layers and all(layer.after_end == "leave_as_is" for layer in layers)
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
        assert (outro[0].end - outro[0].start, outro[0].opacity, outro[0].after_end) == (
            4, 1, "keep_nothing")


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
        assert outro[0].after_end == "keep_nothing"


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
    """The Pi feeding FRAME, played from Central's deliveries as the Player service applies
    them: each sync takes the current configuration, plan and revocations, fetches and
    prepares what is imminent, reports readiness to Central, applies the commits Central
    grants, and draws. `lose_central` draws on with no delivery and no time from Central."""

    def __init__(self, registry, coordinator, player, directory):
        self.registry, self.coordinator, self.player = registry, coordinator, player
        self.clock = ManualClock(registry.clock.utc())
        self.mapping = TimeMapping(self.clock)
        self.mapping.establish(.01)
        self.renderer = RecordingRenderer(4)
        self.executor = Executor(player["player_id"], Cache(directory / "cache", 1 << 20),
                                 self.renderer, self.clock, self.mapping)
        self.output = None

    def _apply(self):
        delivery = self.coordinator.delivery(self.player["player_id"], self.player["authority_epoch"])
        self.executor.accept_configuration(delivery["configuration"])
        self.output = delivery["configuration"].bindings[0].output_id
        plan = delivery["plan"]
        if plan is not None:
            self.executor.accept_plan(plan)
            for revocation in delivery["revocations"]:
                self.executor.accept_revocation(revocation)
            self.executor.prepare_imminent()
            for commit in delivery["commits"]:
                try:
                    self.executor.accept_commit(commit)
                except AuthorityError:
                    pass  # a replayed grant for readiness the Player has since revoked
        return plan

    def sync(self):
        self.coordinator.advance()
        plan = self._apply()
        if plan is not None:
            for layer in plan.layers:
                if layer.variant is not None:
                    self.executor.acquire(layer.assignment_id, [PHOTO])
            self.executor.prepare_imminent()
            self.coordinator.readiness(self.player["player_id"], self.executor.readiness())
            self._apply()
        self.executor.tick()
        return self.renderer.outputs[self.output]

    @staticmethod
    def _steps(seconds, most):
        while seconds > 0:
            step = min(seconds, most)
            seconds -= step
            yield step

    def advance(self, seconds):
        """Both clocks move on together while the Frame syncs and draws at least every 2 s
        (a readiness report is fresh for 2 s); returns the last drawing."""
        for step in self._steps(seconds, 2):
            self.registry.clock.advance(step)
            self.clock.advance(step)
            self.mapping.establish(.01)
            composition = self.sync()
        return composition

    def lose_central(self, seconds):
        """Central is unreachable: time passes and the Frame draws every second, but nothing
        arrives (after 30 s the Player's mapping of Central's time is stale)."""
        for step in self._steps(seconds, 1):
            self.registry.clock.advance(step)
            self.clock.advance(step)
            self.executor.tick()
        return self.renderer.outputs[self.output]


def _shown(composition):
    """What the Frame shows: each layer seen, bottom to top, as (black or photo, strength),
    from the topmost one at full strength up (one covered by it is not seen), and whether it
    is the fallback (nothing planned)."""
    layers = [("black" if local.layer.presentation == "black" else "photo", round(local.alpha, 2))
              for local in composition.layers]
    covering = max((index for index, (_, alpha) in enumerate(layers) if alpha >= 1), default=0)
    return composition.fallback, layers[covering:]


KEPT = (True, [("photo", 1.0)])  # nothing plays, and the Frame shows the kept photo
BLACK = (True, [])  # nothing plays, and the Frame keeps nothing


def _play(client, frame, body, *, cycles=1, program=True):
    """`body` started now and run for `cycles` 20 s cycles: as a Program (its end planned ahead)
    or, with `program=False`, an activation that stops on its own or by Finish. Then 10 s on:
    mid-cycle, the photo shows at full strength."""
    saved = client.put(f"/v1/operator/scenes/{body['scene_id']}", json=body, headers=AUTH)
    assert saved.status_code == 200, saved.text
    if program:
        now = frame.registry.clock.utc()
        program = {"program_id": "p-" + body["scene_id"], "scene_id": body["scene_id"],
                   "starts_at": now, "ends_at": now + 20 * cycles}
        scheduled = client.put(f"/v1/operator/programs/{program['program_id']}", json=program,
                               headers=AUTH)
        assert scheduled.status_code == 200, scheduled.text
    else:
        _save_and_start(client, body)
    frame.sync()
    assert _shown(frame.advance(10)) == (False, [("photo", 1.0)])
    return frame


def _play_one_cycle(client, frame, body):
    return _play(client, frame, body)


def _run_id(coordinator, scene_id):
    runs = coordinator.runtime.read().export_state()["runs"].values()
    return next(run["run_id"] for run in runs if run["scene"]["scene_id"] == scene_id
                and run["phase"] in ("body", "outro"))


def _operate(client, coordinator, scene_id, operation):
    response = client.post(f"/v1/operator/runs/{_run_id(coordinator, scene_id)}/{operation}",
                           headers=AUTH)
    assert response.status_code == 200, response.text


def test_after_a_black_ending_the_kept_photo_does_not_come_back(registry, tmp_path):
    player, app, coordinator = _rig(registry)
    frame = _Frame(registry, coordinator, player, tmp_path)
    with TestClient(app) as client:
        _play_one_cycle(client, frame, _scene("black-end", ending=("black", 4), keep_last=True))
        assert _shown(frame.advance(11)) == (False, [("black", 1.0)])  # the ending
        # After the ending nothing plays: the Frame stays black, not the kept photo again.
        assert _shown(frame.advance(5)) == BLACK


def test_after_a_black_ending_an_outage_stays_black(registry, tmp_path):
    """Owner, 2026-10-10: a Black ending during an outage should "Stay black". Central is lost
    while the (long) ending shows; the Player's time goes stale mid-ending, the ending plays
    out, and the Frame keeps nothing. Mutation probe: skip the ending's after-state (the kept
    photo comes back at 66 s)."""
    player, app, coordinator = _rig(registry)
    frame = _Frame(registry, coordinator, player, tmp_path)
    with TestClient(app) as client:
        _play_one_cycle(client, frame, _scene("black-end", ending=("black", 40), keep_last=True))
        assert _shown(frame.advance(11)) == (False, [("black", 1.0)])  # 21 s: the ending shows
        assert _shown(frame.lose_central(35)) == (False, [("black", 1.0)])  # 56 s: time is stale
        assert _shown(frame.lose_central(10)) == BLACK  # 66 s: after the ending
        assert _shown(frame.lose_central(60)) == BLACK


def test_an_outage_before_the_ending_shows_keeps_the_kept_photo(registry, tmp_path):
    """Central is lost in the last cycle, before the ending is committed: the ending never
    shows, so the kept photo stays up through the outage."""
    player, app, coordinator = _rig(registry)
    frame = _Frame(registry, coordinator, player, tmp_path)
    with TestClient(app) as client:
        _play_one_cycle(client, frame, _scene("black-end", ending=("black", 4), keep_last=True))
        assert _shown(frame.lose_central(15)) == KEPT  # 25 s: past the cycle and the ending
        assert _shown(frame.lose_central(60)) == KEPT


def test_an_ending_withdrawn_before_it_shows_leaves_the_kept_photo(registry, tmp_path):
    """The ending is committed (it is imminent at 17 s) and then withdrawn by Cancel before it
    starts: it never shows, so the kept photo stays, and nothing drops it later."""
    player, app, coordinator = _rig(registry)
    frame = _Frame(registry, coordinator, player, tmp_path)
    with TestClient(app) as client:
        _play_one_cycle(client, frame, _scene("black-end", ending=("black", 4), keep_last=True))
        assert _shown(frame.advance(7)) == (False, [("photo", 1.0)])  # 17 s, ending committed
        _operate(client, coordinator, "black-end", "cancel")
        assert _shown(frame.advance(1)) == KEPT
        assert _shown(frame.advance(10)) == KEPT  # 28 s: past where the ending would have ended
        assert _shown(frame.lose_central(60)) == KEPT


def test_an_ending_shown_then_dropped_from_the_plan_stays_black(registry, tmp_path):
    """The black ending shows, then Cancel drops it from Central's next plan: the ending had
    already taken effect, so the Frame keeps nothing. Mutation probe: apply the after-state
    only at the ending's end (the kept photo shows at 21.5 s)."""
    player, app, coordinator = _rig(registry)
    frame = _Frame(registry, coordinator, player, tmp_path)
    with TestClient(app) as client:
        _play_one_cycle(client, frame, _scene("black-end", ending=("black", 4), keep_last=True))
        assert _shown(frame.advance(11)) == (False, [("black", 1.0)])
        _operate(client, coordinator, "black-end", "cancel")
        assert _shown(frame.advance(.5)) == BLACK
        assert _shown(frame.lose_central(60)) == BLACK


def test_a_fading_ending_returns_fades_and_ends_black(registry, tmp_path):
    """"Fades out": the last photo returns over half the fade (1 s), fades out over the rest,
    and then the Frame keeps nothing, an outage included."""
    player, app, coordinator = _rig(registry)
    frame = _Frame(registry, coordinator, player, tmp_path)
    with TestClient(app) as client:
        _play_one_cycle(client, frame, _scene("fade-end", ending=("fade", 4), fade=2, keep_last=True))
        assert _shown(frame.advance(10.5)) == (False, [("photo", 0.5)])  # returning
        assert _shown(frame.advance(2)) == (False, [("photo", 0.5)])  # fading out
        assert _shown(frame.lose_central(2.5)) == BLACK
        assert _shown(frame.lose_central(60)) == BLACK


def test_a_fading_ending_fades_to_the_scene_beneath(registry, tmp_path):
    """A Scene playing beneath shows through the fading ending and stays after it."""
    player, app, coordinator = _rig(registry)
    frame = _Frame(registry, coordinator, player, tmp_path)
    with TestClient(app) as client:
        _save_and_start(client, _scene("beneath", keep_last=False))
        _play_one_cycle(client, frame, _scene("fade-end", ending=("fade", 4), fade=2, keep_last=True))
        assert _shown(frame.advance(12.5)) == (False, [("photo", 1.0), ("photo", 0.5)])
        assert _shown(frame.advance(2.5)) == (False, [("photo", 1.0)])


def test_a_kept_photo_at_program_end_holds_full_strength(registry, tmp_path):
    """Stops + fade + keep, ended by its Program: Central plans the last cycle with no fade-out
    from the first plan on, so the photo stays at full strength to the end and after it, with
    no dip to black between. Mutation probe: keep the authored fade-out in the final cycle."""
    player, app, coordinator = _rig(registry)
    frame = _Frame(registry, coordinator, player, tmp_path)
    with TestClient(app) as client:
        _play_one_cycle(client, frame, _scene("stops", fade=3, keep_last=True))
        first = [layer for layer in _layers(coordinator, player) if layer.start <= registry.clock.utc()]
        assert [(layer.fade_in, layer.fade_out) for layer in first] == [(1.5, 0)]
        # 0.5 s before the end, where the authored fade-out would be at a third.
        assert _shown(frame.advance(9.5)) == (False, [("photo", 1.0)])
        assert _shown(frame.advance(1)) == KEPT


def test_a_kept_photo_at_finish_holds_full_strength(registry, tmp_path):
    """Stops + fade + keep, ended by Finish mid-cycle: Central revises the current cycle's
    fade-out to none, and the Player adopts it at the next commit."""
    player, app, coordinator = _rig(registry)
    frame = _Frame(registry, coordinator, player, tmp_path)
    with TestClient(app) as client:
        _play(client, frame, _scene("stops", fade=3, keep_last=True), program=False)
        _operate(client, coordinator, "stops", "finish")
        assert _shown(frame.advance(9.5)) == (False, [("photo", 1.0)])
        assert _shown(frame.advance(1)) == KEPT
        assert _shown(frame.lose_central(60)) == KEPT


def test_a_finish_inside_the_last_fade_brings_the_kept_photo_back_to_full(registry, tmp_path):
    """Finish while the photo is already fading out: the revised plan holds it at full
    strength from the next commit on, and it is kept."""
    player, app, coordinator = _rig(registry)
    frame = _Frame(registry, coordinator, player, tmp_path)
    with TestClient(app) as client:
        _play(client, frame, _scene("stops", fade=3, keep_last=True), program=False)
        assert _shown(frame.advance(9)) == (False, [("photo", 0.67)])  # 19 s, fading out
        _operate(client, coordinator, "stops", "finish")
        assert _shown(frame.advance(.5)) == (False, [("photo", 1.0)])
        assert _shown(frame.advance(1)) == KEPT


def test_a_looped_kept_scene_holds_only_its_final_cycle(registry, tmp_path):
    """Two cycles under a Program: the first fades out into the second as authored; only the
    final one holds."""
    player, app, coordinator = _rig(registry)
    frame = _Frame(registry, coordinator, player, tmp_path)
    with TestClient(app) as client:
        _play(client, frame, _scene("looped", fade=3, keep_last=True), cycles=2)
        assert _shown(frame.advance(9.5)) == (False, [("photo", 0.33)])  # 19.5 s
        assert _shown(frame.advance(20)) == (False, [("photo", 1.0)])  # 39.5 s
        assert _shown(frame.advance(1)) == KEPT


def test_a_kept_scene_with_a_duration_holds_only_its_final_cycle(registry, tmp_path):
    """The same with the Scene's own duration (40 s): its last cycle is known at the start."""
    player, app, coordinator = _rig(registry)
    frame = _Frame(registry, coordinator, player, tmp_path)
    with TestClient(app) as client:
        body = {**_scene("timed", fade=3, keep_last=True), "duration_seconds": 40}
        _play(client, frame, body, program=False)
        assert _shown(frame.advance(9.5)) == (False, [("photo", 0.33)])
        assert _shown(frame.advance(20)) == (False, [("photo", 1.0)])
        assert _shown(frame.advance(1)) == KEPT


def test_without_keep_the_last_photo_fades_out_to_black(registry, tmp_path):
    """The hold is only for a kept photo: a Scene that keeps nothing fades its last photo out."""
    player, app, coordinator = _rig(registry)
    frame = _Frame(registry, coordinator, player, tmp_path)
    with TestClient(app) as client:
        _play_one_cycle(client, frame, _scene("plain", fade=3, keep_last=False))
        assert _shown(frame.advance(9.5)) == (False, [("photo", 0.33)])
        assert _shown(frame.advance(1)) == BLACK


def test_the_shortest_fading_ending_keeps_nothing(registry, tmp_path):
    """Ending length at its minimum, half the fade: the ending's photo fades in for its whole
    length and never reaches full strength, yet it still keeps nothing (from its first draw).
    Mutation probe: apply keep_nothing only after the fade-in (the kept photo comes back)."""
    player, app, coordinator = _rig(registry)
    frame = _Frame(registry, coordinator, player, tmp_path)
    with TestClient(app) as client:
        _play_one_cycle(client, frame, _scene("fade-end", ending=("fade", 1), fade=2, keep_last=True))
        outro = [layer for layer in _layers(coordinator, player) if layer.after_end == "keep_nothing"]
        assert [(layer.fade_in, layer.fade_out) for layer in outro] == [(1, 0)]
        assert _shown(frame.advance(10.5)) == (False, [("photo", 0.5)])
        assert _shown(frame.advance(1)) == BLACK
        assert _shown(frame.lose_central(60)) == BLACK


def _program(client, frame, scene_id, starts, ends):
    program = {"program_id": "p-" + scene_id, "scene_id": scene_id, "starts_at": starts,
               "ends_at": ends}
    scheduled = client.put(f"/v1/operator/programs/{program['program_id']}", json=program,
                           headers=AUTH)
    assert scheduled.status_code == 200, scheduled.text


def test_back_to_back_programs_keep_their_fade(registry, tmp_path):
    """A kept photo whose Program is followed at once by another on the same Frame fades into
    it as authored: the hold is only for a photo nothing follows. Mutation probe: ignore what
    follows (full strength at 19.5 s)."""
    player, app, coordinator = _rig(registry)
    frame = _Frame(registry, coordinator, player, tmp_path)
    with TestClient(app) as client:
        for body in (_scene("first", fade=3, keep_last=True), _scene("second", fade=3, keep_last=True)):
            saved = client.put(f"/v1/operator/scenes/{body['scene_id']}", json=body, headers=AUTH)
            assert saved.status_code == 200, saved.text
        now = registry.clock.utc()
        _program(client, frame, "first", now, now + 20)
        _program(client, frame, "second", now + 20, now + 40)
        frame.sync()
        assert _shown(frame.advance(19.5)) == (False, [("photo", 0.33)])
        assert _shown(frame.advance(1)) == (False, [("photo", 0.33)])  # the next one fading in


def test_a_kept_photo_over_a_scene_beneath_fades_to_reveal_it(registry, tmp_path):
    """A kept photo on top of a Scene that plays on beneath fades out to reveal it, as
    authored, and the Scene beneath stays."""
    player, app, coordinator = _rig(registry)
    frame = _Frame(registry, coordinator, player, tmp_path)
    with TestClient(app) as client:
        _save_and_start(client, _scene("beneath", keep_last=False))
        _play_one_cycle(client, frame, _scene("top", fade=3, keep_last=True))
        assert _shown(frame.advance(9.5)) == (False, [("photo", 1.0), ("photo", 0.33)])
        assert _shown(frame.advance(1)) == (False, [("photo", 1.0)])


def test_a_kept_photo_followed_by_a_see_through_program_keeps_its_fade(registry, tmp_path):
    """A see-through Program right after a kept photo still follows it: the Player draws the
    kept photo only as its fallback, never beneath a playing layer, so the photo fades out as
    authored and the half-strength Program then shows over black. Holding the photo instead
    would cut from full strength to that. Mutation probe: count only opaque followers (1.0 at
    19.5 s)."""
    player, app, coordinator = _rig(registry)
    frame = _Frame(registry, coordinator, player, tmp_path)
    with TestClient(app) as client:
        veil = _scene("veil", keep_last=False)
        veil["contributions"][0]["opacity"] = .5
        for body in (_scene("first", fade=3, keep_last=True), veil):
            saved = client.put(f"/v1/operator/scenes/{body['scene_id']}", json=body, headers=AUTH)
            assert saved.status_code == 200, saved.text
        now = registry.clock.utc()
        _program(client, frame, "first", now, now + 20)
        _program(client, frame, "veil", now + 20, now + 40)
        frame.sync()
        assert _shown(frame.advance(19.5)) == (False, [("photo", 0.33)])
        assert _shown(frame.advance(1)) == (False, [("photo", 0.5)])  # no kept photo beneath


def _kept_scene_beneath(client, frame):
    """A kept Scene playing on the Frame, drawn (and its photo kept) before a Scene starts on
    top of it."""
    _save_and_start(client, _scene("beneath", keep_last=True))
    frame.sync()
    assert _shown(frame.advance(2)) == (False, [("photo", 1.0)])


def test_after_a_black_ending_the_scene_beneath_takes_over(registry, tmp_path):
    """Owner, 2026-10-10: "When the top show ends, it gets out of the way and the show
    underneath takes over." The top Scene's ending plans no after-state of its own when a Scene
    plays beneath at its end."""
    player, app, coordinator = _rig(registry)
    frame = _Frame(registry, coordinator, player, tmp_path)
    with TestClient(app) as client:
        _kept_scene_beneath(client, frame)
        _play_one_cycle(client, frame, _scene("top", ending=("black", 4), keep_last=False))
        assert [layer.after_end for layer in _layers(coordinator, player)
                if layer.presentation == "black"] == ["leave_as_is"]
        assert _shown(frame.advance(11)) == (False, [("black", 1.0)])  # the ending
        assert _shown(frame.advance(5)) == (False, [("photo", 1.0)])  # the Scene beneath


def test_an_outage_during_an_ending_over_a_scene_beneath_shows_its_kept_photo(registry, tmp_path):
    """Central is lost while the top Scene's (long) ending shows; when the ending is over, the
    Scene beneath's own layers have run out too, and the Frame shows its kept photo, not
    black. Mutation probe: let the ending keep nothing whatever plays beneath (black at the
    end)."""
    player, app, coordinator = _rig(registry)
    frame = _Frame(registry, coordinator, player, tmp_path)
    with TestClient(app) as client:
        _kept_scene_beneath(client, frame)
        _play_one_cycle(client, frame, _scene("top", ending=("black", 40), keep_last=False))
        assert _shown(frame.advance(11)) == (False, [("black", 1.0)])  # 23 s: the ending
        assert _shown(frame.lose_central(45)) == KEPT  # 68 s: past the ending and its cover
