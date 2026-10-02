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

The operator interface lists Players and Outputs, creates persistent Frames, binds equipment, retires a Player, and calibrates each Frame (Live calibration, then save). It creates and edits Sources and Scenes, schedules Programs, starts/finishes/cancels Runs, and shows source/worker health. Program timestamps use the browser's displayed local time zone. Configure the private upstream connection on the worker before creating a Source; after the worker checks in, the Source flow offers its configured connection name. The disposable browser walkthrough below covers these controls; final-revision delivery evidence remains separate. With the scheduler enabled, `/healthz` is green only when the database is reachable and a scheduler tick completed successfully within the last 10 monotonic seconds; `starting`, `coordination_unavailable`, `stale`, and `stopped` states return 503 with fixed sanitized status fields. Explicit test mode can disable the scheduler and retain database-only health semantics. A green `/healthz` reports service liveness, not observed presentation.

When a calibrated Frame shows a current readiness failure, follow the
Frame-specific guidance in its Frame Inspector: reduce concurrent video/effect work
or use lighter media for `capacity`; check time synchronization for `clock`;
check supported media or choose another item for `decode`; inspect Player
storage/network and Central delivery for `download`; inspect the Player cache
and Central delivery path for `integrity`. Unknown codes receive generic
diagnostic guidance. If the Player is silent, restore its Central connection
and wait for a fresh accepted report before acting on an older failure. These
reports describe preparation/readiness; neither they nor the enrollment
connected-Output observation confirm visible pixels. See the
[readiness projection contract](operator-console-ux-design.md#current-player-readiness-failures)
for the epoch, offer, binding and time filters.

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

`PHOTO_WALL_HORIZON_SECONDS` defaults to 300 seconds and `PHOTO_WALL_RENEWAL_SECONDS`, the plan renewal quantum (at most 60), to 30; the [wall demo](module-wall-demo.md) shortens both. In the scheduler's PostgreSQL transaction, the media repository records each bounded preparation request and defers its exact job ID through Procrastinate. The separate worker publishes verified derivatives into the `media` volume. Central mounts it read-only and serves exact authorized bytes; Players never receive an upstream URL or credential. Procrastinate owns dispatch, retry timing, and queue-worker liveness. Photo Wall schedules only domain preparation, source refresh, and publication/storage maintenance tasks; it retains publication recovery, reservations, and stale-attempt fencing. The worker has a read-only runtime, a private writable media volume, and container CPU/memory limits.

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

> **Deprecated lane.** This section describes the V1 boot path (the netboot base without a node offer and the promoted `.deb`). The supported configuration is [Node control](#node-control): Pis boot by node path and releases are published and selected in the console. The V1 lane is kept until the [V1 follow-up](player-fleet-implementation-map.md) removes it; the console shows none of it.

[Decision 0009](decisions/0009-minimal-base-and-app-package.md) is the adopted target for the netboot tier: a minimal base OS image that carries no application, plus the Player shipped as a downloadable `.deb` that central serves. Nothing is signed — the owner ruled a home LAN has no threat model, so the sha256 published alongside the `.deb` is a corruption check, not an authenticity proof. Once the boot-chain wiring below lands, the operator flow is:

1. **Stage the boot files in your TFTP tree.** From a published release's boot tarball, `photo-wall-boot-<revision>.tar.gz`, stage `photo-wall-boot/boot/` (kernel, DTBs, initramfs) beneath the boot-server root, as [staging the netboot bundle](#player-provisioning-stage-the-netboot-bundle-and-read-its-console-0014) describes — see [PXE service setup](module-pxe-service.md). It is the same tree as the base tarball's `photo-wall-base/boot/`, without the base squashfs, which Central serves over HTTP ([decision 0012](decisions/0012-netboot-base-auto-mirror.md)). The base carries no Player code and no deployment config; it exists to run the bootstrapper (`appliance/provision.py`) that fetches everything else. The kernel command line must name Central with `photowall.central=http://photo-wall.localdomain/` or your deployment's Central root (see step 2 of [decision 0014](decisions/0014-reaching-central-from-every-boot-stage.md)).
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

1. **Stage the kernel, `initrd.img` and DTBs together, from one release.** Download a published GitHub Release's boot tarball, `photo-wall-boot-<revision>.tar.gz` (automation reads its name from the release's `manifest.json`, `boot_image.filename`), check it against the release's `SHA256SUMS`, and take `photo-wall-boot/boot/` from it whole: `kernel_2712.img`, `initrd.img`, `bcm2712-rpi-5-b.dtb`, `overlays/`, `config.txt`, the `cmdline.txt` template, `pieeprom.upd` and `pieeprom.sig`. It is byte for byte the `photo-wall-base/boot/` of the same release's base tarball, `photo-wall-base-<revision>.tar.gz`, which still carries it, without that tarball's base squashfs: the release's seal refuses to publish the two with different trees. Never stage from a CI (Actions) artifact: one exists for every build, including unreleased pull requests, and expires after seven days, while a published release is complete by construction and is what Central mirrors the base squashfs from. `initrd.img` carries that build's stage-1 code, its CA list (copied from that build's base) and its clock floor, all in front of the cached initrd ([`tests/test_build_netboot_bundle.py`](../tests/test_build_netboot_bundle.py)). The kernel package is not pinned, so an initrd from one build must not be paired with a kernel from another. The CA list is as old as that build's Debian snapshot pin and stays that old until you stage again. Stage again when the gateway's certificate chain changes.
2. **Set the cmdline.** `cmdline.txt` is one line. Replace its one `@@PHOTOWALL_CENTRAL@@` placeholder (in `photowall.central=@@PHOTOWALL_CENTRAL@@`) with Central's root and change nothing else: the firmware passes the file to the kernel verbatim, so never add a line or a comment ([`contracts/release.py`](../contracts/release.py) declares the template; the release seal refuses any other shape). The root may be http: stage 1 follows the gateway's redirects to https across hosts (up to ten), refuses any https→http step, and verifies TLS ([`tests/test_uplink_tls.py`](../tests/test_uplink_tls.py)). On failure the console prints the named cause and the redirect hops, never a value to set, because the located origin can come from an unauthenticated first hop ([decision 0014, U7](decisions/0014-reaching-central-from-every-boot-stage.md#requirements-hard-rules)). Stage 1 no longer prints a `set photowall.central=…` note; on an http root it logs only `note: configured root is http: the first hop is unauthenticated`. Keep `watchdog.stop_on_reboot=0 hung_task_panic=1` beside `panic=10`. If either is missing, stage 1 boots on and names it (`note: kernel liveness missing: …`; [`tests/test_netboot_liveness.py`](../tests/test_netboot_liveness.py)).
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

> **Deprecated lane, shared configuration.** Promoting a `.deb` is the V1 boot path; the supported configuration is [Node control](#node-control), where releases are published and selected in the console. The worker's GitHub polling and its `PHOTO_WALL_RELEASE_*` settings below are shared: the same sync records node publications into the node release catalog that Fleet › Releases shows. The V1 promotion is kept until the [V1 follow-up](player-fleet-implementation-map.md) removes it.

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

> **Deprecated lane.** This section describes the V1 boot path (the netboot base without a node offer and the promoted `.deb`). The supported configuration is [Node control](#node-control): Pis boot by node path and releases are published and selected in the console. The V1 lane is kept until the [V1 follow-up](player-fleet-implementation-map.md) removes it; the console shows none of it.

[Decision 0012](decisions/0012-netboot-base-auto-mirror.md) extends the same discover-and-mirror model to the **base squashfs**, so you no longer hand-stage it into a served directory. The fleet is heterogeneous: central serves **several base images at once**, one per version some Pi needs, resolved **per device**. There is **no fleet default and no promote-the-base action** — rollout is emergent (see *pin a canary* below).

**Storage (the root of the old outage).** As of [decision 0013](decisions/0013-unified-cache-root.md) base bytes live at the derived **`<cache-root>/os-images/base-<tarball sha256>.squashfs`** (named by the sha256 of the release's base tarball, so a re-cut is a new file) under the single cache root (`PHOTO_WALL_CACHE_ROOT`, default `/var/cache/photo-wall`), **mounted RW on the worker and RO on central** — one cache PVC, no separate per-domain volume. The worker is the single writer, **asserts its cache is writable at boot** and fails loud (an ERROR log) if not, and self-heals a missing file at the serve seam (a read that finds no file publishes its fetch) — so a wiped or unmounted volume can no longer produce a silent, permanent `503`. On **NFS**, `flock` and `O_EXCL`/atomic-rename reliability across the mount is a documented precondition. Base serving is **always-on**; there is no "off" state. See the [Central cache subsystem](module-central-cache.md).

| Variable | Where | Default | Meaning |
|---|---|---|---|
| `PHOTO_WALL_CACHE_ROOT` | worker (RW) + central (RO) | `/var/cache/photo-wall` | The one cache root; `base-<tarball sha256>.squashfs` files live in its derived `os-images/` subdir. Optional (baked default); base serving is always-on |
| `PHOTO_WALL_PER_DEVICE_DEB` | Pi provisioner (`photo-wall-provision.service`) | (unset) | Opt-in: the Pi fetches the `.deb` of the exact tag its base was served this boot (`GET /v1/netboot/manifest`, serial-keyed) and posts base-health. Unset ⇒ unchanged 0010 global `.deb`, no base-health |

This setting must be present in the Pi's base-image provisioner environment
before it starts. Setting it only on the Kubernetes Central Deployment does not
change an already booted Pi or cause that Pi to report base-health.

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

## Operator API: reposition, edit profile and remove Frames

The operator console edits the wall plan through admin-authenticated routes on central (`Depends(admin)`, like every `/v1/operator/*` route). They change no schema and add no migration — placement and profile already exist on the `frames` row.

`PATCH /v1/operator/frames/{frame_id}` applies a **partial** placement. The body accepts `surface_id`, `x_mm`, `y_mm`, `width_mm` (> 0), and `height_mm` (> 0); any omitted field keeps its stored value — this is a merge, not a replace. Central re-runs the same orientation-coherence guard as Frame creation against the merged dimensions and the stored profile, returning **422** when the resulting aperture orientation would disagree with the display profile. An id that no longer exists returns **404 `unknown_frame`**; on success the response echoes the merged placement. Placement is **last-write-wins with no concurrency token** — two operators dragging the same Frame silently overwrite each other and the plan corrects on the next snapshot — because geometry is operator-only metadata: it never reaches a Player, it is independent of calibration (whose corners and crop are normalized to `[0,1]`), and a move is trivially re-dragged. The route therefore **does not bump `generation` or `configuration_revision` and never invalidates calibration**.

`PUT /v1/operator/frames/{frame_id}/profile` replaces the declared pixel dimensions, diagonal and video capability. Its body carries `profile` and the Frame's `expected_generation` (zero is valid for a new Frame). Central compares the generation inside the registry transaction, refuses a bound Frame with **409 `frame_bound`**, a live Run targeting it with **409 `frame_in_use`**, and an orientation mismatch with **422 `oriented_profile`**. A changed profile preserves the Frame id and measured placement, clears any calibration preview, invalidates committed calibration, and advances the calibration, binding generation and desired configuration revisions. The operator must bind the new Panel and calibrate the Frame again before relying on output readiness. Repeating an identical profile with the current generation is a no-op. A stale generation receives **409 `binding_generation_conflict`**; an unknown Frame receives **404 `unknown_frame`**.

`DELETE /v1/operator/frames/{frame_id}` removes a Frame only when it is clear and no saved future work still points at it. Existing **409 `frame_in_use`** refusal for a live Run and **409 `frame_bound`** refusal for a bound Output remain in force. Central also refuses a Frame referenced by saved Scene roots or queued activations; a future Program that uses a referenced Scene is included in the reference details. The `frame_referenced` refusal identifies the saved Scene, Program, and queued-activation IDs that block deletion. Edit or remove those saved references, or review the queued activations, then refresh and retry. An unknown id returns **404**. On success it deletes the Frame and returns **200 `{"status": "deleted"}`**.

## Operator console: signing in and Log out

The design, its protocol and its failure table are owned by [pass A](operator-console-ux-pass2-session.md); what happens to unsaved work is owned by [passes C and D §6](operator-console-ux-pass2-flow.md#6-navigation-routes-and-modules).

**Sign in once per browser.** With no session, the console opens on **Sign in to Photo Wall**. Paste `PHOTO_WALL_ADMIN_TOKEN` into **Operator token** and press **Sign in**. Central sets an `HttpOnly` session cookie that lasts 30 days from sign-in; reloads and new tabs in that browser stay signed in. The token is never stored in the browser. The cookie holds only an expiry, the address you signed in at and a signature. A console address you opened before signing in (for example `…/#/scenes`) is kept and shown once the first snapshot has loaded.

**When the session ends, your unsaved work is kept.** When the 30 days end or the token changes, the next refresh (within 5 s) signs the tab out, and the same sign-in dialog opens over the console. It reads "Signed out: the session expired or the token changed. Sign in again." and "Your unsaved work is kept until you sign in again." The console behind it is hidden and inert but kept as it was: the last snapshot and every unsaved draft (a Scene in progress, a Show now awaiting a retry) stay, and the refresh pauses. The dialog cannot be dismissed, and it sits above any confirmation that was open. Signing in closes it and puts focus back where it was. A write refused with 401 in the meantime says so; Show now reads "Not started: the session ended. Sign in again, then activate." and keeps its draft.
- *Cost:* until someone signs in again, the previous snapshot stays in the hidden page. It is not shown, but it can be read through the browser's developer tools, including after a token rotation. Log out (below) clears it.

**Log out** (in the header) ends the sign-in in this browser only. It also **discards every unsaved draft in this tab**, without asking, and clears the snapshot: the console starts empty behind the sign-in screen. Save or discard what you are doing first. A cookie copied from this browser keeps working until it expires. To sign out every browser, change the token (below).

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

## Operator console: sections, links and drafts

The console is one page: a header (Central's health, the snapshot age with **Refresh**, **Log out**, and the [attention strip](#operator-console-wall-health-and-the-attention-strip)) and a sidebar of sections. Under 850 px wide the sidebar is a drawer behind **Menu**. Each section, and each step of a flow, has its own address after `#`, so you can bookmark it, type it or send it to someone signed in to the same Central. The design is owned by [passes C and D](operator-console-ux-pass2-flow.md#6-navigation-routes-and-modules).

| Section | Addresses | For |
|---|---|---|
| **Now showing** | `#/now`; Show now: `#/now/show/scene`, `#/now/show/review` | What each frame is meant to show, [Show now](#now-showing-show-a-scene-now), Run cards, and why a frame shows what it does. |
| **Scenes** | `#/scenes`; `#/scenes/new/<step>` (optionally `?target=<frame-id>` when started from a Frame); `#/scenes/<id>/edit/<step>` | [Making and editing Scenes](#scenes-make-a-scene-step-by-step). Steps: `kind`, `photos`, `frames`, `media` (hand-picked only), `playback`, `review`. |
| **Schedule** | `#/schedule`; `#/schedule/new/<step>` | [Programs](#schedule-a-program-step-by-step). Steps: `scene`, `when`, `review`. |
| **Photo sources** | `#/sources`; `#/sources/new/<step>`; `#/sources/<name>/edit/<step>` | [Sources](#photo-sources-add-and-manage-a-source). Steps: `include`, `name`, `review`. |
| **Wall** | `#/wall`; a frame: `#/wall/frames/<id>/<facet>`, facet `calibration`, `binding` or `nowshowing` (the old `commissioning` address opens `calibration`) | The plan, the Unplaced tray, the first-run guidance and the Frame Inspector. |
| **Players** | `#/players`; one Player: `#/players/<device-id>` | The [Players list and Player pages](#the-players-list-and-the-player-page). The old `#/equipment` address opens `#/players`. |
| **Needs attention** | `#/attention` | Every frame that needs you, each linked to the facet that shows why. |

**Show and Wall are organization, not permission.** There is one shared admin token and no roles (design D-c/Q5 in the [console design](operator-console-ux-design.md)); the sections change what you are looking at, never what you may do. Calibration controls ([the Calibration facet](#operator-console-calibration-and-conflict-states)) are reachable only from the Wall. The four Show sections and Needs attention show frame health as status only (R4). This is checked by a test that walks what the Show and Needs attention pages import, and a browser test that visits each of their sample addresses (`tests/test_console_routes_r4.py`, `tests/browser/test_console_shell_browser.py`).

**Where an address leads.**
- An address the console does not know goes to the Wall while no frame exists (its first-run guidance is there), and to Now showing once one does.
- Pages read "Loading…" until the first snapshot. An address naming a frame or Scene that no longer exists then says so ("Frame lobby-left: This no longer exists.", "Scene evening: This Scene no longer exists.").
- An address naming a step that does not exist, or does not apply (`media` for a live Scene), opens the step the draft last showed, or its first step. An address may skip ahead; the steps it skipped are not ticked in the stepper.
- On the Wall, choosing a frame or a facet updates the address in place, and a frame reached by an address (typed, Back, or a link from the strip, a Player page or Needs attention) shows its Surface.

**Drafts.** Each flow (a Scene, a Program, a Source, Show now) holds one unsaved draft. It survives moving between steps, going to another section (the Wall included), a refresh, and a session that ends (above). A section holding one is marked "Draft" in the sidebar and offers **Resume draft (Draft)** and **Discard draft** in place of its New button. Only saving (or Activate now with a known outcome), Discard, Log out or reloading the page ends a draft. **A reload loses every draft**: drafts live only in this tab's memory (flow design Question 5, built on its default).

**Back and Forward.** A flow is one history entry: its steps replace each other, so browser **Back leaves the flow and keeps the draft**; it does not go to the previous step. Use the flow's own **Back** button, or an earlier step in the stepper, for that. After a save, Back never re-enters the finished flow; a save that lands after you went elsewhere leaves you where you are.

**Opening another draft.** **Edit** on another Scene's card while your draft has changes asks you to discard it first. An address typed, or reached with Back, that names another Scene never replaces your draft: the page reads, for example, "Unsaved draft for a new Scene: Resume or Discard" and says what discarding opens.

**Keyboard.** The first Tab reaches **Skip to content**, which moves focus to the page. Enter on a step presses Continue. In the drawer, Esc closes it and returns to Menu; choosing a section closes it and focuses that page's heading.

## Operator console: refresh, the snapshot-age clock, and the guidance banner

The console has **no push channel** — the player WebSocket is player-only — so every region reads from **one timestamped snapshot**: a single atomic read of `/v1/operator/inventory`, `/v1/operator/runtime` and `/v1/operator/media` (the design calls this **Plane A**; see [§9](operator-console-ux-design.md#9-storage-lifecycle--refresh) and [§4a](operator-console-ux-design.md#4a-the-two-plane-state-model) of the console design). The wall plan, the Players list, the Frame Inspector, and the now-showing chips all render from that same snapshot (a Player page adds its own node and V1 reads, each with its own read time), so a Frame's binding row and its now-showing chip always share one age. Between refreshes the console can be stale, and it never hides this.

**The snapshot-age clock.** The global bar always reads **"updated N s ago"** next to a **Refresh** control. The age is the honest time since the last successful snapshot, not a freshness guarantee — it tells you exactly how stale what you are looking at may be. Pressing **Refresh** re-fetches them together as one new snapshot and resets the clock. Because a stale read can never silently drive a wrong write, mutations still carry their concurrency tokens (`expected_generation`, `expected_revision`, an idempotent `activation_id`), so an action taken against a stale snapshot resolves to an explicit "the world moved" conflict rather than a silent wrong success.

**When the snapshot refreshes.** A new snapshot is fetched **every 5 seconds while the tab is visible** (the poll pauses while the tab is hidden and refreshes immediately when you return), **automatically after every mutation** (a bind, a placement, a commit, an activation), and **on an explicit Refresh**. A response that is older than one already shown, or that was read while one of your writes was in flight, is discarded rather than shown, so a poll never undoes what you just did ([pass 2 §7](operator-console-ux-pass2.md#7-polling-and-the-write-fence)). If a refresh fails, the bar reads **"updated N s ago — last refresh failed"**. Separately, the **Central pill polls `/healthz` about every 10 seconds** and reads "Central: ok", "Central: scheduler stale" (or another scheduler or database reason), or "Central: unreachable"; it reports Central's own health, not any Player and not observed presentation. **These interval numbers are tunable placeholders, not fixed guarantees** — the design states them as cadences to tune during operation ([§9](operator-console-ux-design.md#9-storage-lifecycle--refresh), assumption 4 in [§10](operator-console-ux-design.md#10-decisions-that-are-yours)), not gate decisions, so treat "5 s" and "~10 s" as approximate rather than contractual.

**A refresh never discards unsaved work.** The read snapshot is one plane; everything you are *doing* — an in-progress drag, a calibration draft you are "trying" before commit, the lease countdown, a step flow's draft, and the guidance-dismissed flag — is a **separate** plane the design calls **Plane B**. A Refresh replaces the read snapshot **only**; it merges nothing into your draft and cannot reach into it. Concretely, **a calibration draft in progress is not lost by a refresh** (the corners and crop you have dragged stay put); if the refresh reveals that the committed state moved underneath you — the Frame's `revision` or `generation` advanced — the console raises a "committed changed underneath you" conflict and lets you decide, rather than throwing your draft away. This two-plane separation is enforced structurally by the React state model (design rule R3), not by convention.

**First-run guidance banner.** On the Wall, a **non-blocking, dismissible** banner carries first-run onboarding (design Q8: always-visible inventory plus a guidance banner, never a modal wizard that gates the console — see [§10](operator-console-ux-design.md#10-decisions-that-are-yours)). Its **Add first frame** button opens the plan's measured Frame form and focuses the Frame ID; the same form opens from **Add frame with measurements** below the plan. The banner never blocks a control: the full inventory is visible behind it and you can act before dismissing it. **Dismissing it is per-session** and, because the dismissed flag lives in the draft plane (Plane B), the dismissal **survives a snapshot refresh** and a visit to another section — neither brings the banner back; a reload or Log out does. Its guidance is only as fresh as the last snapshot (there is no push), which is the stated cost of preferring a non-blocking banner over a linear wizard.

**Put content on a Frame.** Select the Frame on the Wall. Once it is bound and its calibration is valid, **Choose content for this Frame** on the Calibration facet opens a new Scene with that Frame selected on the Frames step. The **Now-showing** facet also offers **Make a Scene** with the same initial selection. Review the target checkboxes: add or remove Frames explicitly, then choose a live photo Source or hand-picked media. Save the Scene, then **Show now** for immediate activation or **Schedule it** as a Program. These authoring links do not activate content by themselves. The facet also links to existing Scenes. A Frame owns its location and calibration; the Scene owns the content and target choice, and a Run or Program activates it. The first-run banner names this path after binding and calibrating.

If a new Scene draft is already open, following either Frame link keeps that draft. It does not add a target behind your back; the Scene explains this and offers **Choose Frames** so you can select the Frame yourself. A dirty edit of another Scene retains the flow's **Resume or Discard** choice.

## Operator console: wall health and the attention strip

Every Frame tile, the Frame Inspector header, the Unplaced tray, the frame badges on Now showing and the frame lists of the Scene flow and Run cards show one **health** label for each Frame, so the same Frame never reads differently in two places; each [Player page](#the-players-list-and-the-player-page) shows the Player app's last-reported age from the same source, beside the host layers'. The plan tile shows a short form; the full label, with its age, is in the Inspector header and in the tile's accessible name. The states, their order and their wording are owned by the [pass 2 design, §4](operator-console-ux-pass2.md#4-per-frame-health-one-closed-set-one-classifier).

**What the labels are based on.** A label reports **Central's record of the last readiness report it accepted from the Player**, aged on Central's clock at the moment of the snapshot. It is **not** a live video readback, and nothing on the console says "LIVE", "online" or "connected". A Frame that reads healthy may still be dark (a failed panel, a failed decode); the console cannot confirm lit pixels. The now-showing chip is Central's **intent** for the Frame, never confirmed playback. A healthy Player reports about twice a second.

| Label | What it means | What to do |
|---|---|---|
| **Last heard N s ago** | Central accepted a report from the bound Player within the silence threshold, and nothing else is wrong. This is the healthy state. | Nothing. |
| **Player silent · last heard N min ago** (alarm) | The Player has not had a report accepted for longer than the silence threshold. The Pi may be off, off the network, crashed, or talking to Central but having its reports refused. | Check power and network at the Frame. If the Central pill is not ok, fix Central first (below). If it stays silent after the Pi is back, restart the Player. |
| **Enrolled N s ago, no report yet** | The Player enrolled (its process started) but Central has not yet accepted a report from it. A to-do for up to the threshold after enrolling, then an **alarm**. A Player that restarts in a loop keeps resetting this age and may stay a to-do. | Wait a few seconds after a boot. If it turns into an alarm, or you see the enrolled age keep resetting, check the Player's logs for a crash loop. |
| **Output interrupted (Central's inference: Display Host reported the app surface invalidated or withdrawn · recorded N s ago) · the Run continues** (alarm) | Central holds an unresolved Output-loss record for this Frame's current Binding, from what the named node layer reported: Display Host an invalidated or withdrawn app surface on this Output, or the App Effect Broker "reported the app process exited" (which Central applies to every Output linked to that process). "· the Run continues" appears only when a live Run targets the Frame. Only this Output's contribution is interrupted; the Run, other Frames and Actuators continue. The tile reads "Output interrupted · the Run continues", or "Output interrupted" with no live Run. A Frame with no such record shows nothing, which is not proof the Output is fine: Central records only the losses it could link. | Open the Frame's Binding facet and its Player page; check the Panel, cable and the named layer. |
| **No Panel listed as connected at the Player app's last enrollment (may be stale)** (alarm) | When the Player app last enrolled, it did not list a connected Panel on the bound Output. This is Central's enrollment record, not a live reading. The tile reads "No Panel listed at the last enrollment". | Check the Panel's power and cable, then restart the Player so the Player app enrolls again. |
| **Needs calibration** (to-do) | The Frame's calibration is not valid, so nothing is shown on it. This is normal for a new or newly bound Frame. | Open the Frame's [Calibration facet](#operator-console-calibration-and-conflict-states) and save a calibration. |
| **Needs a Player** (to-do) | No Player output is bound to the Frame. | Open its [Binding facet](#operator-console-onboarding-binding-and-auto-recovery) and choose a free output. |

**The silence threshold is about 31.5 s.** Central serves it with the inventory; its single source is `contracts/liveness.py`, shared with the Player's own retry timing. It is long enough that one failed Player cycle against a responsive Central does not raise a false alarm, so a powered-off Pi reads "Player silent" within about 31.5 s plus one 5 s poll. A Central that is answering slowly (near its 15 s request timeout) can make a live Player briefly read silent. The derivation and its costs are in [pass 2 §2](operator-console-ux-pass2.md#2-the-liveness-signal).

**Panel hot-plug is not detected on the Wall.** Frame health uses the Panel record from the Player app's last enrollment. Unplugging or plugging in a Panel afterwards changes nothing on the Wall until the Player app enrolls again (a restart); the Player page's Display Host row shows Display Host's current per-Output connector state where the node lane runs. This is a known gap, deferred ([pass 2 §12](operator-console-ux-pass2.md#12-costs-deferrals-and-questions)).

**The attention strip.** A one-line strip under the status bar counts the Frames that need you, for example "2 frames need attention · 3 to set up", or "No Frame needs attention", with "· K awaiting a first report" when K Frames are bound to a Player app that enrolled moments ago and has not reported yet (it counts Frame health, not host or Panel health). Alarms count as needing attention and to-dos as to set up; a Player that enrolled within the last second is not yet counted. **Show frames** opens a list (alarms first, then to-dos, up to 8 entries and "and M more"). On the Wall, Players and Needs attention pages each entry is a button: it opens the Wall on the Frame's Surface, selects the Frame, and opens the Inspector on the facet where the cause is shown (Binding for Player, Output and Panel problems, Calibration for calibration). On the Show sections (Now showing, Scenes, Schedule, Photo sources) the entries are text only, so reading them never abandons show work. **Show all** opens **Needs attention** (`#/attention`), the same list at full width with no cap, where each entry links to the Frame's facet (`#/wall/frames/<id>/<facet>`).

**When Central's scheduler is not ok.** If the Central pill reads something like "Central: scheduler stale", the strip replaces the silent-Player entries with one line, for example "5 frames silent — Central's scheduler is stale; Players may be unable to report until it recovers." Fix Central (its logs and `/healthz`) before visiting the Pis. Entries with another cause, such as no Panel listed, stay listed.

## Operator console: placing, moving, and deleting Frames (and the Unplaced tray)

In the console (served at `/` since the cutover, aliased at `/console`) the wall plan is on the **Wall** section (`#/wall`): each Surface is a flat millimetre plan and its Frames are drawn as rectangles from their `x_mm/y_mm/width_mm/height_mm`. Dragging or entering measurements uses the operator routes documented above ([reposition and remove Frames](#operator-api-reposition-and-remove-frames)) or the existing `POST /v1/operator/frames`. Selecting a Surface *filters* the plan to that Surface's Frames; a Surface is a bare text label, not something you act on. This mirrors the "reading & building the wall" walkthrough (J3) in the [console design](operator-console-ux-design.md).

**Place a new Frame.** On a new installation with no Frames, the Surface selector starts at **wall**. Drag a rectangle on the plan or press **Add frame with measurements** to use the keyboard and enter its position and physical size in millimetres. Both paths open the same form for the Frame's **id** and **Frame profile** — pixel width/height and diagonal (plus whether it is video-capable). The console sends one `POST /v1/operator/frames` carrying the chosen `surface_id`, `x_mm`, `y_mm`, `width_mm`, `height_mm` and profile. Position `(0, 0)` is reserved for old Unplaced frames; choose another position. Central runs the same orientation-coherence guard as every Frame creation: if the aperture orientation would disagree with the profile (a portrait matte declared against a landscape panel, or vice versa) the create is refused and nothing is added. The profile values are **Frame facts** — operator-declared and persisting across a later panel swap — not a live Panel reading.

**Frame ids are readable and permanent.** You name the Frame, for example `lobby-left`; every later screen shows that id, and it cannot be changed. The form checks it as you type: letters, digits, `-`, `_` or `.`, starting with a letter or digit, at most 96 characters, and no `:`. This is the same rule a Scene uses to target a Frame, defined once in `contracts/models.py` (`TARGET_ID_PATTERN`) and enforced by Central on create, so an id Central accepts can always be targeted ([slice 2 §8](operator-console-ux-pass2-onboarding.md#8-readable-frame-ids-one-rule-in-contracts)). An id already in use reads "A frame named lobby-left already exists." Frames created before the rule keep their ids and stay readable and deletable. There is no separate display name yet. **Replacing a panel with a different size or resolution:** unbind the Frame, then use **Edit profile** in its Frame facts. The Frame id, placement and Scene targets stay intact. Central refuses the change during a live Run on that Frame or if another operator changed its binding/profile first. A changed profile invalidates calibration and its preview, so bind the new Panel and calibrate the Frame again before relying on output readiness. The profile must still match the physical aperture orientation.

**Move a Frame.** Drag an existing rectangle to reposition it without changing its stored size. For exact position or size, select the Frame and press **Edit placement**; the form sends the entered millimetres through `PATCH /v1/operator/frames/{frame_id}` and checks orientation against the saved Frame profile. Placement is **last-write-wins with no lock**: a concurrent edit of the same Frame by another operator can overwrite yours, and the plan corrects itself on the next snapshot refresh. Because geometry is operator-only metadata — it never reaches a Player and is independent of the calibration corners (normalized to `[0,1]`) — a placement edit **carries no concurrency token and never invalidates calibration or bumps `generation`**. This is the one deliberate exception to the console's "every write carries its token" rule.

**Delete a Frame.** The confirmation dialog lists live Runs, saved root Scenes (including nested child/outro targets) targeting the Frame, and upcoming Programs using those Scenes from the current snapshot. Central checks current references again when you confirm. It preserves the existing refusal while the Frame is bound (409 `frame_bound`) or targeted by a live Run (409 `frame_in_use`). It also refuses with 409 `frame_referenced` while saved Scene roots or queued activations still target the Frame, returning sorted server-side Scene, Program, and queued-activation IDs. A stale refusal says which references Central found and directs you to press Refresh, edit/remove the saved Scene and Program references, and review queued activations before retrying. With those references cleared, the Frame can be deleted. Recreating its id starts uncalibrated.

The reference guard does not ban explicit future Scene targets for a Frame id that is currently absent. That permits recovery: an operator can retain or author future content for an id and recreate the persistent Frame later. Until the Frame exists and is bound and calibrated, Central has no Output to receive that content.

**The Unplaced tray.** Frames created by the old UI all sit at `wall`/(0,0) with no distinct position; rather than pile them at the origin as permanent clutter, the console collects such geometry-less Frames in an **Unplaced tray** beside the plan, as a list. From the tray you can drag a Frame onto the plan or select it and use **Edit placement** to give it a measured position. Either sends the same `PATCH` as a move, and the Frame then leaves the tray and joins the plan. You can also delete it under the same delete checks above.

A freshly placed or moved Frame **settles to its final position on the next snapshot refresh**: the plan re-fits per Surface (it scales each Surface's millimetre space to the viewport), so a rectangle drawn or dragged optimistically can shift slightly once the authoritative snapshot returns, while its stored millimetre geometry is exactly what you entered.

## Operator console: onboarding, binding, and auto-recovery

New hardware appears in the console before it does any work. Following [decision 0006](decisions/0006-central-authority-and-stateless-players.md), Central is the source of truth and a Player is a replaceable box behind a Frame. The design of this flow, and its costs, are owned by [pass 2, slice 2](operator-console-ux-pass2-onboarding.md).

### The Players list and the Player page

The **Players** section (`#/players`) has one row per physical box, keyed by its fleet device identity (derived from the serial it claims at netboot), so the same Pi never appears twice. The design is owned by the [domain-driven console design, pass 1](operator-console-ddd.md#9-screens-and-read-models). The list reads the same `/v1/operator/inventory` snapshot as the plan plus the netboot read below, so a new Pi appears the moment it netboots or enrolls. It makes **no** node read. Each row names the Player ("Player …a1b2c3", the last six characters of its claimed serial), links to its Player page, and shows:

- **Standing**: **Not enrolled** (seen at boot, never enrolled), **Unbound** (enrolled, no Output bound: a new Pi, or one whose Frames were all unbound), **Bound** (at least one Output bound) or **Retired** (permanently out of service).
- **Bound to**: a link to each Frame its Outputs are bound to.

**The Player page** (`#/players/<device-id>`) is the one home for that box. Each section shows its own read time and fails on its own ("This section could not be shown") without taking down the rest:

- **Header**: name, standing, the serial **labelled as a claim** (the console cannot confirm which physical box sent it), **Enrollment** ("Player app enrolled N s ago (authority epoch N)", Central's enrollment record), links to bound Frames, and the full device and Player ids under **Identifiers**.
- **Layers**: one row per node layer, each naming its source. Host Management and App Manager show when they **last reported**. App Effect Broker shows its app-process fact and when Central **first received** it; its last report reads "Unknown: App Effect Broker sends evidence only on change, and Central stores no receipt of its polls". Display Host shows when it **last reported** (its newest display exchange on this boot) and, for each Output, three facts it reported: the Panel connector (connected or not), the admitted surface ("the app's surface for Frame … (binding generation g)" or "Display Host reported no app surface admitted"), and the compositor receipt for that surface with its age on Display Host's own clock. A compositor receipt is not proof of Panel pixels, and the console judges no staleness. When Display Host has reported no Output on this boot, the row says so. The Player app shows its last-reported readiness. "Panel pixels: Unknown: no layer observes them" closes the list. A silent Player app with a reporting host shows both ages, so a crashed app reads differently from a dead box.
- **Outputs**: each Output's state ("Bound to Frame …", "Free", "No Panel listed at the last enrollment", or "Retired with its Player"; "No outputs reported" when there are none), **Bind to a frame…**, **Identify Panel**, and the Panel record at the Player app's last enrollment, labelled "may be stale". A bound Output also shows its [Output interruption](#operator-console-wall-health-and-the-attention-strip) when Central records one.
- **Boot**: the current node session's boot as a claim (or "No current node session"), then the latest node boot offer ("not proof the Player booted"). A later boot never says what caused it. If Central's newest boot record for this box is not a node boot, one warning line says so: "Booted by the deprecated path: Central's newest boot record for this box is a deprecated boot offer (or a base image served without an offer) · recorded N ago; its kernel command line lacks `photowall.node=v2`; Select and Stage do not reach it." Fix that Pi's command line ([Node control](#node-control)).
- **Reboot**: **Reboot Player** targets Host Management's one current session; when it cannot, the button is disabled and says why (for example, Central's effect gate is closed). The dialog names the bound Frames and their live Runs: the Run stays active and no other Frame or Actuator gets a command. Each request then reads one state: Requested · delivery unknown, Outcome unknown, Received, Accepted or Rejected by Host Management, or Host Management reported the reboot started · completion unknown. If the answer is lost, **Retry the same request** resends the identical request. A **new** request cannot be sent while another reboot request for the same session is outstanding (unexpired on Central's clock and not rejected; Accepted and started requests count). The console checks Central's served `outstanding` at the moment of sending, and Central refuses a new command id with 409 `node_reboot_outstanding`, which reads "Another reboot request for this Player is outstanding; close this dialog and review it."
- **App**: the node app operations, each with its named state (for example "Rejected by App Effect Broker", or "Ended by a later boot" for a stage that ran before the Player rebooted). **Stage app** switches this Player's app for this boot only (see [Releases, Stage app and Update the wall](#releases-stage-app-and-update-the-wall)). **Qualified fallback** shows the app environment the Player app linked, lets you qualify it, and lists the stored acceptances.
- **Danger zone**: **Retire player** (Unbound only) and **Unbind all outputs** (Bound only).

A Player page reads that box's node state every 5 s while it is open and visible. If Central was started without node control, a banner sits above every page ("Node management is off on this Central …"), the Player page shows one line, "Node records are not shown: node management is off (see the banner).", in place of its node sections, and its header, Outputs, Bind, Identify, Retire and Unbind still work. That is a misconfiguration: start Central as [Node control](#node-control) describes. A failed read keeps the last rows, marked "refresh failed". A retired Player is not read ("Not read: Player retired").

The serial comes from a separate, optional read of `GET /v1/operator/netboot`, refreshed at most every 30 s or when the set of Players changes. If that read fails, the Players list says **"Boot records unavailable"** and keeps the last serials it knew; you stay signed in and everything else works.

### Releases, Stage app and Update the wall

The console owns node releases; no step needs curl or a hand-written request. The design is owned by [Part E of the console DDD](operator-console-ddd.md#24-what-part-e-covers-and-why).

**Fleet › Releases** (`#/releases`), top to bottom:

- **Boot selection**: the one deployment Central offers every node-path boot from now on, its contents (base tag; app or "no app") and revision, or "No boot selection · Central refuses every boot".
- **Deployments**: newest first, the selected one always listed. **Select for every boot…** sets the boot selection. Its dialog states the scope: every Player that boots by node path from now on is offered it, including Players Central has not seen, and Central cannot list which Players will boot. A deployment with no app says every boot from now on is offered no app. If another operator selected meanwhile, nothing is sent: "The boot selection changed meanwhile; review it".
- **Release catalog**: the releases GitHub releases reported, via the media worker. **Publish…** (with its app, or without it) makes a deployment from a release. Central downloads and verifies every asset (the dialog states the size), which can take minutes; keep the page open. A deployment is **permanent**, and the first deployment on a base fixes that base's App Manager pins. If the answer is lost, the page holds the request and offers no second Publish; **Send again** re-sends the identical request and downloads everything again. **Check GitHub releases now** queues a catalog check; new releases appear when the worker records them.
- **Effect gate**: Central's gate state and reason. Reboot and Stage app are refused while it is closed. Central opens it only from a deployment certification; the console cannot open it ([verifier](#optional-read-only-kubernetes-node-verifier)). Until a deployment controller exists, every real Central reads "Effect gate closed · Central's reason: no deployment certification has opened it".

A rebooted Player is offered the boot selection current at its next boot; the reboot dialog says so and links here.

**Stage app** (Player page › App) switches the Player's app **for this boot only**; any later boot, including an unplanned one, runs the boot selection. It is disabled, with its reason, while the Player is retired, the effect gate is closed, App Effect Broker has no current session on this boot, or a switch is in progress. Pick a deployment that carries an app; Central judges it when you send and the dialog shows Central's answer in its words (for example "No qualified fallback for this Player's current Outputs and base: qualify the running app first"). On a Player that drives Frames, each Frame shows the base page while the app switches, then rejoins its Run at the current point, as on Reboot; missed content is not replayed, and bindings and calibration are kept.

**Qualified fallback** (Player page › App) qualifies the app the Player is running now, so that Stage has a fallback. The Player must be bound and show one steady photo or looping video on every Output for 30 s. **Begin** starts it, and the page asks Central for a sample every 2 s while it stays open and visible. It stops with a reason on a terminal answer or after 2 minutes without progress (a slideshow that changes within 30 s never progresses). Qualification needs no effect gate.

**Update the wall** (Releases › **Update the wall…**, or **Update the wall with this…** on a catalog row) walks one release onto the wall: **Get it** (Publish), then optionally **Try it on one Frame** (the page qualifies the running app if needed, then stages the release on that Player), **Look**, and then **Keep** or **Back out**. Keep sends one Select, then reboots the Players this console knows one at a time, the tried Player first, each waiting until the previous one has rejoined (new boot, target app linked, every bound Output reporting ready); you can skip a Player before its reboot. A Player that has not rejoined after 10 minutes, a rejected or unknown reboot, or a hidden tab pauses the rollout with Retry, Skip and Stop. Back out reboots the tried Player onto the current selection. Nothing about the rollout is stored in Central: closing the page stops further reboots, and on reopening the page re-derives each row and waits for **Resume**. Select is fleet-wide, so any Player that restarts for any reason takes the new deployment. With the effect gate closed, Try is unavailable and Keep is Select alone: Players take the release at their next boot.

### Identify an Output and bind it

You can power on more than one Pi before binding. On a Player's page, each Output that was connected at the last enrollment and is not bound has an **Identify Panel** action, including a Bound Player's free second Output; the Binding facet's Output picker offers the same action on each candidate. Use it to match a Player page to the physical screen: Central queues a request for that exact Output, and the Player briefly shows a high-contrast **IDENTIFY THIS OUTPUT • `<output id>`** banner on it. The banner lasts at most 15 seconds. A new request for the same Player replaces its previous request.

The console reports that the request was accepted by Central; it cannot confirm that a banner reached the panel. Check the physical screen yourself, then bind that Output to its persistent Frame. Identification requests expire, and Central stops delivering one if its Output becomes bound, disconnected, or the Player's authority changes. The action is disabled, with its reason, on an Output not listed as connected at the last enrollment ("Connect a Panel and restart the Player app") and on a bound Output ("Central identifies only unbound Outputs"). When Central holds no negotiated Identify capability for the Player app's current enrollment (the app has not completed its control hello, or did not offer Identify), the request answers "Central has not negotiated Identify with this Player app's current enrollment".

**Outputs with no Panel listed are not offered.** The Player app lists connected Panels only when it enrolls. An Output whose Panel was off or unplugged at that moment reads "No Panel listed at the last enrollment" and cannot be bound or identified. Connect and power the Panel, then restart the Player; the Output becomes Free.

**Local Central-link diagnostic.** On each connected unbound Output, the Player shows its current local link state: **Central: connecting** before this process has applied configuration; **Central: reachable · configuration received** after a successful state exchange; or **Central: retrying · last configuration received** / **Central: retrying · no configuration received** after a failed exchange, depending on whether this process received configuration before. This is a connection-history diagnostic owned by the running Player process. It does not report boot health, playback readiness, or visible content, and it does not change the content on a bound Output.

You can bind from either end. Both write the same Frame binding, carry the Frame's `generation` token, and are refused rather than applied when something changed:

- **From a Frame.** Select the Frame, open its **Binding** facet, and choose a Player's Output from "Choose an output". Nothing is pre-selected, even when there is one option. Then press "Bind to <frame>". If a refresh removes the Output you chose, the choice is cleared and announced. If the bind is refused ("That output was just bound elsewhere", "That Player is no longer available", or "This Frame changed — reload and review its binding."), your choice is cleared; choose again from the current list.
- **From a free Output.** On the Player page, a Free Output offers a "Bind to a frame…" list: pick an unbound Frame and press Bind. On success the console opens that Frame's Binding facet.

**After a bind, calibrate the Frame again.** A successful bind or unbind bumps the Frame's `generation`, clears any live calibration lease, and **invalidates calibration**, so the Binding facet shows **"Review required"** with a **"Calibrate this Frame"** button that opens the [Calibration facet](#operator-console-calibration-and-conflict-states). The Binding facet also shows the bound Player and Output and the Panel record at the Player app's last enrollment. This is a forced re-validation, not data loss: bind and unbind set `calibration_valid=false` but never write the `calibration` column, so the committed calibration persists and is available to re-confirm. Only a fresh save overwrites it. Retire changes no binding and no calibration. If the Frame changed underneath you (a concurrent bind or unbind bumped its `generation`), the write is refused with **409 `binding_generation_conflict`** and nothing is applied.

### Confirmation dialogs

Unbind, **Unbind all outputs**, Delete frame and **Retire** each open a confirmation dialog. It names the target, says what will happen, and records what you saw when you opened it; the write uses exactly that, never a later refresh.

- **Unbind** names the Frame and its live Runs. The Frame stops being served, its calibration is kept but marked invalid, and the Frame on the same Player's other Output is re-planned too.
- **Unbind all outputs** (in-service Players) opens "Unbind each Output of Player …?" and unbinds each Frame in turn and ends with "K of N unbound" and a result for each Frame. A Frame that changed or was already unbound is skipped and the rest continue; an unknown outcome stops the sequence, and the remaining Frames are marked not attempted. Nothing is resent.
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

**Retire only a Player you will never use again.** Retire is offered only on Unbound Players, and Central refuses to retire a Player with any bound Output (409 `player_bound`). It has **no undo, even after re-imaging**: the Player id comes from the hardware serial, and a retired serial is refused at enrollment. A retired Player's Outputs leave the bindable set.

**To replace a Pi, unbind it; do not retire it.** Unbind the old Pi's Frames (or use **Unbind all outputs**), then bind the new Pi's Output to each Frame. The old Pi returns to Unbound and can be bound again later. **Unbind** (`DELETE /v1/operator/frames/{frame_id}/binding`) changes no Player record.

**To revoke a compromised bound Pi, unbind it, then retire it immediately.** Unbind alone leaves its session token valid and its serial free to enroll. Retire (`POST /v1/operator/players/{player_id}/retire`) revokes its authority and refuses its serial from then on. Its netboot device row is **not** retired: its last healthy tag still counts toward the release frontier and its serial still netboots ([base-image selection](#base-image-auto-mirror-0012); owner Question 4 of the [slice 2 design](operator-console-ux-pass2-onboarding.md#12-costs-deferrals-and-questions) is pending).

### Returning known Pi

**Returning known Pi.** When a Pi central already knows reboots, it re-enrolls by hardware serial and central self-heals it straight back to its old bindings — so it reappears already bound (`is_bound=true`) instead of reading Unbound. Recovery is a **serial-match convenience, not cryptographic identity** — the serial is not a secret and central does not prove the box is the same box, only that it presents the same serial. The console shows no banner for it: the Player page header's **Enrollment** fact ("Player app enrolled N s ago (authority epoch N)") is Central's durable record of the latest enrollment, and every boot mints a fresh `authority_epoch`, so a new epoch on its own is normal, not an incident.

## Operator console: calibration and conflict states

In the console — served at `/` (aliased at `/console`) since the cutover retired the old flat page — select a Frame in the wall plan to open the Frame Inspector, then open its **Calibration** facet (`#/wall/frames/<id>/calibration`; the old `#/wall/frames/<id>/commissioning` address opens it too). It is reachable **only from the Wall section**; calibration is a hardware concern deliberately hidden from show programming, which sees only a Frame-health label ([R4](#operator-console-sections-links-and-drafts)). The facet's design and wording are owned by [the domain-driven console design, pass 3](operator-console-ddd.md#19-screens). The legacy path rides the existing admin-authenticated `POST /v1/operator/frames/{frame_id}/calibration` route.

**What the facet shows.**

- **Committed calibration** — the SDR gain, rotation, corners, and crop Central holds for the Frame.
- **Frame profile** (from `FrameProfile`) — pixel width/height, diagonal, and video-capable. These are **operator-declared at Frame creation and persist across a Panel swap**, so they are Frame facts, not Panel readings. **Edit Frame profile** works on an unbound Frame only.
- An unbound Frame reads "No Output bound. Bind one on the Binding facet." The bound Player and Output, and the Panel record at the Player app's last enrollment, are on the **Binding** facet.

When both resolutions are known, the facet compares the declared Frame profile
with the Output's resolution at the Player app's last enrollment, in the committed
calibration's orientation when that calibration is valid; an invalidated
calibration does not suppress a mismatch warning.
A mismatch names both values and asks the operator to verify the Panel,
restart the Player if the Panel changed, and correct the Frame profile
before calibration if needed. The enrollment record may be stale; it never changes the
profile automatically or prevents calibration.

The facet has no colour or power controls: no wired path supports them, and each would arrive with its own served read.

**Calibrate by direct manipulation.** In the *Adjust calibration* editor, drag the four corner handles and the two crop handles, or type the coordinates directly; SDR gain and rotation are separate draft controls. Every edit stays a **local draft** that a background inventory refresh never overwrites. Each geometry change is validated by the **same convex test the server enforces** (the `1e-6` epsilon and clockwise winding): a folded or too-thin quad snaps the handle back with the inline message **"corners must form a convex aperture"**, and an empty crop snaps back with **"crop must describe a nonempty rectangle"** — and **no request is sent**. A rejected drag changes nothing, on the client or the server.

**Live calibration: two paths, one name.** Central chooses the path per Frame from its served calibration capability.

- **With Display Host acknowledgment** (`native_trial`, a Player on the node lane with Display Host). **Start live calibration** sends each edit to Display Host. **Save calibration** stays disabled until Display Host acknowledges the latest edit ("Edit N presented to the compositor by Display Host · not proof of what the Panel shows"). **Stop live calibration** ends it; after it ends, **Start again**. Refusals name their condition, for example "Display Host has not acknowledged the latest edit. Save calibration stays unavailable."
- **Without acknowledgment** (`legacy_preview`, every other Player). **Show on the Panel** sends the draft to the Player app under a server lease that expires in **30 seconds**; the facet shows the *server's* countdown ("Central sent the draft to the Player app; live calibration ends in N s. No layer acknowledges what is presented on this path."). There is **no auto-renew**. When the lease ends, the Player app returns to the saved calibration and the facet says "Live calibration expired; your draft is kept", with **Show again** to re-send the same draft. **Save without acknowledgment** saves the draft as a new calibration revision; it is never called Save calibration, because nothing on this path acknowledges what was presented. **Stop live calibration** clears the lease immediately and returns the draft to the committed calibration. Show and Save require a bound Frame.

**One shared lease, last-writer-wins.** On the legacy path there is a **single lease slot per Frame and no lock** — the console never implies you have exclusive control of the Panel. A second tab or operator who shows or saves the same Frame overtakes you. The console's 5 s snapshot poll surfaces these changes as explicit states rather than a silent overwrite:

| What happened | What you see | What to do |
|---|---|---|
| Your lease expired with no interaction | "Live calibration expired; your draft is kept" | Show again to keep adjusting; your draft is retained. |
| Another tab/operator saved or showed a calibration for the same Frame (the single slot was taken) | "Someone saved a calibration for this Frame meanwhile; review it, then start again" | Review the fresh state before writing again. |
| Save refused — another save advanced the calibration revision (stale `expected_revision`) | "Another session changed this frame's calibration — reload and re-review." | Reload and re-review; your stale save was **refused**, not silently applied. |
| Save refused — the Frame's binding changed via bind or unbind (stale `expected_generation`) | "This Frame's binding changed — its Output is no longer under your control; reload." | Reload and re-check the binding. |

Every write carries **both** concurrency tokens (`expected_revision` and `expected_generation`) against the baseline captured when you opened the facet, so a stale save is refused with a **409** rather than silently overwriting state you never reviewed. (A show or save against a Frame whose binding was removed underneath you is refused with "This Frame is no longer bound to an Output — bind it before calibrating.")

## Operator console: running the show

The Show sections (**Now showing**, **Scenes**, **Schedule** and **Photo sources**) are the content-and-schedule layer: Sources, Scenes, Programs and Runs. The wording and states below are owned by the [slice 3 design](operator-console-ux-pass2-showrunner.md), and the step flows by [passes C and D](operator-console-ux-pass2-flow.md#7-the-flows-defaults-have-a-source); this section is how to use them, job by job.

**Show sections never show the Calibration facet (R4).** Hardware setup (calibration geometry, SDR gain and the Frame profile) lives only on the Wall's [Calibration facet](#operator-console-calibration-and-conflict-states). The **only** hardware fact the Show sections see is each Frame's **health label**, the same one the Wall shows ([wall health](#operator-console-wall-health-and-the-attention-strip)), including "Needs calibration" when `calibration_valid` is false: a Frame that cannot present matters at show time. The label is **status, not a control**; you fix the cause on the Wall.

### Step flows: one question at a time

Making a Scene, scheduling it, adding a photo source and showing a Scene now are **step flows**. Each step asks one thing. A stepper above it names the steps (under 850 px it reads, for example, "Step 3 of 5 · Frames"), and earlier steps in it are buttons that go back to them. **Back** and **Continue** sit below the step; Back on the first step returns to the section. Values that have a stated default sit under a collapsed **Advanced** on their step, whose summary line says what is inside. The last step, **Review**, lists every answer, the advanced ones included, each with **Change**, which opens its step.

- **Continue checks only the step you are on.** Its reasons appear beside its fields and focus moves to the first one; later steps say nothing until you reach them. After a Change, Continue returns toward Review, stopping at any step that still has a problem.
- **The final button** (Save Scene, Replace Scene, Schedule Program, Add separate windows, Save source, Activate now) checks every step. With problems it sends nothing: a list at the top of the step names each, and choosing one opens its step (and its Advanced) with focus on the field.
- **A button is never disabled over a problem.** Only a write in flight disables one, and the step is read-only meanwhile; Replace Scene also waits while the Scene is stale (below). **Finish** on a Run that is already finishing is disabled, and its card's "Finishing: requested 20 s ago" says why.

Drafts, addresses and browser Back are described in [sections, links and drafts](#operator-console-sections-links-and-drafts).

### Names and ids

You **name** Scenes and Programs on their Review step; the console derives the id Central stores. Accents are dropped, letters are lowercased, every other run of characters becomes `-`, and the id is cut at 96 characters: "Family Evening" is shown as "Saved as `family-evening` · Change". Central keeps only the id (there is no stored display name), so cards and Runs show ids.

Type an id yourself, with **Change** (it opens Review's **Advanced** with its **Id** field), when:
- the name has no Latin letter or digit: Advanced stays open with "This name needs a Latin letter or digit for its id; type an id."; or
- you want a particular id. An id starts with a letter or digit, then letters, digits, `-`, `_`, `.` or `:`, up to 128 characters.

A name whose id already exists is refused before anything is sent ("A Scene called `family-evening` already exists; choose another name."). The check uses the last refresh, so two operators can still pick the same new id within a few seconds of each other. For a **Scene**, Central refuses the later save: "A Scene with this id was saved meanwhile; nothing was replaced." Choose another name, or Edit the stored Scene. For a **Program**, the later save still replaces the earlier one (Program saves have no guard; slice 3 Question 4). Activation ids are never shown.

### Now showing: show a Scene now

Press **Show now** on Now showing, on a Scene's card, or after saving a Scene. The flow is **Scene → Review** (`#/now/show/scene`, `#/now/show/review`):

1. **Scene to activate**, with the frames it reaches and their health. A Frame that needs attention links to its Wall Inspector recovery facet; a healthy Frame remains informational. The health label is Central's latest accepted Player readiness report, not confirmation of visible pixels. Opening a recovery link and returning keeps the Show now draft. From a Scene's card or a Scene's Save it is already chosen.

Both Show now steps also show **Saved Source freshness** for the selected Scene's live Source references. It reads Central's latest completed catalog status for each Source and may offer **Refresh Source** when the status needs attention. A **202** response means Central accepted the refresh request; the displayed Source status remains the last completed worker result until a later snapshot carries the requested completion. A refresh receipt does not mean the catalog has refreshed, media is prepared for a Frame, or anything is visible. A missing historical Source has no current status or refresh action. Hand-picked media is described separately because refreshing a Source does not change its saved choices. The status is advisory: it does not block **Activate now**.
2. **Review** lists the Scene, its frames, the **Priority** and **If it is already running**. Under **Advanced** are **Activation priority** and **If it is already running**. Press **Activate now**.

**The priority defaults to the Run already on top.** Review reads, for example, "5 (the default: the highest Run on its frames has priority 5; at equal priority the newer Run shows on top)", or "0 (the default: no Run covers its frames)". It is the highest priority among the live Runs covering any of the Scene's frames. That is enough to show on top, and one more is not needed: Central ranks layers by priority, then by admission order, and at equal priority the Run admitted later wins, so the new Run shows over the one already there ([flow design §7 J7](operator-console-ux-pass2-flow.md#7-the-flows-defaults-have-a-source)). Until you type a priority, the default follows the Runs as they change, even while Review is showing. A priority below it holds Advanced open and says where the Run would stay, for example "At priority 3 this stays underneath the Run of evening (priority 5) on lobby-left." No priority shows it on top of a Run that **protects** one of its frames: Central refuses the activation at any priority, so Review drops "shows on top", holds Advanced open and says, for example, "Central will refuse this at any priority: lobby-left is protected by the Run of evening." Finish or cancel that Run first.

**If it is already running:**
- **Leave it running** (default): nothing changes; the outcome reads "Not started: evening is already running, left as is."
- **Restart it:** ends the current Run and starts a new one now. A restarted Run has **no Program end**. A Scene with Keep playing on plays until you Finish or Cancel it; one with Keep playing off plays one cycle, then ends.

**Activate now** (`POST /v1/operator/activations`) answers synchronously, and the console shows exactly that answer on Now showing:

| Outcome | Reads | What to do |
|---|---|---|
| Admitted | "Started: Central admitted a Run of evening." | Nothing. It is Central's plan, not confirmation from the panels. |
| Refused by protection | "Not started: lobby-left is protected by the Run of evening." | The Run named is the one Central reported as blocking. Finish or cancel it, or wait until it ends. A Scene that itself protects a frame covered by a higher-priority Run reads "Not started: this Scene protects frames that evening's Run (priority 5) covers; use priority at least 5." |
| Other refusal | "Not started: 16 activations are already waiting.", or "Not started: `<error>`." | Correct the cause and try again. |
| Outcome unknown | "Outcome unknown. Try again; it will not start twice. Changing the form makes this a new activation." | The request timed out, failed, or Central answered with a server error, so the Run may or may not have started. **Press Activate now again, unchanged.** |
| Session ended | "Not started: the session ended. Sign in again, then activate." | Sign in; the draft is still there. |

**Why a retry cannot start the Scene twice.** Each activation carries a hidden key. It is made when the draft opens and again whenever you change a value (choosing the value already there changes nothing). It stays with the draft across steps, sections, a Wall visit and the sign-in screen. After an unknown outcome the flow stays on Review with the same key, and Central answers a key it already knows with its stored result. A known outcome ends the flow. The flow never replaces a draft that holds changes or awaits a retry; a clean one follows the Scene you last saved or picked.

Queueing an activation and overriding protection ("force") are not offered: design bead 3B-3 is deferred until the owner answers its Question 6 ([slice 3 design](operator-console-ux-pass2-showrunner.md#18-costs-deferrals-and-questions)).

### Now showing: Runs and Central's plan

Now showing starts with each Frame's health label, then the **Runs**: "Central's plan: what each frame is meant to show now, not a readback of the panels." Each live Run is a card named `Scene X`, with a **Running** or **Finishing** chip and its state ("Running", "Ending (outro)" or "Finishing: requested …"), where it came from ("Program Y", "activated directly" or "part of Z"), when it started, its cycle, its priority, the frames it protects, its frames with their health, and its child Scenes. With none, it reads "No Run is running." **Finish** (`POST /v1/operator/runs/{id}/finish`) asks for a natural end. **Cancel** (`…/cancel`) asks for confirmation, then stops the Run now, skipping its outro; its child Scenes stop too. Runs that ended in the last day are under a closed "Recently ended (N)", as Completed and Cancelled.

Below them, **Why each frame shows what it does** has one row per frame with two disclosures. **Why?** (and the Frame Inspector's Now-showing facet on the Wall) states **Central's plan** for that frame, for example "Central's plan for lobby-left: evening (priority 5, Program weekday-evenings) on top." Each layer underneath gets one sentence, always with its **priority N**:
- a lower priority: "morning (priority 1) is underneath: evening has priority 5.";
- the same priority: the Run Central **admitted later** is on top. This is admission order, not the Program's start time; Programs starting at the same instant are admitted in Program-id order;
- the same Run: the later child Scene is on top.

**Its limits are always shown.** If the winner has no usable media for this frame (none eligible, still preparing, or no compatible variant), Central plans the next layer down instead. An unbound frame gets no layers at all. A partly transparent or fading layer shows what is underneath. The panel reports what Central intends; it never says a frame is LIVE or confirms what a panel displays (R2). **Why nothing new?** is the next section.

### Why nothing new on a frame?

On Now showing, open **Why nothing new?** on the frame's row. It opens its own group beside Central's plan, **"Why nothing new on lobby-left?"**. It walks from intent to equipment; each step restates a served fact, and the first step that is not ok is marked **Stops here**. Fix that one first.

| Step | Stops when | What to do at that stop |
|---|---|---|
| 1. Intended? | No Scene is intended for the frame now. (If a Run on the frame ended, this step is only informational and the chain stops at step 2.) | [Show a Scene now](#now-showing-show-a-scene-now), or [schedule a Program](#schedule-a-program-step-by-step), that targets the frame. |
| 2. Run ended? | The last Run on the frame ended ("evening's Run ended at 18:00:30 after one cycle") or was cancelled. If the Scene keeps its last still, it adds "if its last item was a photo, the frame keeps that still (a video is not kept)". | "After one cycle" means Keep playing was off: edit the Scene and turn it on, then start it again. |
| 3. Authored? | The winning Scene uses fixed, hand-picked media ("new photos never appear by design"), or shows black by design. | Nothing is wrong. To show new photos, use a live-source Scene. |
| 4. The Source | None of the Scene's Sources is ok. | Read the [Source states](#the-media-pipeline) below. |
| 5. Check this frame | Press **Check this frame**. It reads each ok Source's items that fit the frame's shape and counts what Central would do with them: "12 usable · 3 still preparing · 1 failed to prepare · 2 with no compatible version". It stops on "Nothing usable yet: …" or "No item in the Source fits lobby-left's shape." | Still preparing: wait for the worker. Failed to prepare: read the worker line. No compatible version, or nothing fits: the frame's shape (for example portrait) excludes the Source's items; widen the Source. An item two Sources share is counted once; a failing Source is left out, as planning leaves it out. |
| 6. The worker | The worker is not ok. | See the worker line below. |
| 7. Frame health | The frame's health is not ok. | Fix it on the Wall ([wall health](#operator-console-wall-health-and-the-attention-strip)). |

**Limit:** the check is a count of each item's standing. It does not report which item Central picks for the next cycle; with some items usable and others not ready, a cycle that lands on one not ready plans the next layer down, as the step's note says.

### The media pipeline

The **Media pipeline** panel is at the foot of Now showing (it is left out while the Show now flow shows a step). Central fetches media from the photo library and prepares it; Players get it only from Central. All ages are on Central's clock.

**Worker.** One line:
- "checked in 40 s ago · preparing 3 · waiting 12 · failed 2 · failed, retry pending 1 · cache 4.1 of 8 GB" when it is healthy. Preparing is running or publishing; waiting is queued; "failed, retry pending" appears only when a failed job is waiting to be retried (planning treats it as failed until then). Only jobs of the **current preparation recipe** are counted; a recipe change fails the old recipe's queued jobs and planning asks for them again.
- "never checked in", "reported: storage is full" (or another reported error), or "quiet for 14 min" (no check-in for over 11 min; it checks in every 5 min) is an alarm, and a separate **Jobs and cache** line shows the counts. Check the worker process and its logs; for storage pressure, free space or raise the cache limit.

**Each Source** gets a row: its plain name, a **State**, the **Last refresh** counts ("found 800 · valid 790 · pending 4 · rejected 6"), any **Reported** diagnostic codes in words, and when it next refreshes. The State carries the Source's **filters** (media types, favourites, capture window such as "taken 2024" or "taken 1 Mar 2025 to 31 Mar 2025"):

| State | Reads | What to do |
|---|---|---|
| Awaiting refresh (to-do) | "Awaiting refresh" | New Source; wait for its first refresh, or press Refresh on its card in Photo sources. |
| Failing (alarm) | "Library unreachable", "Library refused access" or "Library unsupported", then "· last good 2 h ago" | Unreachable: check the library host and network. Refused: check the worker's library key and its permissions. Unsupported: check the library version. Hand-picked Scenes from this Source cannot be saved until it succeeds. |
| Overdue (alarm) | "Refresh overdue by 6 min" | Refreshes run every 30 s; check that the worker is running. |
| Nothing valid (to-do) | "nothing valid in the last refresh" | The query found no acceptable item: widen the filters, or read the Reported codes. |
| OK | "refreshed 1 min ago · 790 valid in the last refresh · only favourites · taken 2024" | Nothing. "Valid" counts items the refresh accepted, not items ready for a particular frame. |

### Scenes: make a Scene step by step

A Scene is a per-frame composition. Scenes lists every stored Scene as a card, then **New Scene**. The flow (`#/scenes/new/<step>`) asks:

| Step | Asks | Default |
|---|---|---|
| 1 Kind | **Live from a photo source** (each frame shows the Source's media as it changes, re-checked centrally) or **Hand-picked per frame** (you choose one item for each frame). It comes first because it decides whether step 3b is asked. | Live |
| 2 Photos | **Source**: a saved selection from your photo library. Or **New selection from your photo library**, which adds one [inline](#photo-sources-add-a-source) and brings you back with it chosen. | none: required |
| 3 Frames | The target frames, grouped **"Frames on `<surface>`"** and **"Frames not on any wall"**, each with its [health label](#operator-console-wall-health-and-the-attention-strip). | none: required |
| 3b Media per frame | Hand-picked only: one chooser per frame. | none: required per frame |
| 4 Playback | **Seconds per cycle**; under Advanced, **Keep playing until the Program ends**. | 30 s; Keep playing on |
| 5 Review | Every answer with Change; **Scene name**; under Advanced, its **Id** (derived from the name). Then **Save Scene**. | — |

- **Source readiness.** Photos and Review show the chosen Source's current refresh state and what it may mean for visible content. A Source that is awaiting refresh, failing, overdue or empty offers **Refresh Source** there; a 202 response means Central accepted the request, while the worker's later status shows its outcome. When the worker completes that requested refresh, the hand-picked candidate lists are read again once for the current Source and target Frames. **Manage in Photo sources** opens the Source list to edit filters or connection details, and the Scene draft stays in this tab for when you return. An older saved Source reference that is absent from the current list has no current status or inline refresh action. Live Scenes can still be saved while a Source is not ready; saving does not guarantee a panel has usable content. Hand-picked Scenes still require valid Source membership and compatible choices at save time.
- **Frames.** A legacy frame id containing `:` (or longer than 96 characters) is listed with the reason no Scene can target it, and cannot be ticked. A frame you ticked that is deleted while you draft is dropped and announced on whichever step shows ("lobby-left was deleted and removed from this Scene.").
- **Media per frame.** Each chooser lists **only media compatible with that frame's profile**: Central hard-filters the candidates by profile (`GET /v1/operator/sources/{ref}/candidates?frame_id=`), so an incompatible item cannot be chosen. While they load it reads "Loading compatible media…"; a frame with none is a problem routed to Frames; a failed read offers Retry. **Reload compatible media** rereads a successful list when a refresh outcome is unknown or another operator changes the Source. Each choice is labelled with its kind, size, capture time and what Central would do with it on that frame, for example "Photo 108×192 · taken 3 Mar 2025 14:02 · ready" (or "preparing", "failed to prepare", "no compatible version"); "(2)" is added only when two labels would otherwise read the same. A chosen item that is no longer among a frame's candidates is dropped from the draft, whichever step shows.
- **Save** stores the Scene and every per-frame choice in **one request** (`PUT /v1/operator/scenes/{id}`, or `…/authored` for a hand-picked Scene); Central re-checks Source freshness, membership and compatibility. The flow returns to the cards with "Saved Scene X." and offers **Show now** and **Schedule it**.

**Keep playing until the Program ends** is **on by default** for new Scenes (slice 3 Question 1, pending owner confirmation):
- **On.** In a Program, the Run keeps cycling until the window ends, then stops at the end of the cycle running at that moment, so it can **overrun the window by up to one cycle**. Shown without a Program, it plays until you Finish or Cancel it.
- **Off.** The Run plays **one cycle, then ends**: a 30 s Scene in an 18:00–20:00 Program ends at 18:00:30. Its Run card reads "plays one 30 s cycle, then ends". Scenes saved by earlier console versions were always saved this way.

### Viewing and editing a Scene

Each Scene card, `Scene X`, shows what feeds it ("live from `family`", or "authored: 3 chosen items"), its frames with their health, its cycle ("30 s per cycle, keeps playing until its Program ends or, when started by hand, until you Finish or Cancel it", or "plays one 30 s cycle, then ends"), the Programs that use it, and a **Running now** chip while a Run of it is live. Its actions are **Edit**, **Show now**, **Schedule it** and **Delete**. Saved revision numbers are used by Central to protect concurrent work, but are not shown on Scene or Run cards.

**Delete** confirms removal from future choices. It does not stop a Run or erase completed Run history. Central refuses deletion while a Program refers to the Scene, a live Run or queued activation has captured it, or another stored Scene embeds it. The refusal names the blockers; remove or edit those references first. The console checks that the Scene has not changed since its card was loaded. If the Scene being edited has unsaved changes, the confirmation says they will be discarded; a successful deletion closes that draft.

**Edit** is offered only when the console can save the Scene back **without losing anything**. A Scene written through the API with features the flow cannot author (child Scenes, an outro, fades, and similar) reads "Edit unavailable: Uses features the console can't author (child Scenes, outro, fades…)." instead; change that Scene through the API, since a save from the flow would silently drop those features.

To edit:
1. Press **Edit**. The flow opens at **Review** (`#/scenes/<id>/edit/review`), filled from the saved Scene: "Editing `evening`. Its name stays the same." To use a different name, make a new Scene. Back from Review goes to Playback.
2. Use **Change** for any answer. For a hand-picked Scene, each frame's stored item is pre-selected while it is still in the Source; a frame whose item left the Source has no choice, so pick again.
3. Press **Replace Scene**, then **Confirm replace** in the dialog.

**Stale: Reload.** When a refresh shows that someone saved the Scene since you opened the edit, Review reads "This Scene changed since you opened it. Reload it to review the latest saved Scene; Replace waits until you do." and Replace is disabled. **Reload** refills the draft from storage and names what changed ("Reloaded the latest saved Scene. Changed: Frames."), adding when it replaced your unsaved changes.

**What Replace changes.** It saves the new Scene definition for future starts. **Runs already going keep what they started with**, and so do activations already queued: each captured its Scene when Central admitted or queued it. Programs that start later, and new activations, use the saved changes. Replace does not touch Programs, Sources or other Scenes.

| The dialog ends | Means | What to do |
|---|---|---|
| "Scene evening saved." | Stored. | Nothing. |
| "This Scene was changed since you opened it; nothing was replaced. Review now offers Reload." | Someone replaced this Scene between two refreshes. Central refused yours (409 `scene_revision_conflict`), so their version stands. | Close; focus moves to **Reload**. Reload, then redo your change if it still applies. |
| "Not replaced. The Source's last refresh failed; authored choices can be saved once it succeeds." | A hand-picked Scene's Source is failing. | Fix the Source (see [the media pipeline](#the-media-pipeline)), Refresh it, then try again. |
| "Not replaced. That item is no longer in the Source; choose again." | A chosen item left the Source. The choosers reload. | Pick again and Replace. |
| "Central did not answer. Check this after the next refresh." | Central answered with a server error, so the save may or may not have been stored. | After the next refresh, review the Scene card and its saved answers. An identical retry is safe if the outcome is still unclear. |

Pressing Replace again with exactly the same Scene after an outcome you did not see is safe: Central accepts an identical save of the stored revision.

### Schedule: a Program step by step

A Program binds a Scene to **one time window** with a **priority** (`PUT /v1/operator/programs/{id}`; `DELETE` removes it). Schedule lists the Programs as cards, then **Schedule a Program**; **Schedule it** on a Scene's card, or after saving a Scene, opens the flow with that Scene chosen. The flow (`#/schedule/new/<step>`) asks:

| Step | Asks | Default |
|---|---|---|
| 1 Scene | **Scene** | the Scene you last saved or picked |
| 2 When | **Window start** and **Window end**; under Advanced, **Repeat on** and **Number of windows** | none: required; every day; 1 |
| 3 Review | Every answer with Change; **Program name**; under Advanced, **Priority** and the **Id** (derived from the name). Then **Schedule Program**. | priority 0 |

**Times are in your browser's time zone.** The page, When and Review name it ("Times in Europe/London"); if the browser and the wall are in different zones, that label is the only warning. The flow refuses a window that ends before it starts, a window that has already ended (Central would record it as missed), and a priority that is not a whole number.

**A handed-over Scene never replaces your changes.** If you press Schedule it for another Scene while a Schedule draft holds changes, the Scene step keeps your draft and offers "Schedule Scene X instead".

**Separate windows.** For a repeating show, open **Advanced** on When:
- **Number of windows** is 1 by default, which schedules one Program under its id. Any other number, 2 to 60, creates that many **separate Programs** `<id>-1`, `<id>-2`, …, and the final button becomes **Add separate windows**. A number outside that range is a reason on the field, never silently reset.
- **Repeat on** is a weekday mask; every day is ticked by default. It is used only with more than one window.
- Window 1 is the window entered on When. Each later window falls on the **next ticked day at the same local clock times**, so a daylight-saving change keeps 18:00 at 18:00. Review lists every planned window.
- Each window is a **real, separately stored Program**, which you manage and remove individually. There is **no stored recurrence rule**, and no control implies a living recurring schedule (slice 3 Q2).
- When's Continue refuses windows that would overlap ("Each window must end before the next starts." — for example, a window longer than the day spacing). Review refuses a window id that already exists, or an `<id>-<n>` longer than 128 characters.
- If some windows fail, the flow stays on Review and the status lists them as **not created** (Central refused them) or **not confirmed** (a request that failed or did not complete may still have been stored). Pressing **Add separate windows** again sends only the windows Central does not yet list.

**Removing a Program.** **Remove** on an upcoming or past card sends the removal immediately. The card reports whether Central accepted it, refused it, or gave an unknown outcome; after a refusal or unknown outcome, review the current Program state before retrying. If Central accepted removal but the refreshed list has not caught up yet, the card says it is waiting for the list to update. Removing a Program that is **running now**, or whose window has started, asks for confirmation: its Run is asked to finish at the end of its current cycle, after any outro, and later windows stay.

**Editing an upcoming Program.** Use **Edit** on its card to correct the Scene, window or priority while its window is still in the future. The editor opens at Review with the saved answers filled in. Its Program id stays the same, and a Program created by the separate-windows helper is edited as one independent window. A saved time in the later occurrence of a repeated daylight-saving hour remains editable: When and Review identify the saved occurrence with its zone and UTC offset. Leaving that time field unchanged preserves the exact saved instant; changing it uses the browser's local-time interpretation. Save compares the exact Program you opened with Central's current Program, so another operator's change or removal is refused rather than overwritten. Reload the current Program and review your edits if it changed. Central also refuses the save if either the old or proposed window has started by the time the write reaches it. An already started Program's Run keeps its admitted snapshot; use the Run controls for live changes.

### Reading Program states

Each Program card, `Program X`, shows its Scene, its window in local time, its priority and one state, read from what Central served; Running, Refused and Missed also carry a chip. Times are the Run's, never the window's.

| State | Reads | Means |
|---|---|---|
| Upcoming | "Starts in 2 h · Tue 2 Mar 18:00–20:00" | Its window has not started. |
| Running | "Running since 18:00" | Central admitted its Run and it is live. |
| Ran | "Ran 18:00–18:00:30 (one cycle, then ended)", or "Cancelled at 19:10" | Its Run ended. "(one cycle, then ended)" marks a Scene with Keep playing off. |
| Refused (alarm) | "Did not start: lobby-left was protected by the Run of evening." | Another Run protected one of its frames when the window started. The Run named is the one Central recorded as blocking it; if that Run is no longer listed, the card says "…protected by another Run, no longer listed." A Scene that protects a frame covered by a higher-priority Run reads "Did not start: it protects frames that a higher-priority Run of evening covered." |
| Missed (to-do) | "Missed: its window had ended before Central first scheduled it." | Only two cases: the Program was saved after its window ended, or its window ended before Central's very first scheduler tick. |

**A warm restart is not a miss.** If Central was down during a window, it catches up logically when it comes back: it admits the Run and ends it as the plan would have. The card reads "Ran" even though the wall showed nothing, and its hint says so. "Ran" describes Central's plan, never what the panels showed. Past Programs sit under a closed "Past (N)". Central serves Runs and outcomes for one day, so older cards read "details older than a day".

### Photo sources: add and manage a Source

The page opens with: "Photo Wall selects media that lives in your photo library. It never uploads, edits or deletes anything there." A Source is a **saved live query** with a plain name (for example, `holiday`): never a downloaded album and never something a Player browses or opens; its membership is re-evaluated centrally. Each Source is a card with its status, its last successful refresh ("Awaiting refresh" before any attempt, or "No successful refresh" after a failed first attempt), a short issue when refresh failed, what it includes and its connection, and **Refresh**, **Edit** and **Delete**. Refresh re-runs its query (`POST /v1/operator/sources/{ref}/refresh`). The card reports whether Central accepted or refused the request, or whether its outcome is unknown. An accepted message is a request receipt; the Source status and issue remain the authority on the latest completed worker refresh. Central manages internal Source revisions; the console does not ask you to number them.

Press **New source**. The flow (`#/sources/new/<step>`) asks:

| Step | Asks | Default |
|---|---|---|
| 1 What to include | **Media type** (Images and video, Images only, Video only); **Favourites** (Any, Only favourites, Not favourites); **Taken from** and **Taken until** | Images and video; Any; no dates |
| 2 Name | **Source name** (like `holiday`); **Connection name**, as the rule below says | none: required |
| 3 Review | Every answer with Change. Then **Save source**. | — |

- **The capture window.** Taken from and Taken until are local days. The window runs from the start of the "from" day **up to the start of** the "until" day (the "until" day itself is excluded), so "Taken until" must be after "Taken from". Either may be left empty.
- **Preview matches.** On What to include, choose the configured connection and press **Preview matches** to check the current filters before saving. The result counts matching photos and videos observed by the media worker; it does not promise they are prepared or suitable for a particular Frame. An empty completed preview means no items matched. A pending, unavailable, permission, incompatible, or limit result is not an empty selection. Change filters and preview again to check the new selection. Previewing does not save a Source or start a refresh.
- **The connection rule.** Connection name is the exact `connection_id` provisioned in the private worker connection file; choosing it does not set an Immich URL or API key. Once the worker reports its configuration, the console offers those names even before any Source exists. One configured name is filled in under **Advanced**; several names appear in a chooser. An empty reported list tells you to provision the worker first. If the worker has not reported a list yet, the form explains the uncertainty and permits entry of an already configured name for an older worker. If a saved Source names a connection removed from the worker, edit it to choose a current name before saving. A worker check-in confirms configuration, not Immich reachability or key permissions. A 403 on Immich `GET /users/me` requires checking the worker key's `user.read` permission and configured owner; the key also needs `asset.read` and `asset.download` for search and originals.
- **Save** writes `PUT /v1/operator/source-names/{name}` and returns to the cards with "Saved Source holiday.". A new Source starts its first refresh automatically when the worker queue is available; its card may briefly say "Awaiting refresh".
- **Edit** opens the saved Source at Review. Use **Change** to revise its name, filters or connection, then **Save changes**. Central makes the next internal revision, updates stored Scenes that use the Source for future Runs, and keeps already admitted Runs on their prior selection. If another operator changed the Source while you were editing, Reload the current version and review it again.
- **Delete** asks for confirmation and removes the Source from new selections. If a Scene still uses it, the delete is refused with the Scene names; edit those Scenes to choose another Source first. Deleting a Source never deletes photos from Immich.
- **From a Scene.** **New selection from your photo library** on the Scene flow's Photos step opens this flow for your Scene: "This photo source is for your Scene. Saving it takes you back there, with it chosen." Save returns to the Scene's Photos step with the new Source chosen. Back on the first step, or **Discard and return to your Scene**, returns with nothing chosen. If you had gone to another page by the time the Save lands, the Scene draft only takes the new Source; if the Scene draft was discarded meanwhile, the Source is simply saved.

**Albums are not supported:** a Source has no album filter. There is deliberately **no** album, "open in the library" or credential field anywhere in the flow (the media boundary, design decision D-e in the [console design](operator-console-ux-design.md)); the library key is provisioned into the worker out of band (see [Connecting a real media library, in the README](../README.md#connect-a-real-media-library-immich)).

## Tests and local development

Install the free `uv` Python package manager, then:

```sh
uv sync --frozen
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
python3 scripts/check_docs.py
```

The portable command reports PostgreSQL integration tests as **skipped** unless `PHOTO_WALL_TEST_DATABASE_URL` is set. To run all tests, start the disposable test database and run the suite through the wrapper:

```sh
docker compose -f tests/integration/compose.test-database.yml up -d --wait
.venv/bin/python scripts/test_local.py -q -n auto
```

The [test database](../tests/integration/compose.test-database.yml) runs the deployment's pinned PostgreSQL image on loopback port 54330 (`PHOTO_WALL_TEST_DB_PORT` overrides it), in tmpfs with every durability setting off; it holds only throwaway databases and a fixed test-only credential, and `docker compose -f tests/integration/compose.test-database.yml down` discards it. The wrapper points `PHOTO_WALL_TEST_DATABASE_URL` at it unless that variable is already set; any server where the user may `CREATE DATABASE` serves. Keep it away from deployment data. A [template database](../tests/support/database.py) holding every migration is built once per migration set, under a name derived from the migrations and their runner, and each database test runs in its own clone (`pw_t_*`), dropped after the test; a later run drops clones a crashed run left behind once they are an hour old. Nothing touches a deployment database.

**Test tiers.** `tests/conftest.py` gives every test exactly one tier: `browser` for `tests/browser/`, `db` for any test whose fixtures reach the database (all of them build on `database_provisioner`), and unit for the rest. Select a tier with `-m db`, `-m "not db and not browser"`, or the `tests/browser` path, and parallelize with `-n` ([pytest-xdist](https://pytest-xdist.readthedocs.io/)); `-m db` needs `--dist loadgroup`, which keeps a module sharing one `module_registry` on one worker. Two rules keep a tier from silently losing tests:

- With `PHOTO_WALL_TEST_REQUIRE_DATABASE=1`, a test that reaches a database fixture without `PHOTO_WALL_TEST_DATABASE_URL` fails instead of skipping. Every CI job sets it, so a database test that escaped the `db` tier fails the unit job.
- Under `CI`, a skip whose reason no entry of `CI_SKIP_ALLOWLIST` (tests/conftest.py) owns fails the run. Each entry names a capability that the CI job running the test deliberately lacks (FFmpeg, root, `dtc`, a locally built image, a fork's missing token, the opt-in dpkg-deb build). Add one only with that justification.

A test that observes a lock wait counts only its own database's waiters through `support.database.waiting_backends`; `pg_locks` is cluster-wide, so an unscoped count also sees other workers' tests. `tests/test_lock_observation_scope.py` refuses unscoped queries of `pg_locks` or `pg_stat_activity`.

CI runs the [checks](../.github/workflows/checks.yml) as parallel jobs: `static` (ruff, import contracts, documentation links), `unit` and `db` (four xdist workers each), `browser` (below), `image-smoke` (builds the central and media worker images, starts Compose and checks central HTTP health and the served console), and `linux-media`, which runs all preparation tests inside the pinned Linux worker image, so missing host FFmpeg cannot silently remove that gate. Each tier job's timeout is about twice its expected time and it prints its 25 slowest test phases. The Immich adapter checks and the two fault segments of the two-Player/three-Output wall scenario run as three parallel jobs of the [software e2e](../.github/workflows/software-e2e.yml) (see the [wall demo](module-wall-demo.md)). Passing CI does not establish physical Pi/PXE, rendering or visible timing.

All CI worker builds reuse the architecture-matched native media base through
the [shared dependency workflow](module-appliance-ci.md#shared-service-and-test-dependencies).
Application edits do not permit a missing OS dependency to be rebuilt. To
prepare an unchanged missing definition explicitly, dispatch `service-base.yml`
with its architecture and `prepare_base=true`. Fork runs require the definition to have
been published by a trusted run. Ordinary local Compose builds retain their
explicit cold native target.

The real-browser walkthroughs of the [binding and calibration](../tests/browser/test_operator_binding_browser.py) and [showrunner content](../tests/browser/test_operator_showrunner_browser.py) surfaces drive the production React operator console (served at `/`, aliased at `/console`) and its HTTP API against their own disposable PostgreSQL databases. The shell, the look and each step flow have their own walkthroughs (`tests/browser/test_console_*_browser.py`, `test_scene_flow_browser.py`, `test_source_flow_browser.py`, `test_schedule_flow_browser.py`, `test_show_now_browser.py`), built on the task-level helpers in `tests/browser/console_tasks.py`. The console's pure modules run under Node in ordinary pytest (`tests/test_console_flow.py`, `test_console_schedule_flow.py`, `test_console_show_now.py`); without Node they skip on a developer machine but fail under `CI` or `PHOTO_WALL_BROWSER_TESTS` (the route round trip in `test_console_routes_r4.py` only skips). Install the locked development dependencies and their matching Chromium build, then run:

```sh
uv sync --frozen
.venv/bin/python -m playwright install chromium
PHOTO_WALL_BROWSER_TESTS=1 .venv/bin/python scripts/test_local.py -q tests/browser -n 4 \
  --browser chromium --tracing retain-on-failure --output artifacts/operator-browser
```

Pass `tests/browser` as the path even under `-n`: its conftest gathers the evidence report from every worker's test reports in the process that writes it, and that process loads the conftest only for a path argument.

CI runs these checks in the official Playwright Python 1.62.0 Noble container,
pinned by digest in `checks.yml`. That image already contains Chromium and its
Linux dependencies, so CI does not run a browser APT installation. A separate
container environment installs the repository's frozen Python dependencies and
connects to the job's disposable test database; CI requires the report to say
`passed`. Reports and failure traces are
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


## Node control

The V2 node composition uses the same database, cache layout, dependency lock, and
content worker as Central. Deploy Central and its worker from the same revision
so both know migrations 053–054 and the sealed-environment job kind. Keep the
ordinary `PHOTO_WALL_DATABASE_URL` and `PHOTO_WALL_ADMIN_TOKEN` configuration in
its existing protected deployment settings. No token is placed in a command line.

Node control is the one supported configuration. Set an installation-specific
`PHOTO_WALL_NODE_AUDIENCE` and run Central with the node factory:

```sh
uvicorn central.node_app:create_app --factory --host 0.0.0.0 --port 8000 --ws-max-size 1048576
```

The ordinary `central.app:create_app` factory (still the default image and Compose
command until the [V1 follow-up](player-fleet-implementation-map.md) switches them)
keeps node transport disabled. That is a **misconfiguration**: Players that boot by
node path are refused, and the console shows one banner ("Node management is off on
this Central") above every page. Each Pi must also boot by node path: its kernel
command line must carry `photowall.node=v2`. A Pi without it boots by the deprecated
path, and its Player page shows one warning line; Select and Stage do not reach it.
The release default command line does not carry the flag yet, so add it per Pi (for
example through iac `cmdline_extra`). The node factory enables observation, explicit session enrollment, immutable V2 boot
offers and scoped command routes; it does **not** open the durable effect gate.
`/healthz` remains process/service health. Authenticated
`GET /v1/operator/node/status` reports transport selection and the persistent gate
state separately. A running HTTP server, accepted serial claim, stored sample, or
catalogued artifact is not command qualification, authenticated physical identity,
verified downloaded bytes, or observed pixels.

Publish a release and select its deployment from the console
([Releases](#releases-stage-app-and-update-the-wall)); Stage and qualification are on
the Player page, and the Update the wall journey composes them. Under the hood, Publish
creates an immutable deployment from a catalogued release and Select sets the boot
policy with a revision compare-and-set. The base release must already
have exact catalog provenance; manager primary and any accepted fallback are
pinned to that base digest. Environment sources feed the existing content worker.
A missing byte artifact is reported unavailable until the worker acquires and
verifies it. An explicit no-app deployment still boots the independent base.
Only the V2 cohort (`photowall.node=v2`) uses these frozen offers; it cannot silently
fall back to a legacy manifest.

For ambiguity, `GET /v1/operator/node/devices/{device_id}` separates current and
historical scoped credentials, observation sample/receipt ages, reboot requests,
responses and effect evidence. Each reboot request carries `outstanding`: true while
it is unexpired on Central's clock and has no `rejected` response, evaluated at the
read's own time. `POST …/reboots` refuses a new command id with 409
`node_reboot_outstanding` while another request on the same session is outstanding;
a retry of the same command id is unaffected. The read also serves
`display_outputs`: for the current boot admission, Display Host's newest display
exchange per Output (`output_id`, `received_at`, `connected`, the admitted
`surface` or null, and the compositor `receipt` with `matches_surface` and
`age_ms`, measured on one producer's clock), never an earlier boot's. The latest boot to enroll is the current boot: it
supersedes the prior boot and revokes that boot's sessions, so the operator never
chooses which boot of a box is current (the fleet-wide boot selection above chooses
what every boot is offered). Two Pis claiming one serial flap visibly, each enrollment revoking
the other. This does not prove which physical Pi exists or that an
already-delivered effect stopped. Effect rollout still requires the
existing D17 all-serving/rollback certification and a real injected serving-image
verifier; there is no environment-variable bypass. The automated node scenarios and
their limits are listed in [validation](validation.md).


### Optional read-only Kubernetes node verifier

The node factory accepts `PHOTO_WALL_NODE_VERIFIER_CONFIG`, the path to a
read-only deployment-owned JSON file. Its exact keys are `identity_directory`,
`namespace`, `ci_record`, `guard_record`, `ci_public_key`, `guard_public_key`, and
`audience`. Public keys are distinct raw Ed25519 keys encoded as hex. The identity
directory provides `namespace`, `pod_name`, `pod_uid`, `container_name`,
`deployment_name`, and `deployment_uid`; populate Pod identity through the
[Kubernetes downward API](https://kubernetes.io/docs/concepts/workloads/pods/downward-api/).
The reader checks the actual Pod owner chain and registry-qualified
[`status.containerStatuses.imageID`](https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/pod-v1/).
A mutable image tag or runtime config hash is insufficient.

The service-account reader uses verified Kubernetes TLS and GET only. It needs
complete Pod/ReplicaSet/Deployment, Service/EndpointSlice, Ingress/NetworkPolicy,
API discovery and supported Gateway resource inventories, plus the two named
public evidence ConfigMaps. It never reads Secrets. Unknown custom API groups,
unsupported route resources, forbidden lists or incomplete pagination refuse
certification. This conservative adapter needs extension and corresponding tests
before using a cluster with other routing controllers.

Each ConfigMap contains `data["evidence.json"]` with exact `payload` and hex
`signature` fields. Sign canonical sorted compact JSON after the domain prefix
`photo-wall-rollout-ci-v1` or `photo-wall-rollout-guard-v1`, each followed by a NUL
byte. The source `SignedRolloutEvidence` defines the exact payload fields. Both
records bind audience, record UID, increasing generation, active/revoked state,
issue/expiry times (at most 300 seconds). CI binds all exact serving and rollback
image digests plus immutable compatibility, fence and readiness evidence hashes.
The guard binds Deployment UID/generation, complete endpoint/route hashes, CI
payload hash, exact image sets, and an irrevocable `mutation_not_before` equal to
its expiry. Revocation blocks new observation/admission but cannot shorten this
promised no-mutation interval. The durable local watermark records revocation
before returning refusal, and rejects earlier signed active records after restart.
Key rotation or ConfigMap replacement fails closed and requires explicit operator
reprovisioning; deleting replay floors is not a normal recovery action.

**External implementation dependency:** the separate IaC owner must implement
and deploy a controller that closes the durable effect gate before changing any
certified direct Service/Pod/Gateway path, waits out outstanding signed holds,
prevents uncertified rollback, and retains fenced images while any effect remains
unreconciled even after certification expiry. It must produce truthful exact-image
CI matrices and signed guard records from those enforcement results. The adapter
and signatures do not implement that controller. No real such controller or
qualification is established by the local tests. An unconfigured or uncertified
node factory therefore continues to serve observations with effects closed.
