"""The new path composed (player/renderer_root.py, render planes S3, bead B6): every part faked
through the module's attributes, so the wiring is a unit fact. The parts themselves have their own
tests and the display harness."""
from __future__ import annotations

from pathlib import Path

import pytest

import player.renderer_root as root_module
from contracts.enrollment import OutputReport
from player.renderer_root import start_renderer
from player.rendering import UnavailableRenderer

OUTPUTS = (OutputReport(output_id="HDMI-A-1", width_px=0, height_px=0, connected=True),
           OutputReport(output_id="HDMI-A-2", width_px=0, height_px=0, connected=False))


class Parts:
    """Fakes of every part start_renderer builds, recording into one log."""

    def __init__(self, *, modes=None, known_after=2, failing=None):
        self.log: list[str] = []
        self.modes = {"HDMI-A-1": (3840, 2160)} if modes is None else modes
        self.known_after, self.failing = known_after, failing
        self.screens = self.gpu = self.renderer = self.loop = None
        self.videos: list = []

    def install(self, monkeypatch):
        parts = self

        def made(name):
            parts.log.append(name)
            if parts.failing == name:
                raise RuntimeError(f"{name} refused")

        class Screens:
            def __init__(self, *, socket=None):
                made("screens")
                self.socket, self.dispatched = socket, 0
                parts.screens = self

            @property
            def fd(self):
                return 17

            def dispatch(self):
                self.dispatched += 1
                parts.log.append("dispatch")

            def mode(self, output_id):
                return parts.modes.get(output_id) if self.dispatched >= parts.known_after else None

            def close(self):
                parts.log.append("screens.close")

        class Gpu:
            gl = "the gl"
            renderer = "V3D 7.1"

            def __init__(self):
                made("gpu")
                parts.gpu = self

            def close(self):
                parts.log.append("gpu.close")

        def pass_of(name):
            class Pass:
                def __init__(self, gl):
                    assert gl == "the gl"
                    made(name)

                def close(self):
                    parts.log.append(f"{name}.close")
            return Pass

        class Renderer:
            def __init__(self, screens, device, fitter, patterns, luts, *, stills, videos, gl_renderer):
                made("renderer")
                self.screens, self.device, self.luts = screens, device, luts
                self.stills, self.videos, self.gl_renderer = stills, videos, gl_renderer
                parts.renderer = self

            def frame(self):
                parts.log.append("frame")

            def finish(self):
                parts.log.append("finish")

            def due(self):
                parts.log.append("due")
                return 12.5

            def close(self):
                parts.log.append("renderer.close")

        class Loop:
            def __init__(self, glib, fd, turn):
                made("loop")
                self.glib, self.fd, self.turn = glib, fd, turn
                parts.loop = self

            def wake(self):
                parts.log.append("wake")

            def stop(self):
                parts.log.append("loop.stop")

        class Video:
            def __init__(self, path, on_frame):
                self.path, self.on_frame = path, on_frame
                parts.videos.append(self)

        monkeypatch.setattr(root_module, "PlaneScreens", Screens)
        monkeypatch.setattr(root_module, "Gpu", Gpu)
        monkeypatch.setattr(root_module, "FitPass", pass_of("fit"))
        monkeypatch.setattr(root_module, "PatternPass", pass_of("pattern"))
        monkeypatch.setattr(root_module, "PlaneRenderer", Renderer)
        monkeypatch.setattr(root_module, "StillDecode", "the still decode")
        monkeypatch.setattr(root_module, "VideoFrames", Video)
        monkeypatch.setattr(root_module, "FrameLoop", Loop)
        monkeypatch.setattr(root_module, "_readable", lambda fd, timeout: parts.log.append("wait"))
        return self


class Lane:
    """A dispatcher lane: posts queue until `run`, and a full lane refuses (drops) them."""

    def __init__(self, slots):
        self.slots, self.queued, self.full = slots, [], False

    def __call__(self, callback):
        if not self.full:
            self.queued.append(callback)

    def run(self):
        while self.queued:
            self.queued.pop(0)()


class Dispatcher:
    def __init__(self):
        self.lanes: list[Lane] = []

    def lane(self, slots):
        self.lanes.append(Lane(slots))
        return self.lanes[-1]


def _start(monkeypatch, parts, outputs=OUTPUTS, **kwargs):
    parts.install(monkeypatch)
    dispatcher = Dispatcher()
    return start_renderer("glib", dispatcher, "luts", outputs, **kwargs), dispatcher


def test_the_root_enrolls_the_outputs_with_their_modes_and_wires_the_renderer(monkeypatch):
    parts = Parts()
    root, _ = _start(monkeypatch, parts, socket="wayland-1")
    assert root.fault is None
    assert root.outputs == (OUTPUTS[0].model_copy(update={"width_px": 3840, "height_px": 2160}), OUTPUTS[1])
    assert root.renderer is parts.renderer
    assert parts.screens.socket == "wayland-1"
    assert (parts.renderer.screens, parts.renderer.device) == (parts.screens, parts.gpu)
    assert (parts.renderer.luts, parts.renderer.stills, parts.renderer.gl_renderer) == (
        "luts", "the still decode", "V3D 7.1")
    assert (parts.loop.glib, parts.loop.fd) == ("glib", 17)
    # The modes were read by dispatching the connection until known, waiting on it between.
    assert parts.log[:4] == ["screens", "dispatch", "wait", "dispatch"]


def test_a_mode_not_announced_in_time_leaves_the_output_zero_by_zero(monkeypatch):
    parts = Parts(known_after=10**9)
    root, _ = _start(monkeypatch, parts, mode_wait=0.02)
    assert root.fault is None
    assert root.outputs == OUTPUTS
    assert root.renderer is parts.renderer


def test_the_frame_turn_reads_presents_finishes_and_answers_when_it_is_due(monkeypatch):
    parts = Parts()
    _start(monkeypatch, parts)
    parts.log.clear()
    assert parts.loop.turn() == 12.5
    assert parts.log == ["dispatch", "frame", "finish", "due"]


def test_a_videos_new_frame_goes_through_its_lane_and_then_wakes_the_loop(monkeypatch):
    parts = Parts()
    _, dispatcher = _start(monkeypatch, parts)
    heard: list[str] = []
    video = parts.renderer.videos(Path("/cache/clip.mp4"), lambda: heard.append("frame"))
    assert video.path == Path("/cache/clip.mp4")
    lane = dispatcher.lanes[-1]
    parts.log.clear()
    video.on_frame()                    # a streaming thread: nothing runs there
    assert heard == [] and parts.log == []
    lane.run()                          # the main loop reaches the post
    assert heard == ["frame"] and parts.log == ["wake"]
    lane.full = True                    # a full lane drops the post: the next frame comes
    video.on_frame()
    lane.run()
    assert heard == ["frame"] and parts.log == ["wake"]


def test_close_stops_the_loop_then_closes_each_part_once_in_order(monkeypatch):
    parts = Parts()
    root, _ = _start(monkeypatch, parts)
    parts.log.clear()
    root.close()
    root.close()
    assert parts.log == ["loop.stop", "renderer.close", "fit.close", "pattern.close", "gpu.close",
                         "screens.close"]


@pytest.mark.parametrize("failing", ["screens", "gpu", "pattern", "loop"])
def test_a_part_that_cannot_be_made_leaves_the_player_registrable(monkeypatch, failing):
    parts = Parts(failing=failing)
    root, _ = _start(monkeypatch, parts)
    assert isinstance(root.renderer, UnavailableRenderer)
    assert root.fault == "native_initialization"
    assert root.outputs == OUTPUTS            # as discovered
    made = set(parts.log[:parts.log.index(failing)])
    closed = [entry.split(".")[0] for entry in parts.log if entry.endswith(".close")]
    # A partial start is closed, in close's order.
    assert closed == [name for name in ("renderer", "fit", "pattern", "gpu", "screens") if name in made]
    parts.log.clear()
    root.close()
    assert parts.log == []


def test_no_connected_output_builds_nothing_and_reports_no_fault(monkeypatch):
    parts = Parts()
    outputs = (OUTPUTS[1],)
    root, _ = _start(monkeypatch, parts, outputs=outputs)
    assert isinstance(root.renderer, UnavailableRenderer)
    assert (root.fault, root.outputs, parts.log) == (None, outputs, [])
    root.close()
