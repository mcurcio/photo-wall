# 0019 — Photo Wall's Node software built as Debian packages with debhelper

**Date:** 2026-10-09 · **Layer:** module contracts and data flow (how the Node's software is built, packaged and put into images) · **Status:** built (this PR, [57](https://github.com/mcurcio/photo-wall/pull/57)); owner-approved at the gate on 2026-10-09. How the built system works is the [Debian packaging module](../module-debian-packaging.md); what changed from this design while building it is [Built: deviations from the design](#built-this-pr-deviations-from-the-design). The requirements below bind. The owner's three answers are current choices and stay revisable. The design was drafted against `origin/main` at `aa694bb`; a claim marked *(inferred)* was not checked against code or a run, and the [implementer must prove](#premises-to-prove-first-in-p1) the load-bearing ones first.

**Gate page:** <https://claude.ai/artifact/JexW8dAb1mU9D8VL85xEbc>

## Problem

The Node's software is packaged by about 3,100 lines of hand-written Python under `scripts/` (§ [Today](#today)): each builder stages files, renders Depends, writes control files and calls `dpkg-deb` itself, and some builders still serve the retired V1 posture. The owner asked for the Photo Wall code to ship as packages built by an established Debian framework, with no hand-rolled Python packaging and with OS features kept out of Photo Wall's packages.

## Requirements (bind)

| ID | Requirement (verbatim) | Source |
|---|---|---|
| R1 | "i want all of the custom photo-wall code built and deployed as packages" | Owner, chat, 2026-10-09 |
| R2 | "building as packages should be done via existing package management tools. whats a good framework for designing debian packages? choose one and use that." The framework proposed was debhelper; the owner accepted: "debhelper is fine". | Owner, chat, 2026-10-09 |
| R3 | "we should not be hand-rolling python code" | Owner, chat, 2026-10-09 |
| R4 | "SSH is not 'our package'. it should go straight into the OS like any other basic OS dependency" | Owner, chat, 2026-10-09 |

## Owner answers at the gate (current choices, revisable)

Given on 2026-10-09 on the [gate page](https://claude.ai/artifact/JexW8dAb1mU9D8VL85xEbc). These are answers to the questions the page asked; they are current choices, not rulings, and the owner can revise them.

| # | Question | Answer / current choice | Cost of the alternative not taken |
|---|---|---|---|
| Q1 | Versions | **Content-derived versions.** Each binary package's version is a hash of what it installs, so an unchanged part keeps its version and its image keeps its bytes (today's behaviour). | One release version per release would give every Node a new base digest (a reboot) and a new app digest (a visible swap) on every release, even a Central-only one, and every packaging commit would rebuild the base (about 20 min per the `base-image.yml` comment; one measured cold step took 247 s) and the components (cold about 9 min, `node-components.yml`). |
| Q2 | Scope of R1 | **Central stays an OCI image built by Docker and uv.** This record covers the Node's software. | Central as `.deb`s inside its image would take FastAPI, pydantic and psycopg from Debian trixie instead of `uv.lock`. |
| Q3 | Stage 1 (the initrd) | **Moves last, as P4**, after P1–P3. | Moving it now would change the initrd cache key and `verify_netboot_initrd.py` alongside everything else. |

## New terms

| Term | Meaning | Why needed | Existing term it could be |
|---|---|---|---|
| source package | `debian/` plus the tree one `dpkg-buildpackage` run builds | Debian's unit of build | ".deb builders" |
| binary package | one `.deb` the source package produces | Debian's unit of install | ".deb" |
| local repo | one build's `.deb`s plus an apt index, used as a `file:` apt source by image builds only | lets apt resolve Photo Wall packages like OS packages | none |
| launcher | a program's entry directory, run as `python3 -I -B <dir>` | the unit each program starts from | 0014's private package directory |

## Today

| Builder | Produces → consumer | Lines | Posture |
|---|---|---|---|
| `build_node_base_deb.py` | node-base `.deb` (7 launcher closures, units, nats-server, nats-py) → base, PID1 fixture | 197 | V2 (plus V1 cohort overrides) |
| `build_node_display_deb.py` | display `.deb`, client `.so`, graphics ABI → base, `app.deb` | 114 | V2 |
| `build_node_manager_deb.py` | AppManager `.deb` → manager image | 54 | V2, retiring |
| `build_player_deb.py` | published Player `.deb`; `app.deb` → release; app image | 445 | V1 + V2 |
| `build_bootstrapper_deb.py`, `build_player.py` (wheelhouse → `demo_wall.py --wheelhouse`), `build_player_payload.py` | provision `.deb` → base; e2e Player; data-only archive → release | 962 | V1 |
| `build_app_environment.py`, `sealed_archive.py`, `build_environment_image.py` | sealed tar → squashfs ×2 → release | 429 | V2 |
| `build_node_components.py`, `node_component_inputs.py`, `node_build_inputs.py` | component set, cache key, `components.json` | 317 | V2 |
| `build_node_pid1_fixture.py` | test image, stage targets | 152 | V2 test |
| `debian_packages.py`, `vendored_packages.py`, `module_closure.py`, `nats_server.py`, `pinned_fetch.py` | declarations, closures, fetches | 991 | both |
| `build_boot_data.py`, `build_netboot_bundle.sh` | initrd boot data, netboot bundle | 184 + 329 sh | stage 1 |

## Constraints tested

Each constraint today's builders enforce was tested for whether it is real or an artefact of the hand-rolled builders.

| Constraint | Verdict |
|---|---|
| Per-launcher closures | **Real.** The contexts have separate import graphs ([Player node domain model](../player-node-domain-model.md); `pyproject.toml`: "The host-core launcher ships alone"), and the lists are computed ([0014](0014-reaching-central-from-every-boot-stage.md)). Kept as per-context directories plus a computed check. |
| Content-derived versions, input-keyed caches | **Real effect:** unchanged Pi software keeps its digest. Kept (Q1). |
| Two-build digest equality | Kept on a cache miss: a rebuilt image must keep its digest. |
| Squashfs roots, line checks | **Real** ([0017](0017-node-redesign-r3.md) C8; `capacity.py`: app line 320 MiB against 288 measured, manager 96). Kept. |
| ABI ids, environment seal | **Real** ([Player node domain model](../player-node-domain-model.md); the release check). Kept, same semantics. |
| nats-server 2.15 | **Real.** The bus uses `allow_msg_ttl` (`nodeapi/buffers.py`; nats-server 2.11+, *inferred*), and the fence was measured on 2.15 (`contracts/node_link.py`); trixie ships 2.10.27. |

## The shape

**One source package, `photo-wall`.** `debian/` sits at the repository root: `3.0 (native)`, `debhelper-compat (= 13)`, `dh $@`. The display context builds with Meson via `dh_auto_* -D appliance/display_host --buildsystem=meson`. `debian/changelog` is committed once and never edited. It is built by `dpkg-buildpackage` in a pinned Debian container (sbuild's unshare mode needs an AppArmor sysctl on Ubuntu 24.04 runners, *inferred*).

**Binary packages cut by import-linter layer.** Each owns one directory, `/usr/lib/photo-wall/<name>/`, so each file has one owner and nothing lists files.

| Package | Contents | Root |
|---|---|---|
| `photo-wall-common` | `contracts`, `nodeapi` | base, app, manager |
| `photo-wall-uplink` | `uplink` (stdlib-only) | base, app, manager |
| `photo-wall-node-{kernel,central-session,host,display,health,apps,boot,manager}` | one context each (`display` is arch any and adds the Meson shell, the diagnostic and `abi.json`) | base; the manager root takes AppManager's imports |
| `photo-wall-frame-client` | the client `.so` | app |
| `photo-wall-player` | `player/` and its launcher | app |
| `photo-wall-app-manager` | AppManager's launcher | manager |
| `photo-wall-node` | composition: base units, slices, target (`dh_installsystemd`), `.sysusers`, `.tmpfiles`, bus conf, seven launchers, `abi.json`; Depends: each context at its exact version, systemd, udev, `nats-server (= pin)` | base |

`appliance` becomes a namespace package (its `__init__.py` is only a docstring). Each launcher's `__main__.py` puts on `sys.path` its own context and only the lower layers import-linter allows, plus the retiring directories its listed ignore lines name. Each launcher physically sees only its own graph, keeping 0014's `-I -B`; no `.pyc` lands in the RAM root.

**Computed check (construction time, `override_dh_auto_test`).** The existing modulefinder closure per launcher refuses a build when a reached module lies outside that launcher's path, when a third-party root's owner (`dpkg -S` in the build root) is not in its package's Depends, or when a Depends entry is unreached. Declared Depends therefore cannot drift from the imports; `dh_python3` does not do this for private directories.

**Versions and cache keys (Q1).** Each binary's version is `0+<12 hex>` over its installed tree and its Depends, set with `dpkg-gencontrol -v` in `override_dh_gencontrol` (*inferred*), so unchanged code keeps its bytes. Caches key on the local repo's `Package=Version` lines an image installs, plus the snapshot pin and the recipe.

**ABI ownership (same semantics as today).** `photo-wall-node` writes `base_abi`, a hash of its exact Depends plus the image format. `photo-wall-node-display` writes `graphics_abi` (`weston14-` plus a hash of its version, the frame-client version and the resolved runtime versions) and the declared `plugin_abi` `frame-v3`.

**Third party.** nats-server: upstream's own `nats-server-v2.15.0-arm64.deb` (the release asset exists), unmodified, checked by sha256 and added to the local repo; apt installs it as a dependency, like any OS package (R4). One pin file is its only version home, read by the repo step, by `debian/rules` (`${nats:Version}`) and by `scripts/nats_server.py` (CI's node-bus and bus-fence tests). nats-py: `packaging/python-nats/`, built with pybuild as `python3-nats`; a test binds its version to `uv.lock`, as today.

**Images.** rpi-image-gen installs `photo-wall-node` from the local repo; OS features stay rpi-image-gen layers, as PR 54 did for SSH (R4). The app and manager roots: `mmdebstrap --variant=apt` from the pin and the repo, with `path-exclude` for docs, man pages and locales and `--skip=output/dev`, then `tar2sqfs` (zstd, 128K blocks). A shell customize-hook strips setid bits, makes today's `materialize` placeholders (an empty 0444 `etc/photo-wall/public.json`, the `/run` mount points) and runs the seal. The PID1 fixture: a Dockerfile running `apt-get install photo-wall-node`; its failing target is an `equivs` stub.

## Design rules

1. Each fact lives once, in the file its tool reads: Depends in `debian/control`, OS features in rpi-image-gen layers, the snapshot pin in `snapshot.list`, the nats-server version in its pin file.
2. One binary package per import-linter layer, one directory each; a launcher's path lists only its allowed layers.
3. Build-time Python only checks imports or writes the release contract; it never stages package files or fetches bytes.

## Module contracts

| Module | Takes | Gives | Never | Owns |
|---|---|---|---|---|
| source build | tree, snapshot pin, nats pin | `.deb`s with `abi.json` | uses the network; takes a hand-typed version | versions, ABI ids |
| import check | closures, Depends, build root | pass or refuse | edits a package | nothing |
| local repo | `.deb`s, the nats `.deb` | `file:` apt source, index | reaches a Node | nothing |
| base build | pin, `rpi_image_gen/**`, repo | base squashfs | names a Photo Wall dependency | nothing |
| root build | pin, repo, root package | app and manager squashfs | uses a Docker builder image | nothing |
| seal hook | chroot, ref fields | `environment.json`, package lock | changes package files | the manifest |
| release writer | images, `abi.json`, line table | `components.json` (refs) | builds or computes an ABI | `components.json` |
| `snapshot.list` | — | the pin | is rendered by Python | the pin |

## Data flow

| Step | Reads | Writes | Cache key |
|---|---|---|---|
| 1. Source build, local repo | tree, pins, the nats `.deb` | `.deb`s, index | none (minutes) |
| 2. Base | repo, pin, layers | base squashfs | layers + pin + installed `Package=Version` |
| 3. Roots and seal | repo, pin | two squashfs, built twice on a cache miss | recipe + pin + the root's `Package=Version` |
| 4. Release writer | steps 2–3, `capacity.py` | `components.json`; refuses an image over its line | none |

## Python that goes and stays

**Goes** (about 3,100 lines plus about 2,300 lines of tests): every row of [Today](#today) except `nats_server.py` and stage 1, and `module_closure.py`'s staging.

**Stays:** the closure check (about 250 lines; no Debian tool computes imports for private directories); the seal and the release writer (the contract the Node reads, on `appliance/apps/environment.py` and `capacity.py`); `nats_server.py` (tests); stage 1 until P4; the release flow.

## Relation to earlier records

- [0014](0014-reaching-central-from-every-boot-stage.md), "One Debian declaration": **superseded.** `scripts/debian_packages.py` no longer renders Depends; Depends live in `debian/control` and the pin in `snapshot.list` (rule 1). Provisioning's `dpkg --install` of the Player is V1 and goes.
- [0014](0014-reaching-central-from-every-boot-stage.md), "Private package directories (Q1)" and "Computed module lists": **kept in substance.** Programs still run as `python3 -I -B <dir>` from directories under `/usr/lib/`, and imports are still computed; the computation becomes a check on per-layer package directories instead of the source of staged file lists. The initramfs's computed list stays until P4.
- [0017](0017-node-redesign-r3.md) C8 (release roots as squashfs images, the line table): **kept**; only the build of the images changes.
- [0017](0017-node-redesign-r3.md) C19 (the bus unit): **kept**, with a new vehicle: nats-server comes from upstream's `.deb` through the local repo instead of inside `node-base.deb`, and nats-py ships as `python3-nats` instead of a vendored wheel.

## Costs

- Versions do not sort; about ten lines of `debian/rules` shell compute them.
- About 13 binary packages instead of 5.
- The nats-py version is held equal to `uv.lock` by a test, not by construction.
- `demo_wall.py` changes: its Player runs from the app image (`docker import` of the root tar) instead of a wheelhouse.

## Deferred

- Stage 1 (Q3): P4.
- A hashed base ABI orphans app images on any base change (an existing finding, unchanged here).

## Non-goals

A public apt repository, package signing, dpkg on the Pi.

## Delivery outline

| Phase | Units | Scope |
|---|---|---|
| **P1, tracer** | 2 | `debian/` skeleton, the nats pin file, the build container, the local repo; `photo-wall-node-display` and `photo-wall-frame-client` via Meson with `graphics_abi`, consumed by rpi-image-gen, the components and the release; delete `build_node_display_deb.py`. |
| **P2, Python packages** | 4 | The context packages, launchers, the closure check, `photo-wall-node`, nats-server and `python3-nats`. |
| **P3, roots** | 3 | mmdebstrap images, the seal hook, the release writer, the PID1 Dockerfile, `demo_wall.py` on the app image; then delete the V1 rows of [Today](#today) and the cohort overrides. |
| **P4, stage 1** | 2 | Per Q3: `photo-wall-netboot-init` installs an initramfs-tools hook (Debian's idiom, already used in `appliance/netboot_initramfs/hooks`) that copies `photo-wall-uplink` and stage 1's directory, so `build_boot_data.py` goes and `build_netboot_bundle.sh` only assembles. |

Net: about −5,400 lines of Python and tests, +400 lines of configuration.

## Built (this PR): deviations from the design

The design above is kept as approved. Building it found these differences, each recorded as an erratum (`E-0019-*`, [`.claude/errata.md`](../../.claude/errata.md)). Where the owner decided, his answer is quoted in the erratum.

**Layout and ownership**

- `packaging/python-nats/` is `debian-packaging/python-nats/`: a root directory named `packaging` shadows PyPI `packaging` for the closure tooling (P1A-1). The pin is `debian-packaging/snapshot.list` (P1A-2), and its first line's instant is the one `SOURCE_DATE_EPOCH` (`debian-packaging/snapshot-epoch.sh`, P4-8).
- The build container prefers the local repo over the snapshot (apt pin 1002), because trixie's own `nats-server` 2.10.27 would otherwise out-rank the pinned 2.15.0 (P1A-3). nats-server's licence ships as upstream's text in `photo-wall-node`'s doc directory (P1A-4).
- `graphics_abi` is circular as written in this record. It hashes the Meson outputs of the display and frame client and the third-party runtime, never the context's Python (P1A-5, P1B-1, P2A-9). `base_abi` hashes `photo-wall-node`'s expanded Depends and the image format only, so a change to a base unit or launcher keeps `base_abi` and app images stay compatible (P2B-5).
- Build-time Python that stays is larger than listed: `scripts/import_check.py`, `scripts/seal_root.py` and `scripts/node_release_writer.py` (the check, the seal and the release contract), plus `scripts/module_closure.py`'s finder.

**Packages and the import check**

- The check runs at `execute_after_dh_install`, not `override_dh_auto_test`: no `debian/<package>/` tree exists earlier (RECUT-5), and `nocheck` cannot skip it. Depends name **direct** imports, not closures (RECUT-4).
- The Depends cycle through the retiring ignore lines is resolved by the owner's pick, "Exempt listed edges": an edge listed in `pyproject.toml`'s `layers` `ignore_imports` gives no Depends (OWNER-1, RECUT-12). Costs: a launcher's `PATH` exposes every module of the package directories it reaches (P2B-4); installing `photo-wall-uplink` pulls `python3-pydantic` and `python3-nats` through `photo-wall-common` (P2A-8).
- The record's table had no home for the `appliance/*.py` modules the launchers reach: `feed.py` and `feed_socket.py` are in `photo-wall-node-kernel`, `process_identity.py` in `photo-wall-node-apps`, `node_boot_handoff.py` in `photo-wall-node-boot` (P1A-11, P2A-2).

**Roots, seal and fixture**

- The image is not the root: `appliance/apps/environment.py`'s layout is three metadata files beside `rootfs/` (P3A-2). mmdebstrap's own cleanup runs after its customize hooks, so the seal runs after mmdebstrap, from `build-root.sh`, not as a hook (P3A-1). The app root's file capabilities are handled by the seal (P3A-3). `build-root.sh` writes a fixed empty `/etc/hostname`, since two builds in containers with different hostnames differed in that file (P1A-6, P3A-9).
- The PID1 fixture is a Dockerfile FROM the build container, not the slim target, because its head needs the compiler and `libweston-14-dev`; its stage targets are built with `build-root.sh --once` (P3A-8).

**Stage 1 (P4)**

- The shipped `initrd.img` is three archives, not two: the floor's layer, `mkinitramfs`' own early archive, the compressed one (P4-1). The hook copies each listed directory's import roots, so unused modules ride along (about 0.3 MB, P4-2). `dh_installinitramfs` adds an `update-initramfs` trigger (P4-3).

**V1 removal (owner's pick, "Do it in this PR")**

- The V1 builders, the bootstrapper, the OS agent, the Player `.deb` and payload, the V1 release files, Central's V1 routes (migrations 069 and 070) and the `photowall.node=v2` switch are deleted in this PR (OWNER-2, RECUT-6; [runbook](../runbook.md#player-provisioning-the-v1-netboot-and-promote-path-removed)). Deleted, not ported: no dual path remains.
- Coverage lost: the V1 start probe also proved that the base's own libkmod finds `vc4` and `v3d` by device alias; nothing replaces it yet (V1B-2).
- The GitHub origin lists a release only by its node manifest: its `manifest.json` read and the `PHOTO_WALL_RELEASE_PRERELEASES` gate are deleted (V1P-5, V1P-6, FIX-3), as are the V1 units `player.service`, `weston.service` and `weston.ini`, `app_launcher.py` and the process sampler (FIX-2).
- Not done: the Dockerfile and Compose still default to `central.app:create_app`; `README.md` still describes the V1 promote flow and its removed runbook anchors (outside the docs pass's paths).

**Not run**

- No Raspberry Pi boot, HDMI output or timing check ran; CI proves packages, images and systemd behaviour on a generic arm64 runner only ([CI module](../module-appliance-ci.md)).

## Implementer brief

This record is the hand-off for the agent that builds it.

- **One run.** The owner wants the whole thing, P1 through P4, run by one background agent or workflow.
- **DRY and SOLID over YAGNI** ([design principles](../../CONTRIBUTING.md#design-principles)).
- **No backwards compatibility.** The V1 builders are deleted, not ported; no dual paths or default-to-old flags.
- **CI budget.** Each CI leg about 4 minutes; no test of 10 minutes or more.
- **Tests prove behaviour through the real artifact** (the built `.deb`s, images and release), not its structure.
- **Delivery.** PRs are reviewed and merged by the owner, never merged by the agent. Releases come only from the automated flow after merge; no hand tags or hand-edited versions.
- **OS dependencies go into rpi-image-gen layers** (as PR 54 did for SSH), never into Photo Wall packages (R4).
- **No hand-rolled Python packaging code** (R3): no Python that stages package files, writes control files or calls `dpkg-deb`.

### Premises to prove first in P1

None of these was run while designing (no build, `mmdebstrap`, `tar2sqfs` or `dpkg-gencontrol -v`). P1 proves each before P2 builds on it; a premise that fails goes back to the owner with its alternative.

1. `dpkg-gencontrol -v` in `override_dh_gencontrol` sets a distinct, content-derived version per binary package of one source package.
2. A pinned Debian build container running `dpkg-buildpackage` works on GitHub's Ubuntu 24.04 runners in place of sbuild's unshare mode (which needs an AppArmor sysctl there).
3. `mmdebstrap | tar2sqfs` reproduces the same image digest when the same inputs are rebuilt.
