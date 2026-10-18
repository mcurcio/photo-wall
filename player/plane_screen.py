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

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from contracts.node_frame import PictureOperations
from player.gpu import Target
from player.pacing import Word
from player.planes import Composition, Screen
from player.rendering import OutputComposition
from player.wayland_frames import CLIENT_LIBRARY, FrameGrant

PresentRefused = Literal["picture_refused", "output_gone"]
SCREEN_PLANES: Final = 15       # frame-client.h PW_SCREEN_PLANES: one plane is each screen's black (bench T2)


def multiplier(strength: float) -> int:
    """A plane strength as wp_alpha_modifier_v1's multiplier (module rule): round(strength *
    0xffffffff). ValueError("plane_strength") unless 0 < strength <= 1 (a plane at 0 is removed,
    player/planes.py, never sent). Pure."""
    raise NotImplementedError("render-planes S1 bead B3")


@dataclass(frozen=True)
class ScreenPicture:
    """A target the compositor imported (zwp_linux_dmabuf_v1), shown by any commit until forgotten:
    the library's `pw_picture` handle and the target's size as laid out."""
    handle: int
    size: tuple[int, int]


class PlaneScreens:
    """The Player's screens (module rules). Every method on the owning thread."""

    def __init__(self, library: str = CLIENT_LIBRARY, socket: str | None = None) -> None:
        """Load `library` and connect to `socket` (None: $WAYLAND_DISPLAY) with
        `pw_frame_client_connect`. RuntimeError("display_frame_protocol_unavailable") when the
        connection or a required global (xdg_wm_base, wl_subcompositor, zwp_linux_dmabuf_v1 v3+,
        wp_viewporter, wp_single_pixel_buffer_manager_v1, wp_alpha_modifier_v1, wp_presentation,
        pw_frame_manager_v1 v4) is missing."""
        raise NotImplementedError("render-planes S1 bead B3")

    @property
    def fd(self) -> int:
        """The connection's fd: readable when the compositor may have spoken (player/mainloop.py
        watches it)."""
        raise NotImplementedError("render-planes S1 bead B3")

    def dispatch(self) -> None:
        """Read and dispatch what arrived (configures, pings, releases, grants, picture operations,
        presentation feedback). RuntimeError("display_frame_protocol_lost") when the connection is
        gone."""
        raise NotImplementedError("render-planes S1 bead B3")

    def screen(self, output_id: str) -> Screen | None:
        """The Output as Weston reports it now (module rule); None before Weston announced it or
        once it is gone."""
        raise NotImplementedError("render-planes S1 bead B3")

    def grant(self, composition: OutputComposition) -> FrameGrant | None:
        """The shell's grant for `composition`'s binding (`pw_frame_client_grant_revision`), as
        player/wayland_frames.py `WaylandFrames.grant`."""
        raise NotImplementedError("render-planes S1 bead B3")

    def picture(self, output_id: str) -> PictureOperations | None:
        """The Output's newest picture operations, as `WaylandFrames.picture` (an undecodable text
        logged and ignored)."""
        raise NotImplementedError("render-planes S1 bead B3")

    def import_picture(self, target: Target) -> ScreenPicture | None:
        """`target`'s dmabuf as a compositor buffer (`pw_picture_import`, one roundtrip); None when
        the compositor refuses it."""
        raise NotImplementedError("render-planes S1 bead B3")

    def busy(self, picture: ScreenPicture) -> bool:
        """Whether a commit showed `picture` and the compositor has not released it since."""
        raise NotImplementedError("render-planes S1 bead B3")

    def forget(self, picture: ScreenPicture) -> None:
        """Destroy `picture`'s compositor buffer (the target itself stays the Gpu's)."""
        raise NotImplementedError("render-planes S1 bead B3")

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
        raise NotImplementedError("render-planes S1 bead B3")

    def presentation(self, output_id: str, now: float) -> Word | None:
        """The compositor's newest word for the screen's tagged commits (player/pacing.py), as
        `WaylandFrames.presentation`; None before any."""
        raise NotImplementedError("render-planes S1 bead B3")

    def close(self) -> None:
        """Destroy every screen and picture and disconnect."""
        raise NotImplementedError("render-planes S1 bead B3")


__all__ = ["SCREEN_PLANES", "PlaneScreens", "PresentRefused", "ScreenPicture", "multiplier"]
