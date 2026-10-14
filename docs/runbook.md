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

The operator interface lists Players and Outputs, creates persistent Frames, binds equipment, retires a Player, and sets each Frame's position and brightness on its page (live on the Display, then Done). It creates and edits Sources and Scenes, schedules Programs, starts/finishes/cancels Runs, and shows source/worker health. Program timestamps use the browser's displayed local time zone. Connect Immich before creating a Photo source ([connecting Immich](#connecting-immich)): a deployment sets it in Central's environment, and Settings › Photo library takes a pasted one with no file and no restart. The disposable browser walkthrough below covers these controls; final-revision delivery evidence remains separate. With the scheduler enabled, `/healthz` is green only when the database is reachable and a scheduler tick completed successfully within the last 10 monotonic seconds; `starting`, `coordination_unavailable`, `stale`, and `stopped` states return 503 with fixed sanitized status fields. Explicit test mode can disable the scheduler and retain database-only health semantics. A green `/healthz` reports service liveness, not observed presentation.

When a calibrated Frame shows a current readiness failure, follow the
Frame-specific guidance on its Frame page: reduce concurrent video/effect work
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

The worker needs no connection configuration: connections are rows in Central's database, and it remains healthy while none exists. Its Compose healthcheck is the module entry point `python -m media.healthcheck`, which composes the repository through the worker's own `build_repository` and exits 0 only when the last worker check-in (`media_settings.worker_seen`) is fresh. The age is read on the database clock in one read-only transaction that takes no media lock ([one media clock](module-media-worker.md#one-media-clock)), so the probe container's clock is never compared with a database time. A missing database URL, an unreachable database, or no check-in yet exits 1. Freshness follows the worker's guaranteed check-in cadence, not the refresh tick: an idle worker (no Source due) checks in only at boot and at each 5-minute maintenance pass, so the window is the console's worker-quiet window (two maintenance intervals plus a minute, 660 s; a test pins the two equal). The cost: a hung worker can read healthy for up to 11 minutes. Compose consumes worker health only for `--wait`; nothing restarts an unhealthy worker. Never write a healthcheck as an inline `python -c` snippet that imports first-party code; `tests/test_compose_healthchecks.py` refuses one. Its configuration is described in [the worker module](module-media-worker.md). Never put an Immich API key in Source definitions, Player configuration, the repository, logs or command-line arguments; its ways in are the deployment's secret (`PHOTO_WALL_IMMICH_API_KEY`, [the deployment's Immich](#the-deployments-immich)) and the key field of Settings › Photo library. The [Immich fixture](module-immich-fixture.md) generates its own synthetic media and disposable keys for reproducible tests.

### Connecting Immich

A deployment sets its Immich in Central's environment ([the deployment's Immich](#the-deployments-immich), below); Settings then shows it read-only. For local and development use, and for a second Immich user, paste a connection in Settings instead:

Open **Settings › Photo library** (`#/photo-library`) and press **Add a connection**. Enter a name, the Immich address (for example `http://immich.local:2283`; Photo Wall appends `/api` when it is missing, and plain `http` is accepted) and an API key made in Immich (Account settings › API keys) with the [permissions below](#the-library-key). **Save** stores the connection and asks the media worker to check it at once: within about a minute the row reads Immich's own counts ("41,203 photos · 2,110 videos · 212 albums · 96 people") and the Immich user the key belongs to. These are Immich's totals, archived photos included; a pool never includes archived photos, so a Source of everything can show fewer photos than this count. No file and no restart: the worker picks a new or changed connection up at its next task.

- **The key is never shown again.** After Save the row reads only "Key ending …1234". To change it, press **Paste a new key**; leaving the key empty while you rename or re-address the connection keeps the stored one. The key is kept in Central's database, written through the API and never read back ([decision 0020](decisions/0020-immich-key-in-central-database.md)): a database backup holds it, so treat backups as secret.
- **Check now** asks the worker to check the connection again; it also checks every 15 minutes on its own.
- **Remove** is refused, in plain words, while a Photo source or an unsaved draft uses the connection.
- **A connection belongs to the Immich user of its first passing check.** Pasting a key of another Immich user reads as a problem (below); a second Immich user is a second connection.
- **A private certificate authority** must be in the media worker's system trust store; there is no per-connection CA file.

#### The deployment's Immich

Owner, 2026-10-12: "The immich url and api key should be injected by iac"; he chose read-only in Settings. Central reads two settings from its environment at every start and keeps one connection, marked as set by the deployment, in step with them (twelve-factor configuration; shown read-only like Grafana's provisioned data sources). The rule's one home is `central/photo_library/provisioning.py`.

| Variable | Holds |
|---|---|
| `PHOTO_WALL_IMMICH_URL` | The Immich address, as Settings takes it (`http://immich.local:2283`; `/api` appended when missing) |
| `PHOTO_WALL_IMMICH_API_KEY` | The API key, with the [permissions below](#the-library-key) |
| `PHOTO_WALL_IMMICH_API_KEY_FILE` | Instead of the key itself: the path of a file holding it (a mounted secret). Set one of the two, never both |

- **In the owner's deployment** (`mcurcio/iac`) the key is already the `photo-wall-immich` Secret's `api_key` (from 1Password); a pull request there gives the `central` container `PHOTO_WALL_IMMICH_URL` as a plain value and `PHOTO_WALL_IMMICH_API_KEY` from that Secret by `secretKeyRef`, never on a command line. With Compose, put both in `.env`; `compose.yaml` passes them to `central` (empty means unset).
- **Settings shows it read-only:** "Set by your deployment. Change its address or key there.", with its health, Immich's counts and **Check now**, and no Change, Paste a new key or Remove. The operator API refuses to change or remove it (409 `connection_managed`). A key problem's fix reads "Set a new API key in your deployment; Photo Wall uses it from its next start." The key is never shown, as for a pasted one.
- **Changing it** means changing the deployment and restarting Central: a new key or address updates the same connection in place and checks it again; it keeps the Immich user of its first passing check, so a key of another Immich user reads as a problem, as in Settings.
- **The first start** takes over an existing connection rather than adding a second: one with the same key, else the same address, else the oldest one still waiting for its address and key since the delivery 4 upgrade (below). It keeps that connection's Sources and pools, and its name unless the name is still its internal ref (the upgrade's rows, such as `immich-main`): that one is renamed "Immich" at the next start, since a managed connection cannot be renamed in Settings. With none, it adds one named "Immich" ("Immich 2" when that name is taken).
- **Removing both variables** makes it an ordinary connection at the next start: it keeps its address, key, Sources and pools and can then be changed in Settings. Nothing is deleted. Setting them again takes it back.
- **Only one of the two** (or both key variables, an unreadable or empty key file, an address Photo Wall refuses) is an error in Central's log at start, "Immich connection from the environment not applied: …", which never names the key. Central still starts, and the connections stay as they were.

**Needs a look.** Each row carries one state: **Connected** (with when it was checked), **Checking** (not checked yet), **Paste the address and key** (a row waiting since the upgrade, below) or **Needs a look**. A connection whose check fails shows a card, on the page and on **Needs attention**, with one fix. **Photos keep playing** while it does: each Source's last pool stays on the wall, and new photos wait until the connection is healthy again.

| Card | Fix |
|---|---|
| "Immich rejected the API key." | **Paste a new key** |
| "The key cannot do everything Photo Wall needs. Make a key with these permissions: …" | Make that key in Immich, then **Paste a new key** |
| "Immich 3.0.0 is older than 3.1.0. Update Immich, then check again." | Update Immich, then **Check again** |
| "Photo Wall cannot reach Immich at <address>" | Check the host and network, then **Check again** |
| "Immich answered in a way Photo Wall does not understand." | **Check again**; **Details** shows the code |
| "This key belongs to <found>; the connection belongs to <expected>" | **Paste a key for <expected>** |

Two notes are shown without stopping anything: "Immich 2.7.0 is newer than Photo Wall has been tested with; it keeps working", and the full-size-copies note (Immich keeps full-size copies only when its image settings say so). Only RAW needs them: Photo Wall reads JPEG, PNG, WebP, HEIC and AVIF originals itself and fetches an edited photo's full-size copy, which Immich always keeps, so with full-size copies off only an unedited RAW photo plays at Immich's preview size on a Frame larger than the preview. Turn on full-size copies in Immich to show RAW photos in full.

**After upgrading to delivery 4.** The private connections file, its `connections` volume and `PHOTO_WALL_CONNECTIONS_FILE` are gone. Each connection name in use becomes an empty row on Settings › Photo library reading "Paste the address and key of this Immich to start new photos again"; existing Photo sources keep pointing at it. Until that row has its address and key, no new photos play and Frames keep their last photo. A deployment gives it them through [the deployment's Immich](#the-deployments-immich): Central's first start with both variables fills that row (it keeps its name and Sources) and checks it; pasting them in Settings is the way without a deployment. Existing Sources are converted to the new queries: a Source that asked for non-favourites only now selects all photos, and every Source now leaves out screenshots, so a pool may shrink. A photo placed by hand on a Scene before the upgrade keeps playing and can still be prepared; if it was edited in Immich since, its preparation fails and you pick it again ([authored media](module-authored-media.md)). **The owner's deployment:** the worker environment in the infrastructure repository (`mcurcio/iac`) still names the connections file and mounts its volume; a follow-up pull request there removes both and sets `PHOTO_WALL_IMMICH_URL` and `PHOTO_WALL_IMMICH_API_KEY` on `central` from the same Immich address and `photo-wall-immich` Secret the file was made from, and the owner merges it once this release is deployed. The old private file can then be deleted with the deployment's secret-management procedure.

### The library key

Make the key in Immich with exactly these permissions and no upload, edit, delete, share or administration permission. The list has one home in code, `central/photo_library/policy.py` (`REQUIRED_PERMISSIONS`); a key missing any of them reads "The key cannot …" with the missing ones listed, and its connection needs a look until a key with all of them is pasted.

| Permission | Used for |
|---|---|
| `user.read` | Which Immich user the key belongs to |
| `asset.read` | Searching Immich for a Source's photos, and its places |
| `asset.statistics` | The photo and video counts, and a Source's "up to N" |
| `album.read`, `album.statistics` | The album list and count |
| `person.read` | The people list and count |
| `tag.read` | The tag list |
| `asset.view` | Thumbnails and Immich's previews, which preparation fetches when they cover the wall's largest Frame |
| `asset.download` | Full-size copies and videos for preparation |
| `asset.edit.get` | Reading a photo's Immich edits, so an edit makes a new revision |
| `face.read` | Where faces are in a photo (read by delivery 5a's photo fit) |

What each feature reads is in the [media module](module-media.md#connections-and-health). Never put the key in a command argument, a log or a committed file.

In the Scene form, keep live source selection for a changing collection, or
select **Choose a photo or video for each Frame** to keep a specific current
asset for each participating Frame. Each chooser lists only media compatible
with that Frame. The Scene and its references save together; pool membership
and compatibility are rechecked centrally. Playback still requires
prepared media.
The [authored-media contract](module-authored-media.md) describes retention and
capacity. Development checks for this form require Node.js and execute its
event flow with a synthetic DOM; this is separate from the authenticated
browser walkthrough.

For immediate activation, choose a priority and whether an already active Scene
should be ignored, restarted, or queued. Queue requests require an expiry;
**Force** is an explicit override. Queued and ignored requests report their
actual outcome without claiming that a new Run started.

`PHOTO_WALL_HORIZON_SECONDS` defaults to 300 seconds and `PHOTO_WALL_RENEWAL_SECONDS`, the plan renewal quantum (at most 60), to 30; the [wall demo](module-wall-demo.md) shortens both. In the scheduler's PostgreSQL transaction, the media repository records each bounded preparation request and defers its exact job ID through Procrastinate. The separate worker publishes verified derivatives into the `media` volume. Central mounts it read-only and serves exact authorized bytes; Players never receive an upstream URL or credential. Procrastinate owns dispatch, retry timing, and queue-worker liveness. Photo Wall schedules only domain preparation, Immich connection and pool checks, and publication/storage maintenance tasks; it retains publication recovery, reservations, and stale-attempt fencing. The worker has a read-only runtime, a private writable media volume, and container CPU/memory limits.

Fresh key-proof enrollment uses `/v1/enrollment/challenge` plus `/v1/enrollment/register`. The single-process Player entry point is `python -m player.service --config /etc/photo-wall/public.json`; see [service configuration and runtime requirements](module-player-service.md). The Player derives its boot context from the Pi's serial and a fresh boot id, creates a new process key, says hello on its control session before Central answers any state, and receives a new central authority epoch. A recognized returning equipment observation restores central bindings; unknown equipment remains unbound. `/v1/player/time` supplies independent authenticated clock samples. Base images are fetched through the node boot offer (`/v2/node/boot-offers/{offer_id}/artifacts/{role}`, by content hash). Physical Pi/PXE and complete current-image qualification remain pending. Startup-only DRM discovery currently requires a Player restart after connector topology changes.

The `database` volume persists all authoritative state, including Procrastinate jobs and release/trial records. The `cache` volume holds the cache root; its `media/` subdirectory holds central authoritative media blobs and preparation files ([decision 0013](decisions/0013-unified-cache-root.md)). There is no `connections` volume any more: Immich connections, keys included, are database rows. Central and worker startup apply forward Photo Wall SQL migrations under a database advisory lock and check hashes of already-applied migrations; they also install/upgrade Procrastinate's versioned schema. Do not edit an applied Photo Wall migration; add another numbered migration. Both containers run as an unprivileged user. The exact Python dependency graph is in `uv.lock`.

## Player provisioning: netboot and enroll

A Player has one way in: it netboots. There is no flashed image, no boot ticket, no pre-registration and no mDNS discovery; the kernel command line names Central (`photowall.central=…`, [step 2 below](#player-provisioning-stage-the-netboot-bundle-and-read-its-console-0014)).

1. **Netboot the Pi** on the same trusted LAN as Central. Stage 1 locates Central from the command line and mounts the base; the Player app root and the App Manager root arrive as squashfs images through [Node control](#node-control). Nothing installs a `.deb` on a Node: the 0009 bootstrapper, `photo-wall-provision.service`, the OS agent, Central's `/v1/app/*` and `/v1/netboot/*` routes and the Player `.deb` are gone ([0019](decisions/0019-debian-packaging-with-debhelper.md)).
2. **Watch the pending queue.** The Player derives its `device_id` from its own hardware serial and enrolls. It appears in the operator inventory (`/v1/operator/inventory`, or the console's Hardware list) as **unbound**; no prior registration is required.
3. **Bind** the pending Player's Output to a Frame from the operator interface, the same control used for any equipment change, then calibrate it.
4. **Unbind** (`DELETE /v1/operator/frames/{frame_id}/binding`) releases a Frame's binding without retiring the Player: the serial can be re-bound later, to the same or a different Frame. This is distinct from **retire**, which is permanent and refuses the serial's re-enrollment.
5. **Reboot** of an already-bound Pi re-associates with its Frame automatically by serial; no operator action is required unless the binding itself needs to change.

Physical Pi boot of the netboot tree is not yet hardware-qualified.

## Player provisioning: stage the netboot bundle and read its console (0014)

[Decision 0014](decisions/0014-reaching-central-from-every-boot-stage.md) changes how the netboot's first stage reaches Central and how it stays alive. None of this is hardware-qualified yet. Its first Pi observations (the M0 and M1 milestones) are still to be recorded.

1. **Stage the kernel, `initrd.img` and DTBs together, from one release.** Download a published GitHub Release's boot tarball, `photo-wall-boot-<revision>.tar.gz` (automation reads its name from the release's `manifest.json`, `boot_image.filename`), check it against the release's `SHA256SUMS`, and take `photo-wall-boot/boot/` from it whole: `kernel_2712.img`, `initrd.img`, `bcm2712-rpi-5-b.dtb`, `overlays/`, `config.txt`, the `cmdline.txt` template, `pieeprom.upd` and `pieeprom.sig`. It is byte for byte the `photo-wall-base/boot/` of the same release's base tarball, `photo-wall-base-<revision>.tar.gz`, which still carries it, without that tarball's base squashfs: the release's seal refuses to publish the two with different trees. Never stage from a CI (Actions) artifact: one exists for every build, including unreleased pull requests, and expires after seven days, while a published release is complete by construction and is what Central mirrors the base squashfs from. `initrd.img` carries that build's stage-1 code, its CA list (copied from that build's base) and its clock floor, all in front of the cached initrd ([`tests/test_build_netboot_bundle.py`](../tests/test_build_netboot_bundle.py)). The kernel package is not pinned, so an initrd from one build must not be paired with a kernel from another. The CA list is as old as that build's Debian snapshot pin and stays that old until you stage again. Stage again when the gateway's certificate chain changes.
2. **Set the cmdline.** `cmdline.txt` is one line. Replace its one `@@PHOTOWALL_CENTRAL@@` placeholder (in `photowall.central=@@PHOTOWALL_CENTRAL@@`) with Central's root and change nothing else: the firmware passes the file to the kernel verbatim, so never add a line or a comment ([`contracts/release.py`](../contracts/release.py) declares the template; the release seal refuses any other shape). The root may be http: stage 1 follows the gateway's redirects to https across hosts (up to ten), refuses any https→http step, and verifies TLS ([`tests/test_uplink_tls.py`](../tests/test_uplink_tls.py)). On failure the console prints the named cause and the redirect hops, never a value to set, because the located origin can come from an unauthenticated first hop ([decision 0014, U7](decisions/0014-reaching-central-from-every-boot-stage.md#requirements-hard-rules)). Stage 1 no longer prints a `set photowall.central=…` note; on an http root it logs only `note: configured root is http: the first hop is unauthenticated`. Keep `watchdog.stop_on_reboot=0 hung_task_panic=1` beside `panic=10`. **Keep `cgroup_enable=memory`; never remove it.** Every release's `cmdline.txt` from the 4 GB tracer release on carries it, so do not add it again; a boot tree staged from an earlier release does not, and must be restaged. The Pi 5's device tree puts `cgroup_disable=memory` in the command line the firmware builds, before `cmdline.txt` ([raspberrypi/linux#6980](https://github.com/raspberrypi/linux/issues/6980)); the kernel's later `cgroup_enable=memory` wins ([raspberrypi/linux commit ab520ab14d64](https://github.com/raspberrypi/linux/commit/ab520ab14d64)). Without it every memory limit on the node is ignored. The base still mounts its store and the Player runs, because the boot tree is staged separately from Central's boot selection and Select is fleet-wide ([Releases](#releases-stage-app-and-update-the-wall)): refusing would darken every Player on an older tree. Host Management reports the absence, and the console's Memory limits item warns "memory controller absent: memory limits not enforced". The release seal refuses a `cmdline.txt` that lacks it or repeats it ([`scripts/package_release_artifacts.py`](../scripts/package_release_artifacts.py), [`tests/test_package_release_artifacts.py`](../tests/test_package_release_artifacts.py)); a node release carries that one boot tree, and the node release's seal refuses a release whose `cmdline.txt` differs from it ([`scripts/node_release_artifacts.py`](../scripts/node_release_artifacts.py), [`tests/test_node_release_artifacts.py`](../tests/test_node_release_artifacts.py)). If either liveness parameter is missing, stage 1 boots on and names it (`note: kernel liveness missing: …`; [`tests/test_netboot_liveness.py`](../tests/test_netboot_liveness.py)).
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

6. **Reading Node failures after stage 1.** The V1 provisioner and its `photo-wall-provision.service` are gone ([0019](decisions/0019-debian-packaging-with-debhelper.md)); a Node's programs are the units of `photo-wall-node.target`, read over [SSH](#reaching-a-node-over-ssh) (`journalctl -b -u <unit>`) or on the Player page's host facts and boot report. A unit that systemd could not start logs a status such as `216/GROUP`, `217/USER` or `203/EXEC`: the booted base cannot run it; stage a base whose `base-image` CI leg and [PID1 scenarios](evidence/player-node-handoff-support/node-lifecycle-qualification.md) passed. `cause=time` is a stage 1 fault: it reboots and re-steps the clock on the next boot.
   - Bumping the Debian snapshot pin (developers): see [Changing it](module-debian-packaging.md#changing-it). Every artifact keyed on the pin rebuilds: the base, the initrd build root, the release roots and the CI device root. Re-stage the whole TFTP bundle from that new build.

**Console success on phase 7.** After a successful mount, stage 1 prints `phase 7/7 mount + handoff: modules=<release> files=<n> bytes=<b>`. That line means the initrd's kernel modules were copied onto the base, which carries none of its own. The display drivers (`vc4`, `v3d`) already loaded in stage 1, because `config.txt`'s `dtoverlay=vc4-kms-v3d-pi5` turns the display on. A bundle without that line has no `/dev/dri`. It then prints `phase 7/7 mount + handoff: success dns=<a,b> search=<x>` (where `dns=none` if stage 1 had no resolver discovered). Stage 1 copies its resolver state to the new root's `/etc/resolv.conf`, and the base carries none. Stage 1 then writes `/run/systemd/system.conf.d/90-photo-wall-watchdog.conf` (`RuntimeWatchdogSec=30s`, `RebootWatchdogSec=300s`), so systemd in the base keeps petting the watchdog stage 1 armed. The clock step stage 1 took is recorded in `/run/photo-wall-clock.json`.

## Release sourcing from GitHub (0010)

[Decision 0010](decisions/0010-github-release-sourcing.md): Central **watches the project's GitHub Releases** and records every semver release as a candidate. The worker's sync records each valid release as a node publication in the node release catalog that Fleet › Releases shows, and downloads the bytes the fleet needs (the base squashfs and the node components) into the cache root. Nothing is signed; the sha256 is a corruption check only. The V1 `.deb` promotion (`GET /v1/operator/app/releases`, `POST …/{tag}/promote`, `GET /v1/app/manifest`, the Player `.deb` and the bootstrapper) was removed with the V1 path ([0019](decisions/0019-debian-packaging-with-debhelper.md)); put a release on the wall from the console ([Releases](#releases-stage-app-and-update-the-wall)).

**Configuration.** Release sourcing is always-on ([decision 0013](decisions/0013-unified-cache-root.md)): the worker polls GitHub and mirrors bytes into the cache root unconditionally; Central serves them read-only. Both mount the one cache root (see the [Central cache subsystem](module-central-cache.md)).

| Variable | Where | Default | Meaning |
|---|---|---|---|
| `PHOTO_WALL_CACHE_ROOT` | central (RO) + worker (RW) | `/var/cache/photo-wall` | The one cache root. Optional (baked default) |
| `PHOTO_WALL_RELEASE_REPO` | worker | `mcurcio/photo-wall` | `owner/name` of the GitHub repo whose releases are polled |
| `PHOTO_WALL_RELEASE_TOKEN` | worker | (unset) | Optional GitHub token; unauthenticated polling is rate-limited to ~60 requests/hour |
| `PHOTO_WALL_RELEASE_POLL_SECONDS` | worker | `900` | Poll cadence in seconds |
| `PHOTO_WALL_RELEASE_API_BASE` | worker | `https://api.github.com` | Base URL of the releases API; unset or empty means the default, an invalid URL fails at boot. Tests point it at a fake origin |

**Poll now (admin-authenticated).** Poll GitHub instead of waiting for the next cadence (coalesced; 202 `{"status": "polling"}`):

```sh
curl -X POST -H 'Authorization: Bearer <admin-token>' \
  http://<central>/v1/operator/app/releases/refresh
```

## Base-image auto-mirror (0012, removed)

[Decision 0012](decisions/0012-netboot-base-auto-mirror.md) served several base images per device, selected by a per-device pin or the latest base-healthy version, with server-side rollback. That whole path is gone ([0019](decisions/0019-debian-packaging-with-debhelper.md)): `/v1/netboot/base`, `/v1/netboot/manifest`, `GET /v1/operator/netboot`, the device pin routes, base-health and `PHOTO_WALL_PER_DEVICE_DEB` no longer exist, and migrations 069 and 070 drop their tables. A Node's base now comes from the release it is offered through its node boot offer, and the operator selects the release in the console ([Node control](#node-control)). The worker still mirrors each needed release's base squashfs into the cache root's `os-images/` subdirectory and the hourly `MaintainCache` sweep keeps the desired ones ([Central cache subsystem](module-central-cache.md)).

## Upgrading to content-keyed OS images (migration 028)

Migration 028 re-keyed every OS image from its release tag to the sha256 of its base tarball ([idempotent jobs](central-idempotent-jobs.md) §8) and carried no produced facts, so each desired OS image downloaded once more after that upgrade. Its upgrade and rollback procedure (a preflight over `app_releases` and the device pins, a rollback of `asset_references` and the release poll's ETag) is **withdrawn**: migration 070 drops `app_releases`, the device pins and the V1 fleet tables ([0019](decisions/0019-debian-packaging-with-debhelper.md)), so the queries cannot run, and a rollback past 070 is not supported (forward-only: restore a database backup instead). A deployment older than migration 028 must first upgrade on a release that predates 0019. The key naming survives: OS images are `os-images/base-<tarball sha256>.squashfs` in the cache root, and a wanted tarball that was deleted upstream is a transient `503` until a download succeeds ([Release sourcing](#release-sourcing-from-github-0010)).

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

**Rolling Central back past the after-state (migration 071).** Migration
`071_layer_after_end.sql` replaced each stored `retain_on_expiry` flag with `after_end`
([what a Frame keeps](execution-contract.md#what-a-frame-keeps)) in runtime state, offered plans
and locks. Builds before it forbid the key, so a previous Central build cannot restore runtime
state (the same symptoms as above). Prefer rolling forward. Otherwise stop Central, then run this
once before starting the previous build. It maps `keep_this_photo` back to the flag and every
other after-state to no (an ending then no longer clears the kept photo, as before), and lets 071
run again when you roll forward:

```sql
UPDATE runtime_state SET snapshot = replace(replace(replace(snapshot::text,
    '"after_end": "keep_this_photo"', '"retain_on_expiry": true'),
    '"after_end": "keep_nothing"', '"retain_on_expiry": false'),
    '"after_end": "leave_as_is"', '"retain_on_expiry": false')::jsonb;
UPDATE plan_offers SET manifest = replace(replace(replace(manifest::text,
    '"after_end": "keep_this_photo"', '"retain_on_expiry": true'),
    '"after_end": "keep_nothing"', '"retain_on_expiry": false'),
    '"after_end": "leave_as_is"', '"retain_on_expiry": false')::jsonb;
UPDATE assignment_locks SET layer = replace(replace(replace(layer::text,
    '"after_end": "keep_this_photo"', '"retain_on_expiry": true'),
    '"after_end": "keep_nothing"', '"retain_on_expiry": false'),
    '"after_end": "leave_as_is"', '"retain_on_expiry": false')::jsonb;
DELETE FROM schema_migrations WHERE name = '071_layer_after_end.sql';
```

## Operator API: reposition, edit profile and remove Frames

The operator console edits the wall plan through admin-authenticated routes on central (`Depends(admin)`, like every `/v1/operator/*` route). They change no schema and add no migration — placement and profile already exist on the `frames` row.

`PATCH /v1/operator/frames/{frame_id}` applies a **partial** placement. The body accepts `surface_id`, `x_mm`, `y_mm`, `width_mm` (> 0), and `height_mm` (> 0); any omitted field keeps its stored value — this is a merge, not a replace. Central re-runs the same orientation-coherence guard as Frame creation against the merged dimensions and the stored profile, returning **422** when the resulting aperture orientation would disagree with the display profile. An id that no longer exists returns **404 `unknown_frame`**; on success the response echoes the merged placement. Placement is **last-write-wins with no concurrency token** — two operators dragging the same Frame silently overwrite each other and the plan corrects on the next snapshot — because geometry is operator-only metadata: it never reaches a Player, it is independent of the Frame's Position (its Frame fit, whose corners are fractions of the output in `[0,1]` and whose angle is in degrees), and a move is trivially re-dragged. The route therefore **does not bump `generation` or `configuration_revision` and never invalidates calibration**.

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
- Every script still using the old token gets 401 until it is given the new one: the `curl` examples in this runbook and `scripts/demo_wall.py` (`DEMO_ADMIN_TOKEN`).
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

The console is one page: a header (Central's health, the snapshot age with **Refresh**, **Log out**, and the [attention strip](#operator-console-wall-health-and-the-attention-strip)) and a sidebar of sections in five fixed groups: **Wall**; **Show** (Now, Scenes, Schedule, Sources); **Fleet** (Players, Releases); **Settings** (Photo library); **Needs attention**. Nothing in the sidebar reorders or counts on state, and there is no Set up section: each home lists its own unfinished items ([console DDD §48](operator-console-ddd.md#48-the-proposed-shape)). Under 850 px wide the sidebar is a drawer behind **Menu**. Each section, and each step of a flow, has its own address after `#`, so you can bookmark it, type it or send it to someone signed in to the same Central. The design is owned by [passes C and D](operator-console-ux-pass2-flow.md#6-navigation-routes-and-modules).

| Section | Addresses | For |
|---|---|---|
| **Wall** | `#/wall`; Edit layout: `#/wall/layout`; a Frame's page: `#/wall/frames/<id>/<tab>`, tab `overview` (the default; `#/wall/frames/<id>` opens it), `position`, `picture` or `hardware` | The read-only plan, the Unplaced tray, the To finish list and the first-run guidance; one click on a Frame opens [its page](#operator-console-calibration-and-conflict-states); [Edit layout](#operator-console-placing-moving-and-deleting-frames-and-the-unplaced-tray) holds every layout write. |
| **Now** | `#/now`; Show now: `#/now/show/scene`, `#/now/show/review` | Central's live Runs, [Show now](#now-show-a-scene-now), Run cards, and why a frame shows what it does. |
| **Scenes** | `#/scenes`; `#/scenes/new/<step>` (optionally `?target=<frame-id>` when started from a Frame); `#/scenes/<id>/edit/<step>` | [Making and editing Scenes](#scenes-make-a-scene-step-by-step). Steps: `kind`, `photos`, `frames`, `media` (hand-picked only), `playback`, `review`. |
| **Schedule** | `#/schedule`; `#/schedule/new/<step>` | [Schedules](#schedule-a-repeating-schedule-step-by-step). Steps: `scene`, `when`, `review`. |
| **Sources** | `#/sources`; a new Source and an edit open the Photo source editor | [Sources](#sources-add-and-manage-a-source): one editor with Include, Exclude and the pool as it fills. |
| **Hardware** | `#/hardware`; one Pi in focus: `#/hardware?pi=<device-id>`; one Pi: `#/hardware/<device-id>` | The [Hardware list and Hardware pages](#the-hardware-list-and-the-hardware-pages). |
| *Software and screens* (no sidebar entry) | one Pi: `#/players/<device-id>` | The [Software and screens page](#the-software-and-screens-page), reached from a Pi's Hardware page. There is no list at `#/players`, and the old `#/equipment` and `#/players` addresses open the Wall. |
| **Releases** | `#/releases`; Update the wall: `#/releases/update/<tag>…` | [Releases, Stage app and Update the wall](#releases-stage-app-and-update-the-wall). |
| **Photo library** (group Settings) | `#/photo-library` | [Connecting Immich](#connecting-immich): connections, keys, counts, health. |
| **Needs attention** | `#/attention` | Every Frame and Player incident, and every Immich connection that needs a look, each linked to where it shows. |

**Show and Wall are organization, not permission.** There is one shared admin token and no roles (design D-c/Q5 in the [console design](operator-console-ux-design.md)); the sections change what you are looking at, never what you may do. Display controls ([a Frame's Position and Picture tabs](#operator-console-calibration-and-conflict-states)) are reachable only from the Wall. The four Show sections and Needs attention show frame health as status only (R4). This is checked by a test that walks what the Show and Needs attention pages import, and a browser test that visits each of their sample addresses (`tests/test_console_routes_r4.py`, `tests/browser/test_console_shell_browser.py`).

**Where an address leads.**
- An address the console does not know goes to the Wall (`#/wall`), always; with no Frames its first-run guidance is there.
- Pages read "Loading…" until the first snapshot. An address naming a frame or Scene that no longer exists then says so ("Frame lobby-left: This no longer exists.", "Scene evening: This Scene no longer exists.").
- An address naming a step that does not exist, or does not apply (`media` for a live Scene), opens the step the draft last showed, or its first step. An address may skip ahead; the steps it skipped are not ticked in the stepper.
- On the Wall, one click on a Frame opens its page as a new history entry (Back returns to the plan); choosing a tab updates the address in place, and going back to the plan from a Frame shows its Surface. A Frame opens on **Overview** unless the visit comes from the attention strip, Needs attention or a Player page, which open the tab that shows the cause (Hardware for Player, Output and Panel problems, Position for calibration). The Wall link in the sidebar opens the Wall as last shown from another section, the plan from a Frame page, and never Edit layout.

**Drafts.** Each flow (a Scene, a Program, a Source, Show now) holds one unsaved draft. It survives moving between steps, going to another section (the Wall included), a refresh, and a session that ends (above). A section holding one is marked "Draft" in the sidebar and offers **Resume draft (Draft)** and **Discard draft** in place of its New button. Only saving (or Activate now with a known outcome), Discard, Log out or reloading the page ends a draft. **A reload loses every draft**: drafts live only in this tab's memory (flow design Question 5, built on its default). A Photo source's unsaved draft is also kept in Central, so its check can run before Save; a reload loses this tab's hold on it, and Central removes it after a day.

**Back and Forward.** A flow is one history entry: its steps replace each other, so browser **Back leaves the flow and keeps the draft**; it does not go to the previous step. Use the flow's own **Back** button, or an earlier step in the stepper, for that. After a save, Back never re-enters the finished flow; a save that lands after you went elsewhere leaves you where you are.

**Opening another draft.** **Edit** on another Scene's card while your draft has changes asks you to discard it first. An address typed, or reached with Back, that names another Scene never replaces your draft: the page reads, for example, "Unsaved draft for a new Scene: Resume or Discard" and says what discarding opens.

**Keyboard.** The first Tab reaches **Skip to content**, which moves focus to the page. Enter on a step presses Continue. In the drawer, Esc closes it and returns to Menu; choosing a section closes it and focuses that page's heading.

## Operator console: refresh, the snapshot-age clock, and the guidance banner

The console has **no push channel** — the player WebSocket is player-only — so every region reads from **one timestamped snapshot**: a single atomic read of `/v1/operator/inventory`, `/v1/operator/runtime` and `/v1/operator/media` (the design calls this **Plane A**; see [§9](operator-console-ux-design.md#9-storage-lifecycle--refresh) and [§4a](operator-console-ux-design.md#4a-the-two-plane-state-model) of the console design). The wall plan, the Players list, the Frame page, and each Frame's `planned` fact all render from that same snapshot (a Player page adds its own node and V1 reads, each with its own read time), so a Frame's binding row and its `planned` fact always share one age. Between refreshes the console can be stale, and it never hides this.

**The snapshot-age clock.** The global bar always reads **"updated N s ago"** next to a **Refresh** control. The age is the honest time since the last successful snapshot, not a freshness guarantee — it tells you exactly how stale what you are looking at may be. Pressing **Refresh** re-fetches them together as one new snapshot and resets the clock. Because a stale read can never silently drive a wrong write, mutations still carry their concurrency tokens (`expected_generation`, `expected_revision`, an idempotent `activation_id`), so an action taken against a stale snapshot resolves to an explicit "the world moved" conflict rather than a silent wrong success.

**When the snapshot refreshes.** A new snapshot is fetched **every 5 seconds while the tab is visible** (the poll pauses while the tab is hidden and refreshes immediately when you return), **automatically after every mutation** (a bind, a placement, a commit, an activation), and **on an explicit Refresh**. A response that is older than one already shown, or that was read while one of your writes was in flight, is discarded rather than shown, so a poll never undoes what you just did ([pass 2 §7](operator-console-ux-pass2.md#7-polling-and-the-write-fence)). If a refresh fails, the bar reads **"updated N s ago — last refresh failed"**. Separately, the **Central pill polls `/healthz` about every 10 seconds** and reads "Central: ok", "Central: scheduler stale" (or another scheduler or database reason), or "Central: unreachable"; it reports Central's own health, not any Player and not observed presentation. **These interval numbers are tunable placeholders, not fixed guarantees** — the design states them as cadences to tune during operation ([§9](operator-console-ux-design.md#9-storage-lifecycle--refresh), assumption 4 in [§10](operator-console-ux-design.md#10-decisions-that-are-yours)), not gate decisions, so treat "5 s" and "~10 s" as approximate rather than contractual.

**A refresh never discards unsaved work.** The read snapshot is one plane; everything you are *doing* — an in-progress drag, a calibration draft you are "trying" before commit, the lease countdown, and a step flow's draft — is a **separate** plane the design calls **Plane B**. A Refresh replaces the read snapshot **only**; it merges nothing into your draft and cannot reach into it. Concretely, **a calibration draft in progress is not lost by a refresh** (the fit, corners and angle you have set stay put); if the refresh reveals that the committed state moved underneath you — the Frame's `revision` or `generation` advanced — the console raises a "committed changed underneath you" conflict and lets you decide, rather than throwing your draft away. This two-plane separation is enforced structurally by the React state model (design rule R3), not by convention.

**First-run guidance banner.** On the Wall, while no Frame exists, a **non-blocking** banner carries first-run onboarding (design Q8: always-visible inventory plus a guidance banner, never a modal wizard that gates the console — see [§10](operator-console-ux-design.md#10-decisions-that-are-yours)). It has **no Dismiss**: it leaves by itself once the first Frame exists, and the Wall's **To finish** list takes over. Its **Add first frame** button opens [Edit layout](#operator-console-placing-moving-and-deleting-frames-and-the-unplaced-tray) (`#/wall/layout`). The banner never blocks a control. Its guidance is only as fresh as the last snapshot (there is no push), which is the stated cost of preferring a non-blocking banner over a linear wizard.

**To finish.** Once Frames exist, the Wall lists its own unfinished items above the plan, one row per item in Frame order, each with one link: "lobby-left · not on the plan" [Edit layout], "lobby-left · needs a Player" [Binding], "lobby-right · needs calibration" [Calibration]. Calibration is asked only of a bound Frame. The list is worked out from the snapshot each time, with no stored flag, and is absent when nothing is left; a new Frame or a rebind brings an item back by itself. These items are never attention rows. The Players page lists its own: boxes **Not driving a Frame** ([below](#the-players-list-and-the-player-page)).

**Put content on a Frame.** Open the Frame's page from the Wall. Its **Overview** tab (the default) offers **Make a Scene**, which opens a new Scene with that Frame selected on the Frames step. Review the target checkboxes: add or remove Frames explicitly, then choose a live Source or hand-picked media. Save the Scene, then **Show now** for immediate activation or **Schedule it** as a Program. These authoring links do not activate content by themselves. The tab also links to existing Scenes. A Frame owns its location and calibration; the Scene owns the content and target choice, and a Run or Program activates it. The first-run banner names this path after binding and calibrating.

If a new Scene draft is already open, following either Frame link keeps that draft. It does not add a target behind your back; the Scene explains this and offers **Choose Frames** so you can select the Frame yourself. A dirty edit of another Scene retains the flow's **Resume or Discard** choice.

## Operator console: wall health and the attention strip

Every Frame tile, the Frame page header, the Unplaced tray, the frame badges on Now and the frame lists of the Scene flow and Run cards show one **health** label for each Frame, so the same Frame never reads differently in two places; each [Player page](#the-players-list-and-the-player-page) shows the Player app's last-reported age from the same source, beside the host layers'. The plan tile shows a short form; the full label, with its age, is in the Frame page header and in the tile's accessible name. The states, their order and their wording are owned by the [pass 2 design, §4](operator-console-ux-pass2.md#4-per-frame-health-one-closed-set-one-classifier).

**What the labels are based on.** A label reports **Central's record of the last readiness report it accepted from the Player**, aged on Central's clock at the moment of the snapshot. It is **not** a live video readback, and nothing on the console says "LIVE", "online" or "connected". A Frame that reads healthy may still be dark (a failed panel, a failed decode); the console cannot confirm lit pixels. The `planned` fact ("On top: …") is **Central's Runtime projection** (the top Run on the Frame), never confirmed playback. A healthy Player reports about twice a second.

| Label | What it means | What to do |
|---|---|---|
| **Player app last reported N s ago** | Central accepted a report from the bound Player's app within the silence threshold, and nothing else is wrong. This is the healthy state. | Nothing. |
| **Player app silent · last reported N min ago** (alarm; the tile says "Player app silent") | The Player app has not had a report accepted for longer than the silence threshold; the box itself may still be up (its other layers are on the Player page). The Pi may be off, off the network, crashed, or talking to Central but having its reports refused. | Check power and network at the Frame. If the Central pill is not ok, fix Central first (below). If it stays silent after the Pi is back, restart the Player. |
| **Enrolled N s ago, no report yet** | The Player enrolled (its process started) but Central has not yet accepted a report from it. A to-do for up to the threshold after enrolling, then an **alarm**. A Player that restarts in a loop keeps resetting this age and may stay a to-do. | Wait a few seconds after a boot. If it turns into an alarm, or you see the enrolled age keep resetting, check the Player's logs for a crash loop. |
| **Output interrupted (Central's inference: Display Host reported the app surface invalidated or withdrawn · recorded N s ago) · the Run continues** (alarm) | Central holds an unresolved Output-loss record for this Frame's current Binding, from what the named node layer reported: Display Host an invalidated or withdrawn app surface on this Output, or the App Effect Broker "reported the app process exited" (which Central applies to every Output linked to that process). "· the Run continues" appears only when a live Run targets the Frame. Only this Output's contribution is interrupted; the Run, other Frames and Actuators continue. The tile reads "Output interrupted · the Run continues", or "Output interrupted" with no live Run. A Frame with no such record shows nothing, which is not proof the Output is fine: Central records only the losses it could link. | Open the Frame's Hardware tab and its Player page; check the Panel, cable and the named layer. |
| **No Panel listed as connected at the Player app's last enrollment (may be stale)** (alarm) | When the Player app last enrolled, it did not list a connected Panel on the bound Output. This is Central's enrollment record, not a live reading. The tile reads "No Panel listed at the last enrollment". | Check the Panel's power and cable, then restart the Player so the Player app enrolls again. |
| **Needs calibration** (to-do) | The Frame's calibration is not valid, so nothing is shown on it. This is normal for a new or newly bound Frame. | Open the Frame's [Position tab](#operator-console-calibration-and-conflict-states) and press Done. |
| **Needs a Player** (to-do) | No Player output is bound to the Frame. | Open its [Hardware tab](#operator-console-onboarding-binding-and-auto-recovery) and choose a free output. |

**The silence threshold is about 31.5 s.** Central serves it with the inventory; its single source is `contracts/liveness.py`, shared with the Player's own retry timing. It is long enough that one failed Player cycle against a responsive Central does not raise a false alarm, so a powered-off Pi reads "Player app silent" within about 31.5 s plus one 5 s poll. A Central that is answering slowly (near its 15 s request timeout) can make a live Player briefly read silent. The derivation and its costs are in [pass 2 §2](operator-console-ux-pass2.md#2-the-liveness-signal).

**Panel hot-plug is not detected on the Wall.** Frame health uses the Panel record from the Player app's last enrollment. Unplugging or plugging in a Panel afterwards changes nothing on the Wall until the Player app enrolls again (a restart); the Player page's Display Host row shows Display Host's current per-Output connector state where the node lane runs. This is a known gap, deferred ([pass 2 §12](operator-console-ux-pass2.md#12-costs-deferrals-and-questions)).

**The attention strip.** A one-line strip under the status bar counts incidents only, for example "2 Frames · 1 Player need attention", or "No Frame or Player needs attention", with "· K awaiting a first report" when K Frames are bound to a Player app that has not reported yet and is still within the silence threshold. A Frame counts when its health is an alarm; a Player counts when it is Bound and has a [host incident](#host-health-on-the-players-pages). A Player incident alone makes the strip an alarm. Unbound and needs-calibration Frames are not counted: they are the Wall's **To finish** items. While the fleet host read has failed or not yet loaded, the summary ends " · host health not read", so missing host rows never read as health. **Show list** opens a list (Frame rows, then Player rows, up to 8 entries and "and M more"). On the Wall, Players and Needs attention pages a Frame entry is a button: it opens the Frame's page on the tab where the cause is shown (Hardware for Player, Output and Panel problems). A Player entry reads, for example, "pi-07 (Frame lobby-left) — Host Management silent · last reported 2 min ago" with a [Frame lobby-left] button and a link to the Player page. On the Show sections (Now, Scenes, Schedule, Sources) every entry is text only, so reading them never abandons show work. **Show all** opens **Needs attention** (`#/attention`), the same list at full width with no cap, where each entry links to the Frame's page at that tab (`#/wall/frames/<id>/<tab>`) and Player rows to the Player page.

**When Central's scheduler is not ok.** If the Central pill reads something like "Central: scheduler stale", the strip replaces the silent-Player entries with one line, for example "5 frames silent — Central's scheduler is stale; Players may be unable to report until it recovers." Fix Central (its logs and `/healthz`) before visiting the Pis. Entries with another cause, such as no Panel listed, stay listed.

## Operator console: placing, moving, and deleting Frames (and the Unplaced tray)

In the console (served at `/` since the cutover, aliased at `/console`) the wall plan is on the **Wall** section (`#/wall`). The daily Wall is read-only: one click on a tile (or an Unplaced tray entry) opens the Frame's page. Every write in this section happens in **Edit layout** (`#/wall/layout`), opened by the **Edit layout** button (or **Add first frame**, or a To finish "not on the plan" link). Its header reads "Editing layout" with **Done**, which returns to the daily face. Each Surface is a flat millimetre plan and its Frames are drawn as rectangles from their `x_mm/y_mm/width_mm/height_mm`. Dragging or entering measurements uses the operator routes documented above ([reposition and remove Frames](#operator-api-reposition-and-remove-frames)) or the existing `POST /v1/operator/frames`. Selecting a Surface *filters* the plan to that Surface's Frames; a Surface is a bare text label, not something you act on. This mirrors the "reading & building the wall" walkthrough (J3) in the [console design](operator-console-ux-design.md).

**Place a new Frame.** In Edit layout, on a new installation with no Frames, the Surface selector starts at **wall**. Drag a rectangle on the plan or press **Add frame with measurements** to use the keyboard and enter its position and physical size in millimetres. Both paths open the same form for the Frame's **id** and **Frame profile** — pixel width/height and diagonal (plus whether it is video-capable). The console sends one `POST /v1/operator/frames` carrying the chosen `surface_id`, `x_mm`, `y_mm`, `width_mm`, `height_mm` and profile. Position `(0, 0)` is reserved for old Unplaced frames; choose another position. Central runs the same orientation-coherence guard as every Frame creation: if the aperture orientation would disagree with the profile (a portrait matte declared against a landscape panel, or vice versa) the create is refused and nothing is added. The profile values are **Frame facts** — operator-declared and persisting across a later panel swap — not a live Panel reading.

**Frame ids are readable and permanent.** You name the Frame, for example `lobby-left`; every later screen shows that id, and it cannot be changed. The form checks it as you type: letters, digits, `-`, `_` or `.`, starting with a letter or digit, at most 96 characters, and no `:`. This is the same rule a Scene uses to target a Frame, defined once in `contracts/models.py` (`TARGET_ID_PATTERN`) and enforced by Central on create, so an id Central accepts can always be targeted ([slice 2 §8](operator-console-ux-pass2-onboarding.md#8-readable-frame-ids-one-rule-in-contracts)). An id already in use reads "A frame named lobby-left already exists." Frames created before the rule keep their ids and stay readable and deletable. There is no separate display name yet. **Replacing a panel with a different size or resolution:** unbind the Frame, then use **Edit profile** in its Frame facts. The Frame id, placement and Scene targets stay intact. Central refuses the change during a live Run on that Frame or if another operator changed its binding/profile first. A changed profile invalidates calibration and its preview, so bind the new Panel and calibrate the Frame again before relying on output readiness. The profile must still match the physical aperture orientation.

**Move a Frame.** In Edit layout, drag an existing rectangle to reposition it without changing its stored size. For exact position or size, select the Frame and press **Edit placement**; the form sends the entered millimetres through `PATCH /v1/operator/frames/{frame_id}` and checks orientation against the saved Frame profile. Placement is **last-write-wins with no lock**: a concurrent edit of the same Frame by another operator can overwrite yours, and the plan corrects itself on the next snapshot refresh. Because geometry is operator-only metadata — it never reaches a Player and is independent of the calibration corners (normalized to `[0,1]`) — a placement edit **carries no concurrency token and never invalidates calibration or bumps `generation`**. This is the one deliberate exception to the console's "every write carries its token" rule.

**Delete a Frame.** The confirmation dialog lists live Runs, saved root Scenes (including nested child/outro targets) targeting the Frame, and upcoming Programs using those Scenes from the current snapshot. Central checks current references again when you confirm. It preserves the existing refusal while the Frame is bound (409 `frame_bound`) or targeted by a live Run (409 `frame_in_use`). It also refuses with 409 `frame_referenced` while saved Scene roots or queued activations still target the Frame, returning sorted server-side Scene, Program, and queued-activation IDs. A stale refusal says which references Central found and directs you to press Refresh, edit/remove the saved Scene and Program references, and review queued activations before retrying. With those references cleared, the Frame can be deleted. Recreating its id starts uncalibrated.

The reference guard does not ban explicit future Scene targets for a Frame id that is currently absent. That permits recovery: an operator can retain or author future content for an id and recreate the persistent Frame later. Until the Frame exists and is bound and calibrated, Central has no Output to receive that content.

**The Unplaced tray.** Frames created by the old UI all sit at `wall`/(0,0) with no distinct position; rather than pile them at the origin as permanent clutter, the console collects such geometry-less Frames in an **Unplaced tray** beside the plan, as a list. On the daily Wall, a tray entry opens the Frame's page, so a bound Frame that is not placed is still one click away. In Edit layout you can drag a Frame from the tray onto the plan or select it and use **Edit placement** to give it a measured position. Either sends the same `PATCH` as a move, and the Frame then leaves the tray and joins the plan. You can also delete it under the same delete checks above.

A freshly placed or moved Frame **settles to its final position on the next snapshot refresh**: the plan re-fits per Surface (it scales each Surface's millimetre space to the viewport), so a rectangle drawn or dragged optimistically can shift slightly once the authoritative snapshot returns, while its stored millimetre geometry is exactly what you entered.

## Operator console: onboarding, binding, and auto-recovery

New hardware appears in the console before it does any work. Following [decision 0006](decisions/0006-central-authority-and-stateless-players.md), Central is the source of truth and a Player is a replaceable box behind a Frame. The design of this flow, and its costs, are owned by [pass 2, slice 2](operator-console-ux-pass2-onboarding.md).

### The Hardware list and the Hardware pages

The **Hardware** section (`#/hardware`) has one row per physical box (a **Pi**), keyed by its fleet device identity (derived from the serial it claims at netboot), so the same Pi never appears twice. The design is owned by [Console by Domain](decisions/0018-console-by-domain-and-design-system.md) and the earlier [domain-driven console design](operator-console-ddd.md#9-screens-and-read-models). The list reads the same `/v1/operator/inventory` snapshot as the plan plus the netboot read below, so a new Pi appears the moment it netboots or enrolls. It makes no per-box node read; its host columns, and its **Link** column (whether Central's hub holds the Pi's Node API link, from the fleet host read's `bus_link`), come from the console's one [fleet host read](#host-health-on-the-players-pages). The list has three groups, each its own table: **Driving a Frame** (Bound Pis, worst first by host tier: alarm, notice, unknown, ok, then by name), **Not driving a Frame** (boxes seen at boot but never enrolled first, then Unbound Pis, newest registration first; plain values and their receipt age but none of Central's threshold judgements, so a spare is never alarmed) and **Retired** (host columns read "Not read: Player retired"). There is no counts line (the attention strip counts incidents). At phone width a table scrolls sideways. Each row names the Pi ("Player …a1b2c3", the last six characters of its claimed serial), links to its Hardware page, and offers **Focus on** that Pi: `#/hardware?pi=<device-id>` narrows every group to it, a chip clears it, and leaving the list ends it (the focus lives only in the address).

- **Standing and Frames**: **Not enrolled** (seen at boot, never enrolled), **Unbound** (enrolled, no Output bound), **Bound** (at least one Output bound) or **Retired** (permanently out of service), then a link to the Binding of each Frame its Outputs are bound to, and, for a Bound Pi, one health badge.
- **Host columns**: Link (the Node API link and Host Management's last report), Temperature and throttling, CPU and `/run` storage (plus App Manager's last storage refusal), and Network (link and link state, link speed, address, and when the host facts were first received). Each cell keeps its label ("Temperature: 81 °C · hot …"). The host columns are hidden while node control is not on.

**A Pi's Hardware page** (`#/hardware/<device-id>`) is the one home for the box itself. Each section shows its own read time and fails on its own ("This section could not be shown"):

- **Header**: name, standing, the serial **labelled as a claim**, **Enrollment**, links to the bound Frames' Bindings, the full device and Player ids under **Identifiers**, and a link to the Pi's Software and screens page.
- **Reboot** (with its history): **Reboot Player** targets Host Management's one current session; when it cannot, the button is disabled and says why (for example, Host Management has no current session on this boot). The dialog names the bound Frames and their live Runs: the Run stays active and no other Frame or Actuator gets a command. Each request then reads one state: Requested · delivery unknown, Outcome unknown, Received, Accepted or Rejected by Host Management, or Host Management reported the reboot started · completion unknown. If the answer is lost, **Retry the same request** resends the identical request. A **new** request cannot be sent while another reboot request for the same session is outstanding (unexpired on Central's clock and not rejected; Accepted and started requests count). The console checks Central's served `outstanding` at the moment of sending, and Central refuses a new command id with 409 `node_reboot_outstanding`, which reads "Another reboot request for this Player is outstanding; close this dialog and review it."
- **Health**: the box's [host health](#host-health-on-the-players-pages) in groups (Thermal, Power and throttling, Compute, Storage, Network, Software), the host facts' one receipt line, and an **Every reported metric** disclosure with the raw sample, its fault code and "Host samples do not show visible pixels." Health is hidden while node control is not on and for a retired box.
- **Link and sessions**: whether Central's hub holds the Node API link, and Host Management's session: when it last reported, and which session and boot.
- **Danger zone**: **Retire player** (Unbound only). A Bound Pi shows links to its Frames' Bindings instead: unbind it there first.

### The Software and screens page

**A Pi's Software and screens page** (`#/players/<device-id>`, reached from its Hardware page) holds what runs on the box and the screens it drives. Each section shows its own read time and fails on its own ("This section could not be shown") without taking down the rest:

- **Header**: name, standing, the serial **labelled as a claim** (the console cannot confirm which physical box sent it), **Enrollment** ("Player app enrolled N s ago (authority epoch N)", Central's enrollment record), links to bound Frames, the full device and Player ids under **Identifiers**, and a link to the Pi's Hardware page.
- **Layers**: one row per node layer, each naming its source. Host Management and App Manager show when they **last reported**. App Effect Broker shows its app-process fact and when Central **first received** it; its last report reads "Unknown: App Effect Broker sends evidence only on change, and Central stores no receipt of its polls". Display Host shows when it **last reported** (its newest display exchange on this boot) and, for each Output, three facts it reported: the Panel connector (connected or not), the admitted surface ("the app's surface for Frame … (binding generation g)" or "Display Host reported no app surface admitted"), and the compositor receipt for that surface with its age on Display Host's own clock. A compositor receipt is not proof of Panel pixels, and the console judges no staleness. When Display Host has reported no Output on this boot, the row says so. The Player app shows its last-reported readiness. "Panel pixels: Unknown: no layer observes them" closes the list. A silent Player app with a reporting host shows both ages, so a crashed app reads differently from a dead box.
- **Outputs**: each Output's state ("Bound to Frame …", "Free", "No Panel listed at the last enrollment", or "Retired with its Player"; "No outputs reported" when there are none), **Bind to a frame…**, **Identify Panel**, and the Panel record at the Player app's last enrollment, labelled "may be stale". A bound Output also shows its [Output interruption](#operator-console-wall-health-and-the-attention-strip) when Central records one.
- **Boot**: the current node session's boot as a claim (or "No current node session"), then the latest node boot offer ("not proof the Player booted"). A later boot never says what caused it.
- **App**: the node app operations, each with its named state (for example "Rejected by App Effect Broker", or "Ended by a later boot" for a stage that ran before the Player rebooted). **Stage app** switches this Player's app for this boot only (see [Releases, Stage app and Update the wall](#releases-stage-app-and-update-the-wall)). **Qualified fallback** shows the app environment the Player app linked, lets you qualify it, and lists the stored acceptances.
- **Danger zone**: **Unbind all outputs** (Bound only). Retire is on the Pi's Hardware page.

A Pi's pages read that box's node state every 5 s while it is open and visible. A failed read keeps the last rows, marked "refresh failed". A retired Player is not read ("Not read: Player retired").

The serial comes from the console's one fleet host read (`GET /v1/operator/node/hosts`, every 15 s once you are signed in). If that read fails, the Hardware list says **"Boot records unavailable"** and keeps the last serials it knew; you stay signed in and everything else works.

### Host health on the Players pages

The Players table, the Player page's **Health** section, the Frame page Overview's host chip and the Player rows on Needs attention all come from one read, `GET /v1/operator/node/hosts`, which the console sends every 15 s while the tab is visible and node control reads on. It serves each active box's values from its **current boot only**, aged on Central's clock, plus Central's thresholds as numbers; the console bands values against those numbers and composes every basis from them. The design and every wording are owned by [console DDD §62](operator-console-ddd.md#62-wording-and-truth-kinds); the route and its storage are mapped in the [fleet implementation map](player-fleet-implementation-map.md#fleet-host-health-batch-4-2026-10-02).

Host Management posts one observation every 15 s, and Central stores at most one per box per 15 s. **Host silence is called after 60 s** (four intervals) with no stored report. A host value is a report, not a live reading: "Host Management last reported 4 s ago · 62 °C".

| What the row says | What it means | What to do |
|---|---|---|
| **Host Management last reported N s ago**, with banded values ("81 °C · hot", "Throttled now") | This boot's Host Management is reporting. Bands and incidents are raised only in this state. "Warm" and a sticky "occurred recently" flag are notices; "hot", any "now" flag and a storage refusal are alarms | For hot or throttled: check cooling and the power supply (under-voltage means the supply or cable is too weak) |
| **Host Management silent · last reported N min ago (Central's inference: no report for over 60 s, Central's limit)** | No report from this boot (or, before this boot reported, from the previous one) for longer than the limit. The box may be off, off the network, hung, or rebooting. The last values are shown "at last report", without a band, and raise no "now" incident | Check power and network. A planned reboot (Reboot, Update the wall) also reads as silence for about a minute per Player, because the incident is not linked to the reboot |
| **Central refused Host Management reports today (Central's inference: its daily intake cap for this box is full)** | The newest report is older than the limit **and** Central's daily observation intake for this box is full, so the box may be reporting but Central is not storing it. It is never worded as silence. With one box per serial this cannot happen; it means three or more boxes claim one serial | Find the boxes sharing the serial (they flap visibly: each enrollment revokes the other) |
| **Unknown: on this boot · the previous boot's Host Management last reported N s ago** | The box has started a new boot and its Host Management has not reported yet; the previous boot reported within the limit. The previous boot's values and facts are never shown | Wait; it becomes silence after the limit |
| **Unknown: no Host Management report from this boot or the one before** | Central holds no report from this boot or the most recently superseded one (Central sees no older boot). On a Bound Player this is one Unknown row on Needs attention | Check that Host Management runs on the box and that it boots by the node path ([Node control](#node-control)) |
| **Unknown: not read** | The box is not in the host read (for example, it is retired or revoked) | Nothing |
| **Unknown: not reported** on one item | That metric was not sent: an older base, another board, or an unreadable sensor. It does not raise the row's tier | Nothing, unless every box should report it |
| **Unknown: no host facts received on this boot** | The kernel, interface, link state, address and reported base record has not arrived from this boot (bases before batch 4 never send it). Central's offer line still shows, as Central's record of the boot offer this boot claimed | Nothing |
| **Base differs: Host Management reported X, Central's offer Y** (Software) | Both tags come from the same boot offer: the node's from the copy in its boot handoff (the offer its initramfs verified, whose `offer_id` Host Management claims with), Central's from the payload it stored for that `offer_id` (every claim of a boot must present the admission's offer). So this never means a different offer or different bytes: the two copies of one offer disagree, which only a defect produces (the node reporting a tag its handoff does not hold, or Central storing a payload other than the one it served). No band and no incident | Report it as a bug with the Player page's Boot section and the node's `/run/photo-wall-node/host.json`; a reboot takes a fresh offer, so a mismatch that survives one points at a persistent defect, not a stale boot |
| **boot preparation refused at storage: needs 3.8 GB of memory, the box has 1.9 GB** (Software) | The board is below the smallest supported memory class (fault `node_memory_class`; the supported minimum is a Pi 5 with 4 GB, [requirements](requirements.md#supported-player-hardware)). Owner: the base's storage step (`photo-wall-node-storage.service`, [memory classes](player-node-domain-model.md#memory-classes-and-the-node-store-2026-10-03)). Nothing after it starts: no Player app, no App Manager. Host Management still reports. Alarm, and an incident on a Bound Player | Replace the board with a Pi 5 with 4 GB or more. Nothing on Central fixes it |
| **memory controller absent: memory limits not enforced** (Storage, Memory limits) | The kernel's memory controller is off, so no memory limit on the box is enforced; the Player still runs (the base mounts its store regardless). The Pi 5's device tree turns it off; the boot command line must turn it back on with `cgroup_enable=memory` ([step 2](#player-provisioning-stage-the-netboot-bundle-and-read-its-console-0014)). Usually the staged boot tree predates the release that added the token. Owner: the boot tree you staged and its `cmdline.txt`. A warning (notice band), never an incident | Stage `boot/` from a current release (releases from the 4 GB tracer on carry the token; trees staged earlier do not) and check nothing in your staging, such as iac `cmdline_extra`, removes it or adds `cgroup_disable=memory` after it. Reboot the Player |
| **boot preparation refused at prepare: needs 2.1 GB, room 1.8 GB** (Software) | The deployment this boot was offered does not fit the node store, or the memory left above the 512 MiB emergency headroom. Owner: the prepare step (cold admission) | Select a smaller deployment, or use a board of a larger memory class. A reboot takes the boot selection current then |
| **boot preparation failed at handoff / storage / prepare (fault)** (Software) | That step stopped with a fault token: `os:<ERRNO>` for an operating-system error, a named code such as `node_measured_base_abi_mismatch`, or `unexpected:<type>`. Without a parenthesis no fault was recorded. Owner: that step's unit, `photo-wall-node-<step>.service`. Alarm, one incident; its failed unit is not repeated under Base units | Note the fault from the Player page; reboot once. A fault that survives a fresh boot is a defect: report it with the fault and the release |
| **boot preparation running: prepare** (Software) | The step is still running on this boot; prepare fetches the offered root images and mounts them, so it runs longest. A step killed or timed out is not shown as running: its unit fails and the item reads "failed at … (unit failed, no exit record)" | Wait. If it never moves on, the step is hung; reboot the Player |
| **boot preparation failed at handoff / storage / prepare (unit failed, no exit record)** (Software) | The step's unit (`photo-wall-node-<step>.service`) failed but the step wrote no exit record: it was killed before its exit write (an out-of-memory victim, or its start timeout's SIGTERM), or the record write itself failed. Alarm, one incident; the unit is not repeated under Base units | Check Out of memory on the Player page (a base or preparation kill points at memory); reboot once. A repeat on a fresh boot is a defect: report it with the step, the release and the board's memory size |
| **Unknown: failed units not read** (Software, Base units) | Host Management has not read PID1's failed-unit list on this run (`systemctl` was slow or missing). Once read, a later failed read keeps the last reading, so this never hides a failure it already saw, and never reads as "no base unit failed". No band | Nothing; if it persists, check `systemctl` on the box |
| **base unit failed on this boot: photo-wall-app-broker.service** (Software), or **base units failed on this boot: a · b and N more** | A base service failed. A crash-looping base service stops after 10 starts in 10 minutes and stays failed: the box never reboots itself for it. Owner: the named unit. A boot preparation unit is left out here only when Boot preparation reports its step stopped; otherwise it is listed like any base unit. Alarm, one incident per Player | Reboot the Player from its page. A unit that fails again on a fresh boot is a defect: report it with the unit name and the release |
| **Out-of-memory kills on this boot: app 2 · base 1** (Storage) | The kernel killed processes in that slice (app, base or preparation) because it reached its memory limit or the box ran out of memory. A notice, never an incident. The per-slice peaks are in the Player page's Every reported metric (`memory_peak:`) | An app kill: the Player release needs more memory than the board's class leaves it; report it with the release and the board's memory size. A base or preparation kill: report it as a defect |
| **Unknown: not reported** on Boot preparation, Base units, Out of memory or Memory limits | The box runs a base from before the 4 GB tracer, which reports none of these. It raises no tier | Nothing |
| **Last read failed: …**, or " · host health not read" on the strip | The host read failed. The last good values stay, but no host incident is raised or cleared on stale data, and the Status chip reads "host health not read" | Check Central (the pill and its logs) |

**App Manager refused a preparation: needs 1.4 GB, room 0.9 GB** (Storage) means App Manager's newest sample on this boot is a storage refusal: the release it was asked to prepare needs more room than the node store of the board's memory class, free space and memory headroom allow. Nothing is prepared, and App Manager retries on its next poll; a newer sample clears the line. Free space or choose a smaller release.

Host incidents are raised for **Bound** Players only. An Unbound Player's state shows on its row and under Not driving a Frame, never as an incident. The Frame's own health still follows the Player app, so a silent host with a reporting app shows two separate causes.

### Releases, Stage app and Update the wall

The console owns node releases; no step needs curl or a hand-written request. The design is owned by [Part E of the console DDD](operator-console-ddd.md#24-what-part-e-covers-and-why).

**Fleet › Releases** (`#/releases`), top to bottom:

- **Boot selection**: the one deployment Central offers every node-path boot from now on, its revision and the previous selection. On a wall that has never had a selection, Central selects the newest stable release by itself once it is downloaded, and this line says which release and how much is left (or why it cannot); after that, upgrades are always your choice.
- **Deployments**: newest first, the selected one always listed. **Select for every boot…** sets the boot selection. Its dialog states the scope: every Player that boots by node path from now on is offered it, including Players Central has not seen, and Central cannot list which Players will boot. A deployment with no app says every boot from now on is offered no app. If another operator selected meanwhile, nothing is sent: "The boot selection changed meanwhile; review it".
- **Release catalog**: the releases GitHub releases reported, via the media worker. Central turns each valid release into its deployment by itself and downloads the newest three stable releases, the current selection and the previous one in the background; pre-releases are listed but not downloaded ahead. Each row shows one download fact: **Ready**, **Downloading · N GB left**, **Not downloaded**, **Failed: reason**, or **Rejected: reason** (a malformed manifest or a tag conflict; only that release is affected). **Put vX on the wall…** opens Update the wall for that release; on the selected release it reads **Continue putting vX on the wall…**, the way back into a paused rollout. **Check GitHub releases now** queues a catalog check; new releases appear when the worker records them.

A rebooted Player is offered the boot selection current at its next boot; the reboot dialog says so and links here.

**Stage app** (Player page › App) switches the Player's app **for this boot only**; any later boot, including an unplanned one, is offered the boot selection. It is disabled, with its reason, while the Player is retired, App Effect Broker has no current session on this boot, or a switch is in progress. Pick a deployment that carries an app; Central judges it when you send and the dialog shows Central's answer in its words (for example "No qualified fallback for this Player's current Outputs and base: qualify the running app first"). On a Player that drives Frames, each Frame shows the base page while the app switches, then rejoins its Run at the current point, as on Reboot; missed content is not replayed, and bindings and calibration are kept. Only Central's half of this is qualified so far (the dialog says so): the Player's side of a bound switch has no automated scenario yet.

**Qualified fallback** (Player page › App) qualifies the app the Player is running now, so that Stage has a fallback. The Player must be bound and show one steady photo or looping video on every Output for 30 s. **Begin** starts it, and the page asks Central for a sample every 2 s while it stays open and visible. It stops with a reason on a terminal answer or after 2 minutes without progress (a slideshow that changes within 30 s never progresses).

**Update the wall** (Releases › **Update the wall…**, or **Put vX on the wall…** on a catalog row) walks one release onto the wall: optionally **Try it on one Frame** (the page qualifies the running app if needed, then stages the release on that Player), **Look**, and then **Put vX on the wall** or **Back out**. Put on the wall asks for one confirmation, which names the Players it will reboot and that each Frame is blank while its Player reboots. It then sends one Select and reboots the Players this console knows one at a time (the first reboot waits until Central has downloaded the release), the tried Player first, each waiting until the previous one has rejoined (Central's snapshot lists the Player app on its new boot, the target app is linked, every bound Frame is live and no readiness failure is reported; the page's judgement, not proof each Output shows its assignment). You can skip a Player before its reboot; skips are kept in the page's address. Only the Players the confirmation named are rebooted: a Player that enrolls later reads "Not in this rollout", and after a reload Resume names the Players again before rebooting any. A reported readiness failure on the new boot pauses the rollout too. A Player that has not rejoined after 10 minutes, a rejected or unknown reboot, or a hidden tab pauses the rollout with Retry, Skip and Stop. Back out reboots the tried Player; its next boot is offered the current selection. One reboot at a time holds per page only: a second page, or a Player-page Reboot, can reboot another Player at the same time. Nothing about the rollout is stored in Central: closing the page stops further reboots, and on reopening the page re-derives each row and waits for **Resume**. Select is fleet-wide, so any Player that restarts for any reason is offered the new deployment at that boot. **Undo**, when the rollout is done, selects the previous selection again.

### Identify an Output and bind it

You can power on more than one Pi before binding. On a Player's page, each Output that was connected at the last enrollment and is not bound has an **Identify Panel** action, including a Bound Player's free second Output; the Frame page's Hardware tab offers the same action in its Output picker on each candidate. Use it to match a Player page to the physical screen: Central queues a request for that exact Output, and the Player briefly shows a high-contrast **IDENTIFY THIS OUTPUT • `<output id>`** banner on it. The banner lasts at most 15 seconds. A new request for the same Player replaces its previous request.

The console reports that the request was accepted by Central; it cannot confirm that a banner reached the panel. Check the physical screen yourself, then bind that Output to its persistent Frame. Identification requests expire, and Central stops delivering one if its Output becomes bound, disconnected, or the Player's authority changes. The action is disabled, with its reason, on an Output not listed as connected at the last enrollment ("Connect a Panel and restart the Player app") and on a bound Output ("Central identifies only unbound Outputs"). When Central holds no negotiated Identify capability for the Player app's current enrollment (the app has not completed its control hello, or did not offer Identify), the request answers "Central has not negotiated Identify with this Player app's current enrollment".

**Outputs with no Panel listed are not offered.** The Player app lists connected Panels only when it enrolls. An Output whose Panel was off or unplugged at that moment reads "No Panel listed at the last enrollment" and cannot be bound or identified. Connect and power the Panel, then restart the Player; the Output becomes Free.

**Local Central-link diagnostic.** On each connected unbound Output, the Player shows its current local link state: **Central: connecting** before this process has applied configuration; **Central: reachable · configuration received** after a successful state exchange; or **Central: retrying · last configuration received** / **Central: retrying · no configuration received** after a failed exchange, depending on whether this process received configuration before. This is a connection-history diagnostic owned by the running Player process. It does not report boot health, playback readiness, or visible content, and it does not change the content on a bound Output.

You can bind from either end. Both write the same Frame binding, carry the Frame's `generation` token, and are refused rather than applied when something changed:

- **From a Frame.** Open the Frame's page, open its **Hardware** tab, and choose a Player's Output from "Choose an output". Nothing is pre-selected, even when there is one option. Then press "Bind to <frame>". If a refresh removes the Output you chose, the choice is cleared and announced. If the bind is refused ("That output was just bound elsewhere", "That Player is no longer available", or "This Frame changed — reload and review its binding."), your choice is cleared; choose again from the current list.
- **From a free Output.** On the Player page, a Free Output offers a "Bind to a frame…" list: pick an unbound Frame and press Bind. On success the console opens that Frame's page at its Hardware tab.

**After a bind, calibrate the Frame again.** A successful bind or unbind bumps the Frame's `generation`, clears any live calibration lease, and **invalidates calibration**, so the Hardware tab shows **"Review required"** with a **"Set its position"** button that opens the [Position tab](#operator-console-calibration-and-conflict-states). The Hardware tab also shows the bound Player and Output and the Panel record at the Player app's last enrollment. This is a forced re-validation, not data loss: a Frame's readiness is worked out, never stored, and a bind or unbind leaves its latest Position commit behind the new `generation`, so it reads not ready until Position is committed again. Neither writes the `calibration` column, so the committed calibration persists and is available to re-confirm. Only a fresh save overwrites it. Retire changes no binding and no calibration. If the Frame changed underneath you (a concurrent bind or unbind bumped its `generation`), the write is refused with **409 `binding_generation_conflict`** and nothing is applied.

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

**To revoke a compromised bound Pi, unbind it, then retire it immediately.** Unbind alone leaves its session token valid and its serial free to enroll. Retire (`POST /v1/operator/players/{player_id}/retire`) revokes its authority and refuses its serial from then on. Its serial can still netboot ([the removed V1 base selection](#base-image-auto-mirror-0012-removed); owner Question 4 of the [slice 2 design](operator-console-ux-pass2-onboarding.md#12-costs-deferrals-and-questions) is pending).

### Returning known Pi

**Returning known Pi.** When a Pi central already knows reboots, it re-enrolls by hardware serial and central self-heals it straight back to its old bindings — so it reappears already bound (`is_bound=true`) instead of reading Unbound. Recovery is a **serial-match convenience, not cryptographic identity** — the serial is not a secret and central does not prove the box is the same box, only that it presents the same serial. The console shows no banner for it: the Player page header's **Enrollment** fact ("Player app enrolled N s ago (authority epoch N)") is Central's durable record of the latest enrollment, and every boot mints a fresh `authority_epoch`, so a new epoch on its own is normal, not an incident.

## Operator console: calibration and conflict states

Every Frame has **one page** (`#/wall/frames/<id>/<tab>`), opened with one click on its tile on the Wall plan (or its Unplaced tray entry, a To finish link, a Needs attention entry or a Player page's Frame link). Its heading is the Frame's name, with a link back to the Wall and its health. It has five tabs: **Overview** (what Photo Wall sends to it now and what waits underneath, how its Pi is, and how to put content on it; Central's own account, its Runs and their priorities, is under **Details**), **Position**, **Picture**, **Power** ([display identity and power](#display-identity-and-power)) and **Hardware**. Position and Picture are reachable **only from the Wall section**; calibration is a hardware concern deliberately hidden from show programming, which sees only a Frame-health label ([R4](#operator-console-sections-links-and-drafts)). The page is `central/console/src/pages/frame-page.tsx`.

**Position** sets where the picture sits on the Display. Its **Fit** is **Cover** by default: the four corners are the opening, the picture keeps the Frame profile's shape, turned by **Rotation** (the 0, 90, 180 and 270 buttons, or any angle to a tenth of a degree), and is the smallest such rectangle that fills the opening; the angle never changes the profile or the Frame's orientation; nothing is drawn outside the corners. **Corners**, an explicit choice, stretches the picture exactly onto the four corners and turns it only in quarter turns. Drag a corner of the drawing, or choose **Move** (the whole picture or one corner) and a **Step** of 1, 10 or 50 pixels and press the arrow buttons (or the arrow keys on the focused drawing). **Reset to full screen** keeps the fit and sets the angle to 0; under **Exact values** each corner is in pixels of the Pi's output. **Picture** has two sections that never affect each other ([0022](decisions/0022-picture-hardware-and-software-apart.md)). **Display (hardware)** holds the display's own picture settings, the ones on the picture list it answered when its Pi asked over the HDMI cable's settings channel (DDC/CI), in the display's own count; **As the display has it** hands one back to the display's buttons, and nothing is written for it. **Photo Wall (software)** holds the four the Player applies to the picture it sends: brightness and contrast 0–150 % (100 % changes nothing), colour temperature and gamma. Both belong to the Display, so they follow it to another Pi. The controls act live with their own session, **Done** and **Revert**, and **Copy to other Frames** copies the four Photo Wall values and each display value set in Photo Wall that the target's display offers the same way, naming the rest as skipped, never a position ([display-control skill](../.claude/skills/display-control/SKILL.md)).

**Changes show on the Display as you make them.** While the Position tab is open (and the browser tab is shown), the page keeps a live session with Central (Central calls it a calibration trial): each change is sent to Display Host on the Frame's Pi. The bar at the bottom of the tab reads "Previewing — waiting for the Pi" until the Pi presents the latest change, then "Presented by the Pi · 21:04:07" (Central's time of the acknowledgement). **Done**, in the same bar, saves the change and is offered only once the Pi has presented the latest change (Central refuses a save otherwise); the bar says why when it is not. **Revert** ends the session and returns the controls to the saved values. The session does not time out while the tab is open: the page sends `keepalive` (with its status read) every half second, and before Central's hard limit (110 s, under the Node's own 120 s cap) it hands the session over with `renew`, which ends it and begins the next one at your draft in one step, so the Display never shows the saved values in between. Your draft keeps the saved revision it was made from and sends it with every change, `begin` and `renew`, so a save made elsewhere meanwhile is never overwritten: Central refuses the draft and the page asks you to Revert. Leaving the tab with changes not kept asks **Keep** (Done), **Revert** or **Stay**; leaving the page reverts them, and the page says when on your next visit; hiding the browser tab ends the session at once and showing it again resumes your draft; a closed browser tab ends within Central's 5 s idle window. A refusal that passes (the Pi briefly away) shows while the page keeps trying and goes by itself. Every geometry change is validated by **the same convex test the server enforces** (the `1e-6` epsilon and clockwise winding): a folded or too-thin picture or a corner past the screen's edge is refused where it is made with a message such as **"corners must form a convex aperture"**, and nothing is sent. A background refresh never overwrites the draft.

A Frame whose Pi runs software without Display Host says "This Frame's Pi runs software that cannot show changes live" and links to its Software page; an unbound Frame says to choose its Pi and HDMI output on Hardware first.

| What happened | What you see | What to do |
|---|---|---|
| Another window or operator is adjusting the same Frame | "Someone else is adjusting this Frame. Photo Wall connects as soon as they finish." | Wait; an abandoned session ends within 5 s. |
| Someone saved this Frame's position or picture after your session began | "Someone saved a different position or picture for this Frame meanwhile. Press Revert to load it, then adjust again." | Revert, then adjust again; your stale Done was **refused**, not applied. |
| The Pi has not presented your latest change | Done stays disabled ("Previewing — waiting for the Pi"; "Done is offered once the Pi presents your latest change.") | Wait; check the Display and its Pi if it does not change. |
| The Pi or its Display is not reporting | The refusal in plain words ("Retrying"), while the page keeps trying; Central's code under **Details** | Check the Pi is on and the Display plugged in; it clears by itself. |

**Hardware** holds the Pi and HDMI port feeding the Frame (**Connect Frame …** from a free port, **Disconnect from this Pi**, **Identify display** on a free port, and the Display the Pi reported when its photo app last started; Central's record, the Player app's last enrollment, is under **Details**) the **Display** the Pi reports on that port (make, model, serial or "Recognised by make and model on this Frame", and the modes it lists, read-only; with a **Display changed** card when it is not the one the Position was set against), and the **Frame profile** (pixel width and height, diagonal, video capable). The Frame profile is **operator-declared at Frame creation and persists across a Display swap**; **Edit Frame profile** works on a Frame with no Pi, or while its display has changed, and clears its position. When both resolutions are known, the Frame profile is compared with the resolution the Pi last reported, in the saved rotation when the position is set; a mismatch is a "Check the Display" card naming both values, with **Edit Frame profile** as its action and how to restart the photo app or change the profile under Details. The record may be stale; it never changes the profile.

A Frame whose Pi did not start from Photo Wall (Central holds no node boot for it, `not_admitted`: a Compose or demo Player) cannot be positioned from the console: it is told so and linked to its Pi's page. Central has no other calibration path; the legacy preview and its `POST /v1/operator/frames/{id}/calibration` route were deleted in PR 45. The wall demo and the tests save a Frame's Position through Registry's Save compare-and-set, the one a trial's Save crosses (`tests/support/position.py`).

Power is on the Power tab.

## Operator console: running the show

The Show sections (**Now**, **Scenes**, **Schedule** and **Sources**) are the content-and-schedule layer: Sources, Scenes, Programs and Runs. The wording and states below are owned by the [slice 3 design](operator-console-ux-pass2-showrunner.md), and the step flows by [passes C and D](operator-console-ux-pass2-flow.md#7-the-flows-defaults-have-a-source); this section is how to use them, job by job.

**Show sections never show a Frame's Position or Picture (R4).** Hardware setup (position, brightness and the Frame profile) lives only on [the Frame's page](#operator-console-calibration-and-conflict-states) on the Wall. The **only** hardware fact the Show sections see is each Frame's **health label**, the same one the Wall shows ([wall health](#operator-console-wall-health-and-the-attention-strip)), including "Needs calibration" when the Frame is not ready (its worked-out `readiness` is not `ready`; [display identity and power](#display-identity-and-power)): a Frame that cannot present matters at show time. The label is **status, not a control**; you fix the cause on the Wall.

### Step flows: one question at a time

Making a Scene, scheduling it, adding a Source and showing a Scene now are **step flows**. Each step asks one thing. A stepper above it names the steps (under 850 px it reads, for example, "Step 3 of 5 · Frames"), and earlier steps in it are buttons that go back to them. **Back** and **Continue** sit below the step; Back on the first step returns to the section. Values that have a stated default sit under a collapsed **Advanced** on their step, whose summary line says what is inside. The last step, **Review**, lists every answer, the advanced ones included, each with **Change**, which opens its step.

- **Continue checks only the step you are on.** Its reasons appear beside its fields and focus moves to the first one; later steps say nothing until you reach them. After a Change, Continue returns toward Review, stopping at any step that still has a problem.
- **The final button** (Save Scene, Replace Scene, Schedule Program, Add separate windows, Save Source, Activate now) checks every step. With problems it sends nothing: a list at the top of the step names each, and choosing one opens its step (and its Advanced) with focus on the field.
- **A button is never disabled over a problem.** Only a write in flight disables one, and the step is read-only meanwhile; Replace Scene also waits while the Scene is stale (below). **Finish** on a Run that is already finishing is disabled, and its card's "Finishing: requested 20 s ago" says why.

Drafts, addresses and browser Back are described in [sections, links and drafts](#operator-console-sections-links-and-drafts).

### Names and ids

You **name** Scenes and Programs on their Review step; the console derives the id Central stores. Accents are dropped, letters are lowercased, every other run of characters becomes `-`, and the id is cut at 96 characters: "Family Evening" is shown as "Saved as `family-evening` · Change". Central keeps only the id (there is no stored display name), so cards and Runs show ids.

Type an id yourself, with **Change** (it opens Review's **Advanced** with its **Id** field), when:
- the name has no Latin letter or digit: Advanced stays open with "This name needs a Latin letter or digit for its id; type an id."; or
- you want a particular id. An id starts with a letter or digit, then letters, digits, `-`, `_`, `.` or `:`, up to 128 characters.

A name whose id already exists is refused before anything is sent ("A Scene called `family-evening` already exists; choose another name."). The check uses the last refresh, so two operators can still pick the same new id within a few seconds of each other. For a **Scene**, Central refuses the later save: "A Scene with this id was saved meanwhile; nothing was replaced." Choose another name, or Edit the stored Scene. For a **Program**, the later save still replaces the earlier one (Program saves have no guard; slice 3 Question 4). Activation ids are never shown.

### Now: show a Scene now

Press **Show now** on Now, on a Scene's card, or after saving a Scene. The flow is **Scene → Review** (`#/now/show/scene`, `#/now/show/review`):

1. **Scene to activate**, with the frames it reaches and their health. A Frame that needs attention links to its Frame page's recovery tab; a healthy Frame remains informational. The health label is Central's latest accepted Player readiness report, not confirmation of visible pixels. Opening a recovery link and returning keeps the Show now draft. From a Scene's card or a Scene's Save it is already chosen.

Both Show now steps also show a **Saved Sources** panel: "A Source keeps playing what it has found while it is checked, and while Immich cannot be read." It lists each live Source of the selected Scene with its check state in the [media pipeline's](#the-media-pipeline) words and **Check now**. Check now asks Central once (`POST /v1/operator/sources/{ref}/checks`) and shows "Check requested. Photos keep playing while Photo Wall reads Immich." until Central serves a different check for that Source; then the row shows the new state. The comparison is with what Central served, never with the browser's clock. If the request's outcome is unknown, it says "The request's outcome is unknown. Look at the Source's state before asking again." The state does not mean media is prepared for a Frame, or that anything is visible. A hand-picked Scene reads "This Scene uses hand-picked media; checking a Source does not change its chosen items." The state is advisory: it does not block **Activate now**.
2. **Review** lists the Scene, its frames, the **Priority** and **If it is already running**. Under **Advanced** are **Activation priority** and **If it is already running**. Press **Activate now**.

**The priority defaults to the Run already on top.** Review reads, for example, "5 (the default: the highest Run on its frames has priority 5; at equal priority the newer Run shows on top)", or "0 (the default: no Run covers its frames)". It is the highest priority among the live Runs covering any of the Scene's frames. That is enough to show on top, and one more is not needed: Central ranks layers by priority, then by admission order, and at equal priority the Run admitted later wins, so the new Run shows over the one already there ([flow design §7 J7](operator-console-ux-pass2-flow.md#7-the-flows-defaults-have-a-source)). Until you type a priority, the default follows the Runs as they change, even while Review is showing. A priority below it holds Advanced open and says where the Run would stay, for example "At priority 3 this stays underneath the Run of evening (priority 5) on lobby-left." No priority shows it on top of a Run that **protects** one of its frames: Central refuses the activation at any priority, so Review drops "shows on top", holds Advanced open and says, for example, "Central will refuse this at any priority: lobby-left is protected by the Run of evening." Finish or cancel that Run first.

**If it is already running:**
- **Leave it running** (default): nothing changes; the outcome reads "Not started: evening is already running, left as is."
- **Restart it:** ends the current Run and starts a new one now. A restarted Run has **no Program end**. A Scene with Keep playing on plays until you Finish or Cancel it; one with Keep playing off plays one cycle, then ends.

**Activate now** (`POST /v1/operator/activations`) answers synchronously, and the console shows exactly that answer on Now:

| Outcome | Reads | What to do |
|---|---|---|
| Admitted | "Started: Central admitted a Run of evening." | Nothing. It is Central's Run, not confirmation from the panels. |
| Refused by protection | "Not started: lobby-left is protected by the Run of evening." | The Run named is the one Central reported as blocking. Finish or cancel it, or wait until it ends. A Scene that itself protects a frame covered by a higher-priority Run reads "Not started: this Scene protects frames that evening's Run (priority 5) covers; use priority at least 5." |
| Other refusal | "Not started: 16 activations are already waiting.", or "Not started: `<error>`." | Correct the cause and try again. |
| Outcome unknown | "Outcome unknown. Try again; it will not start twice. Changing the form makes this a new activation." | The request timed out, failed, or Central answered with a server error, so the Run may or may not have started. **Press Activate now again, unchanged.** |
| Session ended | "Not started: the session ended. Sign in again, then activate." | Sign in; the draft is still there. |

**Why a retry cannot start the Scene twice.** Each activation carries a hidden key. It is made when the draft opens and again whenever you change a value (choosing the value already there changes nothing). It stays with the draft across steps, sections, a Wall visit and the sign-in screen. After an unknown outcome the flow stays on Review with the same key, and Central answers a key it already knows with its stored result. A known outcome ends the flow. The flow never replaces a draft that holds changes or awaits a retry; a clean one follows the Scene you last saved or picked.

Queueing an activation and overriding protection ("force") are not offered: design bead 3B-3 is deferred until the owner answers its Question 6 ([slice 3 design](operator-console-ux-pass2-showrunner.md#18-costs-deferrals-and-questions)).

### Now: Runs and the planned fact

Now starts with each Frame's health label, then the **Runs**: "Central's Runs, not a readback of the panels. Each Frame's Why? shows which Run is on top there." Each live Run is a card named `Scene X`, with a **Running** or **Finishing** chip and its state ("Running", "Ending (outro)" or "Finishing: requested …"), where it came from ("Program Y", "Program Y, since removed", "started directly (Show now or the API)" or "part of Z's Run"), when it started (every clock time carries its zone, for example "18:00 BST", and Now and Schedule say "Times in Europe/London (this browser's time zone)"), its cycle, its priority, the frames it protects, its frames with their health, and its child Scenes. With none, it reads "No Run is running." **Finish** (`POST /v1/operator/runs/{id}/finish`) asks for a natural end. **Cancel** (`…/cancel`) asks for confirmation, then stops the Run now, skipping its outro; its child Scenes stop too. Runs that ended in the last day are under a closed "Recently ended (N)", as Completed and Cancelled.

Below them, **Central's Runs per frame, and why nothing new** has one row per frame with two disclosures. **Why?** (and the Frame page's Overview on the Wall) states **Central's Runs** on that frame, for example "Central's Runs on lobby-left: evening (priority 5, Program weekday-evenings) on top." The Plan tile and the Overview first show the same answer as one `planned` fact: "On top: evening · Program weekday-evenings (Central's Runs; media not checked; the Panel is not observed)"; on an unbound frame the basis reads "this Frame is unbound, so Central sends it no layers", and with nothing visible it reads "On top: nothing · no Run puts a layer on this Frame now (…)". It names Central's projection, made before any media is chosen, never what the Panel shows. Each layer underneath gets one sentence, always with its **priority N**:
- a lower priority: "morning (priority 1) is underneath: evening has priority 5.";
- the same priority: the Run Central **admitted later** is on top. This is admission order, not the Program's start time; Programs starting at the same instant are admitted in Program-id order;
- the same Run: the later child Scene is on top.

**Its limits are always shown.** If the winner has no usable media for this frame (none eligible, still preparing, or no compatible variant), Central plans the next layer down instead. An unbound frame gets no layers at all. A partly transparent or fading layer shows what is underneath. The panel reports what Central intends; it never says a frame is LIVE or confirms what a panel displays (R2). **Why nothing new?** is the next section.

### Why nothing new on a frame?

On Now, open **Why nothing new?** on the frame's row. It opens its own group beside Central's Runs, **"Why nothing new on lobby-left?"**. It walks from intent to equipment; each step restates a served fact, and the first step that is not ok is marked **Stops here**. Fix that one first.

| Step | Stops when | What to do at that stop |
|---|---|---|
| 1. Intended? | "On top: nothing · no Run puts a layer on this Frame now". (If a Run on the frame ended, this step is only informational and the chain stops at step 2.) | [Show a Scene now](#now-show-a-scene-now), or [schedule a Scene](#schedule-a-repeating-schedule-step-by-step), that targets the frame. |
| 2. Run ended? | The last Run on the frame ended ("evening's Run ended at 18:00:30 after one cycle") or was cancelled. If the Scene keeps its last still, it adds "if its last item was a photo, the frame keeps that still (a video is not kept)". | "After one cycle" means Keep playing was off: edit the Scene and turn it on, then start it again. |
| 3. Authored? | The winning Scene uses fixed, hand-picked media ("new photos never appear by design"), or shows black by design. | Nothing is wrong. To show new photos, use a live-source Scene. |
| 4. The Source | None of the Scene's Sources has photos in its pool: "None of this Scene's Sources has photos yet.", or "Photo Wall is still reading this Scene's Sources." while a check runs. Each Source is named with its pool's count and its check state. A Source whose last check failed but whose pool has photos passes: its photos keep playing. | Read the [Source states](#the-media-pipeline) below; an empty complete check means the Source matches nothing, so widen it. |
| 5. Check this frame | Press **Check this frame**. It reads every Source's pool items that fit the frame's shape and counts what Central would do with them: "12 usable · 3 still preparing · 1 failed to prepare · 2 with no compatible version". It stops on "Nothing usable yet: …" or "No item in the Source fits lobby-left's shape." | Still preparing: wait for the worker. Failed to prepare: read the worker line. No compatible version, or nothing fits: the frame's shape (for example portrait) excludes the Source's items; widen the Source. An item two Sources share is counted once; a Source whose last check failed still counts, as planning still plays its pool. |
| 6. The worker | The worker is not ok. | See the worker line below. |
| 7. Frame health | The frame's health is not ok. | Fix it on the Wall ([wall health](#operator-console-wall-health-and-the-attention-strip)). |

**Limit:** the check is a count of each item's standing. It does not report which item Central picks for the next cycle; with some items usable and others not ready, a cycle that lands on one not ready plans the next layer down, as the step's note says.

### The media pipeline

The **Media pipeline** panel is at the foot of Now (it is left out while the Show now flow shows a step). Central fetches media from the photo library and prepares it; Players get it only from Central. All media ages are on one clock, the database's: the worker's and Central's times are both stamped by it, so a skewed server clock cannot raise a false "quiet" or "overdue" ([one media clock](module-media-worker.md#one-media-clock)).

**Worker.** One line:
- "Media worker last reported 40 s ago · preparing 3 · waiting 12 · failed 2 · failed, retry pending 1 · cache 4.1 of 8 GB" when it is healthy. Preparing is running or publishing; waiting is queued; "failed, retry pending" appears only when a failed job is waiting to be retried (planning treats it as failed until then). Only jobs of the **current preparation recipe** are counted; a recipe change fails the old recipe's queued jobs and planning asks for them again.
- "never checked in", "reported: storage is full" (or another reported error), or "quiet for 14 min" (no check-in for over 11 min; it checks in every 5 min) is an alarm, and a separate **Jobs and cache** line shows the counts. Check the worker process and its logs; for storage pressure, free space or raise the cache limit.

**Each Source** gets a row: its plain name, its state, how far a running check has read ("Checking: 12,400 photos so far") and "Last complete check 21:04". A **check** is one complete read of the Source's queries from Immich, which runs every 15 minutes, on Save and on **Check now** ([how a check works](module-media.md#live-discovery-pagination-and-metadata)). The pool is what the checks confirmed; **a check removes photos only when it completes**, so a failed or half-done check never empties a Source, and the wall keeps playing the pool it has. That is why a failed check is a notice, never an alarm.

| Check | Reads | What to do |
|---|---|---|
| Never checked (to-do) | "Not checked yet" | A new Source, or one on a connection that has no key yet ([connecting Immich](#connecting-immich)). |
| Checking | "Checking: 12,400 photos so far", with " · last complete check 21:04" after an earlier one | Nothing: photos join page by page and play from the first page. |
| Complete | "Checked 21:04" (with its day when not today) | Nothing. |
| Incomplete | "Checked part of it: more than 1,000 photos share one moment" | Nothing removed; the pool keeps what it found. Narrow the Source (for example by dates) around that moment. |
| Failed | "Immich could not be read: see Photo library. Photos keep playing." | The connection needs a look; its card on [Settings › Photo library](#connecting-immich) names the fix. |
| Failed | "An album, person or tag in this Source was deleted in Immich. Photos keep playing." | Edit the Source and choose again. |

**A check never stays stuck.** A check whose next step has not begun for 15 minutes (its worker died, say) is resumed from where it stopped by the next 30-second tick, and a dead worker's unfinished media job, preparation included, is handed back to the queue within about a minute ([the media queue's rescue](module-media-worker.md#the-media-queues-rescue)). Neither needs Check now or a restart.

**The pool beside the Photo source editor** words the same check with its counts: "12,400 in the pool so far · up to 41,203 in Immich" while checking ("up to" is Immich's own count, which includes partners' photos, stacked copies and exclusions), "2,301 photos · 14 videos · checked 21:04" when complete (zero here is truly empty: "No photos match."; widen the Source). Under the counts: "Immich is still reading 37 photos" (Immich has not read their size yet; they join once it has), and the photos left out by reason, "2 could not be read" (Immich's answer for that photo did not parse) and "1 is too large" (over Photo Wall's pixel, size or video-length limits). The counts are photos in the pool, not photos ready for a particular Frame.

### Scenes: make a Scene step by step

A Scene is a per-frame composition. Scenes lists every stored Scene as a card, then **New Scene**. The flow (`#/scenes/new/<step>`) asks:

| Step | Asks | Default |
|---|---|---|
| 1 Kind | **Live from a Source** (each frame shows the Source's media as it changes, re-checked centrally) or **Hand-picked per frame** (you choose one item for each frame). It comes first because it decides whether step 3b is asked. | Live |
| 2 Frames | The target frames, grouped **"Frames on `<surface>`"** and **"Frames not on any wall"**, each with its [health label](#operator-console-wall-health-and-the-attention-strip). Asked before Photos (follow-ups C1), so Photos can show what fits them. | none: required |
| 3 Photos | **Photos from**: one or more saved Photo sources as cards, chosen by checkbox; several Sources play the photos of all of them. **New Photo source** opens the [Photo source editor](#sources-add-and-manage-a-source) and brings you back with it chosen. Under them the Fit board: how the chosen Sources' photos fit the chosen Frames. | none: at least one |
| 3b Media per frame | Hand-picked only: one chooser per frame. | none: required per frame |
| 4 Playback | **Each photo shows for** (seconds); the Crop limit (unset follows Settings › Photo fit) with the Fit board answering it as you drag; under Advanced, **Keep repeating**. | 30 s; Keep repeating on |
| 5 Review | **Scene name** first; under Advanced, its **Id** (derived from the name); every answer grouped by step, one **Change** per step; the compact Fit board, with **Raise the crop limit to N %** for a Frame nothing fits. Then **Save Scene**. | — |

- **Source readiness.** Photos and Review show each chosen Source with its check state in the [media pipeline's](#the-media-pipeline) words, **Check now** (with the same receipt as on Show now), **Remove** on Photos, and its readiness: "Still being read: what is found so far plays." while a check runs, or "This Source has no photos." when its pool is empty. **Manage in Sources** opens the Source list, and the Scene draft stays in this tab for when you return. Live Scenes can be saved while a Source is not ready; saving does not guarantee a panel has usable content. Every Frame of the Scene gets the same Sources, in the order chosen. Hand-picked Scenes still require that each choice is in its Source's pool and compatible at save time.
- **Frames.** A legacy frame id containing `:` (or longer than 96 characters) is listed with the reason no Scene can target it, and cannot be ticked. A frame you ticked that is deleted while you draft is dropped and announced on whichever step shows ("lobby-left was deleted and removed from this Scene.").
- **Media per frame.** Each chooser lists **only media compatible with that frame's profile**: Central hard-filters the candidates by profile (`GET /v1/operator/sources/{ref}/candidates?frame_id=`), so an incompatible item cannot be chosen. While they load it reads "Loading compatible media…"; a frame with none is a problem routed to Frames; a failed read offers Retry. **Reload compatible media** rereads a successful list, for example after a check or when another operator changes the Source. Each choice is labelled with its kind, size, capture time and what Central would do with it on that frame, for example "Photo 108×192 · dated 3 Mar 2025 14:02 · ready" (or "preparing", "failed to prepare", "no compatible version"); "(2)" is added only when two labels would otherwise read the same. A chosen item that is no longer among a frame's candidates is dropped from the draft, whichever step shows.
- **Save** stores the Scene and every per-frame choice in **one request** (`PUT /v1/operator/scenes/{id}`, or `…/authored` for a hand-picked Scene); Central re-checks pool membership and compatibility. The flow returns to the cards with "Saved Scene X." and offers **Show now** and **Schedule it**.

**Keep repeating** (until the Program's window ends) is **on by default** for new Scenes (slice 3 Question 1, pending owner confirmation):
- **On.** In a Program, the Run keeps cycling until the window ends, then stops at the end of the cycle running at that moment, so it can **overrun the window by up to one cycle**. Shown without a Program, it plays until you Finish or Cancel it.
- **Off.** The Run plays **one cycle, then ends**: a 30 s Scene in an 18:00–20:00 Program ends at 18:00:30. Its Run card reads "plays one 30 s cycle, then ends". Scenes saved by earlier console versions were always saved this way.

Playback also holds the Scene settings Central already played but the console could not reach ([roadmap 1x](roadmap.md#1x-expose-what-already-exists)). Each row shows its default and offers **Reset** once changed; the flow's Save applies them like every other Scene value:

| Setting | Default | What Central plays |
|---|---|---|
| **Fade between photos** (0–5 s) | 1.5 s for a new Scene | Each photo fades out over half of it and the next fades in over the other half, through black or what plays beneath (every body Contribution's `fade_out_seconds` and `fade_in_seconds`). It must be no longer than **Each photo shows for**. |
| **How it ends**: Stops, Black or Fades out, with **Ending length** (0.5–10 s) | Stops; 3 s once chosen | The Scene's outro after a Finish or its Program's end: **Black** is opaque black covering the Frames for the length; **Fades out**: the last photo returns over half the fade between photos, then fades out over the rest of the length to reveal what plays beneath (the length must be at least half the fade). Either ending keeps nothing after it (`after_end: keep_nothing`): the Frame is then black, never this Scene's kept photo again, during an outage too. When another Scene plays beneath, the ending gets out of its way instead: that Scene takes over, its own kept photo included ([what a Frame keeps](execution-contract.md#what-a-frame-keeps)). |
| **Keep the last photo up** (Advanced) | On | When nothing new can play on a Frame, even after the Scene stops, it keeps its last photo instead of going dark (`after_end: keep_this_photo`); when nothing else plays on the Frame after the Scene, Central plans its last cycle with no fade-out, so the kept photo never dips to black and snaps back. While another Scene plays beneath it, the photo is that Scene's to keep: when this Scene ends, the one beneath takes over, its own kept photo included. Central owns these rules and the Player applies them ([what a Frame keeps](execution-contract.md#what-a-frame-keeps)). |
| **Keep these Frames together** (Advanced) | Off | While the Scene plays, Central turns away other Scenes that need any of its Frames, and does not start it while something more important covers them (`protect_frames`). |

### Viewing and editing a Scene

Each Scene card, `Scene X`, shows what feeds it ("live from `family`", or "authored: 3 chosen items"), its frames with their health, its cycle ("30 s per cycle, keeps playing until its Program ends or, when started by hand, until you Finish or Cancel it", or "plays one 30 s cycle, then ends"), the Programs that use it, and a **Running now** chip while a Run of it is live. Its actions are **Edit**, **Show now**, **Schedule it** and **Delete**. Saved revision numbers are used by Central to protect concurrent work, but are not shown on Scene or Run cards.

**Delete** confirms removal from future choices. It does not stop a Run or erase completed Run history. Central refuses deletion while a Program refers to the Scene, a live Run or queued activation has captured it, or another stored Scene embeds it. The refusal names the blockers; remove or edit those references first. The console checks that the Scene has not changed since its card was loaded. If the Scene being edited has unsaved changes, the confirmation says they will be discarded; a successful deletion closes that draft.

**Edit** is offered only when the console can save the Scene back **without losing anything**. A Scene written through the API with features the flow cannot author (child Scenes, opacity, unequal fades, settings that differ per Frame, values the sliders cannot show exactly, and similar) reads "Edit unavailable: Uses features the console can't edit yet (child Scenes, see-through photos, different settings per Frame, or values outside its ranges)." instead; change that Scene through the API, since a save from the flow would silently drop those features.

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
| "Not replaced. That item is no longer in the Source; choose again." | A chosen item left the Source's pool. The choosers reload. | Pick again and Replace. |
| "Central did not answer. Check this after the next refresh." | Central answered with a server error, so the save may or may not have been stored. | After the next refresh, review the Scene card and its saved answers. An identical retry is safe if the outcome is still unclear. |

Pressing Replace again with exactly the same Scene after an outcome you did not see is safe: Central accepts an identical save of the stored revision.

### Schedule: a repeating Schedule step by step

A Schedule (Central's and the API's **Program**) plays one Scene every week on its days, from its start to its end, at a named priority (`POST /v1/operator/programs`, `PUT …/{id}` to save, `POST …/{id}/apply-now`, `DELETE …/{id}?revision=`). Its windows are worked out by Central on the house clock (Settings › House), never by the browser ([module-runtime](module-runtime.md), [0021](decisions/0021-power-timetable-and-repeating-schedules.md)). The Schedule page (`#/schedule`) shows the week, then the Schedules as cards, then the Default Scene and Night off. **New Schedule**, or **Schedule it** on a Scene's card or after saving a Scene, opens the flow (`#/schedule/new/<step>`):

| Step | Asks | Default |
|---|---|---|
| 1 Scene | **Scene** | the Scene you last saved or picked |
| 2 When | **Days**, **Starts** and **Ends**, each a clock time or sunrise or sunset plus or minus minutes; an end at or before the start is the next day, shown as "(ends next day)" | every day; none: required |
| 3 Review | Every answer with Change; the **Schedule name**; **Priority** (Normal, Special event, Always wins) | Normal |

- **The Default Scene** (the week page) plays wherever no Schedule does, beneath every one; Nothing leaves those Frames with their last photo. It never protects its Frames.
- **Saving a change applies from the next window**: a window playing now keeps its end, and the card names it until it ends, then the new rule's next window. **Save and apply now** finishes the window playing now and plays the new rule's window at once. A Schedule made during one of its own windows plays at once, for the rest of that window (owner, 2026-10-12: "Start right away, for the rest of tonight's window"); a window of a saved rule that started before the save does not play, and the week draws it dashed and says so in its name.
- **A handed-over Scene never replaces your changes.** Pressing Schedule it for another Scene while a draft holds changes keeps the draft and offers "Schedule Scene X instead".
- **Removing** a Schedule whose window is playing asks first: its Scene finishes at the end of its current cycle, after any outro, and no later window plays. The card says whether Central accepted, refused or gave no answer; after a refusal or no answer, check the card before retrying.
- **Edits are compared** with the Schedule you opened: a change made elsewhere since is refused ("changed since you opened it"), and the flow offers Reload.

### Reading a Schedule's card

Each card names the Schedule, its Scene, its days and times, the window playing now or the next one (Central's, on the house clock), its priority and a state. "Playing" (ok), "Refused" (alarm: a protected Scene on one of its Frames kept the window out; the card names it) and "No window" (to-do: none in the coming week, as a sunrise time on days without one) carry a chip. Why is this playing on the Frame page names the Schedule on top and what it beat. If Central was down during a window, it catches up when it is back: the card reads the window as played even though the wall showed nothing, because it describes Central's plan, never what the panels showed.

### Sources: add and manage a Source

The page opens with: "A Photo source asks Immich for photos: albums, people, a place, dates, favourites and tags, minus what you leave out. Photo Wall never uploads, edits or deletes anything there." A Photo source is **a saved question to Immich** with a plain name (for example, `holiday`): never a downloaded album and never something a Player browses or opens. Its answer, the **pool**, updates by itself ([how a check works](module-media.md#live-discovery-pagination-and-metadata)). Sources lists each Source with its [check](#the-media-pipeline), what it includes in one line ("Anna and Ben · last 3 years · no screenshots"), **Check now**, **Edit** and **Delete**. Central manages internal Source revisions; the console does not ask you to number them.

**New Source** and **Edit** both open the **Photo source editor**. Every change is checked by Immich straight away: the editor keeps an unsaved **draft** whose check really runs, so the pool beside the form fills page by page as Immich is read, before you save. Name it and **Save Source** (or **Save changes** when editing) at any time, even mid-check; the saved Source keeps the pool found so far. An unsaved draft is removed after a day.

| Part | Asks | Default |
|---|---|---|
| Photo library | Which Immich (#14). Hidden when there is only one; choosing another starts the query again. | the only one |
| **Include** | Albums (#15) and people (#16): "All of these must be in the photo", with "For any of them, add another Source to the Scene" (#17). A place (#18): city, state and country chosen together. Dates (#19, #20): **Any time**, **Between** two dates (in this browser's time zone), or **In the last** N years and months (counted back from each check on the house calendar, Settings › House). **Favourites only** (#22). Tags (#23): every chosen tag, each including its nested tags. Media types (#25): **Photos**, **Live photos**, **Videos** (at least one). **Include partners' photos** (#32). | everything in the library; all three media types; partners off |
| **Exclude** | **Leave out albums** (#26), **Leave out people** (#27), **Leave out tags** (#28), **Leave out screenshots** (#31) | none; screenshots left out |
| The fixed line | "Archived, hidden and locked photos are never included" (#29, #30): not a switch | — |
| Name | **Name**: what Scenes and Show now call this Source (like `holiday`) | none: required to save |

- **The pool beside the form** shows the check as the [media pipeline](#the-media-pipeline) words it, the counts by outcome, and the photos in the pool as thumbnails, newest first, 60 at a time with **Show more**. The thumbnails are Immich's own, edits included, fetched by the media worker; the browser never talks to Immich. Changing the query starts its pool again from the top (a query you had before reuses what was found for it).
- **Pickers.** Albums, people, places and tags are searched by name from lists the media worker reads from Immich with each connection check (every 15 minutes). Something made in Immich a moment ago may need up to 15 minutes, or **Check now** on Settings › Photo library, to appear.
- **"Any of them".** Several albums, people or tags in one Source mean all of them, as Immich searches. For photos of Anna *or* Ben, make one Source for each and put both on the Scene (Scene editor › Photos).
- **Screenshots** are files whose name contains "screenshot", and PNGs with no camera model (the editor's hint says "camera maker"; Immich can only be asked for a missing model). A scanned PNG with no camera details is therefore left out too; turn Screenshots off for a Source that needs one.
- **Save** makes the Source (or its next revision, keeping its name) and returns to the list with "Saved Source holiday.". Scenes that use the Source move to the new revision for future Runs; already admitted Runs keep their prior selection.
- **Check now** on a saved Source asks for a check at once and shows "Check requested. Photos keep playing while Photo Wall reads Immich." until Central serves the new check.
- **Delete** asks for confirmation ("Delete Source holiday?", "Photo Wall stops using Source holiday. Nothing in Immich changes.", **Confirm delete**) and removes the Source from new selections. If a Scene still uses it, the delete is refused: "Used by Scenes evening, morning. Edit them to choose another Source first."
- **From a Scene.** **New Source…** on the Scene editor's Photos step opens this editor for your Scene; Save returns there with the new Source chosen.

There is deliberately **no** "open in Immich" link and no key field in the editor: the key lives only on the connection ([connecting Immich](#connecting-immich)). On this day (#21) waits for delivery 5a's Order; a minimum rating (#24) and skipping blurry photos and duplicates (#33) are deferred.

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

CI runs the [checks](../.github/workflows/checks.yml) as parallel jobs: `static` (ruff, import contracts, documentation links), `unit` and `db` (four xdist workers each), `browser` (below), `image-smoke` (builds the central and media worker images, starts Compose and checks central HTTP health, the served console, and that the worker reached the hub and created WALL), `node-bus` and `bus-fence` ([the Node bus](#the-node-bus-and-centrals-hub)), and `linux-media`, which runs all preparation tests inside the pinned Linux worker image, so missing host FFmpeg cannot silently remove that gate. Each tier job's timeout is about twice its expected time and it prints its 25 slowest test phases. The named photo-source tests against one real Immich (the `photo-sources` job, [Immich fixture](module-immich-fixture.md#the-photo-source-content-set)) and the two fault segments of the two-Player/three-Output wall scenario run as three parallel jobs of the [software e2e](../.github/workflows/software-e2e.yml) (see the [wall demo](module-wall-demo.md)); the `db` job leaves the `immich` tests to it. Passing CI does not establish physical Pi/PXE, rendering or visible timing.

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

All operator mutations use browser controls, and every page rejects uncaught JavaScript errors. The fixtures supply simulated equipment and generated public JPEGs through the photo library store's worker side (pool check steps written as the worker would), acquisition-request, and publication transactions with an explicitly synthetic recipe and preparation metadata. Elapsed time advances through the production Runtime owner; natural finish waits for the current cycle boundary. The server binds an ephemeral loopback port, preserving the separate full demo's network isolation. These checks qualify operator controls and persistence. They do not run an upstream adapter, conversion worker, background scheduler, Player, or renderer, and do not qualify scheduled playback, PXE, or physical output. Run browser checks separately from the ordinary suite: synchronous Playwright owns an event loop for its session, while ordinary Player integration tests create their own loops.

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

The [real Immich fixture](module-immich-fixture.md), [full media-path demo](module-wall-demo.md), [Player package](module-player-package.md) and [Debian packaging module](module-debian-packaging.md), and [central release contract](module-appliance-release.md) provide commands and evidence boundaries. The [appliance builder/bootstrap](module-appliance-builder.md), [GitHub ARM image workflow](module-appliance-ci.md), and [headless image e2e gate](module-appliance-e2e.md) describe exact-artifact checks and their limits. Earlier signed image and hosted boot evidence remains useful for artifact identity and generic-VM behavior, but its durable-Player/local-update assumptions are superseded. Complete current-image native rendering, valid-cache reuse, corrupt-cache reacquisition, real automatic reboot/central rollback, and physical measurements remain pending until recorded against the final revision.


## Node control

The V2 node composition uses the same database, cache layout, dependency lock, and
content worker as Central. Deploy Central and its worker from the same revision
so both know migrations 053–054 and the sealed-environment job kind. Keep the
ordinary `PHOTO_WALL_DATABASE_URL` and `PHOTO_WALL_ADMIN_TOKEN` configuration in
its existing protected deployment settings. No token is placed in a command line.

Central always runs node control; there is no switch to turn it off and no
other factory. Run Central with its one factory (the image's own command):

```sh
uvicorn central.app:create_app --factory --host 0.0.0.0 --port 8000 --ws-max-size 16777216
```

Keep `--ws-max-size` at 16 MiB: a Node's bus leaf rides Central's WebSocket, and nats-server sends a connection's whole backlog as one frame ([the Node bus](#the-node-bus-and-centrals-hub)).

**Locks.** There is no fleet-wide lock: each device has its own fence ([coordination module](module-coordination.md)). Node requests share it, so a slow database commit for one request (the disk stalling a WAL flush) no longer makes the device's next request, or its Hardware page, fail. A boot offer, a node enrolment or a retirement waits for the device's open requests; one not granted within 5 s answers 503 `node_device_busy` with `Retry-After: 1`, and the Node retries. A waiting change also holds back that device's new requests, and some of them (an app link, a display exchange, reconciliation, a Player enrolment, retirement) wait while already holding the Wall-wide Coordination and Runtime locks: one device's boot during a stalled commit can therefore hold up every Player's state poll for up to 5 s. One producer's own posts run one at a time (its producer row), so a Node's retry during its first post's slow commit waits for it and answers `duplicate`. The Wall-wide Coordination lock is unchanged and can still time out while the disk stalls.

**Upgrading across lost grants (migration 095).** Readiness reports now carry `lost_grants`; a Player built before it is refused (422) by a Central after it, and the other way round. An older Player does not sit quietly through this: the 422 ends its control loop, so it drops its Central session, is marked retrying, and locates and reconnects again and again (`player/service.py`, the session loop), each cycle another Coordination transaction; no new cue starts. The window lasts until the Pi runs the new release: the release sync has ingested it, it is selected (Pis never follow a release on their own) and the Pi has rebooted. Deploy Central, select the new release and reboot every Pi at once.

The node session audience is the product constant `photo-wall` (`NODE_AUDIENCE` in
`central/fleet/node_sessions.py`), one Central per wall; no environment variable sets it.
Every Pi boots by the node path: the V1 boot offer
and the `photowall.node=v2` switch are gone ([0019](decisions/0019-debian-packaging-with-debhelper.md)),
so the command line needs no flag. Do not add `cgroup_enable=memory`: every release's
command line carries it, once, and it must stay. Boot trees staged from an earlier
release do not; restage them. Without the memory controller, which the Pi 5's device
tree turns off, the base still mounts its store but no memory limit is enforced, and
the console warns
([step 2](#player-provisioning-stage-the-netboot-bundle-and-read-its-console-0014)).
Node control serves observation, explicit session enrollment, immutable V2 boot
offers and scoped command routes. The operator's word admits Reboot and Stage app:
there is no effect gate (owner answer G-A). `/healthz` remains process/service
health. A running HTTP server, accepted serial claim, stored sample, or
catalogued artifact is not command qualification, authenticated physical identity,
verified downloaded bytes, or observed pixels.

Put a release on the wall from the console
([Releases](#releases-stage-app-and-update-the-wall)); Stage and qualification are on
the Player page, and the Update the wall journey composes them. Under the hood, the
release sync creates an immutable deployment from each valid catalogued release and
Select sets the boot policy with a revision compare-and-set. The base release must already
have exact catalog provenance; manager primary and any accepted fallback are
pinned to that base digest. Environment sources feed the existing content worker.
A missing byte artifact is reported unavailable until the worker acquires and
verifies it. An explicit no-app deployment still boots the independent base.
Every Node boots by the node path; there is no other boot offer and no legacy manifest to fall back to.

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
already-delivered effect stopped. The automated node scenarios and
their limits are listed in [validation](validation.md).


### Node storage: release images and memory lines

Each release root on a Node is a squashfs image in the image pool, mounted read-only by PID1 at its root directory; the design is in the [Player architecture](player-architecture.md#release-roots-as-images-e2c) and the memory lines in the [Player node domain model](player-node-domain-model.md#memory-classes-and-the-node-store-2026-10-03).

**Where things are on a Node.**

| Path | Holds |
|---|---|
| `/run/photo-wall-node-storage/root-images/<sha>.squashfs` | One image per staged root, root:root 0444 |
| `/run/photo-wall-node-storage/root-images/.cold-staging/` | The prepare step's downloads in flight (removed at the next prepare) |
| `/run/photo-wall-node-storage/preparation/downloads/<sha>` | AppManager's online download, before the import worker adopts it |
| `/run/photo-wall-node-storage/app-roots/<sha>`, `manager-roots/<sha>` | The mount points; the Player runs from `<sha>/rootfs` |

**Is a root mounted read-only?** Do not trust `findmnt` OPTIONS: a squashfs superblock always says `ro`, even under a writable mount. Read the mount's own line and its loop device:

```sh
grep ' /run/photo-wall-node-storage/app-roots/' /proc/self/mountinfo   # field 3 is maj:min, field 6 must contain ro, fstype squashfs
cat /sys/dev/block/<maj:min>/ro                                          # must be 1
cat /sys/dev/block/<maj:min>/loop/backing_file                           # must be .../root-images/<sha>.squashfs
systemctl list-units --type=mount | grep photo                         # the transient mount units
```

**Memory readings.** On the Player page's Every reported metric:

- `memory_peak:preparation` is the preparation slice's peak. Images are page cache charged to the slice that wrote them, so the peak includes reclaimable cache and sits near the images held plus the preparing process. It is not by itself a problem.
- `oom_kill:preparation` above 0 is the signal that the preparation line (960 MiB) is too low: an online stage or the cold prepare step was killed. Report it with the release and the board's memory size.
- `memory_peak:app` and `oom_kill:app` cover the Player, whose file pages (its libraries, read from the image) now count against the app slice. Refaults show on the box, not in a metric: `cat /sys/fs/cgroup/photowallapp.slice/memory.events`, where rising `high` or `max` counts mean the Player's working set is pressing its line. These are clean file pages, so they slow the Player rather than kill it.

**Staging faults.** These appear as a prepare-step fault on the Player page (cold path) or in the import worker's journal (online path, below).

| Fault | Meaning | What to do |
|---|---|---|
| `environment_abi_mismatch` | The release was built for another base, graphics or plugin ABI than this Node measured; refused before any download or mount, and again at every launch | Select a release built for this base |
| `root_image_digest_mismatch` | The pool file's sha256 is not the release's; the file was unlinked | Retry; a repeat points at Central's cached bytes |
| `root_image_ownership` | A pool or incoming file is not a sealed regular file (owner, mode, link count or size); it was unlinked | Retry; a repeat is a defect |
| `root_image_missing` | The fetched file was not where the stager expected it | Retry; a repeat is a defect |
| `root_image_adopt_cross_device` | Moving the download into the pool would cross mounts (a unit's binds are wrong); nothing is copied | A base defect: report it with the release |
| `image_mount_failed` | PID1 refused the mount (no loop device, no squashfs support, a timeout) | Reboot once; a repeat is a kernel or base defect |
| `image_mount_not_visible` | PID1 mounted it but the stager's sandbox does not see it (mount propagation) | A base defect: report it |
| `image_mount_conflict` | Something else is mounted at the root directory (another image, another type, or a writable mount); it is never unmounted | Reboot; a repeat is a defect |
| `root_image_not_staged` | At launch, the root is not a read-only squashfs mount of its sealed pool image | Reboot; a repeat is a defect |
| `environment_reference_mismatch`, `environment_provenance_mismatch`, `environment_entrypoint_missing` and the other `environment_*` checks | The image is mounted but does not match the ref Central paired it with; the image and mount stay, and a corrected ref for the same digest stages without a download | Report it with the release: a Central or release fault |

**A refused online stage stalls without a report.** When staging refuses inside the import worker, the worker exits without its result, the broker raises `root_import_outcome_unknown` on every tick, and the operation stays `preparing` on the Node while the console shows it staged. The fault is only in the worker's journal: `journalctl -u 'photo-wall-root-import-*'`. Stage again from the console, or reboot the Player. The owner accepted this as a cost of E2c (2026-10-09); it is fixed when the effect protocol is rebuilt ([0017](decisions/0017-node-redesign-r3.md#costs)).

Superseded images stay in the pool until the next reboot. A third distinct app image in one boot is refused as an App Manager storage refusal with both numbers; reboot the Player to clear the pool.

## Reaching a Node over SSH

Every Node's base OS runs Debian's OpenSSH server, so an agent or the operator on the LAN can read a Node's journal and units directly. It comes from rpi-image-gen's own `openssh-server` layer, enabled in the base build's [config](../appliance/rpi_image_gen/config/photo-wall-base.yaml), like any other OS dependency, not from a Photo Wall package. It starts with the OS, apart from the app, the display and Central, so it works when those are broken.

- **The login.** One user, `photowall`, logs in with the agent key only: no password, no keyboard-interactive login, no root login. It has passwordless `sudo` and is in the `systemd-journal` group.
- **The key.** The one authorized public key is [`appliance/rpi_image_gen/photo_wall_agent.pub`](../appliance/rpi_image_gen/photo_wall_agent.pub); the base build bakes it into `photowall`'s `authorized_keys`. Its private half lives only on the agent's machine, at `~/.ssh/photo_wall_agent`, never in the repo. Changing the key is a PR to that file, and a Node gets the new key only by booting a base from a release built after it.
- **The host key changes at every boot.** The base carries no host key, and a Node's root is a RAM overlay, so each boot makes new host keys. Skip host-key checking for these boxes rather than editing `known_hosts` after each reboot:

```sh
ssh -i ~/.ssh/photo_wall_agent -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null photowall@<pi-ip>
```

- **Finding the Pi's address.** Central does not record a Node's IP yet. Read it from the DHCP server's lease table (the router's client list; a Pi's vendor shows as Raspberry Pi), or scan the LAN for an open port 22: `nmap -p 22 --open 192.168.1.0/24` (your subnet). On the box, `cat /run/photo-wall-node/host.json` shows the serial the Node claims, which its Player page names.
- **The Pi's name.** Each Node is `photo-wall-<serial>`, where `<serial>` is the 8-hex serial of its TFTP directory (the last eight digits of `/proc/device-tree/serial-number`), for example `photo-wall-75628d0c`. Stage 1 sets it at every boot ([`hand_over_hostname`](../appliance/netboot_init.py)); the base image bakes only `localhost`.
- **What to read.** `systemctl --failed`; `systemctl status photo-wall-display.service photo-wall-display-controller.service`; the display units' journal for this boot: `sudo journalctl -b --no-pager -u photo-wall-display.service -u photo-wall-display-controller.service`. The same `-u` form reads any other node unit (`photo-wall-host-core`, `photo-wall-app-broker`, `photo-wall-node-player`, `photo-wall-bus`).

The journal lives in RAM and is lost at reboot, so read it before rebooting a broken Node. Shipping failed units' status and journal tail to Central is [issue 53](https://github.com/mcurcio/photo-wall/issues/53).

## The Node bus and Central's hub

Each V2 Node runs its own NATS server, the **bus**, and dials one leaf link to Central's **hub**; Central reads every Node's streams and state through that link and records them in PostgreSQL. The shape and its reasons are [decision 0017](decisions/0017-node-redesign-r3.md) C3–C5, C13, C19 and C20. Nothing is recorded until Nodes run the bus. The join, and the hub in the node-pid1 fixture, are E3e's (in progress).

**What runs where.**

| Piece | Runs as | What it does |
|---|---|---|
| Bus | `photo-wall-bus.service` on every Node, in `photowallbus.slice` (256 MiB) | nats-server on `127.0.0.1:4222`, memory store only: every start is empty, and each writer refills what it owns. `Restart=always`; nothing waits on it |
| Leaf | the bus's one remote | dials the origin the Node booted from, `ws://` for http or `wss://` for https, host and explicit port, path `/photo-wall/bus/leafnode` |
| Leaf bridge | Central's HTTP app, when `PHOTO_WALL_HUB_LEAF_URL` is set | relays that WebSocket, unchanged, to the hub's leaf listener; without the variable the route refuses |
| Hub | Compose service `hub` (production: a pod sidecar) | nats-server from the file the worker writes, memory store only; no LAN port |
| Bus side | one task in the media worker, when `PHOTO_WALL_HUB_URL` is set | every 2 s: writes the hub configuration for the enrolled Players, reloads the hub when the file or the hub's server id changed, re-creates WALL after a hub restart, closes leaves of Players no longer enrolled, and runs one fleet and one show link per enrolled Player |

**Configuration.**

| Variable | Service | Compose value | Meaning |
|---|---|---|---|
| `PHOTO_WALL_HUB_URL` | worker | `nats://photo-wall-hub:4222` | the hub's client URL. Unset: no bus side, the worker runs as before |
| `PHOTO_WALL_HUB_CONFIG` | worker | `/var/lib/photo-wall/hub/hub.json` | the file the worker writes and the hub starts from (the shared `hub-config` volume; the hub mounts it read-only) |
| `PHOTO_WALL_HUB_LEAF_URL` | central | `ws://photo-wall-hub:8080/leafnode` | the hub's leaf WebSocket. Unset: no leaf bridge |
| `PHOTO_WALL_HUB_MONITOR_PORT` | Compose | `8222` | the host port of the hub's monitor, bound to `127.0.0.1` only |

`docker compose up -d --wait` starts the hub after the worker; the hub waits until the worker's first file exists, which follows the worker's migrations, so its healthcheck allows 120 s. Central's command keeps `--ws-max-size 16777216`. The hub configuration turns leaf compression off: the relay cannot carry nats-server's S2 compression, which sits below the WebSocket framing.

**Checking the hub.** The monitor answers on `http://127.0.0.1:8222` (Compose):

```sh
curl -s 'http://127.0.0.1:8222/connz?auth=true' | python3 -m json.tool   # the worker's clients: fleet, central-wall, and its links in each Player's account
curl -s 'http://127.0.0.1:8222/leafz' | python3 -m json.tool             # every Node leaf linked now, by account
curl -s 'http://127.0.0.1:8222/jsz?accounts=true&streams=true' | python3 -m json.tool   # stream WALL
docker compose exec -T worker cat /var/lib/photo-wall/hub/hub.json | python3 -m json.tool   # one account per enrolled Player
docker compose logs --tail 100 worker hub
```

The Player page shows **Node API link**: linked or not linked, with its age, as Central's inference from its last look at the hub. It reads unknown when that look is older than 30 s or no hub runs. It never gates anything.

**What to expect.**

- **Enrol or retire a Player:** within a look (2 s) the hub's file gains or loses its account and the hub reloads. A retired Player's leaf is closed and its links stop; its records stay.
- **Hub restart or Central deploy** (production restarts the whole pod): the hub starts empty, the worker sees a new server id, reloads it and re-creates WALL. Leaves relink in about a second; Node WALL mirrors catch up within about 60 s. A Node whose leaf dialled before its account existed gets its first WALL about 40 s later. All self-recovering.
- **Bus restart on a Node** (crash, OOM, reboot): the bus starts empty; Central records the unread tail as one gap row with count unknown per stream, and each Node program re-puts its state.
- **No hub reachable:** the worker logs it, keeps the file, retries every look, and keeps media and jobs running. A failure of the bus side itself ends the worker non-zero, as a media or job loop failure does.

**What Central keeps.** `node_link_records` (each message once), `node_link_gaps` (counted losses; `lost` is an upper bound if two workers overlapped during a deploy), `node_link_cursors`, `node_link_documents`, `node_link_actions`, `node_bus_wall` (the WALL mark) and the presence tables `node_bus_presence` and `node_bus_hub_looks`. Records and actions grow without bound until E5 sets their retention.

**On a Node.** `journalctl -u photo-wall-bus` shows the bus; `/run/photo-wall-node/bus.env` holds its environment, written by the handoff stage from the located origin and the serial. HostCore reports `oom_kill:bus` and `memory_peak:bus` from the slice, so an OOM survives the restart it causes. A bus that cannot reach its hub keeps serving its Node; leaf redials are log noise only.

**Kubernetes.** The hub is a native sidecar ordered after the worker, sharing an `emptyDir` for the configuration file; the worker and Central reach it on `127.0.0.1`, and a Node's leaf arrives on Central's existing port at `/photo-wall/bus/leafnode`, so no Service port or route is added. Deployment changes go through an iac PR the owner merges.

**Tests.** The bus tests start real nats-server processes; fetch the pinned binary once:

```sh
export PHOTO_WALL_NATS_SERVER="$(python3 scripts/nats_server.py fetch --dest /tmp/nats)"
.venv/bin/python -m pytest -q tests/integration -k node_bus -n 4
.venv/bin/python scripts/test_local.py -q -m db -n 4 --dist loadgroup   # includes tests/integration/test_central_*.py
```

`test_central_worker_hub.py` binds the deployed hub ports (4222, 8080 and 8222 on all interfaces), so it fails while a local Compose hub or anything else holds them. The memory fence (`tests/integration/test_bus_memory_fence.py`, `PHOTO_WALL_BUS_FENCE_SERVER`) needs Docker on linux-arm64; CI's `bus-fence` job runs it.

## Display identity and power

Roadmap 1b: each Pi reports the display on each HDMI port, Central recognises it, and a Frame's Power tab turns it off and on. The rules live in the [operator console design](operator-console-design.md#6-display-identity) (what the operator sees, the identity rule) and the [Player architecture](player-architecture.md#display-identity-and-power-1b) (the Node side); operating the tools on a Pi by hand is the repo skill [display-control](../.claude/skills/display-control/SKILL.md).

**What moves where.** Both ride the Node bus's `display` line, per HDMI port (`output-HDMI-A-1`, …), through the fleet link ([the Node bus](#the-node-bus-and-centrals-hub)):

| What | Writer | Reader | Kept in Central |
|---|---|---|---|
| Output report (connected, EDID identity and modes, methods that answered, what is in force, last result) | the Pi's display controller | the worker's report judge | `node_link_records` (every report), `displays`, `shared_serials`, `output_displays` (the Display last seen per port) |
| Power attempt (`display.record.power`) | the Pi's display controller | recorded only | `node_link_records` |
| Output document (the power timetable with the test on top, method, switches) | the worker, from Displays, bindings, Night off, holds and `power_requests` (the test) | the Pi's display controller | `output_documents` (the latest per port) |
| Picture document (`picture-HDMI-A-1`, …: the hardware values set in Photo Wall by code, every software value (the print look's included, [0023](decisions/0023-print-look.md)), the `signal_range`, the live session's `test_pattern` ask ([0024](decisions/0024-display-tuning.md)), `picture_change`) | the worker, from the Display recorded on the port and its open picture session | the Pi's display controller | `picture_documents` (the latest per port; `picture_change` rises only when the picture differs) |
| Picture report (`picture-HDMI-A-1`, …: what the display offers from the picture list, each write's result for `for_change`, the Player's `presented_change`, the `signal` answer (range in force, `applied`, `unsupported` or `failed`) and `test_pattern_shown`) | the Pi's display controller | the worker's report judge | `picture_reports` (the latest per port; a report whose list is not known yet keeps the known list of the same display) |

**Readiness.** Nothing stores whether a Frame is ready: `frames.position_generation` and `frames.position_display_id` record each Position commit, and readiness is worked out when read (`unbound`, `display-changed`, `position-needed`, `ready`; `central/displays/model.py`). Migration 073 dropped `frames.calibration_valid`, marking every Frame that was ready as committed at its current generation; at the first report each Frame adopts the display on its port. **Rolling Central back past 073 is not supported:** the previous build reads `calibration_valid`, which no longer exists, and no reverse SQL is kept.

**A console test, end to end.** **Test: turn off** posts `POST /v1/operator/frames/{id}/power-tests` (no body); Central keeps one live test per Frame in `power_requests` for 5 minutes on its own clock and sends `pg_notify('photo_wall_output_documents', <device id>)` in the same transaction. The worker's one LISTEN connection (`DisplayWakes.listen`, reconnecting after a second, waking every Node at each connect) wakes that Pi's links, which put the new document; the Pi acts and reports, and `GET /v1/operator/frames/{id}/power` reads "Waiting for the Pi" until the report names the change that carried the test. **Turn on** is `DELETE` on the same path (204): it deletes the live test, so the power timetable underneath takes over, and the tab waits until the Pi reports the timetable in force on the change that dropped the test. Settings are `PUT /v1/operator/displays/{display_id}/power-settings`; the Hardware tab reads `GET /v1/operator/frames/{id}/display`. All are operator routes behind the admin token.

| What you see | What it means | What to do |
|---|---|---|
| "Waiting for the Pi" does not go away | The Pi has not reported acting on the change. The tab says when it last heard from the Pi when the bus link is down. | Check the Pi's **Node API link** on its Player page; on the Pi, read the controller's journal (below). |
| "Display didn't answer" | The method in use got no answer. | Check the display is on its HDMI input; try another power method. |
| "Picture stopped; the display may sleep" | Signal off: the Pi stopped its picture; nothing is claimed about the display. | Expected when the display answers neither HDMI-CEC nor DDC/CI. |
| "Picture back on" | Turn on through signal off: the Pi sent its picture again. | Expected after Turn on with signal off. |
| No power method answered (`not-supported`) | Nothing the Pi can drive. | A smart plug through Home Assistant arrives in [roadmap 3](roadmap.md#3-home-assistant). |
| The Frame reads **Display changed** | A readable, different display is on its port. | On Hardware, confirm or correct the Frame profile, then re-check Position. |

**On the Pi.** The display controller (`photo-wall-display-controller.service`) runs the display session and the power methods. Read it with `sudo journalctl -b --no-pager -u photo-wall-display-controller.service` ([Reaching a Node over SSH](#reaching-a-node-over-ssh)). Its device access: `DevicePolicy=closed` with `DeviceAllow=char-cec rw` and `DeviceAllow=char-i2c rw`, the `video` group for `/dev/cecN` and the `i2c` group for `/dev/i2c-N` (declared by `debian/photo-wall-node.sysusers`; [gap G15](player-architecture.md#observed-gaps)). `id pw-display` does not list them: the groups are the unit's own, so check the running process with `grep Groups /proc/$(systemctl show -p MainPID --value photo-wall-display-controller.service)/status`. The base loads `i2c-dev` at boot (`/etc/modules-load.d/photo-wall-ddc.conf`); `ls /dev/i2c-*` shows `/dev/i2c-13` and `/dev/i2c-14` on a Pi 5 when it did.

**Picture.** The Picture tab reads `GET /v1/operator/frames/{id}/picture`: the status (`no-display`, `waiting`, `shown`), whether the display's list is known (`offered_known`), one row per hardware setting (offered, the value in force, the last write's result) and the software values. A session posts each change to `…/picture-sessions/{session_id}/hardware-edits` (`code`, `value`, `null` to hand it back) or `…/software-edits` (`setting`: `brightness`, `contrast`, `colour-temperature`, `gamma`, `saturation`, `highlight-rolloff`, `black-floor`, `lut-amount`), the LUT choice to `…/lut-edits` (`unknown_lut` for one not on the LUTs list), the signal range to `…/signal-range-edits` (`signal_range`: `auto`, `full`, `limited`) and the test pattern to `…/test-pattern-edits` (`test_pattern`: `white`, `grey-ramp`, `black-level`, or `null` for Off), each body with its `expected_sequence`; each answers `{sequence}` ([0024](decisions/0024-display-tuning.md)). The route table at the top of `central/display_routes.py` is the one home of every body and answer. Done is refused (`not_presented_yet`) until the Pi reports the latest picture change written, presented (its test pattern included) and its signal range answered. To see what a display answered, read `picture_reports.report->'offered'` for its Pi and port. "Checking what this display offers" that never clears means the Pi has not finished asking: the display gives no EDID identity, or a batch keeps timing out (read the controller's journal); the Photo Wall (software) section works meanwhile.

**Not built:** setting the HDMI mode and the smart plug ([roadmap 1b](roadmap.md#1b-display-identity-and-power)); Night off, All off and holds came with 2a ([below](#night-off-and-the-week)). The bench check (Test off and on against the test Pi's monitor) waits for a deployed release.

## Tune a display with no controls

For a display that hides its picture settings or answers no DDC/CI, with a bright backlight: everything here is on the Frame's Picture tab, decided in [0024](decisions/0024-display-tuning.md); what each control says is the [console design](operator-console-design.md#5-the-frame-page) (§5 Picture).

1. **The paper sheet.** Tape a sheet of white paper beside the Frame, lit as the Frame is. Choose Test pattern › **White**: every Frame of that display shows white through the Photo Wall (software) values and the Frame's mat, so it is the white a photo gets. Move Photo Wall's brightness and colour temperature (and the display's own, where it offers them) until the Frame's white sits with the paper.
2. **The Black level.** Choose Test pattern › **Black level** (the raw signal: no Photo Wall step and no mat acts on it) and read it by the tab's own line, which is the one home of that reading ([console design §5](operator-console-design.md#5-the-frame-page), Picture row): it compares Signal range **Full** with **Limited**.
3. **Full versus Limited.** Full sends black as 0 and white as 255; Limited sends 16 and 235 and labels the signal so; Auto is the Pi's choice, Limited for every TV mode (4K and 1080p). A display that expects one and gets the other shows black as grey (Limited into a Full display) or merges its darkest steps into black (Full into a Limited display).
4. **The Grey ramp.** Choose Test pattern › **Grey ramp** to check the steps from black to white through Photo Wall's values: every step distinct, no step merged, no bands in the smooth ramp.
5. **Done** keeps the range (and the other values); Revert or leaving the tab puts the range back and takes the pattern off. A pattern also goes off by itself 10 minutes after the Pi received it. While a pattern is shown, hiding the browser tab (switching to the phone's camera) does not end the session: Central keeps it a minute without the tab's keepalives, and the tab carries on when shown again; with no pattern, hiding the tab ends the session at once (Revert) and Central's net is 5 s.

**Each range change costs about 10 s without photos on every display of that Pi** (not measured since the broker fix; to re-measure on the bench). The Pi sets the range before Weston starts, and one Weston drives both HDMI ports, so a change restarts the Weston and Player of every display on the Pi, and a test pattern on the other display starts its 10 minutes again ([display host backend](display-host-backend.md#units-and-weston-incarnations)); the display shows black, and the TV may take its own time to re-sync. A Pi start with a Display not on Auto pays it once. A range the Pi could not set shows on the tab with the range the display still gets; read the controller's journal ([Display identity and power](#display-identity-and-power)).

**A Samsung TV (the owner's QN43Q8F) and its PC input.** Nothing here needs a TV menu: the range, the test patterns and every Photo Wall value act from Central. The QN43Q8F's EDID says it follows the range the Pi labels (quantization selectable; the [display-control skill](../.claude/skills/display-control/SKILL.md) shows how to read it), so Full and Limited should look the same on it and a glow left on the Black level is its backlight. Samsung TVs show more or fewer picture settings by the input's type (the Source list's Edit, type PC) and by Game Mode, and name their own range setting HDMI Black Level where they show it; which settings the QN43Q8F shows in each is NOT checked on the bench. If a TV does show its own range setting, leave it on its automatic choice so the Pi's label decides.

**What the kernel sends**, on the Pi with sudo ([Reaching a Node over SSH](#reaching-a-node-over-ssh)):

```sh
sudo sh -c 'grep -A11 "^connector\[.*HDMI-A" /sys/kernel/debug/dri/*/state' | grep -E 'connector|broadcast_rgb|is_limited_range|output_bpc'
```

`broadcast_rgb` is the connector's setting (`Automatic`, `Full`, `Limited 16:235`); `is_limited_range` is what the kernel sends now (`y`: black at 16); `output_bpc` the bits per channel. `debugfs` is root-only. More, and the traps, in the repo skill [display-control](../.claude/skills/display-control/SKILL.md).

## Configuration snapshots

What a Display's picture and a Frame's Position were at any time, for the Picture and Position tabs' History and for the agent reading the owner's photos while he tunes. Why it is shaped so, the kinds and Restore's rules are [0025](decisions/0025-configuration-snapshots.md); what the tabs say is the [console design](operator-console-design.md#5-the-frame-page) (§5 Position and Picture); the agent's procedure is the [display-control skill](../.claude/skills/display-control/SKILL.md#reading-the-values-while-the-owner-tunes).

**Tables** (migration 096; each a record of what was, never current state: `displays`, `picture_documents` and `frames` stay the truth):

| Table | One row per | Columns |
|---|---|---|
| `picture_kept_entries` | Done, Copy or an unrecorded change on a Display | `id`, `display_id`, `number` (the Display's commit order), `kind` (`kept-done`, `kept-copied`, `kept-unrecorded`), `at`, `session_id`, `restored_from`, `picture`, `lut_recipe_id` |
| `picture_sent_entries` | rise of a port's `picture_change` (what Central sent the Pi) | `id`, `player_id`, `output_id`, `picture_change` (its number), `display_id` (seen on the port), `frame_id` (bound to it then; NULL if none or deleted since), `kind` (`sent-draft`, `sent-back`, `sent-kept`), `at`, `session_id`, `restored_from`, `picture`, `lut_recipe_id`, `test_pattern` (`white`, `grey-ramp`, `black-level`, `target-pre-software`, `target-post-software` (migration 097, [0026](decisions/0026-calibration-targets.md)); NULL: off) |
| `position_entries` | Save, profile change, unrecorded change, or change in what the Node is handed | `id`, `frame_id`, `number` (the Frame's commit order), `kind` (`kept-saved`, `kept-profile-changed`, `kept-unrecorded`, `sent-draft`, `sent-back`), `at`, `session_id`, `restored_from`, `fit` (`contracts/models.py` FrameFit's JSON: mode, corners, rotation), `mat` (Mat's JSON: depth and lights), `profile_width_px`, `profile_height_px` and `display_id` (what it was made for: the Frame's profile and the Display seen on its port then) |

`picture` is the Display's picture under the `displays` columns' names (`hardware_picture` by MCCS code, `software_brightness_percent` … `software_lut_profile`, `software_lut_amount_percent`, `signal_range`), as a session's `draft` is; `lut_recipe_id` is the recipe its LUT was baked under then (NULL: no LUT). `session_id` groups one picture session's or one Position trial's entries (a trial keeps it across renewals: `node_calibration_trials.session_id`); `restored_from` names the entry a Restore loaded (also on `picture_sessions` and `node_calibration_trials`).

**Routes** (operator, admin token; the table at the top of `central/tuning_history_routes.py` is the one home of each answer and refusal). Times are ISO 8601 with a zone, answered in UTC; lists are newest first, `limit` 1 to 500 (default 100), `before` an exclusive time for the next page:

```sh
T='Authorization: Bearer <admin-token>'
curl -H "$T" "$CENTRAL/v1/operator/displays/<display-id>/picture-history?limit=50"
curl -H "$T" "$CENTRAL/v1/operator/frames/<frame-id>/position-history?before=2026-10-13T21:00:00Z"
curl -H "$T" "$CENTRAL/v1/operator/frames/<frame-id>/tuning-snapshot?at=2026-10-13T21:04:07Z"
# One port's sent entry by its picture change (what a calibration target's QR names; 404 unknown_entry)
curl -H "$T" "$CENTRAL/v1/operator/players/<player-id>/outputs/HDMI-A-1/picture-changes/<n>"
# Restore: loads an entry into an open session's or trial's draft; Done still keeps it
curl -X POST -H "$T" -H 'Content-Type: application/json' \
  -d '{"entry_id":"<entry-id>","expected_sequence":7}' \
  "$CENTRAL/v1/operator/picture-sessions/<session-id>/restores"
curl -X POST -H "$T" -H 'Content-Type: application/json' \
  -d '{"entry_id":"<entry-id>","expected_sequence":7}' \
  "$CENTRAL/v1/operator/frames/<frame-id>/calibration-trials/<trial-id>/restores"
```

The snapshot answers the Frame's newest Position entry at or before `at`, and its picture: the newest sent entry naming the Frame at or before `at`, else the newest kept entry at or before `at` of the Display seen on its port now. A history answers `unrecorded_change: true` while the stored values differ from the newest kept entry (changed outside Photo Wall; the next kept write records it). Each Position entry carries `restore_refusal` (`other_frame`, `made_for_other_profile`, `made_for_other_display`, or null). A picture Restore answers `hardware_skipped` (codes this display does not offer now), `lut_skipped` (the LUT is off the list; the draft kept its own) and `lut_recipe_changed` (today's recipe renders it); it refuses `other_display` (use Copy).

**Reading with SQL** (a read-only session is enough; `SET TIME ZONE 'UTC'` first so every `at` reads in UTC). A Frame at a time, its Position and its picture (when the second query finds nothing, the snapshot route falls back to the Display's kept entries):

```sql
SET TIME ZONE 'UTC';
\set frame 'living-room'
\set t '2026-10-13T21:04:07Z'
SELECT kind, number, at, session_id, fit, mat FROM position_entries
 WHERE frame_id = :'frame' AND at <= :'t' ORDER BY at DESC, number DESC LIMIT 1;
SELECT kind, picture_change, at, session_id, picture, lut_recipe_id, test_pattern FROM picture_sent_entries
 WHERE frame_id = :'frame' AND at <= :'t' ORDER BY at DESC, picture_change DESC LIMIT 1;
```

A Display's last hour, kept and sent together:

```sql
SET TIME ZONE 'UTC';
\set display '<display-id>'
SELECT at, kind, number, NULL AS frame_id, picture, NULL AS test_pattern FROM picture_kept_entries
 WHERE display_id = :'display' AND at > now() - interval '1 hour'
UNION ALL
SELECT at, kind, picture_change, frame_id, picture, test_pattern FROM picture_sent_entries
 WHERE display_id = :'display' AND at > now() - interval '1 hour'
ORDER BY at DESC, number DESC;
```

**Retention.** Per key, the newest 500 kept and 20,000 sent entries (a Display's kept, a port's sent, a Frame's of each); each writer trims its own key when it appends, and nothing else deletes them. A deleted Frame takes its Position history. History starts at the upgrade to 096: nothing is written for the past.

**The clock.** Every `at` is PostgreSQL's clock when Central recorded the change, never the Pi's or a phone's. Match a photo on Central's clock: as it arrives, read Central's time (`SELECT now()`) or the newest entries, and step back from there; never by the photo's own timestamp. A sent entry is what Central sent; the Pi shows it seconds later, about 10 s for a signal range change ([Tune a display with no controls](#tune-a-display-with-no-controls)). A change made with the TV's own remote or menus is never in the history.

## Calibration targets

Two targets read together from one phone photo, so the agent can say what to change on a display: the **screen board** the Player draws in each Frame's opening and the **paper board** Central prints, both OpenCV ChArUco boards. Why they are shaped so is [0027](decisions/0027-calibration-targets-v2.md); both stages and what each aims at are [0026](decisions/0026-calibration-targets.md)'s; the layout is `contracts/calibration_target.py`'s; the operator's instructions have one home, the Picture tab's words under each target ([console design §5](operator-console-design.md#5-the-frame-page)); the agent's procedure and what each refusal asks is the [display-control skill](../.claude/skills/display-control/SKILL.md#reading-the-calibration-targets).

1. **Print the paper.** Letter, A4 or a 4 × 6 in photo print: from the Picture tab's print link, the [README](../README.md)'s links, or the operator route (admin token):

   ```sh
   curl -o target.pdf -H 'Authorization: Bearer <admin-token>' "$CENTRAL/v1/operator/calibration-target/paper.pdf?paper=letter"
   ```

   All three are the same committed files (`central/displays/calibration_sheets/`): the route serves them as they are. Every sheet keeps 15 mm squares (`PAPER_SQUARE_MM`); a small sheet carries fewer of them, the 4 × 6 in print the board's top-left 9 × 5 squares (`sheet_squares`), which reads as any cut piece does. Any printer and any ink (grey or colour black reads: the reader measures the ink in the same photo), at any size. For a screen smaller than the sheet, cut a piece, or print it smaller. Refusal: 422 `bad_paper` (not `letter`, `a4` or `4x6`).
2. **Lay it on the screen**, anywhere and at any turn, under the glass when the Frame has glass, with a black card behind it: the paper must lie in the screen's plane. It may cover part of the screen board; those squares are skipped.
3. **Pick a stage.** On the Frame's Picture tab, Test pattern › Show:
   - **Target: pre-software** (stage 1, the display): the screen board drawn raw, every Photo Wall step skipped (the signal range still applies). The suggestion is for the display's own controls (its menu, or on a display that answers DDC/CI the tab's Display (hardware) sliders), as a direction and a rough size.
   - **Target: post-software** (stage 2, Photo Wall): the screen board drawn through Photo Wall's settings (all but the mat shadow). The suggestion covers only what the display left over, in Photo Wall's brightness, contrast, colour temperature and gamma, and prefers no change: a display right on its own reads "no change", the best result, once the photo proves it. The print look (LUT, saturation, soft highlights, black floor) is the intended look and never moves.

   Either, in any order: nothing enforces one before the other or remembers an earlier reading. The session's usual rules hold (live while the tab runs, off by Done, Revert, leaving the tab or after 10 minutes on the Pi). You can photograph with the phone the tab runs on: while a test pattern is shown, hiding the tab keeps it up for a minute, and coming back within it keeps the same session ([0024](decisions/0024-display-tuning.md)).
4. **Take one photo** holding the paper and some of the screen, at a reasonable angle, your shadow off the paper. ProRAW reads white, tone and black; a normal photo (JPEG, HEIC) gives a rough white only. A small display is photographed close: its QR copies read from about 30 px a square. AirDrop it to the Mac (`~/Downloads`).
5. **Read it** on the Mac, from the repository, with the admin token (and, for a post-software reading with a LUT on, a read-only database address) from 1Password:

   ```sh
   op run -- .venv/bin/python -m scripts.calibration_target ~/Downloads/IMG_1234.DNG --central <url>
   ```

   `op run` resolves `PHOTO_WALL_ADMIN_TOKEN` and, optionally, `PHOTO_WALL_READ_DATABASE_URL` from their `op://` references in the environment (or `--env-file`); never put either on the command line. It prints one JSON object, `{"reading": …, "suggestion": …}` or `{"refused": …}`, and exits 0 for a reading, 2 for a refusal, 1 for an error (a file unreadable, Central unreachable). The reader only reads: it fetches the sent entry the QR names ([Configuration snapshots](#configuration-snapshots)) and never writes to Central.
6. **Change, then read again.** Move what the suggestion says, take a new photo, and press **Done** on the tab when it reads right.

**Keep, no change, or retake** (owner, 2026-10-14: "It shouldn’t say “change nothing” unless it can prove the system is perfectly calibrated. “Retake the photo” is closer to the truth"). A step reads `keep` (pre-software), and a post-software suggestion `no_change`, only when the gap, widened by `KEEP_SPREADS` of its spread, lies within the property's match tolerance (`MATCH_TOLERANCE`, a placeholder for the bench like every threshold there). A gap over `KEEP_SPREADS` of its spread is named (raise, lower, warmer, cooler). Otherwise the photo cannot tell, and the answer is `retake` (pre-software, with the step's `spread`) or the property in the post-software `retake` list: take the photo again with the light moved off the paper (the reading's `dropped` says where). The rule's one home is `scripts/calibration_target/model.py` `judged`.

**Refusals, and what to do** (each stands for a real failure, [0027](decisions/0027-calibration-targets-v2.md#the-refusals-that-remain-each-for-a-real-failure)):

| Refusal | Do |
|---|---|
| `target-not-found` | get more of the screen in the photo, or come closer; for the paper, come closer or print it larger (the detail gives the squares' size) |
| `paper-off-screen` | lay the paper on the screen, not beside it |
| `code-unreadable` | the detail says which: the screen's squares too small for a QR (come closer), or no whole QR clear of the paper in the photo (take more of the screen, keep the paper off a copy, glare off it) |
| `unknown-entry` | pick the target again on the tab and retake (another Central, or the entry trimmed) |
| `not-a-target` | the tab had moved on: pick a target again and retake |
| `white-unread` | move the light, lamp or yourself away from the places named, and retake |

Dropped squares in a reading (`reflection-edge`, `covered`, `clipped`, `copy-unlike-the-rest`, …) are not refusals: each has its place, so the light can be moved next time.

**Regenerating the boards and the sheets.** The rasters and the printable sheets (one PDF per paper size and the README's PNG preview, in `central/displays/calibration_sheets/`) are drawn at build time, the same bytes for the same layout; after changing the boards' constants or a sheet, rewrite them and commit the result. A test regenerates them all and compares the bytes, so CI never rewrites them:

```sh
.venv/bin/python -m scripts.calibration_boards          # rewrite contracts/calibration_boards.py and the sheets
.venv/bin/python -m scripts.calibration_boards --check  # exit 1 when any file differs
```

The reader (`scripts/calibration_target/`) is the agent's tool and is never shipped; its thresholds have one home, `scripts/calibration_target/model.py`. **NOT RUN on real hardware yet:** [0027](decisions/0027-calibration-targets-v2.md#not-run) lists the bench steps. CI reads synthetic photos only.

## LUTs: paper profiles

The print look's LUT ([0023](decisions/0023-print-look.md)) is made from a **paper profile**: the free ICC file a paper maker publishes for each paper and printer (search the maker's site for "ICC profiles", for example Hahnemühle, Canson or Ilford, and take the profile for your paper on any printer; the printer matters little here). Central keeps the profile (`luts`, migration 093) and bakes the LUT from it whenever a Player asks, so there is nothing else to keep or back up.

**Add one.** In the console, Settings › LUTs › **Add a paper profile**, then choose it on a Frame's Picture tab (Photo Wall (software) › LUT); the page lists each LUT with the Displays that use it and removes one none uses. The same through the operator route:

```sh
curl --fail-with-body -X POST -H 'Authorization: Bearer <admin-token>' \
  --data-binary @'Hahnemuhle Photo Rag.icc' http://<central>/v1/operator/luts
curl -H 'Authorization: Bearer <admin-token>' http://<central>/v1/operator/luts     # the list, with each LUT's Displays
curl -X DELETE -H 'Authorization: Bearer <admin-token>' http://<central>/v1/operator/luts/<profile sha256>
```

Add answers 201 with the LUT (its name is the profile's own description), or 200 when the same file is already listed.

| Refusal | Means | What to do |
|---|---|---|
| 413 `too_large` | The file is over 8 MiB | A paper profile is a few hundred KB: check it is the `.icc`/`.icm` file and not an archive |
| 422 `not_a_colour_profile` | Not an ICC file | Unzip the maker's download and send the profile itself |
| 422 `not_a_printer_profile` | An ICC file, but not an RGB or CMYK printer (output) profile, or it does not bake | A monitor or camera profile is not a paper; take the paper's printer profile |
| 409 `lut_in_use` (with `displays`) | A Display uses it, stored or in an open Picture session | Choose None or another LUT on those Displays first |
| 404 `lut_unknown` | No LUT with that digest | Read the list for the digest |

**On the Pi.** The Player fetches `GET /v1/luts/<profile sha256>/<recipe>` with its own bearer and checks it against the served `Repr-Digest`, then keeps it in its media cache. Until it has it, the Frame shows the sliders without the LUT and the Picture tab's Done waits ("The sliders show at once; the LUT joins when the Pi has it, and Done waits for it"). A Done that never unlocks with a LUT chosen: read the Player's journal for the fetch's fault (`lut_type`, `lut_length`, `lut_digest` or `media_digest` name the refusal; it is retried on the media backoff).

**Bench check (agent-driven, never automated; NOT RUN until a release with the print look is on the test Pi).** On the test Pi 5 and its monitor ([Reaching a Node over SSH](#reaching-a-node-over-ssh)):

1. Black floor 2 % on the Picture tab and one light from above (depth 2 mm) on Position: the black lifts and a soft shadow shows along the opening's top edge; Done unlocks for each.
2. A paper profile added (above) and chosen at 100 %: the picture changes once the Pi has it, and Done waited for it.
3. A 4K video with every step on and four lights: compare frame time with everything neutral (0023's per-step cost is not measured; if it is too slow, 0023's alternative, one baked LUT for the whole colour chain, is the fallback).
4. Test pattern › Grey ramp with the LUT chosen ([Tune a display with no controls](#tune-a-display-with-no-controls)): look for banding from its 8-bit entries.

## Night off and the week

Roadmap 2a and 2b ([#86](https://github.com/mcurcio/photo-wall/pull/86), [0021](decisions/0021-power-timetable-and-repeating-schedules.md)): Central works out each HDMI port's next 7 days of off and on set times from Night off and holds and writes them in the port's Output document; the Pi switches at those times on its own clock. Schedules repeat by the week, and a Default Scene plays where none does. The console's side is the [operator console design §7](operator-console-design.md#7-power); the Pi's is the [Player architecture](player-architecture.md#display-identity-and-power-1b).

**Set the house first.** Settings › House (`#/house`, `GET|PUT /v1/operator/house`) holds the timezone and the optional location. At Central's start the house is seeded once with Central's own server zone (`central/infra/house_store.py` `seed_house`; a container's is often UTC), marked not confirmed: **Night off turns no display off until Settings › House is saved once** (the page and the Night off editor say so). Check the zone and save. Every time of day (Schedule windows, Night off, the week page, On this day, a Photo source's "last 3 years") is worked out in that zone; sunrise and sunset need the location and cannot be chosen until it is set. Saving a new zone or location moves the Schedules' windows and Night off's set times.

**Read a Pi's power timetable.** In Central's database, the latest document per port is `output_documents` (`document->'power'`: `now`, `changes` with each `at` and `power`, `until`, `darkness`, `test`), and the Pi's latest report is `output_displays.report` (`in_force`: `test`, `timetable` with its `power` and `since`, or `timetable-ran-out`):

```sql
SELECT o.player_id, o.output_id, o.change, o.document->'power' AS power, d.report->'in_force' AS in_force
FROM output_documents o LEFT JOIN output_displays d USING (player_id, output_id);
```

On the Pi, the controller's journal (above) logs each attempt; each is also a `display.record.power` event in `node_link_records`. Why is this playing on the Frame page reads the same: what Central wants, why, until when, and what the Pi says.

**All off and All on hold every Frame, each until its own next change** (owner, 2026-10-12: "Each Frame's hold ends at its own next change; a Frame with no Night off holds until 07:00 tomorrow"): a Frame in Night off until Night off's next off or on; any other Frame until Night off next turns displays on (07:00 by default; it follows Night off's on time), the next one after the press, so a press at 01:00 holds until 07:00 that morning. **A hold lasts** beside All off and All on (`GET|PUT /v1/operator/power/hold-lasts`) switches to **Until I resume**; it applies to holds pressed from then on. The hold banner shows one row per group of Frames that end together, each with **Resume schedule**; `power_holds` keeps each Frame's `ends_at`. While Night off waits for the house's zone a hold lasts to the timetable's horizon.

**With Central away** the Pi keeps switching at the set times for up to 7 days (the timetable's `until`), then leaves its displays as they are and reports `timetable-ran-out`. A Pi that boots without Central has no document and no Night off, as before. A Pi whose clock is wrong switches at the wrong time: check its clock first when a display switches at an odd hour.

**Deploying 2a and 2b.**

- Central and every Pi run the display line's schema 3 together. A Pi still on an older release refuses the new document and leaves its display as it is, reported, until it reboots onto the release (Central deploys through an iac pull request the owner merges; a Pi takes a new release at its next reboot).
- Today's Programs end at deploy (migration 089): they were timed in the browser's zone, which Central never recorded. Until a Default Scene is chosen and the routine remade, Frames keep their last photo. After deploy: choose a Default Scene on the week page, then remake each Schedule.
- Night off is seeded on: 23:00 to 07:00, every day, every Frame there was at deploy, displays off. It waits for the house's zone: save Settings › House after deploy, then check Night off on the week page's Power lane.

**Bench step (2a).** On the test Pi, with a release carrying #86 on it: set Night off's off time two minutes ahead and its on time two minutes after that; confirm the monitor goes off within a minute of the off time and on within a minute of the on time, and read the report's `in_force` at each. NOT RUN until such a release is on the Pi.
