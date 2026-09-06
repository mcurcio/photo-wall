# Native Renderer

Status: implemented and tested with native software rendering on 2026-09-05;
Pi, DRM/HDMI, physical continuity and capacity qualification remain pending. Policy belongs to [decision 0005](decisions/0005-native-platform-and-registration-fallback.md).
This module implements the [Renderer boundary](module-player-execution.md) inside
the single Player process. It does not select media, retrieve files, authenticate
control messages or operate the compositor service.

## API and ownership

`NativeRenderer(outputs, *, decoder_limit=4, texture_budget=512*1024**2, prepare_timeout=5)` accepts
fixed `NativeOutput(output_id, app_id, width, height)` surfaces. Construct it and
call `prepare`, `capacity`, `present`, `release`, `close` and `diagnostics` on the
GTK/GLib owning thread. Native imports are lazy so the neutral package and pure
reference tests remain usable without GI or a display. Each persistent GTK3
window contains a GLArea; production Wayland windows receive their distinct app
IDs before their first content buffer. Application code applies a complete control snapshot and the
immediate Executor tick in one GLib callback.

`prepare(LocalLayer)` creates an explicit local-file JPEG, PNG or silent H.264 MP4
pipeline, returns pending until preroll/seek and a matching RGBA sample complete,
and reports decode/capacity failure without touching another successful surface.
Malformed streams without a preroll frame fail after the bounded preparation
timeout. Negotiated buffer size/stride and initialized GL surfaces gate readiness.
A failed decoder can retry after one second; every incarnation has a distinct
texture/draw identity, so a reset sample counter cannot reuse an older picture.
GStreamer streaming threads publish owned samples into a two-entry mailbox; they
never call GTK or GL. Native sample mapping/upload happens on the main thread,
checks negotiated dimensions/offset/stride and copies bounded rows with native
buffer operations. Decoder release disables callbacks and drops queued samples.

Video prepare seeks to the supplied logical position after a covered pause,
using a flush/accurate seek and accepting only samples from the resulting segment.
While playing, bounded drift correction avoids repeated flushing at every tick.
Hidden resources pause; they retain decoder/texture capacity until explicit
release. The central/executor time mapping, loop position and original fade alpha
are authoritative; the renderer does not invent elapsed Run time.

`capacity` includes all resident decoders, textures, per-Output persistent front
and candidate linear framebuffer storage and proposed exact variant dimensions.
Resize accounting includes the old and replacement framebuffer pairs until the
new allocation succeeds. Texture, shader, program and framebuffer allocation
failures clean up partially created objects. Limits are conservative software allocation gates and always report
`qualified=False`. Native decoder internals and driver memory require separate
measurement. No four-video Pi claim follows from a four-slot setting.

`present(OutputComposition)` prepares a complete candidate, queues a render and
returns pending until the GLArea render callback completed that token. Candidate
rendering uses a separate framebuffer; front/candidate swap happens only after
all layers succeed. Failures retain the previous successful front buffer.
Acknowledgment records the exact token snapshot/time and seek generations.
Because positions and fade alpha advance every tick, subsequent calls correlate
the completed token by immutable execution/binding/effective-calibration identity
and seek generation, with a 500ms freshness bound, while queuing current values.
The native PresentationResult carries the actual acknowledged composition and
`presented_at` as local monotonic draw-completion time; Executor maps that time
through its own clock mapping. Video positions come from the drawn sample PTS
converted through its GstSegment to stream time, rather than the requested seek.
Diagnostics retain the actual acknowledged composition rather than claiming the
newly queued position has already drawn. This is application draw evidence, not
scanout or photon timing. A new authority/composition replaces a queued token;
release removes stale queued work before another draw can execute.

## Geometry and colour

`player/geometry.py` is the authoritative pure reference for inversion of the
unit-aperture homography, inverse clockwise quarter-turn, centered cover within
the normalized crop using the logical Frame aspect, sRGB/BT.709 transfer,
premultiplied linear source-over composition, bounded linear gain and final sRGB
encoding. Its scalar reference is for tests, not production pixel loops. Shader
parameters and GLSL implement those equations. Output coordinates and source UV
coordinates use top-left origin; texture upload does not silently add a second
vertical flip. Pixels outside the convex aperture and empty output are black.

Stills use sRGB and video uses BT.709. Each sampled straight RGB value is decoded
to linear before multiplying by source alpha and effective Layer alpha. Ordered
layers compose in linear floating-point framebuffer storage. Gain applies once
after composition, then RGB is encoded to sRGB. Black is a real source-over
layer; alpha zero contributes nothing. HDR, ICC management and measured panel
colour accuracy are outside this accepted SDR convention.

## Verification and native smoke

Portable tests cover projective corner correspondence/inversion, clipped
apertures, crop applied once, Frame/source aspect mismatch, all quarter-turns,
transfer breakpoints, linear-light blending, transparent and opaque black,
sample bounds and bounded mailbox/release behavior.

The bounded `tests/native` recipe uses Noble system Python3.12 with its matching
GI/GTK/GStreamer, Mesa software GL and Xvfb for an application integration smoke.
It generates public synthetic JPEG/PNG/H.264 locally, exercises two persistent
GLAreas, asynchronous preparation/draw acknowledgment, invalid decode, covered
12-to-32-second video reveal and resource limits, and captures package/renderer
diagnostics. Xvfb cannot establish Wayland app-ID placement or physical HDMI
timing. A separate Weston headless smoke checks the production window path;
actual Pi dual-output/PXE/thermal qualification remains in the platform work.
Use Dockerfile COPY and container-local storage rather than host binds on the
currently affected Docker Desktop setup. Build/run results must be recorded
explicitly before changing this document's qualification status.


## Verified native environment and commands

Noble provides `gdk_wayland_window_set_application_id` in GTK's public C API but
its installed GI packages do not provide a GdkWayland typelib. The adapter calls
that pinned C API with the owning GObject's capsule pointer using ctypes. GTK's
default map handler overwrites the earlier backend app ID, so a handler after
`map` restores the distinct ID before GLib paints the first content buffer. The
native protocol test checks that ordering and verifies real kiosk routing to
separate Weston X11-backed Outputs. Weston13's kiosk implementation also defers
mapping a zero-width surface and resolves the app ID before mapping its content.
[GTK3.24 Wayland header](https://raw.githubusercontent.com/GNOME/gtk/3.24.41/gdk/wayland/gdkwaylandwindow.h),
[Weston13 kiosk implementation](https://gitlab.freedesktop.org/wayland/weston/-/raw/13.0.0/kiosk-shell/kiosk-shell.c).

Set `GDK_GL=gles` and `PYOPENGL_PLATFORM=egl` before native imports. In the tested
Noble environment GLArea's ES preference alone selected desktop compatibility GL;
the explicit environment selected OpenGL ES3.2. The smoke's rendered-pixel checks
compare calibrated shader output with the scalar reference, including source
orientation, projective aperture, crop/quarter-turn, source alpha, gain and linear
source-over blending. The native app uses native buffer transfers and GPU pixels;
Pillow and ffmpeg in the smoke only generate its public synthetic test inputs.
[GLArea API](https://docs.gtk.org/gtk3/class.GLArea.html),
[GStreamer stream-time conversion](https://gstreamer.freedesktop.org/documentation/gstreamer/gstsegment.html).

From the repository root:

```sh
.venv/bin/python -m pytest tests/test_native_math.py tests/test_executor.py tests/test_contracts.py -q
python3 tests/native/build_context.py > /tmp/photo-wall-native-context.tar
docker build -t photo-wall-native-smoke:20260905 - < /tmp/photo-wall-native-context.tar
docker run --init --rm --network none photo-wall-native-smoke:20260905
docker run --init --rm --network none -e PHOTO_WALL_NATIVE_MULTI=1 \
  --entrypoint xvfb-run photo-wall-native-smoke:20260905 \
  -a -s '-screen 0 1280x480x24' sh tests/native/wayland_smoke.sh
```

The explicit tar context bypasses the production `.dockerignore` test exclusion
without copying central code, credentials or the workspace into the container.
`--init` supplies the signal handling required by Xvfb's startup wrapper. The test
uses container-local storage and no network or host mounts. On this development
host the build used an isolated anonymous Docker CLI config against the existing
engine because the Desktop credential helper stalled public Ubuntu metadata;
the user's Docker configuration was preserved.

Executed on 2026-09-05: **72 scoped portable tests passed**, including 19 native
math/state tests; scoped Ruff and documentation-link checks passed. Native Xvfb,
headless Weston and final two-Output Weston kiosk runs passed. The final run
verified JPEG/PNG decode, H.264 seek to12 then covered reveal at32 seconds with
actual sample stream time, decoder-incarnation recovery, failed-stream timeout,
async exact metadata/calibration acknowledgments, release cleanup, rendered
geometry/colour pixels, app-ID ordering and distinct kiosk Outputs.

The final arm64 integration image ID was
`sha256:8bf573f5fd7e4d09e4f8c0e0ad51e70d2ebb6058d3f15d7f7651284d20249e6d`.
Its measured packages were Python3.12 `3.12.3-1ubuntu0.16`, python3-gi `3.48.2-1`,
python3-gst-1.0 `1.24.1-1`, GStreamer `1.24.2-1ubuntu0.1`, GTK
`3.24.41-4ubuntu1.3`, PyOpenGL `3.1.7+dfsg-1`, Mesa
`25.2.8-0ubuntu0.24.04.2` and Weston `13.0.0-4build3`. This fixture resolves Noble
packages at build time; it is not the pinned appliance release or its package
closure. PyOpenGL reported its optional NumPy handler unavailable; all tested
buffer/matrix/pixel operations succeeded through its available handlers.

No Pi image, physical boot, DRM connector mapping, HDMI/panel timing, desktop-flash
measurement, thermal performance, hardware decode or qualified four-decoder
capacity is established here. Native decode and upload are explicit initial CPU
and memory costs. The [appliance design](module-appliance-platform.md) owns those
remaining build and hardware gates.

## Native health acceptance adapter

The [native-health fixture](../tests/native/health_smoke.py) additionally
connects real initialized GTK/GStreamer capacity to the production Player
health writer and 30-second signed-trial acceptance gate. Its
[executed evidence](evidence/2026-09-06-native-health.md) passed after 30.437s.
Central authority samples and rootfs payloads are synthetic; it does not
exercise network enrollment, the complete Player process loop, systemd or
physical boot. This is distinct from the separately executed draw/decode tests.

Build the normal native fixture image using the recipe above, and prepare a
verified [Player wheelhouse](module-player-package.md) for the current revision.
From a checkout containing the committed health fixture, replace the absolute
wheelhouse path below. The container is disposable and receives only selected
source and the locked wheel closure; all health-test state is temporary.

```sh
set -eu
native_health_wheelhouse=/absolute/player-wheelhouse
native_health_tmp=$(mktemp -d)
git archive HEAD appliance contracts player tests/native/health_smoke.py > "$native_health_tmp/source.tar"
native_health_id=$(docker create --init --network none --memory 1g --cpus 2 \
  --pids-limit 256 photo-wall-native-smoke:20260905 sleep 600)
trap 'docker rm -f "$native_health_id" >/dev/null' EXIT
docker start "$native_health_id" >/dev/null
docker cp "$native_health_tmp/source.tar" "$native_health_id:/tmp/source.tar"
docker cp "$native_health_wheelhouse" "$native_health_id:/tmp/wheelhouse"
docker exec "$native_health_id" sh -c 'tar -xf /tmp/source.tar -C /app && /opt/native-venv/bin/pip install --no-index --find-links=/tmp/wheelhouse/wheels -r /tmp/wheelhouse/requirements.txt'
docker exec "$native_health_id" xvfb-run -a -s '-screen 0 640x480x24' \
  /opt/native-venv/bin/python tests/native/health_smoke.py --report /tmp/native-health-report.json
docker cp "$native_health_id:/tmp/native-health-report.json" "$native_health_tmp/report.json"
```

The public report remains in the generated temporary directory after the
container is removed. The test refuses non-Linux, non-root or missing-display
environments. It never executes reboot or changes host production paths.
