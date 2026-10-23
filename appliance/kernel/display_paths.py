"""The display stack's paths: one home for what the base units, the controller and the app
broker share. They live in the kernel, like probe timing, because App lifecycle may not import
Display. tests/node/display/test_node_display_runner.py pins them to the units and the base's
tmpfiles.d; docs/display-host-backend.md#units-and-weston-incarnations explains them."""

from __future__ import annotations

from pathlib import Path
from typing import Final

# Weston's unit; one controller runs per incarnation of it.
DISPLAY_UNIT = "photo-wall-display.service"
# Weston's RuntimeDirectory (pw-display, 0700): control.sock and ingress.sock, gone with Weston.
RUNTIME = Path("/run/photo-wall-display")
# The public Wayland socket's own directory (tmpfiles.d: pw-display:pw-display 0750). It outlives
# every Weston incarnation, so an app in group pw-display binds the directory, never the socket file.
WAYLAND_DIRECTORY = Path("/run/photo-wall-display-wayland")
WAYLAND_SOCKET = WAYLAND_DIRECTORY / "wayland-0"
# The display controller's RuntimeDirectory (pw-display, 0700, preserved): what each display's
# picture last had (appliance/display_host/picture.py `FileAppliedPictures`), so a controller
# restarted with Weston does not write it again. It outlives the controller's restarts, not a reboot.
CONTROLLER_STATE = Path("/run/photo-wall-display-controller")
# The signal range the controller last asked a Weston restart for (appliance/display_host/
# signal_range.py `FileRangeRequests`): writing it is the restart request (the path unit below), and
# the display unit's privileged ExecStartPre= reads it before Weston takes the screen. In the
# controller's runtime directory, so it never outlives a reboot.
SIGNAL_RANGE_REQUEST = CONTROLLER_STATE / "signal-range.json"
# The root oneshot photo-wall-display-signal.path starts when the request changes: it restarts the
# display unit without spending its crash start limit.
SIGNAL_RANGE_UNIT = "photo-wall-display-signal.service"
# Display's mark that it restarted Weston itself (issue #125): photo-wall-display-signal.service's
# ExecStartPre= writes the InvocationID of the Weston it is about to restart, in its own
# RuntimeDirectory (root, 0755, preserved, so it outlives the oneshot, never a reboot). The app
# broker reads it (process_linux.SystemdAppProcessDriver.purposeful_restart): a Player that Weston
# took down is started again only when the mark names the Weston it was launched under.
PURPOSEFUL_RESTART: Final = Path("/run/photo-wall-display-restart/restarted")
