"""Harness-only fake health judge for the production overlay client's judge link.

tests/native_display_smoke.py imports this (as root inside the harness container, run by
/usr/bin/python3) and starts `JudgeFeeder` before Weston; Weston runs with
PHOTO_WALL_HEALTH_SOCKET=PATH, so the private client it spawns inherits the path (the magenta-mode
test clients never connect). Like the real judge's `overlay` op (appliance/health/runner.py) it
takes one `{"op":"overlay"}` packet per connection, never replies to it, and then only sends
instructions; unlike it, it sends them only on the smoke's command, keeps one client (a newer
connection replaces the older), records every `PresentedReport`, and can drop its client.
Instructions and reports are encoded and parsed by Display's own language (overlay/instruction.py,
imported from the source tree the smoke puts on sys.path).
"""

import json
import os
import select
import socket
import threading
import time

from overlay.instruction import (
    OverlayInstruction,
    encode_overlay_instruction,
    parse_presented_report,
)

PATH = "/tmp/pw-health/health.sock"


class JudgeFeeder:
    def __init__(self, path=PATH):
        os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
        if os.path.exists(path):
            os.unlink(path)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.listener.bind(path)
        self.listener.listen(4)
        self.changed = threading.Condition()
        self.client = None
        self.opened = 0          # connections that sent the overlay request
        self.reports = []        # (output, serial), every report in arrival order
        self.refused = []        # anything a client sent that was not Display's language
        self._requested = False
        self._stop = False
        self.thread = threading.Thread(target=self._run, name="judge-feeder", daemon=True)
        self.thread.start()

    def _run(self):
        while not self._stop:
            with self.changed:
                watched = [self.listener] + ([self.client] if self.client else [])
            try:
                readable, _, _ = select.select(watched, [], [], 0.1)
            except (OSError, ValueError):   # a socket closed under select by drop()
                continue
            for connection in readable:
                if connection is self.listener:
                    accepted, _ = self.listener.accept()
                    with self.changed:
                        if self.client is not None:
                            self.client.close()
                        self.client, self._requested = accepted, False
                    continue
                try:
                    raw = connection.recv(4096)
                except OSError:
                    raw = b""
                with self.changed:
                    if connection is not self.client:
                        continue
                    if not raw:
                        self.client.close()
                        self.client = None
                    elif not self._requested:
                        if json.loads(raw) != {"op": "overlay"}:
                            self.refused.append(raw)
                        self._requested = True
                        self.opened += 1
                    else:
                        try:
                            report = parse_presented_report(raw)
                            self.reports.append((report.output, report.serial))
                        except ValueError:
                            self.refused.append(raw)
                    self.changed.notify_all()

    def wait(self, predicate, limit, idle=None):
        """Wait until `predicate()` (under the lock) holds; `idle` runs between checks (the
        smoke drains its control socket there)."""
        end = time.monotonic() + limit
        while True:
            with self.changed:
                if predicate():
                    return
                self.changed.wait(0.05)
                if predicate():
                    return
            assert time.monotonic() < end, ("judge_feeder", self.opened, self.reports,
                                            self.refused)
            if idle is not None:
                idle()

    def await_client(self, opened, limit, idle=None):
        self.wait(lambda: self.opened >= opened and self.client is not None, limit, idle)

    def await_report(self, output, serial, limit, idle=None):
        self.wait(lambda: (output, serial) in self.reports, limit, idle)

    def send(self, output, serial, tint, lines):
        packet = encode_overlay_instruction(OverlayInstruction(output, serial, tint, tuple(lines)))
        with self.changed:
            assert self.client is not None, "judge_feeder: no client"
            self.client.send(packet)

    def drop(self):
        with self.changed:
            if self.client is not None:
                self.client.close()
                self.client = None

    def close(self):
        self._stop = True
        self.thread.join(timeout=2)
        self.drop()
        self.listener.close()
