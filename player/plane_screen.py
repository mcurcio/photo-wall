"""The Player's screens on its own connection to Display Host's Weston (render redesign, run ledger
.claude/runs/render-planes/s1-page.md): no GTK. Each Output's screen is an xdg toplevel
`photo-wall-<output>` (player/output_discovery.py `output_app_id`) the shell maps full screen,
showing opaque black, the parent of one sub-window per plane. A ctypes bridge over the frame
client library (appliance/display_host/native/frame-client.h, the `pw_screen_*` and `pw_picture_*`
functions); importing this module loads no library.

Rules this module owns (one home each):
- **One commit per screen** (`present`): every plane's picture, place, stacking, buffer transform
  and strength reach the compositor in one atomic commit (synchronized sub-windows, the screen's
  commit last), tagged with the Frame's witness tag and the picture change, with presentation
  feedback: so lease, tags and Save's evidence keep their meaning (design, "presented" is that
  commit). A refused present leaves the screen as it was.
- **A picture is reused only once released** (`busy`): the compositor's wl_buffer.release.
- **The strength's wire value**: wp_alpha_modifier_v1's multiplier, round(strength * 0xffffffff).
- **The screen's facts** (`screen`): the Output's mode and wl_output.transform as Weston reports
  them, read through contracts/display_turn.py.

Grants, picture operations and presentation words are read as player/wayland_frames.py reads them
over GDK's connection, through the same library functions, until S3 deletes that path.
"""
from __future__ import annotations

import ctypes
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal
from uuid import UUID

from contracts.display_turn import hanging, turn_of, wayland_transform
from contracts.node_frame import (
    PICTURE_OPERATIONS_BYTES,
    PictureOperations,
    decode_picture_operations,
    picture_change_text,
)
from player.fit_pass import target_size
from player.gpu import Target
from player.pacing import Word
from player.planes import Composition, Screen
from player.rendering import OutputComposition
from player.wayland_frames import CLIENT_LIBRARY, FrameGrant

PresentRefused = Literal["picture_refused", "output_gone"]
SCREEN_PLANES: Final = 15       # frame-client.h PW_SCREEN_PLANES: one plane is each screen's black (bench T2)
LOGGER = logging.getLogger(__name__)
_FULL: Final = 0xFFFFFFFF       # wp_alpha_modifier_v1's opaque multiplier


def multiplier(strength: float) -> int:
    """A plane strength as wp_alpha_modifier_v1's multiplier (module rule): round(strength *
    0xffffffff). ValueError("plane_strength") unless 0 < strength <= 1 (a plane at 0 is removed,
    player/planes.py, never sent). Pure."""
    if not 0 < strength <= 1:
        raise ValueError("plane_strength")
    return round(strength * _FULL)


@dataclass(frozen=True)
class ScreenPicture:
    """A target the compositor imported (zwp_linux_dmabuf_v1), shown by any commit until forgotten:
    the library's `pw_picture` handle and the target's size as laid out."""
    handle: int
    size: tuple[int, int]


class _PlaneState(ctypes.Structure):
    """frame-client.h `struct pw_plane_state`."""
    _fields_ = (("picture", ctypes.c_void_p), ("x", ctypes.c_int32), ("y", ctypes.c_int32),
                ("alpha", ctypes.c_uint32))


def _declare(lib) -> None:
    pointer, text, i32, u32, u64 = (ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32, ctypes.c_uint32,
                                    ctypes.c_uint64)
    out_i32, out_u32, out_u64 = ctypes.POINTER(i32), ctypes.POINTER(u32), ctypes.POINTER(u64)
    for name, argtypes, restype in (
        ("pw_frame_client_connect", (text,), pointer),
        ("pw_frame_client_destroy", (pointer,), None),
        ("pw_frame_client_fd", (pointer,), ctypes.c_int),
        ("pw_frame_client_dispatch", (pointer,), ctypes.c_int),
        ("pw_frame_client_output", (pointer, text, out_i32, out_i32, out_i32), ctypes.c_int),
        ("pw_frame_client_grant_revision", (pointer, text, text, u64, u64, text, out_u32), ctypes.c_int),
        ("pw_frame_client_picture", (pointer, text, text, u32), ctypes.c_int),
        ("pw_screen_create", (pointer, text), pointer),
        ("pw_screen_destroy", (pointer,), None),
        ("pw_picture_import", (pointer, ctypes.c_int, u32, u32, u32, u32, u64), pointer),
        ("pw_picture_busy", (pointer,), ctypes.c_int),
        ("pw_picture_destroy", (pointer,), None),
        ("pw_screen_present", (pointer, ctypes.POINTER(_PlaneState), u32, i32, text, text, text),
         ctypes.c_int64),
        ("pw_screen_presentation", (pointer, out_u64, ctypes.POINTER(ctypes.c_int64), out_u32), ctypes.c_int),
    ):
        function = getattr(lib, name)
        function.argtypes, function.restype = argtypes, restype


class PlaneScreens:
    """The Player's screens (module rules). Every method on the owning thread."""

    def __init__(self, library: str = CLIENT_LIBRARY, socket: str | None = None) -> None:
        """Load `library` and connect to `socket` (None: $WAYLAND_DISPLAY) with
        `pw_frame_client_connect`. RuntimeError("display_frame_protocol_unavailable") when the
        connection or a required global (xdg_wm_base, wl_subcompositor, zwp_linux_dmabuf_v1 v3+,
        wp_viewporter, wp_single_pixel_buffer_manager_v1, wp_alpha_modifier_v1, wp_presentation,
        pw_frame_manager_v1 v4) is missing."""
        self._lib = ctypes.CDLL(library)
        _declare(self._lib)
        self._client = self._lib.pw_frame_client_connect(socket.encode() if socket is not None else None)
        if not self._client:
            raise RuntimeError("display_frame_protocol_unavailable")
        self._screens: dict[str, int] = {}          # output id -> pw_screen
        self._pictures: set[int] = set()            # live pw_picture handles
        # Per Output: the newest picture operations' text read, and the last ones that decoded.
        self._picture_texts: dict[str, str] = {}
        self._operations: dict[str, PictureOperations] = {}

    @property
    def fd(self) -> int:
        """The connection's fd: readable when the compositor may have spoken (player/mainloop.py
        watches it)."""
        return int(self._lib.pw_frame_client_fd(self._client))

    def dispatch(self) -> None:
        """Read and dispatch what arrived (configures, pings, releases, grants, picture operations,
        presentation feedback). RuntimeError("display_frame_protocol_lost") when the connection is
        gone."""
        if self._lib.pw_frame_client_dispatch(self._client) < 0:
            raise RuntimeError("display_frame_protocol_lost")

    def screen(self, output_id: str) -> Screen | None:
        """The Output as Weston reports it now (module rule); None before Weston announced it or
        once it is gone."""
        width, height, transform = ctypes.c_int32(), ctypes.c_int32(), ctypes.c_int32()
        result = self._lib.pw_frame_client_output(self._client, output_id.encode(), width, height, transform)
        if result < 0:
            raise RuntimeError("display_frame_protocol_lost")
        if result == 0:
            return None
        turn = turn_of(transform.value)
        return Screen(output_id, hanging((width.value, height.value), turn), turn)

    def grant(self, composition: OutputComposition) -> FrameGrant | None:
        """The shell's grant for `composition`'s binding (`pw_frame_client_grant_revision`), as
        player/wayland_frames.py `WaylandFrames.grant`."""
        binding = composition.binding
        identity, state = ctypes.create_string_buffer(37), ctypes.c_uint32()
        result = self._lib.pw_frame_client_grant_revision(
            self._client, binding.output_id.encode(), binding.frame_id.encode(), binding.generation,
            binding.configuration_revision, identity, state)
        if result < 0:
            raise RuntimeError("display_frame_protocol_lost")
        if result == 0:
            return None
        return FrameGrant(UUID(identity.value.decode()), binding.generation, binding.configuration_revision,
                          binding.frame_id, state.value in (1, 3))

    def picture(self, output_id: str) -> PictureOperations | None:
        """The Output's newest picture operations, as `WaylandFrames.picture` (an undecodable text
        logged and ignored)."""
        raw = ctypes.create_string_buffer(PICTURE_OPERATIONS_BYTES + 1)
        result = self._lib.pw_frame_client_picture(self._client, output_id.encode(), raw, len(raw))
        if result < 0:
            raise RuntimeError("display_picture_protocol_lost")
        if result == 1:
            text = raw.value.decode(errors="replace")
            if self._picture_texts.get(output_id) != text:      # decode (and log) each new text once
                self._picture_texts[output_id] = text
                try:
                    decoded = decode_picture_operations(text)
                    if decoded.output_id != output_id:
                        raise ValueError("picture_operations_output")
                    self._operations[output_id] = decoded
                except ValueError as error:
                    LOGGER.warning("ignoring undecodable picture operations for %s: %s", output_id, error)
        return self._operations.get(output_id)

    def import_picture(self, target: Target) -> ScreenPicture | None:
        """`target`'s dmabuf as a compositor buffer (`pw_picture_import`, one roundtrip); None when
        the compositor refuses it."""
        width, height = target.size
        handle = self._lib.pw_picture_import(self._client, target.fd, width, height, target.stride,
                                             target.fourcc, target.modifier)
        if not handle:
            return None
        self._pictures.add(handle)
        return ScreenPicture(handle, target.size)

    def busy(self, picture: ScreenPicture) -> bool:
        """Whether a commit showed `picture` and the compositor has not released it since."""
        return picture.handle in self._pictures and self._lib.pw_picture_busy(picture.handle) == 1

    def forget(self, picture: ScreenPicture) -> None:
        """Destroy `picture`'s compositor buffer (the target itself stays the Gpu's)."""
        if picture.handle in self._pictures:
            self._pictures.discard(picture.handle)
            self._lib.pw_picture_destroy(picture.handle)

    def present(self, composition: Composition, pictures: Mapping[str, ScreenPicture], grant: FrameGrant,
                tag: str, picture_change: int | None) -> int | PresentRefused:
        """One commit of `composition`'s screen (module rule): each plane shows
        `pictures[plane.layer.layer.assignment_id]` at its placement, bottom to top, with buffer
        transform `wayland_transform(composition.screen.turn)` and its strength; sub-windows beyond
        the planes show nothing, so a composition with no planes shows the screen's black (the
        candidate's handoff commit, tagged "candidate-<grant id>", before admission). `grant` is a
        current one, admitted or candidate (the caller never presents without one). The tag's
        sequence (>= 1) when committed; "picture_refused" when a picture is missing, forgotten or
        not of its plane's `target_size`; "output_gone" when the Output is not there or its screen
        is not configured yet. ValueError("present_planes") past SCREEN_PLANES planes."""
        planes = composition.planes
        if len(planes) > SCREEN_PLANES:
            raise ValueError("present_planes")
        screen = composition.screen
        states = (_PlaneState * max(1, len(planes)))()
        for state, plane in zip(states, planes, strict=False):
            picture = pictures.get(plane.layer.layer.assignment_id)
            if (picture is None or picture.handle not in self._pictures
                    or picture.size != target_size(plane.at, screen.turn)):
                return "picture_refused"
            state.picture, state.x, state.y = picture.handle, plane.at.x, plane.at.y
            state.alpha = multiplier(plane.strength)
        handle = self._screens.get(screen.output_id)
        if handle is None:
            if self.screen(screen.output_id) is None:
                return "output_gone"
            handle = self._lib.pw_screen_create(self._client, screen.output_id.encode())
            if not handle:
                raise RuntimeError("display_frame_protocol_lost")
            self._screens[screen.output_id] = handle
        result = self._lib.pw_screen_present(
            handle, states, len(planes), wayland_transform(screen.turn), str(grant.grant_id).encode(),
            tag.encode(), picture_change_text(picture_change).encode())
        if result >= 1:
            return int(result)
        if result == -3:
            return "output_gone"
        if result == -2:
            return "picture_refused"
        raise RuntimeError("display_frame_protocol_lost")

    def presentation(self, output_id: str, now: float) -> Word | None:
        """The compositor's newest word for the screen's tagged commits (player/pacing.py), as
        `WaylandFrames.presentation`; None before any."""
        handle = self._screens.get(output_id)
        if handle is None:
            return None
        sequence, at_ns, refresh_ns = ctypes.c_uint64(), ctypes.c_int64(), ctypes.c_uint32()
        result = self._lib.pw_screen_presentation(handle, sequence, at_ns, refresh_ns)
        if result < 0:
            raise RuntimeError("display_frame_protocol_lost")
        if result == 0:
            return None
        if result == 2:
            return Word(sequence.value, None)
        return Word(sequence.value, at_ns.value / 1e9 if at_ns.value >= 0 else now, refresh_ns.value / 1e9)

    def close(self) -> None:
        """Destroy every screen and picture and disconnect."""
        if self._client:
            self._lib.pw_frame_client_destroy(self._client)     # its screens and pictures with it
        self._client = None
        self._screens.clear()
        self._pictures.clear()


__all__ = ["SCREEN_PLANES", "PlaneScreens", "PresentRefused", "ScreenPicture", "multiplier"]
