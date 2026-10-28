# Base DisplayHost native backend

Status: implementation in progress, 2026-09-30. This document owns the native
adapter interface. The [node domain model](player-node-domain-model.md#display-and-calibration)
owns product policy and the [fleet implementation map](player-fleet-implementation-map.md)
owns remaining integration. No physical Pi, HDMI, continuity, timing or calibration
Save qualification is established by this source.

## Selected backend and responsibilities

The node display service uses a base-owned Weston **14** shell module,
`photo-wall-shell.so`, replacing kiosk-shell for this path. libweston has a
versioned native ABI; Meson requires `libweston-14`; Weston 14 includes its desktop API in that library.
The native Debian build locks the actual package versions from the repository's
pinned authenticated Debian snapshot and records its package inventory. A major
ABI check alone is not the release package lock.

The shell owns per-Output constrained app views, a protected diagnostic layer,
connection/mode generations, process checks, the five-second renewable surface
lease (renewed by a presented tagged commit or by the app's answer to Weston's ping; see
[the lease](#the-lease-presentation-or-a-ping-answer)), the hold (whether a handed-off Output
stays the app's, judged on the compositor's own scene; see [the hold](#the-hold-what-the-compositor-shows)),
the standard window alpha the Player's
pictures fade by, and each Output's turn ([the Player's screens](#the-players-screens)). The controller owns portable DisplayHost admission and receives protected
root-only lifecycle/configuration requests. The app never receives that socket.
The shell and its Cairo diagnostic client run from the base, independently of app
files or imports. The sealed Player receives only the public Wayland socket.

The diagnostic client is launched by the shell at the fixed base path
`/usr/lib/photo-wall/node-display/diagnostic-client`. A private socketpair is passed as
`WAYLAND_SOCKET`; the compositor retains that exact `wl_client` as the capability
to bind the diagnostic protocol. A public client with the same app ID, UID, or
claimed PID cannot obtain that role. Client failure restores a compositor-owned
solid curtain, raises the amber over the whole Output ([the health layer](#the-health-layer-and-the-amber-tint))
and triggers bounded restart attempts. The curtain is cover, not
proof of a readable error page. Actual diagnostic presentation is reported only
from presentation feedback for the private client's committed page buffer.

The page (with the health card, the debug overlay; [Player architecture](player-architecture.md#the-debug-overlay))
shows bounded predefined status text, the physical Output ID and actual
pixel dimensions, `Frame binding unconfirmed`, and, since 1c, brightness with where it
acts (the controller's line, sent with the `picture` op below; nothing when it has none).
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
application geometry. A still photo commits nothing and needs no renewal: its lease
holds on the app's answers to Weston's ping ([the lease](#the-lease-presentation-or-a-ping-answer)).
Missing tag/feedback support leaves the handoff without evidence and restores the base
diagnostic.

## The lease: presentation or a ping answer

Owner, 2026-10-17: "The render loop should not fire if it there is nothing to render." The
shell renews an Output's lease (`LEASE_MS`, 5 s) on a presented tagged commit of the app's
surface, or on the app's answer to Weston's standard desktop ping (`xdg_wm_base.ping`/`pong`,
libweston's `weston_desktop_client_ping` and the desktop API's `pong`), sent once every
`PING_MS` (1 s) while the Output holds an identity and an app surface (Weston keeps at most one
outstanding). The Player's frame client answers the ping when its frame turn reads the screens'
connection, on the Player's main loop, so a hung or stopped loop loses the grant within the lease, while a still photo, an Output forced off (`output_power`) or under
the black curtain (`black`) keeps it with no commit. A pong renews the lease of every Output
whose app process matches, whatever the app last committed: the lease answers only whether the
app is alive. Whether it shows anything is the hold's question ([below](#the-hold-what-the-compositor-shows)),
so an app that answers pings and shows nothing keeps its grant and loses the screen. A pong is evidence only that the app is
alive: handoff, `presented`, `handoff_presented` and revision adoption still need a real
presented commit, and the handoff still names a buffer presented within `LEASE_MS` (the
Player commits its screen's ground under the candidate tag on every present its pacer allows,
until it is admitted). The Player's
own copy of the presentation feedback paces its commits ([plane renderer](module-plane-renderer.md#the-frame-loop-and-pacing));
it is never evidence. `tests/native_display_smoke.py` proves both sides: a probe that stops
committing keeps its grant past `LEASE_MS`, and a stopped (SIGSTOP) one loses it. Central's display exchange keeps a receipt however old (a still
picture is presented once); only a handoff, its completion, a removal recognized from an issued
role and a calibration trial's presentation need a receipt under 5 s old
(`central/fleet/node_display.py` `NodeDisplay.recent`).

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

## The hold: what the compositor shows

Issue #123 (2026-10-20: a Player out of open files answered every ping and committed empty
frames for 10 hours, so the lease kept a dark screen its own). Liveness and hold are two
questions, each answered once: liveness is the lease (above); the hold is asked every
`lease_tick` (200 ms) on the compositor's current scene, never on anything the app says
(`shell.c` `hold_evidence`, `hold_sync`). The rule's product meaning lives in the
[Player architecture](player-architecture.md#the-debug-overlay).

- **Evidence.** `picture_plane`: a sub-surface anywhere in the app surface's tree (never the
  surface itself) that is mapped, has a buffer and a `wp_alpha_modifier_v1` multiplier above 0.
  `display_drawing`: the controller's latest `picture` op carried `drawing: true`, or a trial is
  live (its primitives stored and its time not run out). The app's main surface (the ground) and
  its frame tag are never evidence.
- **Judged only once handed off.** A candidate, or an Output whose handoff is still to be
  presented, is not judged: the Player's empty candidate commits still bring the handoff. The
  handoff sets `holding` true, and the first judgement follows its presentation.
- **Hold loss** on a released Output: `holding` false, the curtain at 1.0 and the private client's
  page configured with reason `hold_lost` ("Waiting for a picture"), identity and grant kept, so
  the lease and the handoff are untouched. **Hold return** lowers the slate.
- **Event `hold`** on every change and when the handoff completes:
  `{"event":"hold","output":<OutputKey>,"holding":bool,"evidence":"picture_plane"|"display_drawing"|null}`.
  The controller keeps it (`weston.py` `HoldFact`, `domain.py` `DisplayHost.hold`) and puts
  `holding` and the in-force `power` in each display feed snapshot, from which the health judge
  raises `nothing_to_show`.

CI: `tests/native_display_smoke.py` (a picture plane holds; a ground-only app loses the screen to
the slate and keeps its grant on ping answers; Display's drawing takes the slate off),
`tests/native_screen_harness.py` (the Player's real screens: handed off, then the slate until a
plane), `tests/node/display/test_display_hold.py`.

## Units and Weston incarnations

The display stack is two base units in `photowallbase.slice`. Their ordering is
systemd's, not a poll or a retry in either process:

- `photo-wall-display.service` runs Weston with `Type=notify`, `NotifyAccess=main`
  and Weston's own `systemd-notify.so` module (`--modules=`). Weston loads it after
  it has created its Wayland socket and the shell has bound `control.sock`, so
  READY=1 means both sockets accept connections. The `/usr/bin/env` wrapper restates
  `XDG_RUNTIME_DIR`, which `PAMName=login`'s pam_systemd would otherwise replace,
  and execs Weston, so the main PID is Weston's. `WatchdogSec=10s`: the module pings
  from Weston's event loop at half that interval, so a hung compositor is aborted
  (SIGABRT) and restarted like a crashed one.
- `photo-wall-display-controller.service` has `BindsTo=` and `After=` the display
  unit, and the display unit `Wants=` it. Each Weston incarnation therefore gets
  one controller, started after its READY=1 and stopped when it ends; the next
  incarnation brings a new one. The controller never starts, or builds its sandbox,
  while Weston's `RuntimeDirectory` (`/run/photo-wall-display`) is absent, so no
  `RuntimeDirectoryPreserve=` is needed. The directory leaves with the incarnation
  that made it, and no reader can open a dead incarnation's sockets.
- The public Wayland socket is not in that directory. Weston binds it at
  `/run/photo-wall-display-wayland/wayland-0` (`--socket=` takes the absolute path),
  in a directory the base's tmpfiles.d creates as `pw-display:pw-display 0750`. It
  outlives every incarnation, so an app in group `pw-display` binds the directory,
  never a socket file a restart replaces, and never sees `control.sock` or
  `ingress.sock`. Weston's umask under `PAMName=login` is 0002, so the socket is
  group-writable. [`appliance/kernel/display_paths.py`](../appliance/kernel/display_paths.py)
  is the one home of these paths; a unit test pins them to the packaged units and
  tmpfiles.d.

Any other consumer of `/run/photo-wall-display` must follow the same rule: start
after the display unit is ready, and end with the Weston incarnation it connected
to. The app broker's `After=` orders it on READY=1. The Player's transient unit
binds `WAYLAND_DIRECTORY` read-only at the same path, sets `WAYLAND_DISPLAY` to
`WAYLAND_SOCKET`, and carries `StopPropagatedFrom=` and `After=` the display unit
([`app_unit_properties`](../appliance/apps/process_linux.py)): its start waits for a
pending Weston start's READY=1 and it stops with that Weston, but no job on the display
unit ever starts it (`BindsTo=`, `Requires=`, `Requisite=` and `PartOf=` would each turn
a restart of Weston into a restart of the Player; the broker is its one owner). The broker
judges Weston up only while the display unit is active with no job pending, so a restart
in progress never counts as the Player's own exit. Each launch records the display's
`InvocationID`. No automatic restarts ([Player architecture](player-architecture.md#processes-and-supervision),
issue #125): one rule, `start_for_display` ([`broker.py`](../appliance/apps/broker.py)),
serves whichever launch is current, and it starts the same environment again under the same
operation only when Display restarted Weston on purpose under that launch. Before its restart,
`photo-wall-display-signal.service` writes the stopped incarnation's `InvocationID` to
`/run/photo-wall-display-restart/restarted` (`display_paths.py` `PURPOSEFUL_RESTART`, written to
`restarted.partial`, then renamed); the run starts again only when that mark names the launch's
Weston, a newer incarnation is up, and the run was not first seen absent under its own Weston.
Until PID1 has unloaded the old unit nothing starts, and the next turn asks again. A Weston
crash, or a Player that died while its Weston lived, starts nothing: the Player stays down under
the debug overlay's card until a person reboots. The node-pid1 success scenario runs three signal
range restarts under a linked Player (each starts it again), then crashes the Player and then
Weston and requires that nothing starts it.

**The signal range is set before Weston starts** ([0024](decisions/0024-display-tuning.md), Q2 option C).
Weston never sends the HDMI connector's "Broadcast RGB" property and holds the DRM master while it
runs, so the range is set between incarnations, by one privileged step, and nothing else on the
Pi gains a privilege:

- `photo-wall-display.service` gains an `ExecStartPre=` step run as root (`+`), before Weston: it
  reads the wanted range per connector from the **request file** the display controller writes,
  sets each connector whose value differs, and exits 0 whatever happens, so Weston always starts;
  a range it could not set is reported by the controller, which reads the connector back. With no
  request file every connector stays as the kernel has it (Auto after a Pi start).
- A **path unit** watches the request file; a change starts its **oneshot**, which resets the
  start count (`systemctl reset-failed`) of the display unit and of every unit bound to it (the
  controller, which the restart stops and starts again) and then restarts the display unit, so a
  run of range changes never spends a crash start limit: only crashes count against it. The restart ends the incarnation, and with it the controller
  and the Player, under the rules above: the next incarnation sets the range first, then Weston
  takes the screen, a new controller reads the connector and reports, and the broker starts the
  Player again (`display_restarted_on_purpose`, the one restart Display's mark allows).
- The controller (`appliance/display_host/signal_range.py`, unprivileged) only reads the connector
  and writes the request file, and only when the document's range differs from the connector's,
  so applying is idempotent and a Weston, Player or controller restart changes nothing (the
  connector keeps its range). While the range changes the wall shows black: the console stays
  blank, never a login prompt or console text.

The node-pid1 scenarios start the controller alone, so Weston comes up only
through the units' own dependencies. Every scenario then checks that `control.sock`
existed before the controller's main process started, that the controller never
restarted, and that its journal holds no `FileNotFoundError` or `226/NAMESPACE`.
The `refused` scenario also sends Weston SIGSEGV, then SIGSTOP (its watchdog aborts
it), and requires a new Weston and a new clean controller each time. Weston and the controller
are base units and keep their systemd restarts; only the Player is never restarted automatically.

## Seeing what the compositor shows

Weston runs with `--debug` (`photo-wall-display.service`), its frontend's own introspection
switch (Weston 14 `frontend/main.c`): it turns on the `weston_debug_v1` protocol and adds a
screenshot authority that allows every capture (`screenshot_allow_all`). With it, root on the Pi
uses the two stock clients from the `weston` package against the public Wayland socket:

- `weston-screenshooter` captures every Output through `weston_capture_v1`'s framebuffer source
  into one PNG. For that one frame Weston turns the display planes off and its GL renderer
  composes the whole Output, the Player's planes and the debug overlay included, so the PNG is the
  picture as the GPU composes it, not a read of the HDMI signal: the display controller's own
  blending and scaling of the planes are not in it, and that one frame on the wall is the GPU's.
  The Pi 5's writeback connectors sit on their own CRTCs, so Weston's writeback source, which
  needs one on the Output's CRTC, is not offered.
- `weston-debug` lists and streams the debug scopes: `scene-graph` (a one-shot dump: each Output's
  mode and position, each layer's views with their surface, client PID, buffer and plane),
  `log`, `drm-backend` (the plane assignment, every repaint), `timeline`, `proto` (every client's
  Wayland messages), `gl-renderer`.

Before `--debug` (through v0.46) neither worked: the only authority was the frontend's
screenshooter's, which allows only the client the frontend itself spawns for Super+S, so every
capture failed "unauthorized", and `weston-debug` found no `weston_debug_v1`.

**What it opens.** Any client on the Wayland socket (root, and the apps in group `pw-display`:
the Player) may capture any Output and read every scope, `proto` included; Weston logs a
"debug protocol has been enabled" warning at each start. A subscribed scope costs Weston CPU for
as long as it streams; an unsubscribed one costs nothing.

**What a capture costs.** All of a capture's memory is charged to Weston's cgroup and Mesa keeps
it after: three times the Output's 4-byte pixels (measured on the fireplace Pi, 2026-10-28, with
a headless GL Weston in its own transient unit: 1920 x 1080 +24 MB, 3840 x 2160 +98 MB at the
peak), held after the first capture and reused by the next. The display line
(`appliance/kernel/capacity.py` `CAPTURE_BYTES`, `MemoryMax=160M`) holds one Output's capture at
the largest mode the Pi 5's HDMI drives, 4096 x 2160. It does not hold two 4K Outputs captured
at once, which `weston-screenshooter` does when both are lit: on such a Pi, an out-of-memory kill
of Weston leaves the Player down until a reboot.

CI: the node-pid1 `dark` scenario runs the packaged unit's command line and, with the card up,
requires `weston-debug` to list the scopes and dump the scene graph naming `Virtual-1`, and
`weston-screenshooter` to write a PNG of the Output's size (`tests/test_node_pid1.py`
`introspect`). A capture of a real HDMI Output is a manual bench step
([display-control skill](../.claude/skills/display-control/SKILL.md#seeing-what-the-compositor-shows)).

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

## The Player's screens

Render redesign ([0029](decisions/0029-render-planes.md)): the Player shows each Frame's picture
as its own display plane, so the display controller places and fades it with no GPU. The shell
applies; it computes nothing about a Frame. Everything here is standard Wayland: frame protocol
v4's private XML is unchanged.

- **One screen per Output.** The Player's own connection (`player/plane_screen.py` over the frame
  client's `pw_screen_*` functions; no GTK or GDK) makes an xdg toplevel per Output with app id
  `photo-wall-<output>`, which the shell maps full screen as the Output's app surface. Grants,
  tags, `tag_picture`, presentation feedback and the trial overlay ride that surface, as before.
- **The ground.** The screen's own buffer is an opaque black picture of the whole screen
  (XR24, the mode's size, turned with the screen's buffer transform), never a single-pixel buffer:
  Weston 14 never puts a single-pixel buffer on a plane, and once translucent pictures uncover it
  Weston has no renderer framebuffer to mix with and composites everything until the next
  repaint ("cannot propose mixed mode: no previous renderer fb", tracer 2). With a real ground
  Weston puts it on the primary plane and the pictures on overlays. Test patterns and calibration
  targets are drawn into the ground.
- **Sub-windows.** Each picture is a synchronized `wl_subsurface` of the screen, up to 15, stacked
  once when it is made (planes are only added or removed at the top) and committed with the
  screen's commit: one atomic commit per screen. Only what changed is sent: a restack
  (`place_above`) or a sub-window position marks Weston's view list dirty and damages beneath it,
  which forces a GPU repaint whenever the renderer is in play (tracer 2).
- **Window alpha.** The shell serves `wp_alpha_modifier_v1` version 1 as wayland-protocols' staging
  XML states it: `set_multiplier` is double-buffered and applied when the surface's state is (for
  a synchronized sub-window, its parent's commit) as `weston_view_set_alpha` on every view of the
  surface; destroying the object resets it to 1. Weston maps a view's alpha onto the plane's alpha
  property. The implementation is the shell's until Weston 16 serves the protocol upstream; on that
  upgrade it is deleted, never kept beside Weston's.
- **Feedback rides on the topmost plane.** Weston answers a surface's presentation feedback only
  from a paint node with a visible region, and the ground under an opaque full-screen picture has
  none: feedback asked there is never answered (tracer 1 lost the screen in about 32 s). The frame
  client asks it on the topmost sub-window (the screen itself for a commit with no plane), and the
  shell maps it to the app surface its tree hangs from, so the answer is still the screen's word.
  No slot can leak: the frame client keeps at most `FEEDBACK_PER_SURFACE` (4) answers per surface,
  dropping the oldest when the table is full and younger than `FEEDBACK_STALE_NS` (250 ms; Weston
  answers a seen surface at its next repaint) and leaving older commits untracked (a covered
  surface); the shell keeps at most 4 per app surface.
- **Buffers.** Pictures arrive through `zwp_linux_dmabuf_v1` (LINEAR XR24 or pre-multiplied AR24
  GBM buffers, one roundtrip per import) and are reused only after `wl_buffer.release`. CI's
  headless Weston imports memfd-backed buffers through the same protocol with its GL renderer on
  llvmpipe (Weston 14's pixman renderer offers no linux-dmabuf).

CI proves the shell's side in `tests/native_display_smoke.py` (a sub-window at multiplier
0x7fffffff over black captures half white; destroying the modifier brings full white) and the
screens in `tests/native_screen_harness.py` (planes, the ground, one-plane presents leaving no stale
sub-window, a fade burst's feedback, the trial overlay naming its tag). Planes themselves exist
only on the Pi: the render bench is their proof ([runbook](runbook.md#the-render-bench-and-pi-testing-before-merge)).

## The turn

How a Frame's screen hangs is the Frame's ([0029](decisions/0029-render-planes.md), owner Q3: the
turn belongs to the spot on the wall), sent in the Output document (`turn`, display line schema
9). Display Host carries it out; the Player reads it back from Weston.

- **Control op `turn`** (base-only control socket): `output_id`, `output` and `transform` 0 to 3
  (`wl_output.transform` normal, 90, 180, 270; `contracts/display_turn.py` `wayland_transform` is
  the one mapping: a screen hung turned 90 degrees clockwise wants Weston to turn its content 90
  counter-clockwise). The shell sets `weston_output_set_transform`, answers a `turn` event ahead of
  the response, and refuses any other transform. The wanted transform is kept across a hot-plug
  and applied to the Output's new `weston_output`, as `black` is.
- **What a turn does to the Output:** a mode change. The app's grant is invalidated and the Output
  comes back under a new key, which Central's surface decisions grant again; the Frame's Position
  becomes one to check (a rectangle is fractions of the screen as it hangs).
- **The controller** ([`screen_turn.py`](../appliance/display_host/screen_turn.py): `ScreenTurns`
  on its own worker thread, `WestonBackend.turn` its one port) asks once per trigger: a document whose turn differs
  from the last one the shell confirmed, or the Output appearing connected (documents arrive
  before the shell announces any Output). A refused ask waits for the next trigger; the newest
  document wins. It hears of Outputs from the Output reports, as the power and picture controllers
  do.
- **The Player draws turned.** `PlaneScreens.screen` reads the mode and transform Weston reports;
  each picture is laid out turned and committed with the matching buffer transform, so Weston
  shows it on a plane with no copy (bench T5). A picture with the wrong transform is composited by
  Weston's GPU (the bench's mutation probe sees it).

CI: `tests/node/display/test_screen_turn.py` (when to ask), `tests/node/display/test_display_turn.py`
(`WestonBackend.turn` over a socket pair), the display smoke (the event, the turned mode, a
refused transform 4) and the node-bus integration test `tests/integration/test_central_display_documents.py`
(Central's turn reaching the Node's document).

Primary implementation references: [Weston 14 shell APIs](https://gitlab.freedesktop.org/wayland/weston/-/blob/14.0/include/libweston/desktop.h),
[Weston 14 compositor and clipping](https://gitlab.freedesktop.org/wayland/weston/-/blob/14.0/libweston/compositor.c),
[Wayland server protocol logger](https://gitlab.freedesktop.org/wayland/wayland/-/blob/1.23.1/src/wayland-server-core.h),
[presentation feedback protocol](https://gitlab.freedesktop.org/wayland/wayland-protocols/-/blob/main/stable/presentation-time/presentation-time.xml)
and [alpha modifier protocol](https://gitlab.freedesktop.org/wayland/wayland-protocols/-/blob/main/staging/alpha-modifier/alpha-modifier-v1.xml).

## Picture operations (frame protocol v4)

Since 1c the shell offers `pw_frame_manager_v1` at version 4 (`FRAME_VERSION`, the Player's,
unchanged since) and `pw_diagnostic_manager_v1` at version 5 since issue #124
(`DIAGNOSTIC_VERSION`: v4 the brightness line, v5 [the Frame region](#the-frame-region-diagnostic-manager-v5))
([`photo-wall-frame-v1.xml`](../appliance/display_host/native/photo-wall-frame-v1.xml)).
Display Host carries a Display's picture settings and talks DDC/CI; the Player renderer
is the only picture pipeline, so the shell forwards picture operations and never
interprets them. The rules for what the operations are and when they are sent belong
to the [Player architecture](player-architecture.md#picture-settings-1c).

- **Control op `picture`** (base-only control socket): `output_id`, `output`, `operations`
  (a string of at most 1024 bytes: picture operations v8, decision 0028, carry the Display's
  white balance, contrast, gamma and light), `brightness` (the base page's line naming the
  display's own brightness, at most 128) and `drawing` (a required boolean, issue #123; missing
  or not a boolean is refused): true exactly while the Output's picture document carries a test
  pattern or calibration target (`picture.py`). A pattern is drawn on the screen's ground, never
  a picture plane, so this flag, Display's own word, is what
  [holds](#the-hold-what-the-compositor-shows) the Output while one shows.
  The shell stores both per Output, sends `picture(output_id, operations)` to every
  version-4 frame manager of that Output's app client and `brightness(output_id, text)`
  to the diagnostic client. It sends the stored values again on a version-4 bind and
  when it grants the Output, since a client binds before it is granted.
- **Request `tag_picture`**: as `tag`, plus the picture change the buffer was drawn
  with (digits only, at most 20 characters). The `presented` event carries it as
  `"picture_change"`, or `null` for a buffer tagged with version-3 `tag`.
- **The frame client** ([`frame-client.c`](../appliance/display_host/native/frame-client.c))
  binds version 4 when offered and still refuses below 3; `pw_frame_client_picture`
  returns the newest operations per Output and `pw_frame_client_tag_picture_next_commit`
  tags the next commit; `pw_screen_present` tags the screen's commit with the picture change.
  The Player reads them through `player/plane_screen.py`.

CI proves this in `tests/native_display_smoke.py` (node-pid1 `display-harness`):
operations reach a late app verbatim, a 10-byte non-JSON operations text is not parsed,
oversize operations and lines are refused, and `tag_picture` presents its change.

## Black picture (2a)

Night off's "Show black" (#83) and an off no power method can carry out need an opaque black
from Display Host, never from the Player and never a layer of the plan. The rules for when it is
up belong to the [Player architecture](player-architecture.md#display-identity-and-power-1b).

- **Control op `black`** (base-only control socket): `output_id`, `output` and `on` (a
  boolean). The shell maps a solid-colour black view (a Weston curtain, never a shm buffer)
  at the Output's size in its own layer above every other, the health layer included
  (`WESTON_LAYER_POSITION_LOCK + 2`), and answers a `black` event (`output_id`, `on`) ahead of
  the response. It does not capture input. The app's grant holds its lease under it on the
  app's ping answers, as under signal off, so taking it down shows the app at once.
- **Kept across a hot-plug.** The wanted state stays on the Output's slot: the curtain is
  rebuilt when the Output comes back or changes mode, and the `output` event carries `black`,
  so the controller (`weston.py` `black_shown`) knows whether it is up.
- Under the curtain a fault's tint is not seen: a failure during intended darkness shows in
  the console at once and on the display when it is next due on
  ([U1](requirements.md#failure-visibility-and-recovery)).

CI proves the op in `tests/native_display_smoke.py` and the controller's use of it in
`tests/node/display/test_display_black_picture.py`.

## The health layer and the amber tint

The overlay client (`overlay/client.py`) is the private client's per-Output health layer, above the
app and below the black picture. **With the tint off it is unmapped**: a tint-off page commits a
NULL buffer, and the judge hears it presented once the compositor has processed that commit (a
`wl_display.sync` after it). Nothing of the health layer covers the Player's pictures, so they stay
on display planes ([0029](decisions/0029-render-planes.md)); a mapped transparent layer over them,
as before, made Weston composite the whole screen. **With the tint on** it shows one whole-Output
ARGB8888 `wl_shm` buffer scaled by `wp_viewporter`, and Weston composites while it shows. A tint-on
page that cannot be drawn shows the shell's amber as a pre-multiplied `wp_single_pixel_buffer_v1`
over the whole Output, never nothing.

**The shell's amber keys on the private client being alive** (`fallback_sync` in `shell.c`): it is
up exactly while the private client is not connected, or connected without having bound
`pw_diagnostic_manager_v1` at version 3 or later, on every Output Weston drives, handed off or not
(issue #124: with the client dead no debug overlay can show, neither the slate's page nor the card,
so the amber covers the whole Output whatever the Frame region). It no longer keys on a mapped
health surface, which the clear layer no longer has. Cost: a hung but connected health client shows
no amber. `tests/native_display_smoke.py` proves both: with the client alive and nothing to show,
no health view is mapped and the capture equals the app's pixels exactly; with it killed, the amber
covers the whole Output and goes when the restarted client binds.

Why a solid buffer and not a small `wl_shm` one for the fallback: the solid buffer holds no pixels,
and Weston returns before its plane search for it. Weston's DRM backend treats a small ARGB8888 shm
buffer as a cursor-plane candidate. The Node runs Photo Wall's Weston
([packaging module](module-debian-packaging.md#weston)), whose plane masks are 64 bits wide: the
HDMI-A-1 cursor plane (index 50) gets its own bit, `1 << 50`, and HDMI-A-1's primary (index 54)
its own, so the Player's ground can be scanned out from the primary. Background: Debian's and
upstream's Weston 14 build that mask as `1 << plane_idx` in 32 bits, so on the Pi 5's 56 vc4 planes
the cursor's bit wraps to an overlay plane's and `drm_output_find_plane_for_view` aborts Weston on
`assert(fb)`; Weston 15 removes that assertion (upstream commit d3ead778) but keeps 32-bit masks.
CI cannot exercise this path: its headless Weston has no DRM planes, so the Pi bench is the proof.

## The Frame region (diagnostic manager v5)

Issue #124: the debug overlay draws inside the Frame region when it can be met, else on the whole
Output ([Player architecture](player-architecture.md#the-debug-overlay) owns the rule's meaning).
The region is Central's: Display Host computes no Frame geometry ([0030](decisions/0030-frame-box.md)),
and the shell never interprets it.

- **Document.** Central writes `region-<port>` in the display line's desired bucket
  ([`contracts/node_output.py`](../contracts/node_output.py) `FrameRegionDocument`: the Output, its
  own change number and a `FrameRegion` of the Frame id, the screen it was placed on and an
  `OutputRect` in whole pixels, or none). The controller decodes it (`runner.py` `document_feed`);
  a refused document gives the Output no region, logged once per change, and touches neither power
  nor the turn. `ShellRegions` sends each Output's newest region on its own worker thread and again
  when the shell announces the Output (after a Weston restart, as the picture is).
- **Control op `region`** (base-only control socket): `output_id`, `output` and `region`, the
  canonical JSON of the `FrameRegion` (at most 1024 bytes) or `""` for none; a missing or longer
  `region` is refused. The shell stores it on the Output.
- **Event `region(output_id, region)`** (`pw_diagnostic_manager_v1`, since version 5): the stored
  text, forwarded verbatim to the private client on the op, on the client's bind, and right after
  every `configure`, so a configure is always followed by its region.
- **The overlay client** (`overlay/client.py`, manager version 5) keeps it (malformed or empty is
  none) and redraws. [`overlay/area.py`](../appliance/display_host/overlay/area.py) `explain_area`
  is the one layout rule for the slate and the card: the region's rectangle, or the whole Output
  when there is no region, the screen differs from the Output's size now, or the rectangle is
  under 448 x 252. `overlay/render.py` draws opaque black outside the area, the slate's page or
  the tint inside it, and the card in `card_rect` of the area.
- **The `output` event carries `mode`**, Weston's current mode as built (`[w, h]`), which the
  controller reports as the Output report's `current_mode`; Central places the region on it.

CI: `tests/native_display_smoke.py` (the slate and the card inside the region and black outside,
a region placed on another screen on the whole Output, oversize and missing `region` refused),
`tests/node/display/test_display_explain_area.py`, `tests/node/display/test_display_overlay_render.py`,
`tests/test_frame_region.py` (Central's side).

## Frame-scoped replacement and explicit withdrawal

Frame-v3 grants include `frame_id` alongside process identity, local app epoch,
OutputKey, binding generation, and configuration revision. Frame counters alone
are not globally unique. The canonical Surface codec is shared by Central and
DisplayHost; the native grant event carries the same Frame identity to Player.
The app-private bridge requires protocol version 3 and binds 4 when offered
(picture operations, above). Historical facts without
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
