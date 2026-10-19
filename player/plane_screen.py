"""The Player's screens on its own connection to Display Host's Weston (render redesign, run ledger
.claude/runs/render-planes/s1-page.md): the Player's only Wayland client. Each Output's screen is an xdg toplevel
`photo-wall-<output>` (player/output_discovery.py `output_app_id`) the shell maps full screen,
showing its ground (an opaque black picture the display controller scans out beneath the planes),
the parent of one sub-window per plane. A ctypes bridge over the frame
client library (appliance/display_host/native/frame-client.h, the `pw_screen_*` and `pw_picture_*`
functions); importing this module loads no library.

Rules this module owns (one home each):
- **One commit per screen** (`present`): every plane's picture, place, stacking, buffer transform
  and strength reach the compositor in one atomic commit (synchronized sub-windows, the screen's
  commit last), tagged with the Frame's witness tag and the picture change, with presentation
  feedback: so lease, tags and Save's evidence keep their meaning (design, "presented" is that
  commit). A refused present leaves the screen as it was. Only what changed since the last commit
  is sent (frame-client.h `pw_screen_present`).
- **A picture is reused only once released** (`busy`): the compositor's wl_buffer.release.
- **The strength's wire value**: wp_alpha_modifier_v1's multiplier, round(strength * 0xffffffff).
- **The screen's facts** (`screen`): the Output's mode and wl_output.transform as Weston reports
  them, read through contracts/display_turn.py; the mode as built (`mode`), never turned, is what
  the Player enrolls.
- **The Position trial on this connection** (`trial`, render redesign S3, run ledger
  .claude/runs/render-planes/s3-page.md): the envelope Display Host holds for the grant, applied by
  one player/calibration_trial.py `TrialState` per Output on CLOCK_BOOTTIME milliseconds.
- **Every read wakes the frame loop** (`on_read`, `turning`): each library call that reads the
  connection (`_READS`) may dispatch the compositor's answers, presentation feedback included, which
  the frame loop's fd watch then never sees. A read outside the frame loop's own turn calls
  `on_read` (the root wires the loop's `wake`), so an answer the tick consumed still turns the loop.
- **The trial overlay** (`overlay_corners`, `present`'s `overlay`): the drawn placement's corners as
  fractions of the screen as it hangs, sent as JSON on the screen's surface under the very grant and
  tag of the present that follows, so the shell draws it only over that commit.
- **The trial's tag** (`commit_tag`): a commit drawn under a Position trial is tagged with the trial's
  frame tag, never the composition's, whatever the screen shows (photos, the commissioning grid over
  a Frame with nothing to play): the shell draws the overlay only over a commit under that tag, and
  Display Host reports that commit to Central as the trial presented (central/fleet/node_calibration.py
  `deliver_in`), which the console's Done waits for.
"""
from __future__ import annotations

import ctypes
import json
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final, Literal
from uuid import UUID

from contracts.display_turn import hanging, turn_of, wayland_transform
from contracts.frame_fit import Placement
from contracts.node_calibration import MAX_TRIAL_BYTES
from contracts.node_frame import (
    PICTURE_OPERATIONS_BYTES,
    PictureOperations,
    decode_picture_operations,
    frame_witness_tag,
    picture_change_text,
)
from player.calibration_trial import AppliedTrial, TrialState
from player.fit_pass import target_size
from player.gpu import Target
from player.pacing import Word
from player.planes import Composition, Screen
from player.rendering import LocalLayer, OutputComposition

CLIENT_LIBRARY = "/usr/lib/photo-wall/frame-client/libphoto-wall-frame-client.so"
PresentRefused = Literal["picture_refused", "output_gone"]
SCREEN_PLANES: Final = 15       # frame-client.h PW_SCREEN_PLANES: one plane is each screen's ground (bench T2)
LOGGER = logging.getLogger(__name__)
_FULL: Final = 0xFFFFFFFF       # wp_alpha_modifier_v1's opaque multiplier
OverlayCorners = tuple[tuple[float, float], tuple[float, float], tuple[float, float], tuple[float, float]]
"""A trial overlay's four corners (top-left, top-right, bottom-right, bottom-left), each (x, y) as
fractions 0..1 of the screen as it hangs: the shell's diagnostic client draws them over the Output's
logical size (frame protocol v2 `overlay`; appliance/display_host/overlay/render.py `render_trial`)."""


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


def commit_tag(composition: OutputComposition, visible_layers: tuple[LocalLayer, ...],
               trial: AppliedTrial | None) -> str:
    """The tag a screen's commit carries (module rule, The trial's tag): while a Position trial is
    drawn, the trial's own (contracts/node_calibration.py `TrialCandidate.frame_tag`, "trial-" + its
    candidate hash), else `composition_tag` of the drawn layers. Pure."""
    return composition_tag(composition, visible_layers) if trial is None else trial.candidate.frame_tag


def overlay_corners(placement: Placement, screen: tuple[int, int]) -> OverlayCorners:
    """The trial overlay's corners of a Frame drawn at `placement` on a screen of `screen` = (width,
    height) pixels as it hangs: ((x, y), (x + w, y), (x + w, y + h), (x, y + h)), each divided by the
    screen's width and height. The one home of the overlay's primitives (player/calibration_trial.py no
    longer computes them: a Rectangle has no corners of its own). ValueError("overlay_screen") for a
    side below 1, ValueError("overlay_outside") for a placement not wholly on the screen (or with a
    side below 1). Pure."""
    width, height = screen
    if width < 1 or height < 1:
        raise ValueError("overlay_screen")
    left, top, right, bottom = placement.x, placement.y, placement.x + placement.width, placement.y + placement.height
    if placement.width < 1 or placement.height < 1 or left < 0 or top < 0 or right > width or bottom > height:
        raise ValueError("overlay_outside")
    return ((left / width, top / height), (right / width, top / height),
            (right / width, bottom / height), (left / width, bottom / height))


def _boottime_ms() -> int:
    """CLOCK_BOOTTIME now in milliseconds: the clock a trial's expiries are on."""
    return time.clock_gettime_ns(time.CLOCK_BOOTTIME) // 1_000_000


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
                ("width", ctypes.c_int32), ("height", ctypes.c_int32), ("alpha", ctypes.c_uint32))


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
        ("pw_frame_client_trial", (pointer, text, text, text, u32), ctypes.c_int),
        ("pw_screen_create", (pointer, text), pointer),
        ("pw_screen_size", (pointer, out_i32, out_i32), ctypes.c_int),
        ("pw_screen_overlay", (pointer, text, text, text), ctypes.c_int),
        ("pw_screen_destroy", (pointer,), None),
        ("pw_picture_import", (pointer, ctypes.c_int, u32, u32, u32, u32, u64), pointer),
        ("pw_picture_busy", (pointer,), ctypes.c_int),
        ("pw_picture_destroy", (pointer,), None),
        ("pw_screen_present", (pointer, ctypes.POINTER(_PlaneState), u32, pointer, i32, text, text, text),
         ctypes.c_int64),
        ("pw_screen_presentation", (pointer, out_u64, ctypes.POINTER(ctypes.c_int64), out_u32), ctypes.c_int),
    ):
        function = getattr(lib, name)
        function.argtypes, function.restype = argtypes, restype


# The library's calls that read and dispatch the connection (frame-client.c: each calls
# `pw_frame_client_dispatch`), so the compositor's answers may arrive through any of them.
_READS: Final = frozenset({
    "pw_frame_client_output", "pw_frame_client_grant_revision", "pw_frame_client_picture",
    "pw_frame_client_trial", "pw_screen_size", "pw_screen_overlay", "pw_screen_present",
    "pw_screen_presentation"})


class _Reading:
    """The frame client library with every call in `_READS` followed by `read()` (module rule, Every
    read wakes the frame loop); every other attribute is the library's own."""

    def __init__(self, library, read: Callable[[], None]) -> None:
        self._library, self._read = library, read

    def __getattr__(self, name: str):
        value = getattr(self._library, name)
        if name not in _READS:
            return value
        read = self._read

        def call(*args):
            try:
                return value(*args)
            finally:
                read()
        return call


class PlaneScreens:
    """The Player's screens (module rules). Every method on the owning thread."""

    def __init__(self, library: str = CLIENT_LIBRARY, socket: str | None = None) -> None:
        """Load `library` and connect to `socket` (None: $WAYLAND_DISPLAY) with
        `pw_frame_client_connect`. RuntimeError("display_frame_protocol_unavailable") when the
        connection or a required global (xdg_wm_base, wl_subcompositor, zwp_linux_dmabuf_v1 v3+,
        wp_viewporter, wp_single_pixel_buffer_manager_v1, wp_alpha_modifier_v1, wp_presentation,
        pw_frame_manager_v1 v4) is missing."""
        library_handle = ctypes.CDLL(library)
        _declare(library_handle)
        self._lib = _Reading(library_handle, self._read)
        # Called after a read outside `turning` (module rule); the root wires the frame loop's wake.
        self.on_read: Callable[[], None] | None = None
        self._turning = False
        self._client = self._lib.pw_frame_client_connect(socket.encode() if socket is not None else None)
        if not self._client:
            raise RuntimeError("display_frame_protocol_unavailable")
        self._screens: dict[str, int] = {}          # output id -> pw_screen
        self._pictures: set[int] = set()            # live pw_picture handles
        # Per Output: the newest picture operations' text read, and the last ones that decoded.
        self._picture_texts: dict[str, str] = {}
        self._operations: dict[str, PictureOperations] = {}
        self._trials: dict[str, TrialState] = {}    # output id -> its trial's fences

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

    @contextmanager
    def turning(self) -> Iterator[None]:
        """The frame loop's turn (module rule, Every read wakes the frame loop): `dispatch`, then the
        turn's own reads, none of which calls `on_read` (the turn is the read's answer)."""
        self._turning = True
        try:
            self.dispatch()
            yield
        finally:
            self._turning = False

    def _read(self) -> None:
        if not self._turning and self.on_read is not None:
            self.on_read()

    def screen(self, output_id: str) -> Screen | None:
        """The Output as Weston reports it now (module rule); None before Weston announced it or
        once it is gone."""
        output = self._output(output_id)
        if output is None:
            return None
        width, height, transform = output
        turn = turn_of(transform)
        return Screen(output_id, hanging((width, height), turn), turn)

    def _output(self, output_id: str) -> tuple[int, int, int] | None:
        """`pw_frame_client_output`: the mode's (width, height) as built and the wl_output.transform."""
        width, height, transform = ctypes.c_int32(), ctypes.c_int32(), ctypes.c_int32()
        result = self._lib.pw_frame_client_output(self._client, output_id.encode(), width, height, transform)
        if result < 0:
            raise RuntimeError("display_frame_protocol_lost")
        if result == 0:
            return None
        return width.value, height.value, transform.value

    def mode(self, output_id: str) -> tuple[int, int] | None:
        """The Output's current mode as built, (width, height) in physical pixels, never turned
        (`pw_frame_client_output`'s width and height; `screen` turns them): what the Player reports to
        Central at enrollment (contracts/enrollment.py `OutputReport`, Central's `outputs.observation`).
        None before Weston announced the Output or once it is gone. RuntimeError
        ("display_frame_protocol_lost") on transport failure."""
        output = self._output(output_id)
        return None if output is None else output[:2]

    def trial(self, composition: OutputComposition, grant: FrameGrant) -> AppliedTrial | None:
        """The Position trial Display Host holds for `composition`'s Output under `grant`
        (`pw_frame_client_trial`), applied by this Output's player/calibration_trial.py `TrialState`
        (its fences, expiry and baseline checks) at CLOCK_BOOTTIME now; None when none is drawn.
        RuntimeError("display_trial_protocol_lost") on transport failure; ValueError as
        `TrialState.apply` (a trial whose calibration or lighting refuses, a changed hash)."""
        output_id = composition.binding.output_id
        raw = ctypes.create_string_buffer(MAX_TRIAL_BYTES + 1)
        result = self._lib.pw_frame_client_trial(self._client, output_id.encode(), str(grant.grant_id).encode(),
                                                 raw, len(raw))
        if result < 0:
            raise RuntimeError("display_trial_protocol_lost")
        state = self._trials.setdefault(output_id, TrialState())
        return state.apply(raw.value.decode() if result else None, composition, _boottime_ms())

    def grant(self, composition: OutputComposition) -> FrameGrant | None:
        """The shell's grant for `composition`'s binding (`pw_frame_client_grant_revision`)."""
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
                tag: str, picture_change: int | None, ground: ScreenPicture | None = None,
                overlay: OverlayCorners | None = None) -> int | PresentRefused:
        """One commit of `composition`'s screen (module rule): each plane shows
        `pictures[plane.layer.layer.assignment_id]` (its finished picture, `plane.size` as it hangs)
        over its placement (`plane.at`: the sub-window's position and viewport destination, so the
        display controller enlarges a picture finished smaller), bottom to top, with buffer
        transform `wayland_transform(composition.screen.turn)` and its strength; sub-windows beyond
        the planes show nothing, so a composition with no planes shows the screen's ground (the
        candidate's handoff commit, tagged "candidate-<grant id>", before admission). `ground` is
        what the screen's own surface shows beneath the planes: an opaque black picture laid out
        for the whole screen (`target_size` of the screen's full placement), or None for the
        compositor's single-pixel black, which Weston can only composite. `grant` is a
        current one, admitted or candidate (the caller never presents without one). The tag's
        sequence (>= 1) when committed; "picture_refused" when a picture is missing, forgotten or
        not of its plane's `target_size(plane.size, turn)` (the ground likewise, of the whole screen's); "output_gone" when the Output is not there or its screen
        is not configured yet. ValueError("present_planes") past SCREEN_PLANES planes.

        `overlay` (a Position trial's corners, `overlay_corners`; `tag` is then the trial's) is sent
        first, on the screen's surface under `grant` and `tag` (`pw_screen_overlay`), so the shell
        draws it over this very commit (module rule). RuntimeError("display_trial_overlay_failed")
        when the library refuses it, before anything is committed."""
        planes = composition.planes
        if len(planes) > SCREEN_PLANES:
            raise ValueError("present_planes")
        screen = composition.screen
        states = (_PlaneState * max(1, len(planes)))()
        for state, plane in zip(states, planes, strict=False):
            picture = pictures.get(plane.layer.layer.assignment_id)
            if (picture is None or picture.handle not in self._pictures
                    or picture.size != target_size(plane.size, screen.turn)):
                return "picture_refused"
            state.picture, state.x, state.y = picture.handle, plane.at.x, plane.at.y
            state.width, state.height = plane.at.width, plane.at.height
            state.alpha = multiplier(plane.strength)
        if ground is not None and (ground.handle not in self._pictures or ground.size != target_size(
                screen.size, screen.turn)):
            return "picture_refused"
        handle = self._screens.get(screen.output_id)
        if handle is None:
            if self.screen(screen.output_id) is None:
                return "output_gone"
            handle = self._lib.pw_screen_create(self._client, screen.output_id.encode())
            if not handle:
                raise RuntimeError("display_frame_protocol_lost")
            self._screens[screen.output_id] = handle
        grant_id = str(grant.grant_id).encode()
        if overlay is not None:
            width, height = ctypes.c_int32(), ctypes.c_int32()
            configured = self._lib.pw_screen_size(handle, width, height)
            if configured < 0:
                raise RuntimeError("display_frame_protocol_lost")
            if configured == 0:
                return "output_gone"            # as the present would answer: nothing shows yet
            primitives = json.dumps(overlay, separators=(",", ":"), allow_nan=False).encode()
            if self._lib.pw_screen_overlay(handle, grant_id, tag.encode(), primitives) != 0:
                raise RuntimeError("display_trial_overlay_failed")
        result = self._lib.pw_screen_present(
            handle, states, len(planes), None if ground is None else ground.handle, wayland_transform(screen.turn),
            grant_id, tag.encode(), picture_change_text(picture_change).encode())
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


__all__ = ["CLIENT_LIBRARY", "SCREEN_PLANES", "FrameGrant", "OverlayCorners", "PlaneScreens", "PresentRefused",
           "ScreenPicture", "commit_tag", "composition_tag", "multiplier", "overlay_corners"]
