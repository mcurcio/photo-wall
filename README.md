# Photo Wall

Photo Wall is a self-hosted system for a room of physically framed displays. Everyday output should feel like a collection of photographs: mostly still images, restrained video, and occasional coordinated effects. A holiday slideshow can evolve throughout December; two portraits can briefly talk to each other; an overlay can reveal the current background when it finishes.

Immich remains the media library. Photo Wall manages physical locations, display calibration, presentation policy, schedules, and compatible media assignments. A **Frame** identifies a location; a replaceable **Player** supplies its output. Scenes can combine explicitly chosen Frame locations and lights while leaving unrelated targets alone. Players prepare upcoming assignments in a bounded rolling window and render locally.

Players [appear centrally without local setup](docs/requirements.md#player-provisioning) — a diskless Pi netboots a generic base image with Central named on its kernel command line, fetches the Player app from central, and enrolls itself by its own hardware serial on a trusted LAN. They receive assets exclusively from the central show system and [remain unaware of Immich](docs/requirements.md#central-media-boundary).

## Project status

**MVP implementation in progress.** Central and the media worker launch with PostgreSQL, and the full real-media demo passes with two network Players and three simulated Outputs, including live Immich updates and outage/rejoin recovery.

Provisioning follows [decision 0014](docs/decisions/0014-reaching-central-from-every-boot-stage.md): a diskless Pi netboots a **generic base image** that carries no application and all required Debian packages; the kernel command line names Central (e.g., `photowall.central=http://photo-wall.localdomain/`); a small in-image bootstrapper downloads the **Player as a `.deb`** from central, verifies it against corruption, and installs it with `dpkg`; the Player enrolls itself by hardware serial into an operator pending queue over HTTP or HTTPS. No flashing, no per-device secret, and no signing — a home LAN has no threat model to defend, so the only integrity check is a corruption sha256. Shipping a Player update is "promote a new `.deb` in central," not a re-image. The whole path — base-image build, `.deb` build, boot-time install, ticketless enrollment, bind, and render — is intended to be proven end to end in CI; **CI proof is pending** (the base-image and netboot-e2e workflows are not yet green) **and it has not yet been booted on physical Pi 5 hardware**, which is the current frontier. See [Status and caveats](#status-and-caveats), the [setup and recovery runbook](docs/runbook.md), the [delivery checklist](docs/implementation-checklist.md), and [acceptance evidence](docs/evidence/README.md).

The foundation is one modular central application and a media preparation worker, with one Python Player process embedding GStreamer and GTK on each Raspberry Pi. Weston hosts the display session. Rendering capacity, real network boot, and visible synchronization still require hardware qualification.

## Getting started

Photo Wall has two halves, and you can meet the first one in about five minutes:

1. **The central control plane** — a Docker Compose stack (central service, PostgreSQL, media worker) that you host: the operator interface, the scheduler, and the media pipeline. **You can run this today on any machine with Docker.**
2. **The Player appliances** — diskless Raspberry Pi 5 devices that render to framed displays. **No flashing, no per-device install, no per-device secret.** Every Pi netboots one generic base image with Central named on its kernel command line, fetches the Player app as a `.deb` from central, and enrolls itself by its own hardware serial over the LAN. **This half needs a trusted LAN, a boot server, and physical hardware.**

New here? Start with the control plane below. Come back for [Bring a display online](#bring-a-display-online) when you have Pi hardware on the same LAN.

> **Project maturity:** an MVP in active development. The control plane and its full media demo work today. The netboot → self-enroll → bind → render path is meant to be proven end to end in CI — the bootstrapper really installs the real `.deb` with `dpkg` on Debian trixie, the Player enrolls with no boot ticket, and the operator binds it and it renders — but **CI proof of that path is pending** (base-image and netboot-e2e are not yet green). **It has not yet run on physical Pi 5 hardware;** real network boot and real HDMI output are the current frontier. See [Status and caveats](#status-and-caveats) before you depend on the Player half.

### Bring a display online

Once central is running, bringing a display online is four steps — and **none of them touch the individual Pi**:

1. **Choose which Player version central serves, once.** Central **auto-discovers** the `.deb` from your project's published GitHub releases — you just **promote the version you want** from the operator API; no manual download, no sha256 registration ([release sourcing](docs/module-player-package.md#operator-release-sourcing-0010), [runbook](docs/runbook.md#player-provisioning-promote-a-release-from-github-0010)). Every Pi fetches the promoted `.deb` at boot; shipping an update later is just promoting a newer version — no re-imaging. (Offline/air-gapped sites can still stage a `.deb` by hand — see the runbook.)
2. **Stage the generic base bundle on your boot server.** Kernel, initrd, and the base image — the same bytes for every Pi, carrying no application ([PXE service setup](docs/module-pxe-service.md)).
3. **Netboot a Pi 5** on the LAN. It loads the base, finds Central from the kernel command line, downloads and installs the promoted `.deb`, and **enrolls by hardware serial — appearing unbound in the operator UI.** No boot ticket, no signature, no pre-registration, no baked-in secret (see [decision 0014](docs/decisions/0014-reaching-central-from-every-boot-stage.md)).
4. **Draw a Frame, bind the detected Output, and calibrate the Frame.** To put media on it, make a Scene that targets that Frame, then Show now or schedule the Scene as a Program. A reboot re-associates by serial automatically, and **unbind** frees a Frame later without retiring the hardware.

That is the whole model: **two published assets** (the base bundle and the `.deb`), one boot server, zero per-device setup. The sha256 the bootstrapper checks is a **corruption check only** — a home LAN has no threat model, so nothing here is signed. The detailed map — what you provide, the assets, and how a Player comes online — is in [Provision Player appliances](#provision-player-appliances).

### Run the control plane locally

**You need:** Git, Docker Engine or Desktop with Compose, and Python 3.12 (for the config script and development tests). No paid runtime service is required.

```sh
git clone https://github.com/mcurcio/photo-wall.git
cd photo-wall
python3 scripts/configure.py
docker compose up -d --build --wait
curl --fail http://127.0.0.1:8000/healthz
```

What each step does:

1. **`scripts/configure.py`** writes a private `.env` (mode `0600`) holding a generated database password and operator token. It never overwrites an existing `.env`, so it is safe to re-run.
2. **`docker compose up`** builds and starts the central service, PostgreSQL, and the Procrastinate media worker. The web listener and database bind to loopback only.
3. **`curl .../healthz`** should return success. A green `/healthz` means the database is reachable and the scheduler ticked recently — it reports liveness, not that anything is on screen. While starting up it returns `503`; give it a few seconds.

Then open **`http://127.0.0.1:8000`**, read the `PHOTO_WALL_ADMIN_TOKEN` value from `.env`, and paste it to sign in. You sign in once per browser: the sign-in lasts 30 days, **Log out** in the header ends it, and changing the token signs every browser out (see [signing in and Log out](docs/runbook.md#operator-console-signing-in-and-log-out)).

From the operator interface you can list Players and Outputs, create persistent Frames, bind equipment to them, calibrate (preview / commit / revert), create and edit Sources and Scenes, schedule Programs, and drive Runs. Program times are shown in your browser's local time zone.

The redesigned console now serves at **`/`** (with **`/console`** kept as an alias); the old flat page has been retired. It opens on the **Wall** until a Frame exists, then on **Now showing**. Each Surface gets a 2D **wall plan** that draws its Frames as rectangles from their millimetre geometry (`x_mm/y_mm/width_mm/height_mm`), so you read the physical layout instead of a table; legacy Frames created at the origin with no distinct geometry collect in an **Unplaced tray** beside the plan. Selecting a Frame opens a read-only **Frame Inspector** with three facets — **Calibration** (the Frame's calibration and Frame profile), **Binding** (the Player/Output serving it and the Panel record at the Player app's last enrollment), and **Now-showing**. The now-showing tile is deliberately honest: it reads **_Scheduled: `<scene_id>`_** with the Run phase, because it reports what central *intends* to show — it never claims confirmed playback and never labels a Frame "LIVE." Each tile also carries one health state (for example *Player silent*, *Output interrupted*, *Needs calibration*, *Heard recently*) derived from when Central last heard a report from the Player, and an **attention strip** in the header counts the Frames that need you (see [wall health and the attention strip](docs/runbook.md#operator-console-wall-health-and-the-attention-strip)). The **Calibration** facet calibrates the Frame: drag the corner and crop handles (a folded quad or empty crop snaps back), then use **Live calibration** — with Display Host's acknowledgment where the node lane runs (Save calibration only after the latest edit is acknowledged), or under a 30-second server lease with a visible countdown elsewhere ("Show on the Panel", then "Save without acknowledgment") — one shared lease slot, last-writer-wins, with explicit conflict states (see the [runbook](docs/runbook.md#operator-console-calibration-and-conflict-states)). New or replacement Players appear on the **Players** list (`#/players`, one row per box: not enrolled / unbound / bound / retired), and each box has one **Player page** showing its node layers (including Display Host's current per-Output reports), Outputs, boot records, reboot and V1 boot offers, with the serial labelled as a claim ([the Players pages](docs/runbook.md#the-players-list-and-the-player-page)); you bind one by choosing a Player and Output from the **Binding** facet, or a free Output from its Player page (a bind forces a "Review required" recalibration but never discards committed calibration), and a returning known Pi reappears already bound by serial match, not identity, with its enrollment shown on its Player page (see [onboarding, binding, and auto-recovery](docs/runbook.md#operator-console-onboarding-binding-and-auto-recovery)). You now **edit the wall plan by direct manipulation**: drag a rectangle on the empty plan to **place** a new Frame (then enter its Frame profile), drag an existing rectangle to **move** one (last-write-wins, never a resize), drag a Frame out of the **Unplaced tray** to give it a position, or **delete** a Frame that is neither bound nor targeted by a live Run (see [placing, moving, and deleting Frames](docs/runbook.md#operator-console-placing-moving-and-deleting-frames-and-the-unplaced-tray)). A **sidebar of sections** (organization, not permission — the same single admin token) replaces the old Wall / Showrunner toggle, in a look matched to the photo library's colours and type; every section and every step has its own `#/…` address, so links and bookmarks work ([sections, links and drafts](docs/runbook.md#operator-console-sections-links-and-drafts)). The Wall side is **Wall**; the fleet side is **Players**; **Needs attention** is a section of its own, on neither side, listing what needs you with links into the Wall. The Show side is **Now showing**, **Scenes**, **Schedule** and **Photo sources**, where each job is a short **step flow** with one question per step, defaults under **Advanced** and a **Review** that lists every answer: add and edit a **photo source** (a saved live selection with a plain name, optional favourites and capture dates; no albums; internal revisions stay hidden), make a **Scene** (live from a source or hand-picked per frame; the console derives each id; "Keep playing until the Program ends" is on by default; Edit opens at Review and offers Reload when someone else saved meanwhile; see [viewing and editing a Scene](docs/runbook.md#viewing-and-editing-a-scene)), **schedule** it as a Program (one window, or separate windows at the same local times; each card reads Upcoming, Running, Ran, Refused or Missed), and **show it now** (the priority defaults to the Run already on top, and a retry after an unknown outcome cannot start it twice). Now showing has **Run cards** (finish or cancel), a **Why?** per frame that states Central's plan with each layer's priority and its limits, a [Why nothing new?](docs/runbook.md#why-nothing-new-on-a-frame) walk, and the **Media pipeline** panel ([the media pipeline](docs/runbook.md#the-media-pipeline)). Unsaved drafts survive section changes, refreshes and a session that expires under the sign-in screen; Log out or a reload discards them. Every control that cannot act says why. The Show sections deliberately cannot reach the Calibration facet — at show time the only hardware fact they surface is each Frame's health state (see [running the show](docs/runbook.md#operator-console-running-the-show)).

To stop:

```sh
docker compose down        # add -v to also delete the database, media, and connection volumes
```

### Connect a real media library (Immich)

Photo Wall keeps using Immich as the media library; the worker pulls from it over a private connection. Setting one up is deliberately careful — **the API key never goes in operator forms, Source definitions, Git, or command-line arguments.** You stage a private JSON connection file into the worker's `connections` volume over stdin and restart the worker.

After the worker checks in, **Photo sources** offers the configured connection names when you create or edit a Source. The names confirm what the worker loaded; the Source's refresh result reports whether Immich accepts the connection and query.

The full, copy-pasteable procedure (including the file schema and the ownership/permission checks) is in the [runbook](docs/runbook.md#local-launch); the worker's configuration contract is in the [media worker module](docs/module-media-worker.md).

### Development checks

```sh
uv sync --frozen
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
python3 scripts/check_docs.py
```

With the disposable test database running (`docker compose -f tests/integration/compose.test-database.yml up -d --wait`; see the [runbook](docs/runbook.md#tests-and-local-development)), also run `.venv/bin/python scripts/test_local.py -q -n auto`. Report any skipped integration checks explicitly rather than treating a pass as full coverage. See [CONTRIBUTING.md](CONTRIBUTING.md) for the engineering approach.

## Provision Player appliances

A Player is a diskless Raspberry Pi 5 that renders to one or two HDMI displays. Every Pi boots the **same** generic base image and fetches the **same** promoted Player `.deb` from central — the Pi is interchangeable hardware, and a **Frame** is the persistent location it serves. There is **no per-device install, no per-device secret, and no flashing**: the Pi's kernel command line names Central, it fetches and installs the Player with `dpkg`, and enrolls itself over HTTP or HTTPS, showing up **unbound** for you to bind. See [decision 0014](docs/decisions/0014-reaching-central-from-every-boot-stage.md) for the model.

Most of this lives outside this repo — your LAN, your Pi hardware, and a DHCP/TFTP boot server. The pages linked below own the exact commands; this is the map.

### What you provide

- **Hardware:** Raspberry Pi 5 (8 GiB, active cooling). Pi 5 H.264 decode is in software — measure your real video capacity, don't assume it. ([platform notes](docs/module-appliance-platform.md))
- **A trusted LAN** central and the Pi both reach. The model trusts that LAN: Central is named on the netboot command line (mDNS only without it; see [decision 0014](docs/decisions/0014-reaching-central-from-every-boot-stage.md)), transport is HTTP or HTTPS, identity is the Pi's serial. **No signing keys anywhere** — the only integrity check is a corruption sha256.
- **Central reachable from that LAN,** advertising `_photowall._tcp` by default. If you serve central on a port other than `8000`, also set `PHOTO_WALL_HTTP_PORT` so the advertisement points at the real port. Set `PHOTO_WALL_MDNS_ADVERTISE=false` to disable advertising for a deployment that configures every player's origin explicitly instead.
- **A DHCP/TFTP boot server** on that LAN to netboot the base bundle (kernel + initrd over TFTP, base image over HTTP). [PXE service setup](docs/module-pxe-service.md) owns the tree layout and DHCP/tftpd configuration.

### The two published assets

Both are GitHub release assets — or build them yourself on any host with `dpkg-deb` (no disk-imaging, no chroot, no signing tooling):

1. **The base bundle** — `config.txt`, a one-line `cmdline.txt` template (replace `@@PHOTOWALL_CENTRAL@@` with Central's root URL), the Pi 5 kernel, the initrd, the device tree, and the base squashfs, published as `photo-wall-base-<revision>.tar.gz`. Its `boot/` tree is also published alone as `photo-wall-boot-<revision>.tar.gz`; stage that in your boot server's tree ([runbook](docs/runbook.md#player-provisioning-stage-the-netboot-bundle-and-read-its-console-0014)). Central serves the base squashfs itself. The bundle carries no application and almost never changes.
2. **The Player `.deb`** — central discovers it from your GitHub releases; you promote the version you want as current. This is the only thing you re-publish to ship an app update.

### Central's base-image storage and the per-device `.deb` (0012)

Central **auto-mirrors** the base squashfs so you never hand-stage it: the worker discovers every release from GitHub, and when a Pi needs a version's base image the worker downloads that release's base tarball, verifies it, extracts the squashfs, and serves it at `GET /v1/netboot/base`. Two environment variables govern this ([decision 0012](docs/decisions/0012-netboot-base-auto-mirror.md); the operational model is in the [runbook](docs/runbook.md#base-image-auto-mirror-0012)):

- **`PHOTO_WALL_CACHE_ROOT` (optional; default `/var/cache/photo-wall`, baked into the image) — the single cache root under which base squashfs bytes live at the derived `os-images/base-<tarball sha256>.squashfs`, named by the sha256 of the release's base tarball.** Nothing evicts them yet. As of [decision 0013](docs/decisions/0013-unified-cache-root.md) there is one cache root, not per-domain roots: the app derives `media/`, `apps/`, and `os-images/` as internal constants, and the retired `PHOTO_WALL_{MEDIA,APP,BASE}_ROOT` variables are gone from the deploy contract. **Base serving (and release sourcing) are always-on** — there is no opt-in gate and no "off" state. The cache is **mounted RW on the worker and RO on central**; the worker is the single writer and **asserts its cache is writable at boot and fails loud** if not — the fix for the original outage class, where nothing populated the served directory and `GET /v1/netboot/base` returned a silent, permanent `503`. A base miss now returns a **transient** `503` that self-heals: the worker fetches the bytes and the Pi retries on its next boot. If the cache is on **NFS**, `flock` and `O_EXCL`/atomic-rename reliability across the mount is a documented precondition. See the [Central cache subsystem](docs/module-central-cache.md).
- **`PHOTO_WALL_PER_DEVICE_DEB` (opt-in) — enables the per-device `.deb` path** so a Pi fetches the `.deb` of the exact release its base was served this boot (via `GET /v1/netboot/manifest`, serial-keyed), keeping base and `.deb` from diverging. **Unset ⇒ unchanged 0010 behavior**: the Pi fetches the single globally promoted `.deb` and posts no base-health.

### How a Player comes online

1. The Pi netboots the base bundle; the initrd fetches the base image over HTTP (corruption-checked) and RAM-mounts it — no local storage, no state left behind.
2. The in-image bootstrapper reaches Central from the kernel command line, downloads the promoted `.deb` and its manifest, verifies the sha256 (corruption only), and installs it with `dpkg` — all GTK/GStreamer/Mesa dependencies were pre-built into the base image (see [decision 0014](docs/decisions/0014-reaching-central-from-every-boot-stage.md)).
3. The Player starts, reads the Pi's hardware serial, and enrolls over HTTP. **Central shows it pending — no boot ticket, no pre-registration, no baked-in secret.**
4. You bind that Player to a Frame and calibrate it, in the same operator interface from the control-plane section. **Unbind** later (reversible) to free the Frame without retiring the hardware.
5. A reboot of a bound Pi re-associates with its Frame automatically, by serial — and fetches the current `.deb`, so it also picks up any app update you have promoted.

### Status and caveats

Be honest with yourself about what is proven:

- **Working and tested (software, local + unit/integration):** kernel command-line Central naming, fallback mDNS for non-netboot Players, locate-through-redirects, hardware-serial identity, ticketless enrollment over HTTP, named failure logging, the operator pending queue, and bind/unbind — against a real PostgreSQL-backed central. The Player `.deb` builds and its dependencies resolve on Debian trixie; the bootstrapper really installs the real `.deb` with `dpkg` in a trixie container and the Player's Python 3.13 imports load; and the full manifest → fetch → install → enroll → pending → bind → render loop passes locally. **CI proof of the base image build and the netboot end-to-end gate is pending** — the `base-image` and `netboot-e2e` workflows are not yet green; do not treat this path as CI-proven until they are.
- **The current frontier (not yet done):** booting the base bundle on **physical Raspberry Pi 5 hardware** over a real network boot, and confirming real GTK/HDMI rendering on real panels under Debian trixie's Python 3.13 and distro package versions. CI proves the software contract and that dependencies resolve — not real pixels or a real kernel boot.
- **Not yet qualified regardless:** on-device native rendering and cache reuse, dual-HDMI output, and visible multi-Player synchronization.

Track progress against the [delivery checklist](docs/implementation-checklist.md) and dated [acceptance evidence](docs/evidence/README.md). A passing CI run does not establish physical-Pi, real-Immich, or visible-timing behavior.

## Documentation

Start with the [documentation guide](docs/README.md), or choose a topic:

| Document | Purpose |
|---|---|
| [Requirements](docs/requirements.md) | Product behavior, terminology, and scope. |
| [Architecture](docs/architecture.md) | Components, ownership, and platform choices. |
| [Execution contract](docs/execution-contract.md) | Planning, readiness, commitment, timing, and recovery. |
| [Implementation plan](docs/implementation-plan.md) | Bounded slices and their dependencies. |
| [Validation](docs/validation.md) | Scenarios, experiments, and performance evidence. |
| [Design decisions](docs/design-decisions.md) | Open choices and their decision criteria. |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for required engineering principles, recursive module design, agent orchestration, and evidence expectations. The first implementation tracks are central execution with simulated devices and a physical Player prototype driving two panels. They can progress independently and meet at a concrete execution contract. Focused contributions should advance a demonstrable scenario and document what was verified.
