"""Executed explicitly inside the native fixture; never counted as portable tests."""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageDraw

from contracts.models import Calibration, FrameProfile, Layer, OutputBinding, Variant
from player.geometry import compose_pixel, homography, source_uv, transform
from player.native import NativeOutput, NativeRenderer
from player.rendering import LocalLayer, OutputComposition


def wait_for(operation, expected, timeout=8):
    from gi.repository import GLib
    deadline = time.monotonic()+timeout
    last = None
    while time.monotonic() < deadline:
        for _ in range(25):
            if not GLib.MainContext.default().pending():
                break
            GLib.MainContext.default().iteration(False)
        last = operation()
        if last.status == expected:
            return last
        if last.status == "failed" and expected != "failed":
            raise AssertionError(f"native operation failed: {last}")
        time.sleep(.01)
    raise AssertionError(f"native operation timed out: {last}")


def fixture(path, name, *, output="hdmi1", frame="frame1", video=False, position=0):
    data = path.read_bytes()
    variant = Variant(sha256=hashlib.sha256(data).hexdigest(), size=len(data),
                      media_type="video/mp4" if video else
                      "image/jpeg" if path.suffix == ".jpg" else "image/png",
                      width=64, height=48, duration=40 if video else None)
    layer = Layer(assignment_id=name, run_id="run", output_id=output, frame_id=frame,
                  binding_generation=1, start=0, end=100, media_origin=0, variant=variant)
    return LocalLayer(layer, path, position, 1)


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        Image.new("RGB", (64, 48), "white").save(root/"white.jpg")
        Image.new("RGBA", (64, 48), (255, 0, 0, 128)).save(root/"red.png")
        (root/"corrupt.png").write_bytes(b"not a PNG")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=64x48:rate=10:duration=40", "-an", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(root/"video.mp4")],
                       check=True, timeout=30)
        class CapturingRenderer(NativeRenderer):
            def _bus_message(self, bus, message, decoder):
                if message.type == self.Gst.MessageType.ERROR:
                    print("synthetic fixture decoder error:", message.parse_error(), flush=True)
                super()._bus_message(bus, message, decoder)

            def _render(self, area, context, surface):
                result = super()._render(area, context, surface)
                gl = self.GL
                width, height = area.get_allocated_width(), area.get_allocated_height()
                frames[surface.output.output_id] = (width, height, bytes(gl.glReadPixels(
                    0, 0, width, height, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)))
                return result
        frames = {}
        def pixel(output, x=.5, y=.5):
            width, height, data = frames[output]
            offset = ((height-1-int(y*height))*width+int(x*width))*4
            return data[offset:offset+4]
        renderer = CapturingRenderer((NativeOutput("hdmi1", "org.photowall.hdmi1", 320, 240),
                                      NativeOutput("hdmi2", "org.photowall.hdmi2", 320, 240)))
        try:
            binding = OutputBinding(output_id="hdmi1", frame_id="frame1", generation=1,
                                    profile=FrameProfile(width_px=4, height_px=3, diagonal_inches=20))
            second = binding.model_copy(update={"output_id": "hdmi2", "frame_id": "frame2"})
            white = fixture(root/"white.jpg", "white")
            red = fixture(root/"red.png", "red", output="hdmi2", frame="frame2")
            assert renderer.prepare(white).status == "pending"
            wait_for(lambda: renderer.prepare(white), "prepared")
            wait_for(lambda: renderer.prepare(red), "prepared")
            composition = OutputComposition(binding, Calibration(), (white,))
            assert renderer.present(composition).status == "pending"
            ack = wait_for(lambda: renderer.present(composition), "presented")
            assert ack.composition == composition and ack.presented_at is not None
            assert all(abs(value-255) <= 1 for value in pixel("hdmi1"))
            red_composition = OutputComposition(second, Calibration(gain=.5), (red,))
            wait_for(lambda: renderer.present(red_composition), "presented")
            expected = compose_pixel([((1, 0, 0, 128/255), 1, False)], gain=.5)
            assert max(abs(pixel("hdmi2")[i]/255-expected[i]) for i in range(4)) < .015
            black = LocalLayer(Layer(assignment_id="black", run_id="run", output_id="hdmi1",
                                      frame_id="frame1", binding_generation=1, start=0, end=100,
                                      media_origin=0, presentation="black"), None, 0, .5)
            blended = replace(composition, layers=(white, black))
            wait_for(lambda: renderer.present(blended), "presented")
            expected = compose_pixel([((1, 1, 1, 1), 1, False), ((0, 0, 0, 1), .5, False)])
            assert max(abs(pixel("hdmi1")[i]/255-expected[i]) for i in range(4)) < .015
            corrupt = fixture(root/"corrupt.png", "bad")
            wait_for(lambda: renderer.prepare(corrupt), "failed")
            wait_for(lambda: renderer.present(blended), "presented")
            renderer.release("bad")
            video = fixture(root/"video.mp4", "video", video=True, position=12)
            wait_for(lambda: renderer.prepare(video), "prepared")
            video_composition = replace(composition, layers=(video,))
            ack = wait_for(lambda: renderer.present(video_composition), "presented")
            assert 11.85 <= ack.composition.layers[0].position <= 12.3
            # Cover pauses native playback; logical time advances independently by 20s.
            wait_for(lambda: renderer.present(blended), "presented")
            assert not renderer._decoders["video"].playing
            reveal = replace(video, position=32)
            assert renderer.prepare(reveal).status == "pending"
            wait_for(lambda: renderer.prepare(reveal), "prepared")
            ack = wait_for(lambda: renderer.present(replace(composition, layers=(reveal,))), "presented")
            assert 31.85 <= ack.composition.layers[0].position <= 32.3
            assert ack.composition.layers[0].position != 12
            # Exact calibration changes cannot acknowledge a previous draw.
            changed = replace(composition, calibration=Calibration(gain=.25))
            assert renderer.present(changed).status == "pending"
            wait_for(lambda: renderer.present(changed), "presented")
            # A decoder incarnation must not reuse an older texture/sample counter.
            old_incarnation = renderer._decoders["video"].incarnation
            renderer._decoders["video"].failure = "decode"
            renderer._decoders["video"].retry_at = 0
            wait_for(lambda: renderer.prepare(video), "prepared")
            ack = wait_for(lambda: renderer.present(video_composition), "presented")
            assert 11.85 <= ack.composition.layers[0].position <= 12.3
            assert renderer._decoders["video"].incarnation > old_incarnation
            assert renderer._surfaces["hdmi1"].textures["video"][1] == renderer._decoders["video"].incarnation
            renderer.release("video")
            renderer.release("red")
            pattern_image = Image.new("RGBA", (64, 48))
            painter = ImageDraw.Draw(pattern_image)
            for box, color in (((0, 0, 31, 23), "red"), ((32, 0, 63, 23), "lime"),
                               ((0, 24, 31, 47), "blue"), ((32, 24, 63, 47), "white")):
                painter.rectangle(box, fill=color)
            pattern_image.save(root/"pattern.png")
            pattern = fixture(root/"pattern.png", "pattern")
            calibration = Calibration(rotation=90, crop=(.125, .125, .875, .875), gain=.6,
                                      corners=((.1, .1), (.9, .2), (.8, .9), (.2, .8)))
            geometric = replace(composition, calibration=calibration, layers=(pattern,))
            wait_for(lambda: renderer.prepare(pattern), "prepared")
            wait_for(lambda: renderer.present(geometric), "presented")
            for logical in ((.2, .2), (.8, .2), (.8, .8), (.2, .8)):
                x, y = transform(homography(calibration.corners), *logical)
                u, v = source_uv(x, y, calibration, 4/3, 64, 48)
                rgba = tuple(value/255 for value in pattern_image.getpixel((int(u*64), int(v*48))))
                expected = compose_pixel([(rgba, 1, False)], gain=.6)
                assert max(abs(pixel("hdmi1", x, y)[i]/255-expected[i]) for i in range(4)) < .015
            assert pixel("hdmi1", .01, .01) == bytes((0, 0, 0, 255))
            renderer.release("pattern")
            assert renderer.diagnostics()["resident_decoders"] == 1
            renderer._surfaces["hdmi1"].area.make_current()
            print(json.dumps({"result": "passed", "evidence": "native application, software GL",
                              "gtk": [renderer.Gtk.MAJOR_VERSION, renderer.Gtk.MINOR_VERSION],
                              "gstreamer": renderer.Gst.version_string(),
                              "gl": renderer.GL.glGetString(renderer.GL.GL_VERSION).decode(),
                              "renderer": renderer.diagnostics()}, sort_keys=True))
        finally:
            renderer.close()


if __name__ == "__main__":
    main()
