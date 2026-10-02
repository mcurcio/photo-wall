# Base DisplayHost native backend

Status: implementation in progress, 2026-09-30. This document owns the native
adapter interface. The [node domain model](player-node-domain-model.md#display-and-calibration)
owns product policy and the [fleet implementation map](player-fleet-implementation-map.md)
owns remaining integration. No physical Pi, HDMI, continuity, timing or calibration
Save qualification is established by this source.

## Selected backend and responsibilities

The opt-in node display service uses a base-owned Weston **14** shell module,
`photo-wall-shell.so`, replacing kiosk-shell for this path. libweston has a
versioned native ABI; Meson requires `libweston-14`; Weston 14 includes its desktop API in that library.
The native Debian build locks the actual package versions from the repository's
pinned authenticated Debian snapshot and records its package inventory. A major
ABI check alone is not the release package lock.

The shell owns per-Output constrained app views, a protected diagnostic layer,
connection/mode generations, process checks, and the five-second renewable surface
lease. The controller owns portable DisplayHost admission and receives protected
root-only lifecycle/configuration requests. The app never receives that socket.
The shell and its Cairo diagnostic client run from the base, independently of app
files or imports. The sealed Player receives only the public Wayland socket.

The diagnostic client is launched by the shell at the fixed base path
`/usr/lib/photo-wall-display/diagnostic-client`. A private socketpair is passed as
`WAYLAND_SOCKET`; the compositor retains that exact `wl_client` as the capability
to bind the diagnostic protocol. A public client with the same app ID, UID, or
claimed PID cannot obtain that role. Client failure restores a compositor-owned
solid curtain and triggers bounded restart attempts. The curtain is cover, not
proof of a readable error page. Actual diagnostic presentation is reported only
from presentation feedback for the private client's committed page buffer.

The page shows bounded predefined status text, the physical Output ID and actual
pixel dimensions, `Frame binding unconfirmed`, and unavailable panel brightness.
Registry binding-name/freshness adoption and Identify/calibration overlays remain
separate integration work. App pixels cannot set these diagnostic strings.

## Authority and presentation evidence

The private controller socket is Unix `SOCK_SEQPACKET`, mode `0600`, with an exact
configured peer UID check. The Python controller also checks the compositor PID
against its base systemd service. The separate root-only controller ingress checks
`SO_PEERCRED`; it is never mounted into the app environment. Candidate admission
brackets `/proc` PID/start-time/UID/cgroup with the base service manager's MainPID
and InvocationID. Weston independently checks the Wayland client's kernel PID/UID
and current process start ticks before mapping and accepting each presentation.
Invocation identity is supplied by the protected controller; an app ID is only a
connector routing label, never process identity.

The canonical machine interface for per-commit tags and private diagnostic roles
is [`photo-wall-frame-v1.xml`](../appliance/display_host/native/photo-wall-frame-v1.xml).
The shell does not trust a frame token as evidence. The app must submit a current
base-issued grant UUID and bounded frame tag, request `wp_presentation.feedback`,
and commit the **same actual rendered surface buffer**. The tag is consumed once.
A server-side Wayland protocol logger correlates that feedback resource with the
surface commit and records only outgoing compositor `presented` events; `discarded`
produces no presentation evidence. No client frame callback or repaint request is
substituted. Superseded commits cannot refresh a successor's lease.

The resulting evidence carries the exact process, local app epoch, OutputKey,
binding generation, **per-Output `OutputBinding.configuration_revision`**, grant,
frame tag, and compositor-created buffer serial. It means that buffer contributed
to a compositor presentation; it is not proof of physical panel pixels or correct
application geometry. Static photos must renew with fresh tagged commits and
presentation feedback. Missing tag/feedback support leaves the lease unrenewed
and restores the base diagnostic.

A fully opaque diagnostic occludes candidate content and cannot provide meaningful
candidate presentation feedback. During an explicitly authorized `starting_new`
phase the protected page becomes translucent. The candidate must be synthetic
handoff/test content: this is not permission to expose authored Scene content or
withdraw healthy playback during background staging. Handoff authorization follows
fresh candidate evidence. Diagnostic removal is acknowledged only by an actual
app presentation from a commit **after** that removal request. Runtime commitment
remains an independent owner decision.

Output disconnect, resolution/refresh/scale/transform changes, controller restart,
process loss and lease expiry invalidate affected authority. Mode/connection
generations and the compositor incarnation fence connector reuse. A new controller
connection explicitly invalidates old grants rather than inventing recovered
presentation evidence. Compositor loss remains Output unknown until recovery.

## Base controller ingress

`python -m appliance.display_host.runner --config <systemd-credential>` starts the
controller. The credential is a JSON object with an optional absolute `runtime`
path; default `/run/photo-wall-display`. Base unit names are
`photo-wall-display.service` and `photo-wall-node-player.service`.

The root broker ingress is `/run/photo-wall-display/ingress.sock`. Each connection
carries one bounded JSON packet; replies carry `accepted`. Current operations are:

- `outputs`: observed domain state and current OutputKeys.
- `candidate`: exact `surface` identity, Player `uid`, and `starting_new: true`;
  returns a fresh `grant_id`. The caller must already own stop/start/configuration
  authority. This transport does not grant it.
- `handoff`: exact `surface`; returns a new `handoff_id` after local authorization.
  The response is not a diagnostic-removal acknowledgment.
- `withdraw`: one explicit `output_id`; no other Output receives a command.
- `events`: bounded observation records after a local sequence, including separate
  app presentation, diagnostic presentation, and handoff records. Overflow marks
  `stream_gap`; receipt does not settle a Run or create Central command authority.

The shell delivers candidate/admitted/revoked grant events over the custom
Wayland protocol only to clients matching the exact kernel process identity.
An admitted event follows the matching post-removal presentation, not the handoff
request. It permits surface use, not authored execution without Runtime authority. Central upload wraps local records with the admitted DisplayHost
producer/session and validates identity at ingress; the local sequence alone does
not authenticate a Central evidence stream.

## Remaining integration and verification

The app-owned native frame client and `player/wayland_frames.py` tag the actual
GDK/Wayland surface from the successful `Gtk.GLArea::render` callback. GTK3's
[Wayland paint path](https://gitlab.gnome.org/GNOME/gtk/-/blob/3.24.49/gdk/wayland/gdkwindow-wayland.c)
commits that paint after rendering; no later idle/configure callback supplies the
tag. Initialization and a private Wayland event queue stay on the GTK owning
thread. A one-second queue-render timer renews static frames through actual
commits. Before admission only a fixed synthetic colour is rendered. After
admission the existing Executor still supplies independently authorized content.
The adapter is opt-in through `PHOTO_WALL_DISPLAY_HOST=1` and refuses missing
Wayland protocol or missing app-private native library; it does not load a base
fallback. The current transform correlation hash is derived from the actual
composition's binding revision and calibration values. Extending that tag with
Central's canonical latest trial/sequence/hash remains required for live trials;
no second homography is introduced. Calibration Save remains unavailable until
Central has the exact latest matching real backend acknowledgment and all existing
trial authority checks. No simulator callback may open that gate.

Native compilation, compositor launch, typed grant/feedback/handoff recovery tests,
private-client failure, two-Output clipping, static renewal and GPIO/HDMI timing are
separate evidence tiers. Build and controlled virtual-compositor checks can expose
API/lifetime errors but cannot qualify physical scanout. The native client limits
buffer sizes and total allocation; those software bounds do not establish Pi GPU
capacity under media load.

Primary implementation references: [Weston 14 shell APIs](https://gitlab.freedesktop.org/wayland/weston/-/blob/14.0/include/libweston/desktop.h),
[Weston 14 compositor and clipping](https://gitlab.freedesktop.org/wayland/weston/-/blob/14.0/libweston/compositor.c),
[Wayland server protocol logger](https://gitlab.freedesktop.org/wayland/wayland/-/blob/1.23.1/src/wayland-server-core.h),
and [presentation feedback protocol](https://gitlab.freedesktop.org/wayland/wayland-protocols/-/blob/main/stable/presentation-time/presentation-time.xml).


## Frame-scoped replacement and explicit withdrawal

Frame-v3 grants include `frame_id` alongside process identity, local app epoch,
OutputKey, binding generation, and configuration revision. Frame counters alone
are not globally unique. The canonical Surface codec is shared by Central and
DisplayHost; the native grant event carries the same Frame identity to Player.
The app-private bridge requires protocol version 3. Historical facts without
Frame identity are not current authority.

When a live Output moves to another Frame or process/binding identity, Central's
Runtime owner explicitly authorizes withdrawal of the exact previously observed
role. DisplayHost submits its immutable decision identity and old Surface. The
shell checks the whole current tuple, removes that role, and separately emits
`role_removed`. The transport response is not the removal acknowledgment. Events
may arrive before the response and the controller queues them for later dispatch.
Central records the exact completion without recovering Runtime or granting the
replacement; replacement follows ordinary synthetic candidate and handoff flow.
A stale withdrawal cannot remove a new role. The same producer may carry a
historical completion through credential renewal without retargeting the effect.

Same-Frame committed revision adoption retains its separate atomic revision
path: the old grant remains active until genuine pending-revision presentation.
Pending expiry cannot withdraw the active role. Trial previews retain their
baseline and separate sequence/hash; terminal Trial identities cannot revive.
