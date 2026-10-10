# Debian packaging module

This module builds the Node's software as Debian packages and puts them into the Node's images. [Decision 0019](decisions/0019-debian-packaging-with-debhelper.md) owns the design and its reasons; this page owns how the built system works and how to run, prove and change it. Central stays an OCI image built by Docker and uv; it is not part of this module.

## What is built

One source package, `photo-wall` (`debian/`, `3.0 (native)`, `debhelper-compat (= 13)`, plain `dh`), is built by `dpkg-buildpackage` in a pinned Debian trixie arm64 container. It produces these binary packages, each owning one directory, `/usr/lib/photo-wall/<name>/`, so no file has two owners and no list names files:

| Package | Holds | Installed in |
|---|---|---|
| `photo-wall-common` | `contracts`, `nodeapi`, less the modules only Central imports (`debian/rules` `CENTRAL_ONLY`: `nodeapi/hub.py`, `contracts/node_release.py`, `contracts/os_attempt_report.py`) | base, app root, manager root |
| `photo-wall-uplink` | `uplink` (stdlib-only) | base, app root, manager root |
| `photo-wall-node-kernel`, `-central-session`, `-host`, `-display`, `-health`, `-apps`, `-boot`, `-manager` | one Node context each; `-display` is the one architecture-dependent context (the Meson-built shell, the diagnostic client and `abi.json`) | base; the manager root takes the manager's imports |
| `photo-wall-frame-client` | the frame client's `.so` | app root |
| `photo-wall-player` | `player/` and its launcher | app root |
| `photo-wall-app-manager` | AppManager's launcher | manager root |
| `photo-wall-node` | the composition: units, slices, the target, system users, runtime directories, the bus configuration, the seven base launchers, `abi.json`; Depends on every context at its exact version and on `nats-server (= the pin)` | base |
| `photo-wall-netboot-init` | stage 1 and an initramfs-tools hook that copies it, `uplink` and the CA bundle into the netboot initramfs | the scratch root that builds the initrd |

Also in the local repo, built or fetched but not part of the source package: `python3-nats` (`debian-packaging/python-nats/`, the pinned nats-py sdist built with pybuild) and upstream's own `nats-server` arm64 `.deb`, checked against a pinned sha256.

Rules the build enforces:

- **Versions are content-derived.** Each binary's version is `0+<12 hex>` over its installed tree and its Depends (`debian/content-versions`, run from `override_dh_gencontrol`), so an unchanged part keeps its version, its image keeps its bytes and a Central-only release gives no Node a new digest.
- **Depends cannot drift from imports.** `scripts/import_check.py` runs inside the build (`execute_after_dh_install`; it is not a test target, so `nocheck` cannot skip it). It refuses a build when a package's declared Depends differ from what its modules directly import, when a third-party import root's owning Debian package is missing from Depends, or when a Depends entry is unused. It also refuses a `photo-wall-common` module that no Node program (a launcher's entry or stage 1) reaches, so a module only Central imports cannot ship to the Node and a Central-only edit keeps every Node package's version. The edges the `layers` contract in `pyproject.toml` lists as `ignore_imports` (the retiring upward edges) are exempt.
- **Each launcher sees only its graph.** `appliance/launchers/<name>/__main__.py` puts on `sys.path` its own context and the lower layers import-linter allows; programs run as `python3 -I -B <dir>` ([0014](decisions/0014-reaching-central-from-every-boot-stage.md)), so no `.pyc` lands in the RAM root.
- **ABI ids have one writer each.** `photo-wall-node` writes `base_abi` (a hash of its expanded Depends and `debian-packaging/image-format.env`); `photo-wall-node-display` writes `graphics_abi` (`debian/graphics-abi`, over the Meson outputs of the display and frame client and the third-party runtime they resolve to, never the context's Python) and the declared `plugin_abi`.

## Where each fact lives

| Fact | Home |
|---|---|
| What each package depends on | `debian/control` |
| The Debian snapshot pin (archive and security suite, one instant) | `debian-packaging/snapshot.list`; its first line's instant is `SOURCE_DATE_EPOCH` (`debian-packaging/snapshot-epoch.sh`) |
| nats-server version and `.deb` sha256 | `debian-packaging/nats-server.env` (read by `debian/rules`, `build-repo.sh` and `scripts/nats_server.py`; a test holds `compose.yaml`'s hub image equal) |
| nats-py version | `debian-packaging/python-nats/upstream.env` (a test holds it equal to `uv.lock`) |
| OS features (SSH, time, resolver, device packages) | `appliance/rpi_image_gen/` layers, never a Photo Wall package's Depends |
| The image format (squashfs options, base-ABI input) | `debian-packaging/image-format.env` |

## Build steps

| Step | Command | Gives |
|---|---|---|
| 1. Packages and local repo | `debian-packaging/build-repo.sh --output <new dir>` | the `.deb`s and an apt `Packages` index, a flat repo read as `deb [trusted=yes] file:<dir> ./` |
| 2. Base | [base-image.yml](../.github/workflows/base-image.yml): rpi-image-gen installs `photo-wall-node` by name from the local repo | the base squashfs |
| 3. Release roots | `debian-packaging/build-root.sh --repo <dir> --package photo-wall-player` (app root) or `photo-wall-app-manager` (manager root): `mmdebstrap` from the pin and the repo, `debian-packaging/seal-hook.sh` and `scripts/seal_root.py`, `mksquashfs`; built twice on a cache miss, two image digests must agree | `<role>.squashfs` and its reference |
| 4. Release writer | `python -m scripts.node_release_writer write ...` | `components.json`, the repo's `.deb`s byte for byte and the stamp; it refuses an image over its memory line (`appliance/kernel/capacity.py`) |
| 5. Stage 1 | `photo-wall-netboot-init` is installed into base-image's scratch root, where `update-initramfs` runs its hook; `scripts/build_netboot_bundle.sh` assembles the boot tree | the netboot bundle |

[node-components.yml](../.github/workflows/node-components.yml) runs steps 1, 3 and 4 once per pipeline run (its `debs` job builds the repo twice and requires equal indexes); the base, the wall e2e and the PID1 scenarios consume its output ([CI module](module-appliance-ci.md)). The Docker toolchain is arm64 whatever the host is. Build scripts use Docker's `default` builder unless `PHOTO_WALL_NODE_BUILDER` names another.

## Proving the packages

Tests prove behaviour through the built artifact, in `tests/debs/`. Each needs `PHOTO_WALL_LOCAL_REPO` naming a `build-repo.sh` output directory and Docker, and skips without it:

| Test | Proves |
|---|---|
| `test_local_repo.py` | every package of `debian/control` at its own version, the pinned third-party packages, and an apt that installs the whole set through the repo |
| `test_display_packages.py` | the display and frame client's installed files, aarch64 ELF, and `abi.json` recomputed independently |
| `test_context_packages.py` | each context imports in an isolated interpreter from its runtime directories alone |
| `test_node_package.py` | `photo-wall-node`: every launcher imports, every unit's `ExecStart` exists, the target is enabled, the users and directories are installed, `base_abi` recomputes |
| `test_netboot_init.py` | the initramfs hook's tree imports stage 1, and `uplink` runs on the device's python3 and OpenSSL ([0014](decisions/0014-reaching-central-from-every-boot-stage.md) section 11) |
| `test_pins.py` | the pin files have one reader each and agree with `uv.lock` and `compose.yaml` |

The real-systemd proof is the [PID1 scenarios](evidence/player-node-handoff-support/node-lifecycle-qualification.md). Nothing here boots a Pi.

## Changing it

- **Bump the Debian snapshot.** Edit both timestamps of `debian-packaging/snapshot.list` (`YYYYMMDDTHHMMSSZ`, one instant). Every cache keys on it, so the build container, the base, the roots and the initrd root rebuild; re-stage the whole TFTP bundle ([runbook](runbook.md#player-provisioning-stage-the-netboot-bundle-and-read-its-console-0014)).
- **Bump nats-server.** Edit both lines of `debian-packaging/nats-server.env` from the release's `SHA256SUMS`; `compose.yaml`'s hub image must move with it (`test_pins.py` fails otherwise).
- **Add an OS dependency.** Put it in an `appliance/rpi_image_gen/` layer. A Photo Wall package never names an OS feature.
- **Add a Python module or context.** Place it in the package whose layer it belongs to; if the import check refuses a Depends line, fix `debian/control`, not the check.

## Deviations and costs

Findings recorded while building are in [the repository errata](../.claude/errata.md) under `E-0019-*`; [0019](decisions/0019-debian-packaging-with-debhelper.md#built-this-pr-deviations-from-the-design) lists the ones that change the design. Known costs: versions do not sort; a launcher's `PATH` exposes every module of the package directories it reaches, not only its closure; a root's builds are reproducible only with the hostname and `SOURCE_DATE_EPOCH` the recipe fixes.
