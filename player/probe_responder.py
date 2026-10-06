"""Progress-probe responder: the broker learns whether the control queue moves.

A probe is answered from the Player's GLib main loop on the same queue and
priority as control dispatch, never from this thread: an answer proves that
queue ran. The responder takes its own one-slot lane of the control dispatcher,
so a queued probe never holds a control slot. At most one answer callback is
queued; a newer nonce overwrites the pending one, and nothing is answered when
the probe lane refuses. The channel carries no command authority.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import Protocol

from contracts.node_app_link import (
    MAX_NODE_LINK_BYTES,
    NodeProbeV2,
    encode_node_probe_answer,
    encode_node_probe_open,
    parse_node_app_link_result,
    parse_node_probe_channel_message,
)
from player.local_app_proof import _root_socket
from player.node_app_link import DEFAULT_NODE_LINK_SOCKET

LOG = logging.getLogger("photo_wall.player.probe")
# Slow and capped (below the broker's startup budget S): a broker that refuses
# `probe_open` (one without a probe channel) sees at most one attempt per 5 s.
RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0, 5.0)


class Dispatcher(Protocol):
    """Control dispatch onto the GLib queue; `lane` shares its queue and priority, not its slots."""

    def __call__(self, callback: Callable[[], object]) -> Future: ...

    def lane(self, slots: int) -> Dispatcher: ...


class ProbeResponder:
    def __init__(self, dispatcher: Dispatcher, *,
                 on_relink: Callable[[], None], path: Path = DEFAULT_NODE_LINK_SOCKET,
                 connector: Callable[[Path], socket.socket] = _root_socket,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        # Own one-slot lane: a queued probe can never take one of the control slots.
        self.dispatcher, self.on_relink = dispatcher.lane(1), on_relink
        self.path, self.connector, self.sleep = path, connector, sleep
        self._lock = threading.Lock()
        self._connection: socket.socket | None = None
        self._pending: str | None = None
        self._queued = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("probe responder already started")
        self._thread = threading.Thread(target=self._run, name="player-probe", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        attempt = 0
        while True:
            try:
                probed = self._channel()
            except Exception as error:     # never take the Player down with the channel
                LOG.warning("player: probe channel failed: %s", type(error).__name__)
                probed = False
            if probed:
                attempt = 0
            self.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
            attempt += 1

    def _channel(self) -> bool:
        """One channel lifetime; True once the broker sent a probe on it."""
        try:
            connection = self.connector(self.path)
        except (OSError, ValueError) as error:
            LOG.debug("player: probe channel unavailable: %s", error)
            return False
        probed = False
        try:
            connection.settimeout(None)    # blocking, so MSG_DONTWAIT answers never wait
            opening = encode_node_probe_open()
            if connection.send(opening) != len(opening):
                raise ValueError("probe_open_short")
            with self._lock:
                self._connection = connection
            while True:
                raw = connection.recv(MAX_NODE_LINK_BYTES + 1)
                if not raw:
                    return probed
                try:
                    message = parse_node_probe_channel_message(raw)
                except ValueError:
                    try:
                        LOG.debug("player: probe channel result %s", parse_node_app_link_result(raw))
                    except ValueError:
                        LOG.debug("player: probe channel packet invalid")
                    return probed
                if isinstance(message, NodeProbeV2):
                    probed = True
                    self._probe(message.nonce)
                else:
                    try:
                        self.on_relink()
                    except Exception as error:
                        LOG.warning("player: relink request failed: %s", type(error).__name__)
        except (OSError, ValueError) as error:
            LOG.debug("player: probe channel closed: %s", type(error).__name__)
            return probed
        finally:
            with self._lock:
                # A callback still queued stays counted (`_queued`); it finds nothing to send.
                self._connection, self._pending = None, None
                connection.close()

    def _probe(self, nonce: str) -> None:
        with self._lock:
            self._pending = nonce
            if self._queued:
                return
            self._queued = True
        try:
            future = self.dispatcher(self._answer)
            refused = future.done() and not future.cancelled() and future.exception() is not None
        except Exception:
            refused = True
        if refused:
            # dispatch_capacity: the probe lane is busy, so this probe goes unanswered.
            with self._lock:
                self._pending, self._queued = None, False

    def _answer(self) -> None:
        """Runs on the GLib main thread, at the dispatcher's priority."""
        with self._lock:
            nonce, self._pending, self._queued = self._pending, None, False
            if nonce is None or self._connection is None:
                return
            try:
                self._connection.send(encode_node_probe_answer(nonce), socket.MSG_DONTWAIT)
            except OSError:
                pass    # EAGAIN or a closing channel: drop; the next probe asks again


__all__ = ["RETRY_DELAYS", "Dispatcher", "ProbeResponder"]
