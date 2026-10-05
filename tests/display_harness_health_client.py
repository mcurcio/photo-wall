#!/usr/bin/python3 -IB
"""Harness-only private client: B5's overlay client plus one health layer per Output.

tests/native_display_smoke.py installs this file over the shell's spawn path
(/usr/lib/photo-wall-display/diagnostic-client) inside the harness container, so the shell spawns
it as its private client. It composes `overlay.client` unchanged (slate, ack, trial: the harness's
handoff needs the slate's `diagnostic_presented`) through its `OutputHook`, binds the manager v3
and, per Output, takes the health layer and commits one buffer: transparent except a known tint
over the bottom-right quarter, so the B0/B5 pixels (40, 40) and the Output centre stay untinted.

A one-shot mode file, read and removed at start, makes the next spawn misbehave on purpose:
`v2` binds the manager v2 and still asks for a health layer; `duplicate` asks twice for one
Output. Each must end this client with a protocol error (logged by libwayland to Weston's log).
Two modes leave the layer unmapped, writing MARKER_FILE once done: `bare` takes the layer and
commits it without a buffer (marker `bare`); `unmap` maps the tint, then on that commit's
presentation feedback commits a NULL buffer (marker: the feedback, `presented` or `discarded`).
`production` runs the production client unchanged (`main()` with its own health hooks and judge
link), so the smoke can drive the real health drawing against its fake judge.
Same `sys.path` rule as the production launcher (`-I` implies `-P`).
"""
import mmap
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from overlay.client import main  # noqa: E402

MODE_FILE = "/tmp/pw-health-client-mode"
MARKER_FILE = "/tmp/pw-health-client-marker"
TINT = bytes((0x80, 0x00, 0x80, 0x80))  # ARGB8888 little-endian, premultiplied: magenta at 0.5
CLEAR = bytes(4)


def read_mode() -> str:
    try:
        with open(MODE_FILE) as handle:
            mode = handle.read().strip()
        os.unlink(MODE_FILE)
        return mode
    except FileNotFoundError:
        return ""


def mark(text: str) -> None:
    with open(MARKER_FILE + ".tmp", "w") as handle:
        handle.write(text)
    os.replace(MARKER_FILE + ".tmp", MARKER_FILE)


class HealthTint:
    """OutputHook: one health surface per Output, one tinted buffer committed once."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.done: set[str] = set()
        self.held: list[object] = []      # pywayland destroys a collected proxy

    def output_configured(self, client, output) -> None:
        if output.name in self.done or not output.width or not output.height:
            return
        self.done.add(output.name)
        surface = client.compositor.create_surface()
        self.held += [surface, client.manager.get_health_layer(surface, output.name)]
        if self.mode == "duplicate":
            second = client.compositor.create_surface()
            self.held += [second, client.manager.get_health_layer(second, output.name)]
        if self.mode == "bare":
            surface.commit()
            mark("bare")
            return
        width, height = output.width, output.height
        left, top = width * 3 // 4, height * 3 // 4
        row = CLEAR * left + TINT * (width - left)
        size = width * height * 4
        fd = os.memfd_create("photo-wall-health", os.MFD_CLOEXEC)
        try:
            os.ftruncate(fd, size)
            with mmap.mmap(fd, size) as pixels:
                pixels[top * width * 4:] = row * (height - top)
            pool = client.shm.create_pool(fd, size)
            buffer = pool.create_buffer(0, width, height, width * 4, 0)  # WL_SHM_FORMAT_ARGB8888
            pool.destroy()
        finally:
            os.close(fd)
        self.held.append(buffer)
        if self.mode == "unmap":
            feedback = client.presentation.feedback(surface)
            self.held.append(feedback)
            feedback.dispatcher["presented"] = lambda *_: self.unmap(surface, "presented")
            feedback.dispatcher["discarded"] = lambda *_: self.unmap(surface, "discarded")
        surface.attach(buffer, 0, 0)
        surface.damage(0, 0, width, height)
        surface.commit()

    @staticmethod
    def unmap(surface, outcome: str) -> None:
        surface.attach(None, 0, 0)
        surface.commit()
        mark(outcome)


if __name__ == "__main__":
    mode = read_mode()
    if mode == "production":
        sys.exit(main())
    sys.exit(main(hooks=(HealthTint(mode),), manager_version=2 if mode == "v2" else 3))
