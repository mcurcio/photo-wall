# Photo Wall

Photo Wall is a self-hosted system for a room of physically framed displays. Everyday output should feel like a collection of photographs: mostly still images, restrained video, and occasional coordinated effects. A holiday slideshow can evolve throughout December; two portraits can briefly talk to each other; an overlay can reveal the current background when it finishes.

Immich remains the media library. Photo Wall manages physical locations, display calibration, presentation policy, schedules, and compatible media assignments. A **Frame** identifies a location; a replaceable **Player** supplies its output. Scenes can combine explicitly chosen Frame locations and lights while leaving unrelated targets alone. Players prepare upcoming assignments in a bounded rolling window and render locally.

Players must [appear centrally without local setup](docs/requirements.md#player-provisioning) — flash a generic image, boot it on a trusted LAN, and it discovers central by serial. They receive assets exclusively from the central show system and [remain unaware of Immich](docs/requirements.md#central-media-boundary).

## Project status

**MVP implementation in progress.** Central and media worker services launch with PostgreSQL. The full real-media demo passes with two network Players and three simulated Outputs, including live Immich updates and outage/rejoin recovery. Native Linux rendering has separate integration evidence. The provisioning baseline is a generic flash image, mDNS discovery, and hardware-serial identity — a player enrolls itself into an operator pending queue over plain HTTP and is bound to a Frame; that software path is tested. Building and booting the flash `.img` on real hardware, and netboot's signed PXE tree, are built but not yet hardware-qualified. See the [setup and recovery runbook](docs/runbook.md), [delivery checklist](docs/implementation-checklist.md), and [acceptance evidence](docs/evidence/README.md).

The proposed foundation is one modular central application and a media preparation worker, with one Python Player process embedding GStreamer and GTK on each Raspberry Pi. Weston hosts the display session. Exact builds, rendering capacity, deployment mechanics, and visible synchronization still require qualification.

## Getting started

Photo Wall has two halves, and you can meet the first one in about five minutes:

1. **The central control plane** — a Docker Compose stack (central service, PostgreSQL, media worker) that you host. This is the operator interface, the scheduler, and the media pipeline. **You can run this today on any machine with Docker.**
2. **The Player appliances** — Raspberry Pi 5 devices flashed with one **generic** image and render to framed displays. There is no per-device install and no per-device secret baked in: a booted Pi derives its identity from its own hardware serial, finds central over mDNS on the LAN, and enrolls itself. **This half needs a trusted LAN and physical hardware.** Netboot (PXE), in place of flashing, is an opt-in enhancement.

New here? Start with the control plane below. Come back for [Provision Player appliances](#provision-player-appliances) when you have Pi hardware and a network to flash it on.

> **Project maturity:** this is an MVP in active development. The control plane and its full media demo work today. The flash-and-go baseline (mDNS discovery, serial enrollment, pending queue, bind/unbind) is implemented and tested in software, but **building the flash `.img` and booting it on physical Pi 5 hardware are not yet qualified** — see [Status and caveats](#status-and-caveats) before you depend on the Player half.

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

A Player is a Raspberry Pi 5 that renders to one or two HDMI displays. The baseline is **flash and go**: one **generic** image, identical for every deployment, flashed to an SD card or USB drive. There is **no per-device install and no per-device secret** — a Pi is interchangeable hardware, and a Frame is the persistent location it serves. A booted Pi reads its own hardware serial, finds central over mDNS on the LAN, and enrolls itself over plain HTTP; it shows up **unbound** in the operator's pending queue for you to bind. See [decision 0008](docs/decisions/0008-generic-image-and-serial-identity.md) for the full model — three independent ladders (delivery, identity, transport trust) that a deployment can climb, each rung opt-in.

This is more involved than the control plane, and much of it lives outside this repo (your LAN, hardware, and — if you choose the netboot enhancement — DHCP/TFTP/HTTPS and a signing key). The pages linked below own the exact commands; this is the map.

### What you provide

- **Hardware:** Raspberry Pi 5 (8 GiB, active cooling). Note that Pi 5 H.264 decode is in software — measure your real video capacity, don't assume it. ([platform notes](docs/module-appliance-platform.md))
- **A trusted LAN** central and the Pi both reach. The baseline trusts that LAN: discovery is mDNS, transport is plain HTTP, and identity is the Pi's serial — see the [transport-trust ladder](docs/decisions/0008-generic-image-and-serial-identity.md#transport-trust-t--new-from-the-baseline-review) for what that costs and how to harden it later.
- **Central reachable from that LAN,** advertising `_photowall._tcp` by default. If your deployment serves central on a port other than `8000`, also set `PHOTO_WALL_HTTP_PORT` so the advertisement points at the real port — otherwise a discovering player finds a dead port. Set `PHOTO_WALL_MDNS_ADVERTISE=false` to disable advertising for a deployment that configures every player's origin explicitly instead.
- **An Ed25519 signing keypair**, only if you also want the netboot enhancement below. The private key stays offline; the public key (`release.pub.pem`) is the one project-wide value baked into every image, flash or netboot alike.
- **A Linux arm64 build host** with Docker, root, standard disk-imaging tools (`mkfs.vfat`/`mkfs.ext4`/`guestfs`), and roughly 25 GiB of scratch space, to build the image yourself (no published release artifact yet — see [Status and caveats](#status-and-caveats)).

### Build and flash the baseline image

1. **Package the Player** — `python scripts/build_player.py --revision <commit> --output <dir outside Git>` produces the stateless Player app plus an offline wheelhouse. ([player package](docs/module-player-package.md))
2. **Build the generic flash image** — `python3 scripts/build_ci_flash_image.py --release-pub <your release.pub.pem> --output-dir <dir outside Git>` assembles a standard bootable `.img` with nothing deployment-specific baked in beyond that one public key. No origin, no CA, no time server — a booted Pi discovers all of that itself.
3. **Flash it** to an SD card or USB drive (e.g. with Raspberry Pi Imager or `dd`) and boot the Pi on your trusted LAN.

The reference/CI image is signed with a disposable key and is not meant to be flashed as a production deployment's only image; rebuild it with your own release key if you plan to also run the netboot enhancement against the same fleet.

### How a Player comes online

1. The Pi boots the flashed image, reads its own hardware serial, and generates a fresh in-memory enrollment key.
2. It browses `_photowall._tcp` on the LAN (only because no origin is explicitly configured) and learns central's address.
3. It enrolls by serial over HTTP. **Central shows it as pending — no pre-registration, no boot server involved.**
4. You bind that Player to a Frame and calibrate it — the same operator interface from the control-plane section. You can later **unbind** it (reversible) to free the Frame without retiring the hardware.
5. A reboot of an already-bound Pi re-associates with its Frame automatically, by serial — no operator action.

**Netboot, instead of flashing (opt-in enhancement, D1):** a Pi can PXE-boot a stateless, signed image from a TFTP/HTTPS boot server you run instead of carrying local storage. This trades a flash step for boot-server infrastructure; identity and enrollment work the same way once the Player process starts. See [PXE service setup](docs/module-pxe-service.md) and [appliance builder](docs/module-appliance-builder.md) for that path, and [CI build](docs/module-appliance-ci.md) for its signed-release pipeline. Cryptographic per-device identity (certificate pinning) and authenticated transport (pin-on-bind or an explicit CA) are further opt-in enhancements on top of either delivery path — **designed but not yet built**; see decision 0008 before depending on them.

### Status and caveats

Be honest with yourself about what is proven:

- **Working and tested (software):** mDNS discovery (browse and advertise), hardware-serial boot-context derivation, serial enrollment over HTTP, the operator pending queue, and bind/unbind — exercised against a real PostgreSQL-backed central.
- **Built but not yet hardware-qualified:** assembling the actual flash `.img` (the disk-partitioning/filesystem steps need Linux root tooling not available in every build environment) and booting it — or the netboot image — on **physical Raspberry Pi 5 hardware**; the netboot appliance builder, RAM-root bootstrap, and PXE service contract are otherwise qualified against real `tftpd` and an end-to-end CI gate under QEMU.
- **Not built (deferred, see decision 0008):** the certificate identity tier (I1) and both transport-trust enhancements (T1 pin-on-bind, T2 explicit CA/config); a published GitHub Release of the flash image.
- **Not yet qualified regardless of delivery path:** on-device native rendering and cache reuse, automatic failed-candidate reboot with central rollback, dual-HDMI output, and visible multi-Player synchronization.

Track progress against the [delivery checklist](docs/implementation-checklist.md) and dated [acceptance evidence](docs/evidence/README.md). A passing CI run does not establish physical Pi, real-Immich, or visible-timing behavior.

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
