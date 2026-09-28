# Development setup and recovery

Status: central, PostgreSQL, and the Procrastinate media worker launch together. Earlier two-Player/three-Output and native evidence established the MVP shape. Final committed-revision evidence remains required for the complete demo, authenticated browser walkthrough, current exact-image native paths, and automatic central rollback; physical qualification also remains outstanding. Follow the [delivery checklist](implementation-checklist.md) and [evidence](evidence/README.md); do not treat earlier local-state evidence as acceptance of the current design.

## Local launch

Prerequisites: Git, Docker Engine/Desktop with Compose, and Python 3.12 for development tests. Docker runs the central Python dependencies and PostgreSQL; no paid runtime service is required.

```sh
git clone https://github.com/mcurcio/photo-wall.git
cd photo-wall
git checkout feat/runnable-mvp
python3 scripts/configure.py
docker compose up -d --build --wait
curl --fail http://127.0.0.1:8000/healthz
```

Open `http://127.0.0.1:8000`. Read the operator token from the private `.env` file and enter it once to sign in; the browser stays signed in for 30 days ([signing in and Log out](#operator-console-signing-in-and-log-out)). The script creates `.env` with mode 0600 and never overwrites it. No credentials are committed. The development listener and database port bind only to loopback. Appliance deployment requires the separately configured HTTPS/PXE trust boundary; this local listener is not that deployment.

The operator interface lists Players and Outputs, creates persistent Frames, binds equipment, retires a Player, and previews/commits/reverts calibration. It also creates immutable Sources and Scenes, schedules Programs, starts/finishes/cancels Runs, and shows source/worker health. Program timestamps use the browser's displayed local time zone. Configure the private upstream connection on the worker before creating a Source with its connection ID. The disposable browser walkthrough below covers these controls; final-revision delivery evidence remains separate. With the scheduler enabled, `/healthz` is green only when the database is reachable and a scheduler tick completed successfully within the last 10 monotonic seconds; `starting`, `coordination_unavailable`, `stale`, and `stopped` states return 503 with fixed sanitized status fields. Explicit test mode can disable the scheduler and retain database-only health semantics. A green `/healthz` reports service liveness, not observed presentation.

The worker starts with an empty private connection list and remains healthy while idle. Its configuration is described in [the worker module](module-media-worker.md); a deployment must provision that file as UID 10001, mode 0600 in the `connections` volume and restart `worker`. Never put an upstream API key in operator forms, Source definitions, Player configuration, Git or command-line arguments. The [Immich fixture](module-immich-fixture.md) generates its own synthetic media and disposable private configuration for reproducible adapter tests.

To provision a real worker connection, use the Compose service's mounted
`connections` volume and feed a separately prepared private file through
Docker standard input. This keeps the API key out of shell arguments, history
and Docker output. Prepare the source file outside the repository with the
deployment's approved secret editor or secret manager, and make it mode 0600
before using it:

```sh
python3 - <<'PY'
import os
from pathlib import Path
path = Path.home() / ".photo-wall-connections.json"
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.fchmod(fd, 0o600)
os.close(fd)
print("Created private connection file with mode 0600.")
PY
```

The command refuses to overwrite an existing file. Do not place that file in
Git or a shared temporary directory; populate it with the deployment's approved secret
editor or secret manager, keep it for the next command only, and remove it
with the deployment's secret-management procedure afterward.

Use this shape as the file contents, replacing only the values in the approved
private editor. `base_url` must end in `/api` (or `/api/`), and `ca_file` is a path
inside the worker container when a separately provisioned private CA is
required:

```json
{"schema":1,"connections":[{"connection_id":"immich-main","base_url":"https://immich.example.test/api","owner_id":"00000000-0000-4000-8000-000000000000","api_key":"PASTE_KEY_HERE","allow_http":false,"ca_file":null}]}
```

Validate and publish the staged file as the worker UID. The existing worker
loader performs the schema, ownership, mode, size, URL/TLS-policy and
secret-shape checks; it does not replace an actual upstream TLS connection
test. A failed check removes the staged file without printing its contents.

```sh
docker compose run --rm --no-deps -T --user 10001:10001 \
  --entrypoint /bin/sh worker -c '
set -eu
trap "rm -f /etc/photo-wall/private/.connections.json.new" EXIT
umask 077
cat > /etc/photo-wall/private/.connections.json.new
chmod 0600 /etc/photo-wall/private/.connections.json.new
python -c "from pathlib import Path; from media.worker import load_connections; load_connections(Path(\"/etc/photo-wall/private/.connections.json.new\"))" >/dev/null 2>&1
mv /etc/photo-wall/private/.connections.json.new /etc/photo-wall/private/connections.json
' < ~/.photo-wall-connections.json
docker compose restart worker
docker compose exec -T worker stat -c '%u:%g %a' /etc/photo-wall/private/connections.json
```

The final command must report `10001:10001 600`. Updating this file takes
effect after a worker restart. Do not use `cat` to display the private document,
an inline heredoc, or a command argument for the private document or API key.
Worker diagnostics are sanitized, but never send the private document to a log
command. The fixture helpers remain disposable test configuration and do not
provision a deployment worker.

In the Scene form, keep live source selection for a changing collection, or
select **Choose a photo or video for each Frame** to keep a specific current
asset for each participating Frame. Each chooser lists only media compatible
with that Frame. The Scene and its references save together; source freshness,
membership and compatibility are rechecked centrally. Playback still requires
prepared media.
The [authored-media contract](module-authored-media.md) describes retention and
capacity. Development checks for this form require Node.js and execute its
event flow with a synthetic DOM; this is separate from the authenticated
browser walkthrough.

For immediate activation, choose a priority and whether an already active Scene
should be ignored, restarted, or queued. Queue requests require an expiry;
**Force** is an explicit override. Queued and ignored requests report their
actual outcome without claiming that a new Run started.

`PHOTO_WALL_HORIZON_SECONDS` defaults to 300 seconds. In the scheduler's PostgreSQL transaction, the media repository records each bounded preparation request and defers its exact job ID through Procrastinate. The separate worker publishes verified derivatives into the `media` volume. Central mounts it read-only and serves exact authorized bytes; Players never receive an upstream URL or credential. Procrastinate owns dispatch, retry timing, and queue-worker liveness. Photo Wall schedules only domain preparation, source refresh, and publication/storage maintenance tasks; it retains publication recovery, reservations, and stale-attempt fencing. The worker has a read-only runtime, a private writable media volume, and container CPU/memory limits.

Fresh key-proof enrollment uses `/v1/enrollment/challenge` plus `/v1/enrollment/register`. The single-process Player entry point is `python -m player.service --config /etc/photo-wall/public.json`; see [service configuration and runtime requirements](module-player-service.md). The Player reads its RAM boot context, creates a new process key, and receives a new central authority epoch. A recognized returning equipment observation restores central bindings; unknown equipment remains unbound. `/v1/player/time` supplies independent authenticated clock samples. Base images are fetched from `/v1/netboot/base` (by content hash). Physical Pi/PXE and complete current-image qualification remain pending. Startup-only DRM discovery currently requires a Player restart after connector topology changes.

The `database` volume persists all authoritative state, including Procrastinate jobs and release/trial records. `media` holds central authoritative media blobs and preparation files, and `connections` holds private worker configuration. Central and worker startup apply forward Photo Wall SQL migrations under a database advisory lock and check hashes of already-applied migrations; they also install/upgrade Procrastinate's versioned schema. Do not edit an applied Photo Wall migration; add another numbered migration. Both containers run as an unprivileged user. The exact Python dependency graph is in `uv.lock`.

## Player provisioning: flash and go

The baseline Player path (0008) is flash-and-go: no boot ticket, no pre-registration. Central advertises itself over mDNS (`_photowall._tcp`) once its process starts, unless `PHOTO_WALL_MDNS_ADVERTISE=false` is set. If the deployment serves central on a port other than the default `8000`, also set `PHOTO_WALL_HTTP_PORT` to that real port, or the advertisement points a discovering player at a dead port.

1. **Flash the generic image** to an SD card or USB drive (build it per the [README's provisioning section](../README.md#provision-player-appliances); there is no published release artifact yet) and boot the Pi on the same trusted LAN as central.
2. **Watch the pending queue.** The Pi derives its `device_id` from its own hardware serial, finds central by mDNS (only because no explicit origin is configured on the card), and enrolls over plain HTTP. It appears in the operator inventory (`/v1/operator/inventory`, or the operator UI's Players list) as **unbound** — no prior registration required.
3. **Bind** the pending Player's Output to a Frame from the operator interface, the same control used for any equipment change, then calibrate it.
4. **Unbind** (`DELETE /v1/operator/frames/{frame_id}/binding`) releases a Frame's binding without retiring the Player — the serial can be re-bound later, to the same or a different Frame. This is distinct from **retire**, which is permanent and refuses the serial's re-enrollment.
5. **Reboot** of an already-bound Pi re-associates with its Frame automatically by serial; no operator action is required unless the binding itself needs to change.

Netboot (PXE) is the opt-in enhancement path in place of flashing — see the [PXE service module](module-pxe-service.md). Certificate-based identity and pinned/explicit transport trust are further opt-in enhancements described in [decision 0008](decisions/0008-generic-image-and-serial-identity.md); they are designed but not yet implemented, so do not rely on them today. Physical Pi boot of either the flash image or the netboot tree is not yet hardware-qualified.

## Player provisioning: netboot and promote the app (0009, in progress)

[Decision 0009](decisions/0009-minimal-base-and-app-package.md) is the adopted target for the netboot tier: a minimal base OS image that carries no application, plus the Player shipped as a downloadable `.deb` that central serves. Nothing is signed — the owner ruled a home LAN has no threat model, so the sha256 published alongside the `.deb` is a corruption check, not an authenticity proof. Once the boot-chain wiring below lands, the operator flow is:

1. **Stage the boot files in your TFTP tree.** From a published release's base tarball, `photo-wall-base-<revision>.tar.gz`, stage only `photo-wall-base/boot/` (kernel, DTBs, initramfs) beneath the boot-server root, as [staging the netboot bundle](#player-provisioning-stage-the-netboot-bundle-and-read-its-console-0014) describes — see [PXE service setup](module-pxe-service.md). Do not stage the tarball's base squashfs: Central serves it over HTTP ([decision 0012](decisions/0012-netboot-base-auto-mirror.md)). The base carries no Player code and no deployment config; it exists to run the bootstrapper (`appliance/provision.py`) that fetches everything else. The kernel command line must name Central with `photowall.central=http://photo-wall.localdomain/` or your deployment's Central root (see step 2 of [decision 0014](decisions/0014-reaching-central-from-every-boot-stage.md)).
2. **Boot the Pi and watch the pending queue.** The bootstrapper reads `photowall.central` from the kernel command line, locates Central (following the gateway's redirects for `GET /v1/locate` only), downloads the manifest and `.deb` from the located Central, checks the sha256, installs it with `dpkg --install` alone (no apt, nothing downloaded from Debian), writes `/etc/photo-wall/public.json` and starts the Player, which locates Central the same way and enrolls by serial — it appears **unbound** in the same operator inventory (`/v1/operator/inventory`) as the flash path.
3. **Check which app is promoted.** Central serves the `.deb` of the one promoted release, taken
   from the GitHub release list ([below](#player-provisioning-promote-a-release-from-github-0010)).
   The release sync promotes the newest deployable release by itself (`promoted_by: "auto"`)
   until a Player is bound and a `.deb` has been downloaded; after that it holds the promotion.
   To choose a release, promote it yourself:

   ```sh
   curl -X POST -H 'Authorization: Bearer <admin-token>' \
     http://<central>/v1/operator/app/releases/<tag>/promote
   ```

   The release sync never moves an operator promotion. Every Player fetches the newly promoted
   `.deb` on its next reboot; already-running Players are unaffected until then.
4. **Bind** the pending Player to a Frame and calibrate, exactly as in the flash-and-go flow above.
5. **Update the app later** by promoting a newer release tag, as in step 3 — no re-imaging, no
   re-signing, no boot-tree edit. There is no auto-rollback: if a promoted `.deb` crashes on boot,
   the dark screen is the signal, and recovery is re-promoting the previous tag.

**Where this actually stands.** Central's app manifest and package routes (`central/content_routes.py`, over the release
catalog in `central/content_catalog/catalog.py`), the minimal base image build (`scripts/build_ci_base_image.py`), the `.deb` build (`scripts/build_player_deb.py`), and the bootstrapper (`appliance/provision.py`) are each implemented and pass their own tests in isolation. **The PXE boot chain that would load the minimal base and hand off to the bootstrapper — with no boot ticket and no signature — is not yet wired**: today's initramfs still runs the old signed boot-ticket protocol described in [decision 0008](decisions/0008-generic-image-and-serial-identity.md#the-netboot-tier-d1-and-its-config-decoupling), so a netboot deployment today still boots that signed, combined image, not this one. Do not follow the steps above against a real fleet yet; they describe the design 0009 targets, and this section will be reconciled with the [appliance builder](module-appliance-builder.md) module once the wiring lands.

## Player provisioning: stage the netboot bundle and read its console (0014)

[Decision 0014](decisions/0014-reaching-central-from-every-boot-stage.md) changes how the netboot's first stage reaches Central and how it stays alive. None of this is hardware-qualified yet. Its first Pi observations (the M0 and M1 milestones) are still to be recorded.

1. **Stage the kernel, `initrd.img` and DTBs together, from one release.** Download a published GitHub Release's base tarball, `photo-wall-base-<revision>.tar.gz` (check it against the release's `SHA256SUMS`), and take `photo-wall-base/boot/` from it whole: `kernel_2712.img`, `initrd.img`, `bcm2712-rpi-5-b.dtb`, `overlays/`, `config.txt`, the `cmdline.txt` template, `pieeprom.upd` and `pieeprom.sig`. Never stage from a CI (Actions) artifact: one exists for every build, including unreleased pull requests, and expires after seven days, while a published release is complete by construction and is what Central mirrors the base squashfs from. `initrd.img` carries that build's stage-1 code, its CA list (copied from that build's base) and its clock floor, all in front of the cached initrd ([`tests/test_build_netboot_bundle.py`](../tests/test_build_netboot_bundle.py)). The kernel package is not pinned, so an initrd from one build must not be paired with a kernel from another. The CA list is as old as that build's Debian snapshot pin and stays that old until you stage again. Stage again when the gateway's certificate chain changes.
2. **Set the cmdline.** Keep one line from the template and set `photowall.central=` to Central's root. The root may be http: stage 1 follows the gateway's redirects to https across hosts (up to ten), refuses any https→http step, and verifies TLS ([`tests/test_uplink_tls.py`](../tests/test_uplink_tls.py)). On failure the console prints the named cause and the redirect hops, never a value to set, because the located origin can come from an unauthenticated first hop ([decision 0014, U7](decisions/0014-reaching-central-from-every-boot-stage.md#requirements-hard-rules)). Stage 1 no longer prints a `set photowall.central=…` note; on an http root it logs only `note: configured root is http: the first hop is unauthenticated`. Keep `watchdog.stop_on_reboot=0 hung_task_panic=1` beside `panic=10`. If either is missing, stage 1 boots on and names it (`note: kernel liveness missing: …`; [`tests/test_netboot_liveness.py`](../tests/test_netboot_liveness.py)).
3. **The EEPROM step is the two `pieeprom` files.** On its next boot, the Pi's bootloader updates itself from the TFTP directory to `BOOT_ORDER=0xf21` (SD, then network, then start again) and `BOOT_WATCHDOG_TIMEOUT=120`, and resets once. No SD card is needed ([`tests/test_eeprom_update.py`](../tests/test_eeprom_update.py)). To keep a Pi's bootloader as it is, leave the two files out. The Pi must already boot from the network: its factory boot order has no network entry.
4. **Read a failed boot on the console.** Every stage-1 failure prints one line, `photo-wall[netboot] FAILED phase=<n> cause=<cause> reason=<reason> host=<host> detail=<detail>` ([`tests/test_netboot_init.py`](../tests/test_netboot_init.py)). Some lines to know:
   - `cause=tls reason=untrusted … bundle=sha256:… anchors=… floor=…`: the initrd's CA list does not trust the served chain. Stage a newer bundle.
   - `cause=time`: the certificate's dates disagree with the clock. The line shows the clock state and the time sources tried; offer DHCP option 42 if nothing answered.
   - `cause=not_central reason=status detail=status=404`: the Central behind the gateway is too old to answer `/v1/locate`.
   - `cause=redirect reason=unexpected`: the base fetch was redirected after locate. The next boot locates again.
   - `cause=configuration reason=absent` or `invalid`: `photowall.central` is missing, or is still the template's placeholder.
   - `phase=7 code=<code> detail=<cause>`: mounting the base failed. `detail` is the failing helper's stderr tail or an errno name (`code=boot_loop detail=ENOENT`: no loop driver). The initrd's `mount` is klibc's, not util-linux's, so stage 1 attaches the base's loop device itself and passes only kernel mount options. CI runs this mount path from each built initrd ([`scripts/initrd_mount_probe.py`](../scripts/initrd_mount_probe.py)).
   - `phase=7 code=boot_modules detail=no /usr/lib/modules/<release> in this initrd …`: the kernel and `initrd.img` come from different builds, so the base would get no drivers and the display would never come up. Stage `boot/` from one release, whole.

   After the line, the boot script restarts the Pi. It uses a sysrq emergency restart, not `reboot`; the hardware watchdog armed at stage 1's start is the backstop. `photowall.debug=1` holds the console for 60 s first.
5. **If a Pi is found stalled, look at its HDMI screen before resetting it.** The bootloader's diagnostics page with `order` other than `0xf21` means the EEPROM self-update did not apply. Kernel text with no `photo-wall[netboot]` line and no panic is a hang that nothing in the product resets. `sysrq: Resetting` as the last line means the emergency restart hung and the watchdog did not fire. Record what the screen shows in the evidence. The owner of each boot segment is listed in the [execution contract](execution-contract.md#netboot-stage-1-boot-data-the-clock-record-and-liveness).

6. **Reading provisioning and Player failures.**
   - Read the provisioning unit's logs: `journalctl -u photo-wall-provision`. Provisioning logs one line per failed attempt with format `cause=<cause> reason=<reason> host=<host> detail=<detail>`. For `cause=time` or `cause=tls reason=untrusted` it appends the stage-1 clock record: `clock=<state> floor=<date> tried=...`.
   - Read the Player's logs: `journalctl -u photo-wall-player`. The Player logs `player fault: <cause>_<reason>` on enrollment or connection failure, e.g. `tls_untrusted`, `redirect_unexpected`, `connect_refused`, `dns_failed`, `central_error`, followed by the same `host`, `detail` and `clock` fields as provisioning.
   - A Player unit that does not start logs `cause=unit reason=<systemd result> detail=photo-wall-player.service/status=<n>/<NAME>`, and provisioning exits into the restart limit. A named status such as `216/GROUP`, `217/USER` or `203/EXEC` means systemd could not set up the Player's process, so the booted base cannot run it. v0.9.1's base had no udev, so it had no `render` group and failed with `216/GROUP`. Stage a base whose CI passed the Player start check ([`scripts/player_start_probe.py`](../scripts/player_start_probe.py)). A bare `status=1` means the Player itself exited; read `journalctl -u photo-wall-player`. `reason=timeout detail=…/state=activating/<substate>` means the Player was still starting when provisioning stopped waiting (90 s). It never sent READY; `start-pre` means it was still waiting for the compositor's socket.
   - `cause=time` is not retried in place: the provisioning unit exits, and after 10 exits in 10 minutes the systemd service reboots the Pi. On reboot, stage 1 re-steps the clock.
   - A dpkg dependency failure during provisioning looks like `photo-wall-player depends on python3-foo; however: Package python3-foo is not installed`. It means the promoted Player requires a Debian package the booted base lacks. Fix it by staging a base built from a declaration that includes it, or by promoting an older Player. The Pi reboot-loops until fixed. **Operator rule:** stage the base first, then promote a Player that adds a Debian package.
   - Bumping the Debian snapshot pin (developers): edit `PIN.snapshot` in `scripts/debian_packages.py` (format `YYYYMMDDTHHMMSSZ`). Every artifact keyed on the declaration rebuilds at the new snapshot: the base, the initrd build root, and the CI device root. Re-stage the whole TFTP bundle from that new build.

**Console success on phase 7.** After a successful mount, stage 1 prints `phase 7/7 mount + handoff: modules=<release> files=<n> bytes=<b>`. That line means the initrd's kernel modules were copied onto the base, which carries none of its own. The display drivers (`vc4`, `v3d`) already loaded in stage 1, because `config.txt`'s `dtoverlay=vc4-kms-v3d-pi5` turns the display on. A bundle without that line has no `/dev/dri`. It then prints `phase 7/7 mount + handoff: success dns=<a,b> search=<x>` (where `dns=none` if stage 1 had no resolver discovered). Stage 1 copies its resolver state to the new root's `/etc/resolv.conf`, and the base carries none. Stage 1 then writes `/run/systemd/system.conf.d/90-photo-wall-watchdog.conf` (`RuntimeWatchdogSec=30s`, `RebootWatchdogSec=300s`), so systemd in the base keeps petting the watchdog stage 1 armed. The provisioning unit reboots the Pi after 10 exits in 10 minutes, and the Pi then netboots again. The clock step stage 1 took is recorded in `/run/photo-wall-clock.json`.

## Player provisioning: promote a release from GitHub (0010)

[Decision 0010](decisions/0010-github-release-sourcing.md) removed 0009's manual sha256 dance: central **watches the project's GitHub Releases**, records every semver release as a candidate, and downloads the `.deb`s the fleet needs (the promoted release's among them) into the shared `.deb` store Players fetch from. As of [decision 0013](decisions/0013-unified-cache-root.md) that store is the derived `apps/` subdir of the single cache root (`PHOTO_WALL_CACHE_ROOT`), not a separate `PHOTO_WALL_APP_ROOT`. Discovery is automatic. So is promotion on a fresh install: the release sync promotes the newest
deployable release until a Player is bound and a `.deb` has been downloaded, then holds it; an
operator promotion overrides it and is never moved by the sync. Nothing is signed; the sha256 is a corruption check only. See [the operator release-sourcing flow](module-player-package.md#operator-release-sourcing-0010) for the model.

**Configuration.** Release sourcing is **always-on** (0013 retired the opt-in gate): the worker polls GitHub and mirrors bytes into `<cache-root>/apps/` unconditionally; central serves them RO. Both mount the one cache root (see the [Central cache subsystem](module-central-cache.md)); there is no separate `.deb`-store env to wire.

| Variable | Where | Default | Meaning |
|---|---|---|---|
| `PHOTO_WALL_CACHE_ROOT` | central (RO) + worker (RW) | `/var/cache/photo-wall` | The one cache root; the `.deb` store is its derived `apps/` subdir. Optional (baked default) |
| `PHOTO_WALL_RELEASE_REPO` | worker | `mcurcio/photo-wall` | `owner/name` of the GitHub repo whose releases are polled |
| `PHOTO_WALL_RELEASE_TOKEN` | worker | (unset) | Optional GitHub token; unauthenticated polling is rate-limited to ~60 requests/hour |
| `PHOTO_WALL_RELEASE_PRERELEASES` | worker | off | Truthy to also track GitHub prereleases (drafts are always skipped) |
| `PHOTO_WALL_RELEASE_POLL_SECONDS` | worker | `900` | Poll cadence in seconds |
| `PHOTO_WALL_RELEASE_API_BASE` | worker | `https://api.github.com` | Base URL of the releases API; unset or empty means the default, an invalid URL fails at boot. Tests point it at a fake origin |

**List, promote, refresh (all admin-authenticated).** These reach central's operator API; substitute your central origin and admin token:

```sh
# List tracked releases, newest first: tag / is_prerelease / deployable / promoted /
# promoted_by ("auto" | "operator", null unless promoted) / has_os_image.
curl --fail -H 'Authorization: Bearer <admin-token>' \
  http://<central>/v1/operator/app/releases

# Promote a version: 200 {"status": "promoted"}, and central starts downloading its .deb;
# 404 unknown tag; 409 no .deb (undeployable); 422 not a version tag.
curl -X POST -H 'Authorization: Bearer <admin-token>' \
  http://<central>/v1/operator/app/releases/<tag>/promote

# Poll GitHub now instead of waiting for the next cadence (coalesced; 202 {"status": "polling"}).
curl -X POST -H 'Authorization: Bearer <admin-token>' \
  http://<central>/v1/operator/app/releases/refresh
```

All three routes are always available: release sourcing is unconditional as of [decision 0013](decisions/0013-unified-cache-root.md), so the former **503 `release_sourcing_unconfigured`** branch (gated on the old `PHOTO_WALL_APP_ROOT`) has been removed.

**Promoted vs. served.** A promote records your tag at once (`promoted_by: "operator"`) and, in
the same transaction, queues the download of its `.deb`. `GET /v1/app/manifest` names the promoted
`.deb` once its bytes are on disk; until then it names the last-good one (the latest earlier
promotion whose `.deb` was on disk when it was replaced) if that is on disk, so Players are never
broken. With neither on disk it names the promoted one, and the package route downloads it on
request. A failed download is retried: a Player's next request asks again, and the five-minute
`Prefetch` tick re-queues it after a transient failure; no re-promote is needed. Watch `promoted`
and `promoted_by` in the list.

**Offline / air-gapped.** There is no manual stage path: the hand-staging routes
(`POST /v1/operator/app`, `PUT /v1/operator/app/current`) no longer exist, and central downloads
every `.deb` from the download link of the GitHub release that lists it. A `.deb` already on disk
keeps serving while the uplink is down.

## Base-image auto-mirror (0012)

[Decision 0012](decisions/0012-netboot-base-auto-mirror.md) extends the same discover-and-mirror model to the **base squashfs**, so you no longer hand-stage it into a served directory. The fleet is heterogeneous: central serves **several base images at once**, one per version some Pi needs, resolved **per device**. There is **no fleet default and no promote-the-base action** — rollout is emergent (see *pin a canary* below).

**Storage (the root of the old outage).** As of [decision 0013](decisions/0013-unified-cache-root.md) base bytes live at the derived **`<cache-root>/os-images/base-<tarball sha256>.squashfs`** (named by the sha256 of the release's base tarball, so a re-cut is a new file) under the single cache root (`PHOTO_WALL_CACHE_ROOT`, default `/var/cache/photo-wall`), **mounted RW on the worker and RO on central** — one cache PVC, no separate per-domain volume. The worker is the single writer, **asserts its cache is writable at boot** and fails loud (an ERROR log) if not, and self-heals a missing file at the serve seam (a read that finds no file publishes its fetch) — so a wiped or unmounted volume can no longer produce a silent, permanent `503`. On **NFS**, `flock` and `O_EXCL`/atomic-rename reliability across the mount is a documented precondition. Base serving is **always-on**; there is no "off" state. See the [Central cache subsystem](module-central-cache.md).

| Variable | Where | Default | Meaning |
|---|---|---|---|
| `PHOTO_WALL_CACHE_ROOT` | worker (RW) + central (RO) | `/var/cache/photo-wall` | The one cache root; `base-<tarball sha256>.squashfs` files live in its derived `os-images/` subdir. Optional (baked default); base serving is always-on |
| `PHOTO_WALL_PER_DEVICE_DEB` | player | (unset) | Opt-in: the Pi fetches the `.deb` of the exact tag its base was served this boot (`GET /v1/netboot/manifest`, serial-keyed) and posts base-health. Unset ⇒ unchanged 0010 global `.deb`, no base-health |

**Discovery.** Automatic, on the same poll as the `.deb`: the worker reads each release's `manifest.json` `base_image` + `revision` and records the base facts on the catalog row. Discovery moves **no bytes** and changes **no device's target**. The heavy squashfs is downloaded only when a device actually needs a version.

**A `503` is transient and self-heals — it is never a dead end.** The design does **not** promise "never `503`"; it promises a `503` is **bounded and self-heals**. A base a device needs but that is not yet cached returns a `503`, the worker fetches it in the background, and the diskless Pi retries on its next boot (fails-closed-and-reboots). A boot re-hydrate refills any file that went missing. The one accepted exception: a **fresh cluster's very first image** is `latest-discovered` and therefore **unverified** — a bad first image bricks initial bring-up until you pin a known-good version.

**Per-device selection.** A Pi sends its serial; central resolves **one** tag by precedence: the device's **pin**, else **latest-verified** (the highest semver any device row not marked retired has reported base-healthy — a live query, never a stored pointer), else — empty cluster only — **latest-discovered**. Both the base and (with `PHOTO_WALL_PER_DEVICE_DEB`) the `.deb` come from that one tag. **Retiring a Player does not retire its device row**: nothing in Central marks a netboot device retired today, so a retired Pi's last healthy tag still counts toward latest-verified and its serial still netboots. Whether retire should also retire the device row is an open owner question ([slice 2, Question 4](operator-console-ux-pass2-onboarding.md#12-costs-deferrals-and-questions)).

**Pin a canary / recover a device (admin-authenticated).** Rollout is emergent: pin one device to a candidate version; when it boots and posts base-health, `latest-verified` climbs and unpinned devices follow on their next boot — no separate promote step. The same route rolls a broken device back by pinning it to a known-good tag.

```sh
# Pin device <device-id> to <tag> (a canary, or a manual rollback). Proactively
# fetches that tag's base (and .deb) so the device comes up on its next netboot.
curl -X PUT -H 'Authorization: Bearer <admin-token>' -H 'Content-Type: application/json' \
  -d '{"tag":"<tag>"}' http://<central>/v1/operator/devices/<device-id>/pin

# Clear the pin: the device falls back to latest-verified.
curl -X DELETE -H 'Authorization: Bearer <admin-token>' \
  http://<central>/v1/operator/devices/<device-id>/pin
```

**Server-side rollback (the Pi is diskless).** The Pi persists nothing and cannot choose a tag, so rollback lives on central. When a device is served a target (a 200) but never posts it base-healthy and re-netboots, central marks that boot failed, fences the tag, and serves the device its own **known-good** on the next boot — **sticking** there (never re-serving the failing tag) until a newer tag appears or you pin it, so it cannot oscillate. A device with **no** prior known-good that cannot boot its served image boot-loops until you pin it (accepted).

**Garbage collection.** None exists yet. Nothing removes an OS image file or an unreferenced
asset row: a re-cut leaves the old `base-<tarball sha256>.squashfs` (about 1 GiB) and its bare
asset row on disk until `MaintainCache` is built. A wiped cache refills on demand: a read that
finds no file publishes its fetch, and the five-minute `Prefetch` publishes the fetch of every
desired asset missing from disk. A device row marked retired names no tag, so it keeps nothing desired; no operator action marks one today, and retiring a Player leaves its device row active (above).

**Observability (`GET /v1/operator/netboot`, admin-authenticated).** A read-only view to answer "why did this device get this image / why won't it advance / why were bytes evicted": the **BASE_ROOT boot-assertion outcome** (so a failed base volume is visible, not only logged), the live **frontier** (`latest-verified`), each **device's** pin / known-good / last-served tag + boot outcome / sticky failed tag, and each **`base_cache`** row's state + eviction reason. (An operator UI over these fields is deferred; the backend fields ship here.)

```sh
curl --fail -H 'Authorization: Bearer <admin-token>' http://<central>/v1/operator/netboot
```

**Two assumptions worth stating (0012 errata E9).** (1) Base-health's server-side `running_tag == last_served_tag` check binds to the **live** `devices.last_served_tag`, relying on no concurrent re-serve of the same device interleaving between the per-device manifest fetch and the base-health post — which holds on the diskless target, since a genuine reboot restarts the whole squashfs fetch. (2) The appliance's origin-handoff writer preserves existing keys and does not explicitly clear the served tag on a global-path boot; this is harmless on the RAM-overlay netboot target (rebuilt fresh each boot), but a persistent-disk reuse of that path would need to clear it.

## Upgrading to content-keyed OS images (migration 028)

Migration 028 re-keys every OS image from its release tag to the sha256 of its base tarball
([idempotent jobs](central-idempotent-jobs.md) §8). It carries **no produced facts**: the owner
decided that each desired OS image downloads once more after the upgrade, rather than trusting a
file whose tag may have been re-cut since it was fetched. The cost, stated plainly:

- A Pi that reboots before its image lands fails its fetch (`503`) and reboots again.
- If GitHub is unreachable, or a wanted tarball was deleted upstream, Pis loop on `503` and reboot
  until a download succeeds.
- A device pinned to a deleted release stays stuck until you re-pin it.

1. **Preflight, before upgrading.** Confirm the worker reaches the release origin
   (`PHOTO_WALL_RELEASE_API_BASE`, default `https://api.github.com`). Then, for every desired
   tag, check that its tarball URL answers. The desired tags are every active device's pin and
   known-good, every tag served in the last 30 days, and, while no device has a known-good, the
   newest release with an OS image (`GET /v1/operator/app/releases`, `has_os_image`). This lists
   the device-named ones with their URLs:

   ```sh
   docker compose exec -T database psql -U photo_wall photo_wall -At -F ' ' -c "
     SELECT DISTINCT r.tag, r.base_tarball_url FROM app_releases r JOIN devices d
       ON d.retired_at IS NULL AND r.tag IN (d.attached_tag, d.known_good_tag,
          CASE WHEN d.last_served_at >= EXTRACT(EPOCH FROM now()) - 30 * 86400
               THEN d.last_served_tag END)
     WHERE r.base_tarball_url IS NOT NULL ORDER BY r.tag"
   # For each URL, fetch one byte: 206 or 200 means it answers; 404 means deleted upstream.
   curl -sL -r 0-0 -o /dev/null -w '%{http_code}\n' '<base_tarball_url>'
   ```

   A tag whose tarball is gone will not boot after the upgrade. Re-pin its devices first.
2. **Upgrade Central and the workers together** (Compose replaces both). 028 runs at start-up.
3. **Delete the old `os-images/base-<tag>.squashfs` files.** Nothing reads them any more, and
   they double the OS-image disk until removed. A new-style name is 64 hex digits; a tag name
   starts with `v`:

   ```sh
   docker compose exec -T worker sh -c 'rm -f /var/cache/photo-wall/os-images/base-v*.squashfs'
   ```

   (Use your `PHOTO_WALL_CACHE_ROOT` if it is not the default.)
4. **Watch the first downloads.** Each desired OS image downloads once, on the next `Prefetch`
   (every five minutes) or on the first Pi that asks for it.

**Rollback.** Migrations are forward-only. From 028's header, in this order:

```text
  a. End the new-shape deliveries, which the old code cannot decode:
       UPDATE procrastinate_jobs SET status = 'cancelled'
       WHERE task_name = 'photo_wall.os_image.fetch' AND status = 'todo';
       UPDATE procrastinate_jobs SET status = 'failed'
       WHERE task_name = 'photo_wall.os_image.fetch' AND status = 'doing';
  b. Drop the CHECK, which the old code's tag-keyed references violate:
       ALTER TABLE asset_references DROP CONSTRAINT asset_references_locator_names_the_key;
  c. Revert the code.
  d. UPDATE app_release_poll SET etag = NULL;
     so the next sync lists every release again and re-references each OS image under its
     tag: one download per desired OS image.
```

**Roll forward after a rollback:** repeat (a) for the old-shape deliveries, then

```sql
DELETE FROM schema_migrations WHERE name = '028_os_image_content_key.sql';
```

and deploy the new code: 028 runs again and re-keys every OS image from `app_releases`. It is
safe to run twice.

**Rolling Central back past stored protection refusals.** Runtime state (`runtime_state.snapshot`)
records the Run that refused a protected activation or Program as `blocking_run_id` on that
Admission, and only there. Builds before that field forbid the key, so once a protection refusal
has been stored a previous Central build cannot restore runtime state: `/healthz` reports the
scheduler as `coordination_unavailable` on every tick, and operator runtime reads and writes
return 422 `invalid_command`. A state with no stored refusal is unaffected. Prefer rolling
forward. Otherwise stop Central, then run this once before starting the previous build (it only
removes the key, so the refusal keeps its reason; running it again changes nothing):

```sql
UPDATE runtime_state
SET snapshot = jsonb_set(snapshot, '{admissions}', (
    SELECT jsonb_object_agg(key, value - 'blocking_run_id')
    FROM jsonb_each(snapshot->'admissions')))
WHERE EXISTS (
    SELECT 1 FROM jsonb_each(snapshot->'admissions') WHERE value ? 'blocking_run_id');
```

## Operator API: reposition and remove Frames

The operator console edits the wall plan through two admin-authenticated routes on central (both `Depends(admin)`, like every `/v1/operator/*` route). They change no schema and add no migration — the placement columns (`surface_id`, `x_mm`, `y_mm`, `width_mm`, `height_mm`) already exist on the `frames` row.

`PATCH /v1/operator/frames/{frame_id}` applies a **partial** placement. The body accepts `surface_id`, `x_mm`, `y_mm`, `width_mm` (> 0), and `height_mm` (> 0); any omitted field keeps its stored value — this is a merge, not a replace. Central re-runs the same orientation-coherence guard as Frame creation against the merged dimensions and the stored profile, returning **422** when the resulting aperture orientation would disagree with the display profile. An id that no longer exists returns **404 `unknown_frame`**; on success the response echoes the merged placement. Placement is **last-write-wins with no concurrency token** — two operators dragging the same Frame silently overwrite each other and the plan corrects on the next snapshot — because geometry is operator-only metadata: it never reaches a Player, it is independent of calibration (whose corners and crop are normalized to `[0,1]`), and a move is trivially re-dragged. The route therefore **does not bump `generation` or `configuration_revision` and never invalidates calibration**.

`DELETE /v1/operator/frames/{frame_id}` removes a Frame, but only a clear one. It refuses with **409 `frame_in_use`** when a live Run (phase body or outro) targets the Frame — finish or cancel that Run first — and with **409 `frame_bound`** when an Output is still bound to it — unbind first (`DELETE /v1/operator/frames/{frame_id}/binding`). An unknown id returns **404**. On success it deletes the Frame and returns **200 `{"status": "deleted"}`**. The guards protect one invariant: you cannot delete a Frame a Player is currently bound to serve.

## Operator console: signing in and Log out

The design, its protocol and its failure table are owned by [pass A](operator-console-ux-pass2-session.md).

**Sign in once per browser.** The console opens on a sign-in screen. Paste `PHOTO_WALL_ADMIN_TOKEN` and press **Sign in**. Central sets an `HttpOnly` session cookie that lasts 30 days from sign-in; reloads and new tabs in that browser stay signed in. The token is never stored in the browser. The cookie holds only an expiry, the address you signed in at and a signature. When the 30 days end, the next refresh (within 5 s) shows the sign-in screen again.

**Log out** (in the header) ends the sign-in in this browser only. A cookie copied from this browser keeps working until it expires. To sign out every browser, change the token (below).

**Changing the admin token.** Change `PHOTO_WALL_ADMIN_TOKEN` in `.env` (or the deployment secret) and restart every Central process.
- Every browser is signed out within 5 s and must sign in with the new token. This is the only "log out everywhere".
- Every script still using the old token gets 401 until it is given the new one: the `curl` examples in this runbook, `scripts/demo_wall.py` (`DEMO_ADMIN_TOKEN`) and the netboot end-to-end harness (`scripts/test_netboot_e2e.py`).
- Players are not affected; they authenticate with their own enrollment credentials.

**Scripts keep using the bearer header.** `curl`, the demo and the test harnesses send `Authorization: Bearer <admin-token>` and need no cookie, marker header or `Origin`. A Bearer header decides alone: a wrong one gets 401 even if the request also carries a valid cookie.

**403 on a write.** The console shows one alert: "Central refused this write because it did not come from the page you signed in on." Reads still work. The response's `error` says why:

| `error` | Means | Do |
|---|---|---|
| `request_unmarked` | The write lacked the console's `X-Photo-Wall-Console` header. The console always sends it, so a proxy in between is stripping it, or the request did not come from the console. | Check the proxy; scripts should use the bearer header. |
| `origin_mismatch` | The write came from a different address than the one you signed in at (for example the IP instead of the name, or https after signing in over http), or had no `Origin`. | Reload the console from the address you signed in at, or sign in again at the address you are using. |

**Cookie names.** Over https the cookie is `__Secure-photo_wall_session`, marked `Secure`, so an http page cannot plant or overwrite it. Over http it is `photo_wall_session`. Both are `SameSite=Strict` and have no `Domain`. Legacy cookies from the first build are cleared on sign-in and Log out.

**Why the cookie is scoped to `/v1/operator/`.** Browsers send a host's cookies to every port on that host. If the NAS also runs, for example, a photo library on another port, a cookie scoped to `/` would be sent to that library with every request. Scoping the cookie to `/v1/operator/` means the browser sends it only with the console's API requests. This limits exposure; it is not an isolation boundary.

**Costs you should know.**
- **No rate limit on sign-in**, as for the bearer header. A token generated by `scripts/configure.py` is 256 bits.
- **No HSTS.** Central does not pin the host to https, so a first visit over http can be downgraded. Prefer https; over http the cookie also crosses the LAN in clear text.
- **A forced sign-out is possible.** Another site under the same domain, or another port on the same host, can plant a cookie of the same name. Without the token it can only make you sign in again; it cannot sign in as you.

## Operator console: refresh, the snapshot-age clock, and the guidance banner

The console has **no push channel** — the player WebSocket is player-only — so every region reads from **one timestamped snapshot**: a single atomic read of `/v1/operator/inventory` + `/v1/operator/runtime` (the design calls this **Plane A**; see [§9](operator-console-ux-design.md#9-storage-lifecycle--refresh) and [§4a](operator-console-ux-design.md#4a-the-two-plane-state-model) of the console design). The wall plan, the Equipment roster, the Frame Inspector, and the now-showing chips all render from that same snapshot, so a Frame's binding row and its now-showing chip always share one age. Between refreshes the console can be stale, and it never hides this.

**The snapshot-age clock.** The global bar always reads **"updated N s ago"** next to a **Refresh** control. The age is the honest time since the last successful snapshot, not a freshness guarantee — it tells you exactly how stale what you are looking at may be. Pressing **Refresh** re-fetches inventory and runtime together as one new snapshot and resets the clock. Because a stale read can never silently drive a wrong write, mutations still carry their concurrency tokens (`expected_generation`, `expected_revision`, an idempotent `activation_id`), so an action taken against a stale snapshot resolves to an explicit "the world moved" conflict rather than a silent wrong success.

**When the snapshot refreshes.** A new snapshot is fetched **every 5 seconds while the tab is visible** (the poll pauses while the tab is hidden and refreshes immediately when you return), **automatically after every mutation** (a bind, a placement, a commit, an activation), and **on an explicit Refresh**. A response that is older than one already shown, or that was read while one of your writes was in flight, is discarded rather than shown, so a poll never undoes what you just did ([pass 2 §7](operator-console-ux-pass2.md#7-polling-and-the-write-fence)). If a refresh fails, the bar reads **"updated N s ago — last refresh failed"**. Separately, the **Central pill polls `/healthz` about every 10 seconds** and reads "Central: ok", "Central: scheduler stale" (or another scheduler or database reason), or "Central: unreachable"; it reports Central's own health, not any Player and not observed presentation. **These interval numbers are tunable placeholders, not fixed guarantees** — the design states them as cadences to tune during operation ([§9](operator-console-ux-design.md#9-storage-lifecycle--refresh), assumption 4 in [§10](operator-console-ux-design.md#10-decisions-that-are-yours)), not gate decisions, so treat "5 s" and "~10 s" as approximate rather than contractual.

**A refresh never discards unsaved work.** The read snapshot is one plane; everything you are *doing* — an in-progress drag, a calibration draft you are "trying" before commit, the lease countdown, the current mode, and the guidance-dismissed flag — is a **separate** plane the design calls **Plane B**. A Refresh replaces the read snapshot **only**; it merges nothing into your draft and cannot reach into it. Concretely, **a calibration draft in progress is not lost by a refresh** (the corners and crop you have dragged stay put); if the refresh reveals that the committed state moved underneath you — the Frame's `revision` or `generation` advanced — the console raises a "committed changed underneath you" conflict and lets you decide, rather than throwing your draft away. This two-plane separation is enforced structurally by the React state model (design rule R3), not by convention.

**First-run guidance banner.** A **non-blocking, dismissible** banner carries first-run onboarding (design Q8: always-visible inventory plus a guidance banner, never a modal wizard that gates the console — see [§10](operator-console-ux-design.md#10-decisions-that-are-yours)). It never blocks a control: the full inventory is visible behind it and you can act before dismissing it. **Dismissing it is per-session** and, because the dismissed flag lives in the draft plane (Plane B), the dismissal **survives a snapshot refresh** — a Refresh does not bring the banner back. Its guidance is only as fresh as the last snapshot (there is no push), which is the stated cost of preferring a non-blocking banner over a linear wizard.

## Operator console: wall health and the attention strip

Every Frame tile, the Frame Inspector header, the Unplaced tray and the Showrunner frame list show one **health** label for each Frame, so the same Frame never reads differently in two places; the [Equipment roster](#the-equipment-roster) shows each Player's "Last heard" or "Enrolled" line from the same source. The plan tile shows a short form; the full label, with its age, is in the Inspector header and in the tile's accessible name. The states, their order and their wording are owned by the [pass 2 design, §4](operator-console-ux-pass2.md#4-per-frame-health-one-closed-set-one-classifier).

**What the labels are based on.** A label reports **Central's record of the last readiness report it accepted from the Player**, aged on Central's clock at the moment of the snapshot. It is **not** a live video readback, and nothing on the console says "LIVE", "online" or "connected". A Frame that reads healthy may still be dark (a failed panel, a failed decode); the console cannot confirm lit pixels. The now-showing chip is Central's **intent** for the Frame, never confirmed playback. A healthy Player reports about twice a second.

| Label | What it means | What to do |
|---|---|---|
| **Last heard N s ago** | Central accepted a report from the bound Player within the silence threshold, and nothing else is wrong. This is the healthy state. | Nothing. |
| **Player silent · last heard N min ago** (alarm) | The Player has not had a report accepted for longer than the silence threshold. The Pi may be off, off the network, crashed, or talking to Central but having its reports refused. | Check power and network at the Frame. If the Central pill is not ok, fix Central first (below). If it stays silent after the Pi is back, restart the Player. |
| **Enrolled N s ago, no report yet** | The Player enrolled (its process started) but Central has not yet accepted a report from it. A to-do for up to the threshold after enrolling, then an **alarm**. A Player that restarts in a loop keeps resetting this age and may stay a to-do. | Wait a few seconds after a boot. If it turns into an alarm, or you see the enrolled age keep resetting, check the Player's logs for a crash loop. |
| **No display detected when the Player started** (alarm) | When the Player last started, it reported that no panel was connected on the bound output. | Check the panel's power and cable, then restart the Player so it re-detects the display. |
| **Needs commissioning** (to-do) | The Frame's calibration is not valid, so nothing is shown on it. This is normal for a new or newly bound Frame. | Open the Frame's [Commissioning facet](#operator-console-commissioning-calibration-and-conflict-states) and commit a calibration. |
| **Needs a Player** (to-do) | No Player output is bound to the Frame. | Open its [Binding facet](#operator-console-onboarding-binding-and-auto-recovery) and choose a free output. |

**The silence threshold is about 31.5 s.** Central serves it with the inventory; its single source is `contracts/liveness.py`, shared with the Player's own retry timing. It is long enough that one failed Player cycle against a responsive Central does not raise a false alarm, so a powered-off Pi reads "Player silent" within about 31.5 s plus one 5 s poll. A Central that is answering slowly (near its 15 s request timeout) can make a live Player briefly read silent. The derivation and its costs are in [pass 2 §2](operator-console-ux-pass2.md#2-the-liveness-signal).

**Display hot-plug is not detected.** Display detection is reported only when the Player starts. Unplugging or plugging in a panel afterwards changes nothing on the console until the Player restarts; this is a known gap, deferred ([pass 2 §12](operator-console-ux-pass2.md#12-costs-deferrals-and-questions)).

**The attention strip.** A one-line strip under the status bar counts the Frames that need you, for example "2 frames need attention · 3 to set up", or "All 6 frames heard from". Alarms count as needing attention and to-dos as to set up; a Player that enrolled within the last second is not yet counted. **Show frames** opens a list (alarms first, then to-dos, up to 8 entries and "and M more"). In Wall mode each entry is a button: it switches to the Frame's Surface, selects it, and opens the Inspector on the facet where the cause is shown (Binding for Player problems, Commissioning for display and calibration). In Showrunner mode the entries are text only, so reading them never abandons show work.

**When Central's scheduler is not ok.** If the Central pill reads something like "Central: scheduler stale", the strip replaces the silent-Player entries with one line, for example "5 frames silent — Central's scheduler is stale; Players may be unable to report until it recovers." Fix Central (its logs and `/healthz`) before visiting the Pis. Entries with another cause, such as no display detected, stay listed.

## Operator console: placing, moving, and deleting Frames (and the Unplaced tray)

In the redesigned console (served at `/` since the cutover, aliased at `/console`) the wall plan is the home: each Surface is a flat millimetre plan and its Frames are drawn as rectangles from their `x_mm/y_mm/width_mm/height_mm`. You build and edit that plan by direct manipulation — the console turns each gesture into one of the operator routes documented above ([reposition and remove Frames](#operator-api-reposition-and-remove-frames)) or the existing `POST /v1/operator/frames`. Selecting a Surface *filters* the plan to that Surface's Frames; a Surface is a bare text label, not something you act on. This mirrors the "reading & building the wall" walkthrough (J3) in the [console design](operator-console-ux-design.md).

**Place a new Frame.** Drag a rectangle on the empty plan, then enter the Frame's **id** and **display profile** — pixel width/height and diagonal (plus whether it is video-capable). The console sends one `POST /v1/operator/frames` carrying the dragged `surface_id`, `x_mm`, `y_mm`, `width_mm`, `height_mm` and that profile, so the Frame is **created at the position you drew**. It **never lands in the Unplaced tray** — only Frames with no distinct geometry do that (below). Central runs the same orientation-coherence guard as every Frame creation: if the aperture orientation would disagree with the profile (a portrait matte declared against a landscape panel, or vice versa) the create is refused and nothing is added. The profile values are **Frame facts** — operator-declared and persisting across a later panel swap — not live display readback.

**Frame ids are readable and permanent.** You name the Frame, for example `lobby-left`; every later screen shows that id, and it cannot be changed. The form checks it as you type: letters, digits, `-`, `_` or `.`, starting with a letter or digit, at most 96 characters, and no `:`. This is the same rule a Scene uses to target a Frame, defined once in `contracts/models.py` (`TARGET_ID_PATTERN`) and enforced by Central on create, so an id Central accepts can always be targeted ([slice 2 §8](operator-console-ux-pass2-onboarding.md#8-readable-frame-ids-one-rule-in-contracts)). An id already in use reads "A frame named lobby-left already exists." Frames created before the rule keep their ids and stay readable and deletable. There is no separate display name yet. **Replacing a panel with a different size or resolution:** the profile cannot be edited, so unbind and delete the Frame, then recreate it with the same id and the new profile; a recreated Frame starts uncommissioned.

**Move a Frame.** Drag an existing rectangle to reposition it. The console rides `PATCH /v1/operator/frames/{frame_id}` sending only `surface_id`, `x_mm`, and `y_mm`, so **a move never resizes** — `width_mm` and `height_mm` keep their stored values. Placement is **last-write-wins with no lock**: a concurrent move of the same Frame by another operator simply wins, your losing drag is overwritten, and the plan corrects itself on the next snapshot refresh. Because geometry is operator-only metadata — it never reaches a Player and is independent of the calibration corners (normalized to `[0,1]`) — a move **carries no concurrency token and never invalidates calibration or bumps `generation`**. This is the one deliberate exception to the console's "every write carries its token" rule; a mis-drag costs only a re-drag.

**Delete a Frame.** Deleting asks you to confirm in a [dialog](#confirmation-dialogs), then rides `DELETE /v1/operator/frames/{frame_id}`, and only a clear Frame is removed. It is **refused while the Frame is bound** — the message reads "unbind it before deleting" (409 `frame_bound`; unbind from the Frame's [Binding facet](#operator-console-onboarding-binding-and-auto-recovery)) — and **refused while a live Run targets it**, reading "finish or cancel the Run before deleting" (409 `frame_in_use`). With neither guard tripped the Frame is removed from the plan. The guards protect one invariant: you cannot delete a Frame a Player is currently bound to serve.

**The Unplaced tray.** Frames created by the old UI all sit at `wall`/(0,0) with no distinct position; rather than pile them at the origin as permanent clutter, the console collects such geometry-less Frames in an **Unplaced tray** beside the plan, as a list. From the tray you can **drag a Frame onto the plan** to give it a position — that sends the same `PATCH` as a move, and the Frame **then leaves the tray** and joins the plan — or **delete it** under the same two guards above. Either way it stops being permanent clutter.

A freshly placed or moved Frame **settles to its final position on the next snapshot refresh**: the plan re-fits per Surface (it scales each Surface's millimetre space to the viewport), so a rectangle drawn or dragged optimistically can shift slightly once the authoritative snapshot returns, while its stored millimetre geometry is exactly what you entered.

## Operator console: onboarding, binding, and auto-recovery

New hardware appears in the console before it does any work. Following [decision 0006](decisions/0006-central-authority-and-stateless-players.md), Central is the source of truth and a Player is a replaceable box behind a Frame. The design of this flow, and its costs, are owned by [pass 2, slice 2](operator-console-ux-pass2-onboarding.md).

### The Equipment roster

The **Equipment** region lists every Player in three groups, read from the same `/v1/operator/inventory` snapshot as the plan, so a new Pi appears the moment it enrolls:

- **Pending**: no Output is bound yet. A new Pi, or one whose Frames were all unbound, lands here.
- **In service** (headed "Bound players"): at least one Output is bound.
- **Retired**: permanently out of service. This group is collapsed by default.

Each group heading is a toggle showing its count, and its open or closed state survives a refresh. Players are listed in enrolment order. Each Player card shows:

- **Reported serial.** The serial the Pi sent when it last netbooted. It is a claim, not proof: the console cannot confirm which physical box sent it.
- **Boot outcome.** The last netboot result for that serial: healthy on a tag, pending (served a tag, not yet healthy), or failed and rolled back. "No netboot record" means the Pi never netbooted (for example a flashed card); its short handle then comes from the Player id and proves nothing physical.
- **Each Output's state**: "Shows frame …" (bound), "Free", "No display detected at last Player start", or "Retired with its Player". A Player that reported no Outputs reads "No outputs reported".
- **Liveness**: the same "Last heard" or "Enrolled" line as [wall health](#operator-console-wall-health-and-the-attention-strip).

The serial and boot outcome come from a separate, optional read of `GET /v1/operator/netboot`, refreshed at most every 30 s or when the set of Players changes. If that read fails, the roster says **"Boot records unavailable"** and keeps the last serials it knew; you stay signed in and everything else works.

### Bind one Pi at a time

**Power on and bind one Pi at a time.** Connect and power the panel first, then power on the Pi, wait for it to appear under Pending, and bind it before powering on the next. Two Pis on the roster look alike apart from their reported serial and enrolment order, and the console has no "identify this screen" flash yet (deferred), so this ordering is how you tell them apart.

**Outputs with no display are not offered.** Displays are detected only when the Player starts. An Output whose panel was off or unplugged at that moment reads "No display detected at last Player start" and cannot be bound. Connect and power the display, then restart the Player; the Output becomes Free.

You can bind from either end. Both write the same Frame binding, carry the Frame's `generation` token, and are refused rather than applied when something changed:

- **From a Frame.** Select the Frame, open its **Binding** facet, and choose a Player's Output from "Choose an output". Nothing is pre-selected, even when there is one option. Then press "Bind to <frame>". If a refresh removes the Output you chose, the choice is cleared and announced. If the bind is refused ("That output was just bound elsewhere", "That Player is no longer available", or "This Frame changed — reload and review its binding."), your choice is cleared; choose again from the current list.
- **From a free Output.** On the roster, a Free Output offers a "Bind to a frame…" list: pick an unbound Frame and press Bind. On success the console opens that Frame's Binding facet.

**After a bind, the display needs re-commissioning.** A successful bind or unbind bumps the Frame's `generation`, clears any preview, and **invalidates calibration**, so the Binding facet shows **"Review required"** with a **"Commission the display"** button that opens the [Commissioning facet](#operator-console-commissioning-calibration-and-conflict-states). This is a forced re-validation, not data loss: bind and unbind set `calibration_valid=false` but never write the `calibration` column, so the committed calibration persists and is available to re-confirm. Only a fresh **commit** overwrites it. Retire changes no binding and no calibration. If the Frame changed underneath you (a concurrent bind or unbind bumped its `generation`), the write is refused with **409 `binding_generation_conflict`** and nothing is applied.

### Confirmation dialogs

Unbind, **Unbind all outputs**, Delete frame and **Retire** each open a confirmation dialog. It names the target, says what will happen, and records what you saw when you opened it; the write uses exactly that, never a later refresh.

- **Unbind** names the Frame and its live Runs. The Frame stops being served, its calibration is kept but marked invalid, and the Frame on the same Player's other Output is re-planned too.
- **Unbind all outputs** (in-service Players) unbinds each Frame in turn and ends with "K of N unbound" and a result for each Frame. A Frame that changed or was already unbound is skipped and the rest continue; an unknown outcome stops the sequence, and the remaining Frames are marked not attempted. Nothing is resent.
- **Delete frame** lists live Runs; the delete is refused until they finish.
- **Retire** requires typing the Player's short handle (the last six characters of its reported serial, or of its id when there is no boot record; case does not matter). It is the only dialog that asks you to type.

While a write is in flight, Esc and Cancel do nothing. Then the dialog shows one of these results:

| Result | What it means | What to do |
|---|---|---|
| Done | Central applied the write. The dialog closes and a status line confirms it. | Nothing. A "last refresh failed" note means only the refresh after the write failed; the write stands. |
| Refused, with a reason | Central refused the write and changed nothing. | Read the reason, fix it, and confirm again, or cancel. |
| "Changed since you opened this. Reopen to review." | Someone changed the Frame after you opened the dialog. Nothing was written. | Close, reopen, and review the current state. |
| "Already done." | The Frame was already unbound or deleted. | Nothing. |
| "Central did not answer. Check this after the next refresh." (outcome unknown) | The request timed out, the network failed, or Central answered with a server error (5xx). The write may or may not have been applied. | Wait for the next refresh and check before trying again. |

### Retire, replace, and revoke

**Retire only a Player you will never use again.** Retire is offered only on Pending Players, and Central refuses to retire a Player with any bound Output (409 `player_bound`). It has **no undo, even after re-imaging**: the Player id comes from the hardware serial, and a retired serial is refused at enrollment. A retired Player's Outputs leave the bindable set.

**To replace a Pi, unbind it; do not retire it.** Unbind the old Pi's Frames (or use **Unbind all outputs**), then bind the new Pi's Output to each Frame. The old Pi returns to Pending and can be bound again later. **Unbind** (`DELETE /v1/operator/frames/{frame_id}/binding`) changes no Player record.

**To revoke a compromised bound Pi, unbind it, then retire it immediately.** Unbind alone leaves its session token valid and its serial free to enroll. Retire (`POST /v1/operator/players/{player_id}/retire`) revokes its authority and refuses its serial from then on. Its netboot device row is **not** retired: its last healthy tag still counts toward the release frontier and its serial still netboots ([base-image selection](#base-image-auto-mirror-0012); owner Question 4 of the [slice 2 design](operator-console-ux-pass2-onboarding.md#12-costs-deferrals-and-questions) is pending).

### Auto-recovery banner

**Returning known Pi.** When a Pi central already knows reboots, it re-enrolls by hardware serial and central self-heals it straight back to its old bindings — so it reappears already bound (`is_bound=true`) instead of dropping into the Pending group. The console marks this with a one-line banner: **"Recovered — already bound (serial match, not identity)."** Read that wording literally: recovery is a **serial-match convenience, not cryptographic identity** — the serial is not a secret and central does not prove the box is the same box, only that it presents the same serial. The banner is **suppressed on a true first run** (there is no discrete "recovered" flag in the payload; the console infers recovery by diffing the retained prior snapshot's `authority_epoch`, and with no prior snapshot it does not guess). The per-boot **`authority_epoch` bump is never surfaced as an alert on its own** — every boot mints a fresh epoch, so on its own it is normal, not an incident; it only participates in inferring recovery.

## Operator console: commissioning, calibration, and conflict states

In the redesigned console — now served at `/` (aliased at `/console`) since the cutover retired the old flat page — select a Frame in the wall plan to open the Frame Inspector, then open its **Commissioning** facet — the layer where you set up the display behind a Frame. It is reachable **only in Wall mode**; calibration is a hardware concern deliberately hidden from show programming, which sees only a Frame-health badge. Everything below rides the existing admin-authenticated `POST /v1/operator/frames/{frame_id}/calibration` route — no new endpoint, no schema change, no migration.

**What the facet shows (read-only, T0).** Four honest readouts, none of them a control:

- **Committed calibration** — the SDR gain, rotation, corners, and crop currently in force on the panel.
- **Frame facts** (from `FrameProfile`) — pixel width/height, diagonal, and video-capable. These are **operator-declared at Frame creation and persist across a panel swap**, so they are labelled *Frame facts*, not live display facts.
- **Display at last Player start** (from `OutputReport`) — whether a display was **Detected** or **Not detected** on the bound Output, and its reported resolution, as the Player reported them when it last started. This is **not live**: a display plugged or unplugged later does not change it until the Player restarts ([wall health](#operator-console-wall-health-and-the-attention-strip)). Nothing richer (EDID, model, refresh rate, HDR, active-area, bezel, overscan) exists anywhere in the system.
- **Bound equipment** — which Player/Output currently serves the Frame.

The **panel color correction** and **display power / parameters** areas render **"not yet available."** They are capability-gated and no wired path enables them today: panel color is a T1 concern and display power/CEC is a T2 cross-layer epic. No control on the facet implies a stored field that does not exist.

**Calibrate by direct manipulation.** In the *Adjust calibration* editor, drag the four corner handles and the two crop handles, or type the coordinates directly; SDR gain and rotation are separate draft controls. Every edit stays a **local draft** that a background inventory refresh never overwrites. Each geometry change is validated by the **same convex test the server enforces** (the `1e-6` epsilon and clockwise winding): a folded or too-thin quad snaps the handle back with the inline message **"corners must form a convex aperture"**, and an empty crop snaps back with **"crop must describe a nonempty rectangle"** — and **no request is sent**. A rejected drag changes nothing, on the client or the server.

**Preview → 30-second lease → commit / revert.** **Preview** pushes the draft to the panel under a server lease that expires in **30 seconds**; the facet shows the *server's* countdown ("Previewing on the panel — lease expires in Ns"). There is **no auto-renew**. If you do nothing, the lease lapses, the panel returns to its committed calibration, and the facet says so — **"Panel is back on committed. Re-preview to keep trying."** — while keeping your "trying" values so **Re-preview** re-pushes them in one click without re-entering anything. **Commit** saves the draft as a new calibration revision; **Revert** drops the preview and returns the panel to committed immediately. Preview and Commit require a bound Frame.

**One shared preview slot, last-writer-wins.** There is a **single preview slot per Frame and no lock** — the console never implies you have exclusive control of the panel. A second tab or operator who previews or commits the same Frame overtakes you. The console's 5 s snapshot poll surfaces these changes as explicit states rather than a silent overwrite:

| What happened | What you see | What to do |
|---|---|---|
| Your lease expired with no interaction | "Panel is back on committed. Re-preview to keep trying." | Re-preview to keep adjusting; your trying values are retained. |
| Another tab/operator committed or re-previewed the same Frame (the single slot was taken) | "Committed elsewhere / your preview was superseded — re-review." | Reload the fresh state and review before writing again. |
| Commit refused — another commit advanced the calibration revision (stale `expected_revision`) | "Another session changed this frame's calibration — reload and re-review." | Reload and re-review; your stale commit was **refused**, not silently applied. |
| Commit refused — the Frame's binding changed via bind or unbind (stale `expected_generation`) | "This Frame's binding changed — its display is no longer under your control; reload." | The display is no longer yours to calibrate; reload and re-check the binding. |

Every write carries **both** concurrency tokens (`expected_revision` and `expected_generation`) against the baseline captured when you opened the facet, so a stale commit is refused with a **409** rather than silently overwriting state you never reviewed. (A preview or commit against a Frame whose binding was removed underneath you is refused with "This Frame is no longer bound to a display — bind it before calibrating.")

## Operator console: Showrunner (running the show)

The console has two modes, switched by a top-level **Wall / Showrunner** toggle. This is **organization, not permission** — there is one shared admin token and no roles (design D-c/Q5 in the [console design](operator-console-ux-design.md)), so the toggle only changes *what you are looking at*, never *what you are allowed to do*. **Wall mode** is the plan, the Frame Inspector, and hardware **Commissioning**; **Showrunner mode** is the content-and-schedule layer: Sources, Scenes, Programs, and Runs.

**Showrunner never shows Commissioning (R4).** At show time the Commissioning facet is unreachable — hardware setup (calibration geometry, SDR gain, and the gated color/power areas) lives only in Wall mode. The **only** hardware facts the show layer sees are each Frame's **health badge** — the same label the wall shows ([wall health](#operator-console-wall-health-and-the-attention-strip)), including "Needs commissioning" when `calibration_valid` is false: an invalid Frame cannot present, so the showrunner must see that it is not presentable. The badge is **status, not a control** — you read it in Showrunner but you fix it in Wall mode's Commissioning facet.

The Showrunner lays out in two columns on a wide screen: **Now** (Runs and the "why" panel) and **Library** (Scenes, Programs and Sources); below 1024 px it is one column, Runs first. The wording and states below are owned by the [slice 3 design](operator-console-ux-pass2-showrunner.md); this section is how to use them.

### Names and ids

You **name** Scenes and Programs; the console derives the id Central stores. Accents are dropped, letters are lowercased, every other run of characters becomes `-`, and the id is cut at 96 characters: "Family Evening" is shown as "Saved as `family-evening` · Change". Central keeps only the id (there is no stored display name), so tiles, rows and Runs show ids.

Type an id yourself, with **Change** (it reveals an **Id** field), when:
- the name has no Latin letter or digit — the Id field opens by itself with "This name needs a Latin letter or digit for its id; type an id."; or
- you want a particular id. An id starts with a letter or digit, then letters, digits, `-`, `_`, `.` or `:`, up to 128 characters.

A name whose id already exists is refused before anything is sent ("A Scene called `family-evening` already exists; choose another name."). A successful save clears the form. The check uses the last refresh, so two operators can still pick the same new id within a few seconds of each other. For a **Scene**, Central refuses the later save and the form reads "A Scene with this id was saved meanwhile; nothing was replaced." Choose another name, or open the stored Scene and Edit it. For a **Program**, the later save still replaces the earlier one (Program saves have no guard; design Question 4). Activation ids are never shown.

### Every disabled control says why

Forms never disable their button over a problem. A field shows its reason once you have edited it. Pressing the button with problems sends nothing: a summary appears at the top of the form (it stays as it was when you pressed, even as the page refreshes) and focus moves to the first field with a problem. Only a write in flight disables a button. **Finish** on a Run that is already finishing is disabled, and the row's "Finishing: requested 20 s ago" says why.

Target frames are grouped **"Frames on `<surface>`"** and **"Frames not on any wall"**, each with its [health label](#operator-console-wall-health-and-the-attention-strip). A legacy frame id containing `:` (or longer than 96 characters) is listed with the reason no Scene can target it, and cannot be ticked. If a frame you ticked is deleted while you are drafting, it is dropped and announced ("lobby-left was deleted and removed from this Scene.").

### Sources

A Source is a **saved live query** named `name:rev` (e.g. `holiday:1`) — never a downloaded album and never something a Player browses or opens; it is live eligibility re-evaluated centrally. The Sources region lists each Source by its `name:rev` identity and status, with a **Refresh** control per Source that re-runs its saved query (`POST /v1/operator/sources/{ref}/refresh`). A newly created Source reads **"Awaiting refresh"** until its query is first re-run, then shows its refresh time.

To **create a Source**, fill **Source name and revision** (like `holiday:1`), the private worker **Connection name**, and **Media type** (images, video, or both); it saves with `PUT /v1/operator/sources/{ref}` (the reference is path-encoded because it contains a colon). Two optional filters narrow it:
- **Favourites:** Any, Only favourites, or Not favourites.
- **Capture window:** **Taken from** and **Taken until** are local dates. The window runs from the start of the "from" day **up to the start of** the "until" day (the "until" day itself is excluded), so "Taken until" must be after "Taken from".

**Albums are not supported:** a Source has no album filter. There is deliberately **no** album, "open in Immich," or credential field anywhere in this form (the Immich boundary, design decision D-e in the [console design](operator-console-ux-design.md)); the API key is provisioned into the worker out of band (see [Connecting a real media library, in the README](../README.md#connect-a-real-media-library-immich)).

### Scenes and "Keep playing until the Program ends"

A Scene is a per-target composition. You author it either against a **live source** (a changing collection whose membership is re-checked centrally) or as **per-Frame authored** choices, where each participating Frame gets a chooser listing **only media compatible with that Frame's profile** — the candidate list is hard-filtered by profile server-side (`GET /v1/operator/sources/{ref}/candidates?frame_id=`), so an incompatible asset cannot be chosen. The Scene and all its per-Frame references **save together in one request** (`PUT /v1/operator/scenes/{id}/authored`); source freshness, membership, and compatibility are re-checked centrally on save. While candidates load, the chooser reads "Loading compatible media…". Each choice is labelled with its kind, size, capture time and what Central would do with it on that frame, for example "Photo 108×192 · taken 3 Mar 2025 14:02 · ready" (or "preparing", "failed to prepare", "no compatible version"); "(2)" is added only when two labels would otherwise read the same.

Each Scene has **Seconds per cycle** and **Keep playing until the Program ends**, which is **on by default** for new Scenes (the design's default for its Question 1, pending owner confirmation):
- **On.** In a Program, the Run keeps cycling until the window ends, then stops at the end of the cycle running at that moment, so it can **overrun the window by up to one cycle**. Activated without a Program, it plays until you Finish or Cancel it.
- **Off.** The Run plays **one cycle, then ends**: a 30 s Scene in an 18:00–20:00 Program ends at 18:00:30. Its Run row reads "plays one 30 s cycle, then ends". Scenes saved by earlier console versions were always saved this way.

### Viewing and editing a Scene

The Scenes region lists every stored Scene as a closed disclosure named `Scene X`. Open it to read what feeds it ("live from `family:1`", or "authored: 3 chosen items"), its frames with their health, its cycle ("30 s per cycle, keeps playing until its Program ends or, when started by hand, until you Finish or Cancel it", or "plays one 30 s cycle, then ends"), its **revision**, the Programs that use it, and whether a Run of it is running now. There is no Delete (design Question 3).

**Edit** is offered only when the console can save the Scene back **without losing anything**. A Scene written through the API with features the form cannot author (child Scenes, an outro, fades, and similar) shows "Edit unavailable: Uses features the console can't author (child Scenes, outro, fades…)." instead; change that Scene through the API, since a save from the form would silently drop those features.

To edit:
1. Press **Edit Scene X**. The form fills with the stored Scene and reads "Editing `evening` · revision 4. Its id stays; Replace saves revision 5." The id is the **stored** one; there is no name field, and the id cannot change. To make a Scene with a new id, stop editing and save a new one.
2. For an authored Scene, each frame's stored item is pre-selected while it is still in the Source. A frame whose item left the Source has no choice; pick again.
3. Press **Replace Scene**, then **Confirm replace** in the dialog. **Stop editing** leaves the form without saving.

**What Replace changes.** It stores the Scene as the next revision. **Runs already going keep the version they started with**, and so do activations already queued: each captured its Scene when Central admitted or queued it. Programs that start later, and new activations, use the new revision. Replace does not touch Programs, Sources or other Scenes.

| The dialog ends | Means | What to do |
|---|---|---|
| "Replaced Scene evening: now revision 5." | Stored. | Nothing. |
| "Changed since you opened this. Reopen to review." | Someone else replaced this Scene after you pressed Edit. Central refused yours (409 `scene_revision_conflict`), so **nothing was replaced** and their version stands. | Close, open the Scene again (it shows their revision), and redo your change if it still applies. |
| "Not replaced. The Source's last refresh failed; authored choices can be saved once it succeeds." | An authored Scene's Source is failing. | Fix the Source (see [the media pipeline](#the-media-pipeline)), Refresh it, then try again. |
| "Not replaced. That item is no longer in the Source; choose again." | A chosen item left the Source. The choosers reload. | Pick again and Replace. |
| "Central did not answer. Check this after the next refresh." | Central answered with a server error, so the save may or may not have been stored. | After the next refresh, open the Scene and read its revision: if it moved to yours, it was stored. |

Pressing Replace again with exactly the same Scene after an outcome you did not see is safe: Central accepts an identical save of the stored revision.

### The media pipeline

The **Media pipeline** panel sits in the Now column. Central fetches media from the photo library and prepares it; Players get it only from Central. All ages are on Central's clock.

**Worker.** One line:
- "checked in 40 s ago · preparing 3 · waiting 12 · failed 2 · failed, retry pending 1 · cache 4.1 of 8 GB" when it is healthy. Preparing is running or publishing; waiting is queued; "failed, retry pending" appears only when a failed job is waiting to be retried (planning treats it as failed until then). Only jobs of the **current preparation recipe** are counted; a recipe change fails the old recipe's queued jobs and planning asks for them again.
- "never checked in", "reported: storage is full" (or another reported error), or "quiet for 14 min" (no check-in for over 11 min; it checks in every 5 min) is an alarm, and a separate **Jobs and cache** line shows the counts. Check the worker process and its logs; for storage pressure, free space or raise the cache limit.

**Each Source** gets a row: its `name:rev`, a **State**, the **Last refresh** counts ("found 800 · valid 790 · pending 4 · rejected 6"), any **Reported** diagnostic codes in words, and when it next refreshes. The State carries the Source's **filters** (media types, favourites, capture window such as "taken 2024" or "taken 1 Mar 2025 to 31 Mar 2025"):

| State | Reads | What to do |
|---|---|---|
| Awaiting refresh (to-do) | "Awaiting refresh" | New Source; wait for its first refresh, or press Refresh in the Sources region. |
| Failing (alarm) | "Library unreachable", "Library refused access" or "Library unsupported", then "· last good 2 h ago" | Unreachable: check the library host and network. Refused: check the worker's library key and its permissions. Unsupported: check the library version. Authored Scenes from this Source cannot be saved until it succeeds. |
| Overdue (alarm) | "Refresh overdue by 6 min" | Refreshes run every 30 s; check that the worker is running. |
| Nothing valid (to-do) | "nothing valid in the last refresh" | The query found no acceptable item: widen the filters, or read the Reported codes. |
| OK | "refreshed 1 min ago · 790 valid in the last refresh · only favourites · taken 2024" | Nothing. "Valid" counts items the refresh accepted, not items ready for a particular frame. |

### Why nothing new on a frame?

In the Runs region's **Why** panel, choose a **Frame for why**. Below Central's plan for that frame is a separate group, **"Why nothing new on lobby-left?"**. It walks from intent to equipment; each step restates a served fact, and the first step that is not ok is marked **Stops here**. Fix that one first.

| Step | Stops when | What to do at that stop |
|---|---|---|
| 1. Intended? | No Scene is intended for the frame now. (If a Run on the frame ended, this step is only informational and the chain stops at step 2.) | Activate a Scene, or schedule a Program, that targets the frame. |
| 2. Run ended? | The last Run on the frame ended ("evening's Run ended at 18:00:30 after one cycle") or was cancelled. If the Scene keeps its last still, it adds "if its last item was a photo, the frame keeps that still (a video is not kept)". | "After one cycle" means Keep playing was off: edit the Scene and turn it on, then start it again. |
| 3. Authored? | The winning Scene uses fixed, hand-picked media ("new photos never appear by design"), or shows black by design. | Nothing is wrong. To show new photos, use a live-source Scene. |
| 4. The Source | None of the Scene's Sources is ok. | Read the [Source states](#the-media-pipeline) above. |
| 5. Check this frame | Press **Check this frame**. It reads each ok Source's items that fit the frame's shape and counts what Central would do with them: "12 usable · 3 still preparing · 1 failed to prepare · 2 with no compatible version". It stops on "Nothing usable yet: …" or "No item in the Source fits lobby-left's shape." | Still preparing: wait for the worker. Failed to prepare: read the worker line. No compatible version, or nothing fits: the frame's shape (for example portrait) excludes the Source's items; widen the Source. An item two Sources share is counted once; a failing Source is left out, as planning leaves it out. |
| 6. The worker | The worker is not ok. | See the worker line above. |
| 7. Frame health | The frame's health is not ok. | Fix it in Wall mode ([wall health](#operator-console-wall-health-and-the-attention-strip)). |

**Limit:** the check is a count of each item's standing. It does not report which item Central picks for the next cycle; with some items usable and others not ready, a cycle that lands on one not ready plans the next layer down, as the step's note says.

### Programs and the windows helper

A Program binds a Scene to **one time window** with a **priority** (`PUT /v1/operator/programs/{id}`; `DELETE` removes it). Times are entered and shown in your browser's time zone, which the form names ("Times in Europe/London"); if the browser and the wall are in different zones, that label is the only warning. The form refuses a window that ends before it starts, a window that has already ended (Central would record it as missed), and a priority that is not a whole number.

For repeating shows, **Create separate windows**:
- **Repeat on** is a weekday mask; every day is ticked by default.
- **Number of windows** is 1 to 60. A number outside that range is a reason on the field, never silently reset.
- Window 1 is the window entered above. Each later window falls on the **next ticked day at the same local clock times**, so a daylight-saving change keeps 18:00 at 18:00.
- Each window is a **real, separately stored Program** `<id>-1`, `<id>-2`, …, which you manage and remove individually. There is **no stored recurrence rule**, and no control implies a living recurring schedule (design Q2).
- Before sending, the helper refuses if a window id already exists, if `<id>-<n>` would exceed 128 characters, or if windows would overlap ("Each window must end before the next starts." — for example, a window longer than the day spacing).
- If some windows fail, the status lists them as **not confirmed**: a request that failed or did not complete may still have been stored. Pressing the button again sends only the windows Central does not yet list.

The reasons beside the fields follow whichever action you last tried: scheduling one Program or adding separate windows. Removing a Program that is **running now** asks for confirmation: its Run is asked to finish at the end of its current cycle, after any outro, and later windows stay.

### Reading Program states

Each Program row shows one state, read from what Central served. Times are the Run's, never the window's.

| State | Reads | Means |
|---|---|---|
| Upcoming | "Starts in 2 h · Tue 2 Mar 18:00–20:00" | Its window has not started. |
| Running | "Running since 18:00" | Central admitted its Run and it is live. |
| Ran | "Ran 18:00–18:00:30 (one cycle, then ended)", or "Cancelled at 19:10" | Its Run ended. "(one cycle, then ended)" marks a Scene with Keep playing off. |
| Refused (alarm) | "Did not start: lobby-left was protected by the Run of evening." | Another Run protected one of its frames when the window started. The Run named is the one Central recorded as blocking it; if that Run is no longer listed, the row says "another Run, no longer listed". A Scene that protects a frame covered by a higher-priority Run reads "…it protects lobby-left, but a higher-priority Run of evening covered it." |
| Missed (to-do) | "Missed: its window had ended before Central first scheduled it." | Only two cases: the Program was saved after its window ended, or its window ended before Central's very first scheduler tick. |

**A warm restart is not a miss.** If Central was down during a window, it catches up logically when it comes back: it admits the Run and ends it as the plan would have. The row reads "Ran" even though the wall showed nothing, and the row's hint says so. "Ran" describes Central's plan, never what the panels showed. Past Programs sit under a closed "Past (N)" disclosure. Central serves Runs and outcomes for one day, so older rows read "details older than a day".

### Runs and Central's plan

Live Runs are listed with their child Scenes nested beneath. Each row shows `Scene X` and its revision, where it came from ("Program Y", "activated directly" or "part of Z"), when it started, whether it is Running, "Ending (outro)" or "Finishing", its priority, the frames it protects, and its frames with their health. **Finish** (`POST /v1/operator/runs/{id}/finish`) asks for a natural end. **Cancel** (`…/cancel`) asks for confirmation, then stops the Run now, skipping its outro; its child Scenes stop too. Ended Runs from the last day are under a closed "Recently ended (N)" list.

The **why** panel (and the Frame Inspector's Now-showing facet) states **Central's plan** for one frame, for example "Central's plan for lobby-left: evening (priority 5, Program weekday-evenings) on top." Each layer underneath gets one sentence, always with its **priority N**:
- a lower priority: "morning (priority 1) is underneath: evening has priority 5.";
- the same priority: the Run Central **admitted later** is on top. This is admission order, not the Program's start time; Programs starting at the same instant are admitted in Program-id order;
- the same Run: the later child Scene is on top.

**Its limits are always shown.** If the winner has no usable media for this frame (none eligible, still preparing, or no compatible variant), Central plans the next layer down instead. An unbound frame gets no layers at all. A partly transparent or fading layer shows what is underneath. The panel reports what Central intends; it never says a frame is LIVE or confirms what a panel displays (R2).

### Activating a Scene now

Choose the **Scene to activate** and an **Activation priority**, then choose what happens **If it is already running**:
- **Leave it running** (default): nothing changes; the outcome reads "Not started: evening is already running, left as is."
- **Restart it:** ends the current Run and starts a new one now. A restarted Run has **no Program end**. A Scene with Keep playing on plays until you Finish or Cancel it; one with Keep playing off plays one cycle, then ends.

**Activate now** (`POST /v1/operator/activations`) answers synchronously, and the console shows exactly that answer:

| Outcome | Reads | What to do |
|---|---|---|
| Admitted | "Started: Central admitted a Run of evening." | Nothing. It is Central's plan, not confirmation from the panels. |
| Refused by protection | "Not started: lobby-left is protected by the Run of evening." | The Run named is the one Central reported as blocking. Finish or cancel it, or wait until it ends. A Scene that itself protects a frame covered by a higher-priority Run reads "…but evening's Run (priority 5) covers it; use priority at least 5." |
| Other refusal | "Not started: 16 activations are already waiting.", or "Not started: `<error>`." | Correct the cause and try again. |
| Outcome unknown | "Outcome unknown. Try again; it will not start twice." | The request failed or Central answered with a server error, so the Run may or may not have started. **Try again unchanged**: the retry reuses the same hidden activation key, and Central answers a known key with its stored result, so it cannot start twice. **Changing the form makes this a new activation** with a new key, as the form reminds you. |

Queueing an activation and overriding protection ("force") are not offered: design bead 3B-3 is deferred until the owner answers its Question 6 ([slice 3 design](operator-console-ux-pass2-showrunner.md#18-costs-deferrals-and-questions)).

## Tests and local development

Install the free `uv` Python package manager, then:

```sh
uv sync --frozen
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
python3 scripts/check_docs.py
```

The portable command reports PostgreSQL integration tests as **skipped** unless `PHOTO_WALL_TEST_DATABASE_URL` is set. To run all tests against the local Compose database:

```sh
.venv/bin/python scripts/test_local.py -q
```

That wrapper reads local `.env` as data, never sources it as shell code. Each PostgreSQL test creates a random `pw_test_*` schema and removes only that schema. It preserves registry data in the deployment's public schema. A custom integration server may be supplied through `PHOTO_WALL_TEST_DATABASE_URL` with permission to create/drop test schemas. Keep it pointed at a development server.

CI installs the locked dependencies, lints, checks local documentation links, builds/launches Compose, runs the PostgreSQL suite, and checks central HTTP health. It separately runs all preparation tests inside the pinned Linux worker image, so missing host FFmpeg cannot silently remove that gate. Passing CI does not establish physical Pi/PXE, real Immich, rendering or visible timing.

All CI worker builds reuse the architecture-matched native media base through
the [shared dependency workflow](module-appliance-ci.md#shared-service-and-test-dependencies).
Application edits do not permit a missing OS dependency to be rebuilt. To
prepare an unchanged missing definition explicitly, dispatch `service-base.yml`
with its architecture and `prepare_base=true`. Fork runs require the definition to have
been published by a trusted run. Ordinary local Compose builds retain their
explicit cold native target.

The real-browser walkthroughs of the [binding/commissioning](../tests/browser/test_operator_binding_browser.py) and [showrunner content](../tests/browser/test_operator_showrunner_browser.py) surfaces drive the production React operator console (served at `/`, aliased at `/console`) and its HTTP API against their own temporary PostgreSQL schemas. Install the locked development dependencies and their matching Chromium build, then run:

```sh
uv sync --frozen
.venv/bin/python -m playwright install chromium
PHOTO_WALL_BROWSER_TESTS=1 .venv/bin/python scripts/test_local.py -q tests/browser \
  --browser chromium --tracing retain-on-failure --output artifacts/operator-browser
```

CI runs these checks in the official Playwright Python 1.62.0 Noble container,
pinned by digest in `checks.yml`. That image already contains Chromium and its
Linux dependencies, so CI does not run a browser APT installation. A separate
container environment installs the repository's frozen Python dependencies and
connects to the job's local PostgreSQL fixture. Reports and failure traces are
written to the existing artifact directory. Ordinary test runs skip browser
checks unless opted in. Playwright 1.62.0 and pytest-playwright 0.9.0 are pinned
in the development dependency group and `uv.lock`; neither enters production
services or the Player package. Update the image and locked Playwright version
together so the browser binaries match. See the official
[container contract](https://playwright.dev/python/docs/docker) and
[pytest runner](https://playwright.dev/python/docs/intro).

The registry walkthrough proves rejected/accepted authentication, reconnection after a rejected token, rejection of delayed failures from an earlier login attempt even when the same token is reused, two-Player/three-Output inventory, Frame creation/binding, calibration preview/revert/commit, stale-tab conflict recovery, replacement/retirement, and persistence through a fresh server/connection pool. It checks preview expiry using controlled time. The content walkthrough creates a Source, saves live and per-Frame authored Scenes, checks compatible prepared-photo choices and current selection guidance, schedules and removes Programs using the browser's local time zone, and starts, ignores, queues, naturally finishes, and cancels Runs. Definitions, Programs, and Run history survive a fresh server/connection pool.

All operator mutations use browser controls, and every page rejects uncaught JavaScript errors. The fixtures supply simulated equipment and generated public JPEGs through production source-refresh, acquisition-request, and publication transactions with an explicitly synthetic recipe and preparation metadata. Elapsed time advances through the production Runtime owner; natural finish waits for the current cycle boundary. The server binds an ephemeral loopback port, preserving the separate full demo's network isolation. These checks qualify operator controls and persistence. They do not run an upstream adapter, conversion worker, background scheduler, Player, or renderer, and do not qualify scheduled playback, PXE, or physical output. Run browser checks separately from the ordinary suite: synchronous Playwright owns an event loop for its session, while ordinary Player integration tests create their own loops.

The bounded schema-2 `operator-browser.json` report records named assertions, pass/failure status, browser version, PostgreSQL/fixture scope, generated-media and controlled-time inputs, checkout revision, dirty state, and GitHub event/SHA. CI always uploads available reports and retains traces only for failed tests. Pull-request runs identify the synthetic merge checkout. A dirty local run is diagnostic evidence, not final committed-revision acceptance. Reports and failure traces contain only the disposable fixture's public test token and synthetic records; keep unrelated deployment data out of the fixture.

## Recovery

```sh
docker compose restart central worker
docker compose logs --tail 100 central worker database
docker compose up -d --build --wait
```

Restart preserves the database volume. `docker compose down` stops this deployment without removing the volume. Back up PostgreSQL using `pg_dump` before migration or deployment changes; restoring production backups has not yet been qualified. Never use `down --volumes` on a deployment whose registry must be retained.

If an Output moves, bind the destination persistent Frame. Returning recognized equipment automatically receives its centrally stored binding after fresh enrollment. For replacement equipment, explicitly unbind the Frame from the old equipment Output and bind the new registered Output ([retire, replace, and revoke](#retire-replace-and-revoke)); observations alone never transfer operator intent. Frame geometry survives, generation increases, and playback requires revalidated calibration. Starting or reconnecting a Player rotates session credentials/authority without creating another Frame or changing desired geometry.

Preview carries a 30-second expiry and both proposed/committed settings in current process memory so the Executor can revert during a running-process outage. Commit and revert use optimistic revision and binding-generation checks. A stale browser must refresh before retrying. Partitioned equipment respects the bounded plan lease and rejects obsolete work when it obtains fresh session authority. Cold reboot requires central time/release/enrollment/control/media connectivity. A surviving cache file can avoid a media request only after the new process validates it against the current assignment; it cannot restore authority.

The [real Immich fixture](module-immich-fixture.md), [full media-path demo](module-wall-demo.md), [Player-only package builder](module-player-package.md), and [central release contract](module-appliance-release.md) provide commands and evidence boundaries. The [appliance builder/bootstrap](module-appliance-builder.md), [GitHub ARM image workflow](module-appliance-ci.md), and [headless image e2e gate](module-appliance-e2e.md) describe exact-artifact checks and their limits. Earlier signed image and hosted boot evidence remains useful for artifact identity and generic-VM behavior, but its durable-Player/local-update assumptions are superseded. Complete current-image native rendering, valid-cache reuse, corrupt-cache reacquisition, real automatic reboot/central rollback, and physical measurements remain pending until recorded against the final revision.
