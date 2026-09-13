# Photo Wall

Photo Wall is a self-hosted system for a room of physically framed displays. Everyday output should feel like a collection of photographs: mostly still images, restrained video, and occasional coordinated effects. A holiday slideshow can evolve throughout December; two portraits can briefly talk to each other; an overlay can reveal the current background when it finishes.

Immich remains the media library. Photo Wall manages physical locations, display calibration, presentation policy, schedules, and compatible media assignments. A **Frame** identifies a location; a replaceable **Player** supplies its output. Scenes can combine explicitly chosen Frame locations and lights while leaving unrelated targets alone. Players prepare upcoming assignments in a bounded rolling window and render locally.

Players [appear centrally without local setup](docs/requirements.md#player-provisioning) — a diskless Pi netboots a generic base image, fetches the Player app from central, and enrolls itself by its own hardware serial on a trusted LAN. They receive assets exclusively from the central show system and [remain unaware of Immich](docs/requirements.md#central-media-boundary).

## Project status

**MVP implementation in progress.** Central and the media worker launch with PostgreSQL, and the full real-media demo passes with two network Players and three simulated Outputs, including live Immich updates and outage/rejoin recovery.

Provisioning follows [decision 0009](docs/decisions/0009-minimal-base-and-app-package.md): a diskless Pi netboots a **generic base image** that carries no application; a small in-image bootstrapper fetches the **Player as a `.deb`** from central and `apt`-installs it; the Player enrolls itself by hardware serial into an operator pending queue over plain HTTP. No flashing, no per-device secret, and no signing — a home LAN has no threat model to defend, so the only integrity check is a corruption sha256. Shipping a Player update is "promote a new `.deb` in central," not a re-image. The whole path — base-image build, `.deb` build, boot-time `apt` install, ticketless enrollment, bind, and render — is proven end to end in CI; **it has not yet been booted on physical Pi 5 hardware**, which is the current frontier. See [Status and caveats](#status-and-caveats), the [setup and recovery runbook](docs/runbook.md), the [delivery checklist](docs/implementation-checklist.md), and [acceptance evidence](docs/evidence/README.md).

The foundation is one modular central application and a media preparation worker, with one Python Player process embedding GStreamer and GTK on each Raspberry Pi. Weston hosts the display session. Rendering capacity, real network boot, and visible synchronization still require hardware qualification.

## Getting started

Photo Wall has two halves, and you can meet the first one in about five minutes:

1. **The central control plane** — a Docker Compose stack (central service, PostgreSQL, media worker) that you host: the operator interface, the scheduler, and the media pipeline. **You can run this today on any machine with Docker.**
2. **The Player appliances** — diskless Raspberry Pi 5 devices that render to framed displays. **No flashing, no per-device install, no per-device secret.** Every Pi netboots one generic base image, fetches the Player app as a `.deb` from central, and enrolls itself by its own hardware serial over the LAN. **This half needs a trusted LAN, a boot server, and physical hardware.**

New here? Start with the control plane below. Come back for [Bring a display online](#bring-a-display-online) when you have Pi hardware on the same LAN.

> **Project maturity:** an MVP in active development. The control plane and its full media demo work today. The netboot → self-enroll → bind → render path is proven end to end in CI — the bootstrapper really `apt`-installs the real `.deb` on Debian trixie, the Player enrolls with no boot ticket, and the operator binds it and it renders. **It has not yet run on physical Pi 5 hardware;** real network boot and real HDMI output are the current frontier. See [Status and caveats](#status-and-caveats) before you depend on the Player half.

### Bring a display online

Once central is running, bringing a display online is four steps — and **none of them touch the individual Pi**:

1. **Choose which Player version central serves, once.** Central **auto-discovers** the `.deb` from your project's published GitHub releases — you just **promote the version you want** from the operator API; no manual download, no sha256 registration ([release sourcing](docs/module-player-package.md#operator-release-sourcing-0010), [runbook](docs/runbook.md#player-provisioning-promote-a-release-from-github-0010)). Every Pi fetches the promoted `.deb` at boot; shipping an update later is just promoting a newer version — no re-imaging. (Offline/air-gapped sites can still stage a `.deb` by hand — see the runbook.)
2. **Stage the generic base bundle on your boot server.** Kernel, initrd, and the base image — the same bytes for every Pi, carrying no application ([PXE service setup](docs/module-pxe-service.md)).
3. **Netboot a Pi 5** on the LAN. It loads the base, discovers central over mDNS, fetches and `apt`-installs the promoted `.deb`, and **enrolls by hardware serial — appearing unbound in the operator UI.** No boot ticket, no signature, no pre-registration, no baked-in secret.
4. **Bind it to a Frame** and calibrate; it renders. A reboot re-associates by serial automatically, and **unbind** frees a Frame later without retiring the hardware.

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

Then open **`http://127.0.0.1:8000`**, read the `PHOTO_WALL_ADMIN_TOKEN` value from `.env`, and paste it to sign in.

From the operator interface you can list Players and Outputs, create persistent Frames, bind equipment to them, calibrate (preview / commit / revert), define immutable Sources and Scenes, schedule Programs, and drive Runs. Program times are shown in your browser's local time zone.

To stop:

```sh
docker compose down        # add -v to also delete the database, media, and connection volumes
```

### Connect a real media library (Immich)

Photo Wall keeps using Immich as the media library; the worker pulls from it over a private connection. Setting one up is deliberately careful — **the API key never goes in operator forms, Source definitions, Git, or command-line arguments.** You stage a private JSON connection file into the worker's `connections` volume over stdin and restart the worker.

The full, copy-pasteable procedure (including the file schema and the ownership/permission checks) is in the [runbook](docs/runbook.md#local-launch); the worker's configuration contract is in the [media worker module](docs/module-media-worker.md).

### Development checks

```sh
uv sync --frozen
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
python3 scripts/check_docs.py
```

With the local Compose database running, also run `.venv/bin/python scripts/test_local.py -q`. Report any skipped integration checks explicitly rather than treating a pass as full coverage. See [CONTRIBUTING.md](CONTRIBUTING.md) for the engineering approach.

## Provision Player appliances

A Player is a diskless Raspberry Pi 5 that renders to one or two HDMI displays. Every Pi boots the **same** generic base image and fetches the **same** promoted Player `.deb` from central — the Pi is interchangeable hardware, and a **Frame** is the persistent location it serves. There is **no per-device install, no per-device secret, and no flashing**: a booted Pi reads its own hardware serial, finds central over mDNS, `apt`-installs the Player, and enrolls itself over plain HTTP, showing up **unbound** for you to bind. See [decision 0009](docs/decisions/0009-minimal-base-and-app-package.md) for the model.

Most of this lives outside this repo — your LAN, your Pi hardware, and a DHCP/TFTP boot server. The pages linked below own the exact commands; this is the map.

### What you provide

- **Hardware:** Raspberry Pi 5 (8 GiB, active cooling). Pi 5 H.264 decode is in software — measure your real video capacity, don't assume it. ([platform notes](docs/module-appliance-platform.md))
- **A trusted LAN** central and the Pi both reach. The model trusts that LAN: discovery is mDNS, transport is plain HTTP, identity is the Pi's serial. **No signing keys anywhere** — the only integrity check is a corruption sha256.
- **Central reachable from that LAN,** advertising `_photowall._tcp` by default. If you serve central on a port other than `8000`, also set `PHOTO_WALL_HTTP_PORT` so the advertisement points at the real port. Set `PHOTO_WALL_MDNS_ADVERTISE=false` to disable advertising for a deployment that configures every player's origin explicitly instead.
- **A DHCP/TFTP boot server** on that LAN to netboot the base bundle (kernel + initrd over TFTP, base image over HTTP). [PXE service setup](docs/module-pxe-service.md) owns the tree layout and DHCP/tftpd configuration.

### The two published assets

Both are GitHub release assets — or build them yourself on any host with `dpkg-deb` (no disk-imaging, no chroot, no signing tooling):

1. **The base bundle** — `config.txt`, a `cmdline.txt` template (fill in your central's base-image URL), the Pi 5 kernel, the initrd, the device tree, and the base squashfs. Stage it in your boot server's tree. It carries no application and almost never changes.
2. **The Player `.deb`** — central discovers it from your GitHub releases; you promote the version you want as current. This is the only thing you re-publish to ship an app update.

### How a Player comes online

1. The Pi netboots the base bundle; the initrd fetches the base image over HTTP (corruption-checked) and RAM-mounts it — no local storage, no state left behind.
2. The in-image bootstrapper discovers central over mDNS, downloads the promoted `.deb` and its manifest, verifies the sha256 (corruption only), and `apt`-installs it — pulling its GTK/GStreamer/Mesa dependencies from the Debian archive.
3. The Player starts, reads the Pi's hardware serial, and enrolls over HTTP. **Central shows it pending — no boot ticket, no pre-registration, no baked-in secret.**
4. You bind that Player to a Frame and calibrate it, in the same operator interface from the control-plane section. **Unbind** later (reversible) to free the Frame without retiring the hardware.
5. A reboot of a bound Pi re-associates with its Frame automatically, by serial — and fetches the current `.deb`, so it also picks up any app update you have promoted.

### Status and caveats

Be honest with yourself about what is proven:

- **Working and tested (software + CI, end to end):** mDNS discovery, hardware-serial identity, ticketless enrollment over HTTP, the operator pending queue, and bind/unbind — against a real PostgreSQL-backed central. The base image builds via [rpi-image-gen](docs/decisions/0009-minimal-base-and-app-package.md); the Player `.deb` builds and its dependencies resolve on Debian trixie; the bootstrapper really `apt`-installs the real `.deb` in a trixie container and the Player's Python 3.13 imports load; and the full manifest → fetch → install → enroll → pending → bind → render loop passes an automated end-to-end gate.
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
