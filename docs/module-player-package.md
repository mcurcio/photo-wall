# Player package build

This module turns one explicit committed Git revision into the stateless Player application and its offline Python dependency wheelhouse. The [platform decision](decisions/0005-native-platform-and-registration-fallback.md) owns native OS libraries; the [Player service](module-player-service.md) owns process startup. This builder does not contain central services, media preparation, signing keys, OS packages, PostgreSQL/Psycopg, Procrastinate, a local database, or update-state machinery.

## Contract and design

`scripts/build_player.py --revision <40-character commit> --output <new directory outside Git>` reads only `git archive` at that exact commit for `player/`, `contracts/`, `pyproject.toml`, and `uv.lock`. The working checkout may contain concurrent work; none enters the archive. Archive members must be regular Python files in those two packages or the two exact metadata files. Symlinks, unexpected files and oversized archives fail closed. Git stdout is streamed into a bounded temporary file, with an 8 MiB archive limit and a 30-second subprocess deadline; overflow and timeout kill and reap the child before returning.

The separate `photo-wall-player` distribution contains only `player`, neutral Pydantic `contracts`, and its own wheel metadata. Its version is the archived project version plus `+g<full commit>`. A deterministic standard-library wheel writer emits PEP 427 metadata and a complete SHA-256 `RECORD`. It avoids introducing a separately resolved backend dependency tree. The build host uses Python 3.12 and the `packaging` version pinned in `appliance/build-tools.txt` solely for standard marker, version and wheel-tag parsing; that build tool is excluded from the runtime wheelhouse.

The runtime roots are exactly `pydantic`, `httpx`, `websockets`, and `cryptography`, using their exact archived project pins. Their dependency graph, markers, versions, wheel URLs, sizes and SHA-256 hashes come only from the archived lock. This allowlist is also the enforcement point that keeps central persistence and queue clients out of the Player. Selection targets CPython 3.12.3 on Linux AArch64 with glibc 2.39 (Ubuntu Noble); compatible older manylinux and CPython stable-ABI wheels and universal Python wheels are permitted. Unspecified platform-release markers, ambiguous package variants, extras, source distributions, missing compatible wheels and missing hashes fail closed. No resolver or package-index search is invoked. All selected wheel URLs must be HTTPS on `files.pythonhosted.org`; download size and hash are checked before use.

The output is published only after all artifacts validate:

```text
<output>/
  source.tar
  inventory.json
  requirements.txt
  wheels/
    photo_wall_player-<version>-py3-none-any.whl
    <one compatible locked wheel per runtime dependency>
```

`requirements.txt` pins and hashes every runtime package including the locally built Player wheel. The appliance builder installs it with `python -m pip install --no-index --find-links <output>/wheels --require-hashes -r <output>/requirements.txt`. `inventory.json` records the full commit and tree, source archive hash, each packaged source hash, every wheel hash/size and upstream URL, target environment, build-tool version and builder script hash. The caller supplies an unused output path outside every Git tree. A private staging directory in its parent is removed on handled failure; a per-output reservation prevents concurrent builders from publishing to the same path. Publication uses one directory rename. An uncatchable process termination can leave the reservation/staging directory; an operator must establish that the builder stopped before removing those exact artifacts. No unrelated paths are copied.

## Prepared input for offline image assembly

CI builds this wheelhouse before entering its network-disabled final assembly
container. `build_player.restore` accepts the prepared directory, exact Git
revision, and a new external destination. It reuses the canonical builder with
a local-only dependency supplier, verifies every dependency against the
committed lock, and compares the reconstructed application wheel, source
archive, requirements, and inventory to the supplied package. Missing, changed,
additional, or symlinked inputs are rejected. No package server is contacted.

The [reusable OS base decision](decisions/0007-reusable-os-base.md) describes the
separate native dependency artifact. The existing offline pip installation
contract is unchanged.

## The `.deb` package (0009)

[Decision 0009](decisions/0009-minimal-base-and-app-package.md) wraps this exact wheelhouse into a `.deb` central publishes and a netboot bootstrapper downloads, instead of baking the Player venv into a signed base image. `scripts/build_player_deb.py` builds it, reusing `build_player.py`'s `build`/`ROOTS`/`locked_runtime`/`make_player_wheel` verbatim rather than reimplementing them:

- **Payload (gate #6):** a prebuilt venv at the fixed path `/opt/photo-wall/venv` plus the Player and weston systemd units — "install is an unpack," needing no pip or build tools in the base at boot. The venv is assembled the same way `appliance.build.configure_root` does today (`python3.12 -m venv --system-site-packages`, then `pip install --no-index --require-hashes` from the wheelhouse), inside the same arm64/24.04 chroot the appliance build uses.
- **Version:** exactly the player wheel version (`base_version+g<commit>`) — one identifier for source, wheel, and `.deb`.
- **Dependencies:** `Depends:` names only the native system libraries the venv needs at run time (GTK/GStreamer/weston, from `appliance.os_packages.RUNTIME_PACKAGES`); every PyPI package stays vendored in the venv, preserving the hashed-wheelhouse integrity model above.
- **No deployment configuration.** The package carries no `central_origin` and no other deployment config — per 0009, the bootstrapper supplies that at boot, never a published asset.

The central side is register-then-promote by reference, mirroring the existing release-registration pattern: an operator stages the `.deb` bytes under central's `PHOTO_WALL_APP_ROOT`, then `POST /v1/operator/app` records `{version, sha256, size}` and `PUT /v1/operator/app/current` promotes it — see [the app-package contract](module-appliance-release.md#the-app-package-contract-0009-unsigned-in-parallel) and the [runbook](runbook.md#player-provisioning-netboot-and-promote-the-app-0009-in-progress). The sha256 involved is a corruption check only, per the owner's home-LAN ruling — there is no signing anywhere in this path.

**Status:** the builder is implemented, split so its staging/metadata logic is tested on any host and only its venv/`dpkg-deb` assembly (`tests/test_build_player_deb.py`) is gated to Linux with the image tools installed, the same gating `tests/test_appliance_build.py` already uses. This establishes that the `.deb` builds and installs correctly as a package; it does not establish that a netboot base can fetch and run it, since the boot chain that would do that is not yet wired — see [the appliance builder module](module-appliance-builder.md#the-0009-minimal-base-and-bootstrapper-in-progress).

## Operator release sourcing (0010)

[Decision 0010](decisions/0010-github-release-sourcing.md) automates the register-then-promote step above. Instead of an operator hand-staging `.deb` bytes, central's worker polls the project's GitHub Releases, records each semver release with a mirror state, and — only on an operator **promote** — lazily downloads that release's `.deb` into the same `PHOTO_WALL_APP_ROOT` store this module's package lands in. The Player-facing surface is unchanged: Players still `GET /v1/app/manifest` and `/v1/app/package/{sha}.deb` from central, which serves local bytes and never depends on GitHub being reachable.

The operator flow is three admin routes on central — `GET /v1/operator/app/releases` (list tracked releases), `POST /v1/operator/app/releases/{tag}/promote` (200 already-mirrored, 202 pending mirror, 404 unknown tag, 409 undeployable), and `POST /v1/operator/app/releases/refresh` (poll now) — all returning 503 when release sourcing is unconfigured. The feature is opt-in: it activates only when `PHOTO_WALL_APP_ROOT` is set on the worker, and central plus worker must share that store. The [runbook](runbook.md#player-provisioning-promote-a-release-from-github-0010) has the copy-pasteable env config and `curl` commands; the manual `POST /v1/operator/app` + `PUT /v1/operator/app/current` path above coexists as the offline/air-gapped escape hatch.

## Package qualification

Unit checks cover package boundaries and metadata, deterministic output and `RECORD`, dependency markers and target tags, invalid or ambiguous locks, hash failures, unsafe archives and output paths. A real build must also be installed with pip into a clean CPython 3.12 Linux ARM64 environment, using only the emitted wheelhouse and hash-locked requirements. Successful installation is package evidence; it does not establish Pi boot or physical rendering behavior.

On 2026-09-05, 64 focused unit tests passed with `.venv/bin/python -m pytest -q --noconftest tests/test_player_package.py`; scoped Ruff and repository documentation links passed. The standalone file owns all its fixtures; `--noconftest` avoided an unrelated shared PostgreSQL import stalled under host memory pressure. The earlier 62-test version also passed with the normal shared configuration. The real build used committed core revision `dda8e98c5c54dc8ca9c007599f8a919eadbd5248` and produced 16 wheels (the Player plus 15 locked dependencies), with 7,651,401 bytes across the complete output. This command creates an equivalent package from an available committed revision:

```sh
.venv/bin/python scripts/build_player.py \
  --revision dda8e98c5c54dc8ca9c007599f8a919eadbd5248 \
  --output /absolute/external/new-player-artifact
```

The local benchmark artifact is `/Volumes/Dock/PhotoWallArtifacts/2026-09-05-01a0731f/player-dda8e98-bounded`. A final appliance release must regenerate the package from its own exact later commit, including matching version and inventory metadata. The benchmark checksums are:

| Artifact | SHA-256 |
|---|---|
| Player wheel | `11987db71bfb853c83b2570097692c1d1c0c0a80b1eaf61ae6c5dc5f06c85575` |
| Source archive | `d1a6a68fe4555f72b95b9f43f5abf2e771a6850220ab2d7a721af7d91b02b994` |
| Requirements | `af16498fc10114e9121a3bd09e688c2b358f373cda1a5d26c4e19997689ceb1b` |
| Final inventory | `cd6c36ce25d5547870f051184c2fcd1dfa7ac17c8c527e34b621d7caf39c83e0` |

Actual installation ran in a fresh virtual environment on CPython 3.12.3, Linux `aarch64`, glibc 2.39, using a Docker build with network disabled. `pip --isolated install --no-index --find-links /wheelhouse/wheels --require-hashes -r /wheelhouse/requirements.txt` and `pip check` passed. Eighteen module imports resolved inside the new environment, covering all Player/contract modules and the four direct dependencies. Ed25519 key generation/sign/verify and Pydantic native validation passed. Imports for `central`, `media`, `fastapi`, `psycopg`, `PIL`, `uvicorn`, `packaging`, and `hatchling` were absent.

The qualification image is `photo-wall-player-package-check:dda8e98`, built from the existing native fixture image `sha256:8bf573f5fd7e4d09e4f8c0e0ad51e70d2ebb6058d3f15d7f7651284d20249e6d`. Its new environment omitted system site packages and invoked Python with `-I` outside the fixture source directory. This establishes the wheel and installed environment boundary; the underlying fixture image still contains its older separate native test environment. It is not the production appliance image. The benchmark's wheels, archive and requirements were compared byte for byte with the installed artifacts after the output-path guard changed; only the inventory's builder hash changed. The subsequent bounded Git-reader change passed oversized-output and deadline regressions plus a real local Git-read smoke check. Native system GI/OpenGL integration, appliance boot and physical Outputs have separate qualification owners.

After the bounded Git-reader fix, the orchestrator rebuilt the complete bundle offline from already-verified dependency files. Its source archive, requirements and all 16 wheels are byte-identical to the installed qualification payloads. The inventory above records final builder SHA-256 `ff2657623e0ec4e07a4c1ac2a818449a36bbba587978fc26030b6adee754359e`.
