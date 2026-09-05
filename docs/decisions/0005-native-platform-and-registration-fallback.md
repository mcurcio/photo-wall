# Native platform and registration fallback

Status: accepted implementation choices, 2026-09-05; native/image qualification pending.
Owner: orchestrator. References and pinned upstream inputs are in
[the platform design](../module-appliance-platform.md).

Use the pinned Ubuntu Server 24.04.4 Raspberry Pi arm64 base with its own Python
3.12, GI, GTK3, GStreamer 1.24 and Weston13. This changes the proposed Raspberry Pi
OS base because the existing project's Python3.12 ABI matches Noble's native
bindings. Keep the code's Python constraint unchanged. Build a separate Player-only
distribution and dependency closure containing `player` and neutral `contracts`;
the central and Immich packages do not enter the appliance.

The initial renderer uses persistent GTK3 GLArea windows, bounded GStreamer
appsink RGBA buffers and GPU composition. It does not run Python pixel-processing
loops or spawn a media-player process. Native-buffer copies/uploads are an explicit
qualification cost relative to the architecture's preferred shared-GL-memory path.
Start with a configurable four-decoder/512MiB texture budget and two 1080-class
Outputs; this is a software limit, not qualified Pi capacity. Device qualification
must measure Pi5's software H.264 decoding, upload cost and thermal load.

For a given output pixel, invert the committed/preview four-corner homography to
the unit aperture, apply the inverse calibration quarter-turn to logical Frame
coordinates, and use the Frame profile aspect ratio for centered cover layout
inside the normalized source crop. Discard pixels outside the aperture. Crop is
applied exactly once; source orientation was already normalized by preparation.
Decode JPEG/PNG RGB using the sRGB transfer convention and MP4 RGB using BT.709,
compose premultiplied alpha in linear light, apply bounded linear SDR gain and
encode final output as sRGB. CPU reference tests define this convention before
native shaders. It does not claim measured panel colour accuracy or HDR support.

Preparation and draw acknowledgment remain asynchronous. A pending draw preserves
its grant and queued replacement for up to500ms; a missing acknowledgment after
that bounded interval reports failure and selects compatible fallback. Actual
presentation means the matching GTK draw completed, not merely queued work and
not measured photon timing. Control snapshots, revocations and the immediate tick
are applied in one GLib callback. Protocol Revocation carries epoch, plan revision
and an idempotence sequence so delayed revocation cannot cross a rejoin offer.

The common Pi image contains no device identity or private fleet secret. It mounts
an existing Photo Wall-owned `PWSTATE` volume for durable keys/cache/state. The
generated common disk artifact includes an empty owned state partition; the PXE
bootstrap may also discover an existing owned volume. It does not erase arbitrary
attached storage. A network-capable device with missing/unusable persistent state
still automatically generates a volatile key and registers as unbound with a
storage fault before any manual intervention. Its signed enrollment explicitly
declares volatile persistence, and central refuses Frame binding/execution for
that registration. This amends the earlier persistent-key-before-enrollment rule
only for initial fault visibility, not stable identity or playback authority.
Durable operation still requires an owned writable state volume. Later central
storage initialization, if added, must be a distinct authorized operation.

Network-capable EEPROM and an operator-prepared PXE/DNS/trust LAN remain supported
hardware/deployment prerequisites. No local endpoint, credential entry or SSH
step is required before initial central appearance. Common public configuration
names the trusted central origin and public CA; deployment-specific signing
private keys and image binaries stay outside Git. The external artifact directory
for this task is `/Volumes/Dock/PhotoWallArtifacts/2026-09-05-01a0731f`.

The rootfs lives in RAM after verified PXE download; updates use the scoped signed
userspace A/B procedure with fixed boot ABI from the platform design. No physical
PXE, image boot or visible coordination result is established by this decision.
