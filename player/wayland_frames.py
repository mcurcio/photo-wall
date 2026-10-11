"""App-owned, render-thread-only bridge to the base Wayland frame protocol.

Requests correlate a draw with its next GDK surface commit; only DisplayHost's
server-side feedback is evidence. No privileged controller socket is opened here.
"""

from __future__ import annotations

import ctypes
import json
import logging
import threading
from dataclasses import dataclass
from uuid import UUID

from contracts.node_frame import (
    PICTURE_OPERATIONS_BYTES,
    PictureOperations,
    decode_picture_operations,
    frame_witness_tag,
    picture_change_text,
)
from player.rendering import LocalLayer, OutputComposition

CLIENT_LIBRARY = "/usr/lib/photo-wall/frame-client/libphoto-wall-frame-client.so"
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FrameGrant:
    grant_id: UUID
    binding_generation: int
    config_revision: int
    frame_id: str
    admitted: bool

    def matches(self, composition: OutputComposition) -> bool:
        return (
            composition.binding.frame_id == self.frame_id
            and composition.binding.generation == self.binding_generation
            and composition.binding.configuration_revision == self.config_revision
        )


def composition_tag(composition: OutputComposition, visible_layers: tuple[LocalLayer, ...]) -> str:
    """Witness exactly the renderer's drawn layers; visibility has one owner."""
    return frame_witness_tag(
        output_id=composition.binding.output_id,
        frame_id=composition.binding.frame_id,
        binding_generation=composition.binding.generation,
        config_revision=composition.binding.configuration_revision,
        calibration=composition.calibration.model_dump(mode="json"),
        layers=tuple({"assignment_id": local.layer.assignment_id,
                      "variant_sha256": local.layer.variant.sha256 if local.layer.variant else None}
                     for local in visible_layers),
    )


class WaylandFrames:
    def __init__(self, gdk_display):
        self.owner = threading.get_ident()
        self.trials = {}
        # Per Output: the newest picture operations' text read, and the last ones that decoded.
        self.picture_texts: dict[str, str] = {}
        self.operations: dict[str, PictureOperations] = {}
        self.lib = ctypes.CDLL(CLIENT_LIBRARY)  # sealed app path; no base fallback
        self.gdk = ctypes.CDLL("libgdk-3.so.0")
        pointer = ctypes.c_void_p
        self.gdk.gdk_wayland_display_get_wl_display.argtypes = (pointer,)
        self.gdk.gdk_wayland_display_get_wl_display.restype = pointer
        self.gdk.gdk_wayland_window_get_wl_surface.argtypes = (pointer,)
        self.gdk.gdk_wayland_window_get_wl_surface.restype = pointer
        self.lib.pw_frame_client_create.argtypes = (pointer,)
        self.lib.pw_frame_client_create.restype = pointer
        self.lib.pw_frame_client_destroy.argtypes = (pointer,)
        self.lib.pw_frame_client_destroy.restype = None
        # Frame protocol v4 (1c): every tag names the picture change its buffer was drawn with.
        self.lib.pw_frame_client_tag_picture_next_commit.argtypes = (
            pointer,
            pointer,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
        )
        self.lib.pw_frame_client_tag_picture_next_commit.restype = ctypes.c_int
        self.lib.pw_frame_client_picture.argtypes = (
            pointer,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        )
        self.lib.pw_frame_client_picture.restype = ctypes.c_int
        self.lib.pw_frame_client_grant.argtypes = (
            pointer,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint32),
        )
        self.lib.pw_frame_client_grant.restype = ctypes.c_int
        self.lib.pw_frame_client_grant_revision.argtypes = (
            pointer,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_uint64,
            ctypes.c_uint64,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint32),
        )
        self.lib.pw_frame_client_grant_revision.restype = ctypes.c_int
        self.lib.pw_frame_client_trial.argtypes = (
            pointer,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        )
        self.lib.pw_frame_client_trial.restype = ctypes.c_int
        self.lib.pw_frame_client_overlay.argtypes = (
            pointer,
            pointer,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
        )
        self.lib.pw_frame_client_overlay.restype = ctypes.c_int
        display = self.gdk.gdk_wayland_display_get_wl_display(self._pointer(gdk_display))
        self.client = self.lib.pw_frame_client_create(display)
        if not self.client:
            raise RuntimeError("display_frame_protocol_unavailable")

    @staticmethod
    def _pointer(obj) -> int:
        capsule = ctypes.pythonapi.PyCapsule_GetPointer
        capsule.argtypes = (ctypes.py_object, ctypes.c_char_p)
        capsule.restype = ctypes.c_void_p
        pointer = capsule(obj.__gpointer__, None)
        if not pointer:
            raise ValueError("gobject_pointer_unavailable")
        return pointer

    def _thread(self) -> None:
        if threading.get_ident() != self.owner:
            raise RuntimeError("wayland_frame_render_thread_required")

    def grant(
        self, output_id: str, composition: OutputComposition | None = None
    ) -> FrameGrant | None:
        self._thread()
        identity = ctypes.create_string_buffer(37)
        frame = ctypes.create_string_buffer(97)
        generation, revision, state = ctypes.c_uint64(), ctypes.c_uint64(), ctypes.c_uint32()
        if composition is None:
            result = self.lib.pw_frame_client_grant(
                self.client, output_id.encode(), identity, frame, generation, revision, state
            )
        else:
            frame.value = composition.binding.frame_id.encode()
            generation.value = composition.binding.generation
            revision.value = composition.binding.configuration_revision
            result = self.lib.pw_frame_client_grant_revision(
                self.client, output_id.encode(), composition.binding.frame_id.encode(), generation.value, revision.value, identity, state
            )
        if result < 0:
            raise RuntimeError("display_frame_protocol_lost")
        if result == 0:
            return None
        return FrameGrant(
            UUID(identity.value.decode()), generation.value, revision.value, frame.value.decode(), state.value in (1, 3)
        )

    def trial(self, composition: OutputComposition, grant: FrameGrant):
        import time

        from player.calibration_trial import TrialState

        self._thread()
        raw = ctypes.create_string_buffer(4097)
        result = self.lib.pw_frame_client_trial(
            self.client,
            composition.binding.output_id.encode(),
            str(grant.grant_id).encode(),
            raw,
            len(raw),
        )
        if result < 0:
            raise RuntimeError("display_trial_protocol_lost")
        state = self.trials.setdefault(composition.binding.output_id, TrialState())
        return state.apply(
            raw.value.decode() if result else None,
            composition,
            time.clock_gettime_ns(time.CLOCK_BOOTTIME) // 1_000_000,
        )

    def picture(self, output_id: str) -> PictureOperations | None:
        """The Output's newest picture operations from the shell's `picture` event (frame protocol
        v4), decoded by contracts.node_frame; None before any arrived. An undecodable one is logged
        and ignored: the last decoded value stands."""
        self._thread()
        raw = ctypes.create_string_buffer(PICTURE_OPERATIONS_BYTES + 1)
        result = self.lib.pw_frame_client_picture(self.client, output_id.encode(), raw, len(raw))
        if result < 0:
            raise RuntimeError("display_picture_protocol_lost")
        if result == 0:
            return self.operations.get(output_id)
        text = raw.value.decode(errors="replace")
        if self.picture_texts.get(output_id) != text:      # decode (and log) each new text once
            self.picture_texts[output_id] = text
            try:
                decoded = decode_picture_operations(text)
                if decoded.output_id != output_id:
                    raise ValueError("picture_operations_output")
                self.operations[output_id] = decoded
            except ValueError as error:
                LOGGER.warning("ignoring undecodable picture operations for %s: %s", output_id, error)
        return self.operations.get(output_id)

    def tag_rendered_buffer(
        self, gdk_window, grant: FrameGrant, frame_tag: str, primitives=None, *,
        picture_change: int | None = None
    ) -> None:
        """Only call within a successful GLArea render, before GDK's paint commit. The tag names
        the picture change the buffer was drawn with (None: neutral, none received)."""
        self._thread()
        surface = self.gdk.gdk_wayland_window_get_wl_surface(self._pointer(gdk_window))
        if primitives is not None:
            raw = json.dumps(primitives, separators=(",", ":"), allow_nan=False).encode()
            if (
                self.lib.pw_frame_client_overlay(
                    self.client, surface, str(grant.grant_id).encode(), frame_tag.encode(), raw
                )
                != 0
            ):
                raise RuntimeError("display_trial_overlay_failed")
        if (
            not surface
            or self.lib.pw_frame_client_tag_picture_next_commit(
                self.client, surface, str(grant.grant_id).encode(), frame_tag.encode(),
                picture_change_text(picture_change).encode()
            )
            != 0
        ):
            raise RuntimeError("display_frame_tag_failed")

    def close(self) -> None:
        self._thread()
        self.lib.pw_frame_client_destroy(self.client)
        self.client = None
