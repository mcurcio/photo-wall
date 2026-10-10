# Player package

The Player ships as the Debian binary package `photo-wall-player`, one of the packages the `photo-wall` source package builds ([Debian packaging module](module-debian-packaging.md), [decision 0019](decisions/0019-debian-packaging-with-debhelper.md)). It replaces the earlier wheelhouse (`scripts/build_player.py`) and the Player `.deb` that a netboot bootstrapper installed with `dpkg --install` (decisions 0009 and 0010); both builders and the bootstrapper are deleted, and nothing installs a package on a Node at boot. The [platform decision](decisions/0005-native-platform-and-registration-fallback.md) owns native OS libraries; the [Player service](module-player-service.md) owns process startup.

## What the package holds

- **Contents.** `player/*.py` under `/usr/lib/photo-wall/player/player/` and the launcher `appliance/launchers/player/__main__.py` under `/usr/lib/photo-wall/player/`. The program runs as `python3 -I -B /usr/lib/photo-wall/player --config /etc/photo-wall/public.json`. The launcher puts three package directories on `sys.path` (`common`, `player`, `uplink`) and runs `player.service`; the import check refuses any other.
- **Depends.** Debian packages for what the Player imports (`python3-httpx`, `-websockets`, `-pydantic`, `-cryptography`, `-zeroconf`, `-gi`, `-opengl`) and its render stack (GTK, GStreamer, Mesa), plus `photo-wall-common`, `photo-wall-uplink` and `photo-wall-frame-client` at their exact versions. Python libraries come from Debian, not from `uv.lock`; the import check holds each import to a declared Depends.
- **Nothing else.** It carries no `central_origin` and no deployment configuration: the Node binds `/etc/photo-wall/public.json` over the root at start. It contains no Central, media, Immich or database code ([import layering](../AGENTS.md#code-map): `player` never imports `central`, `media`, `appliance` or a database; Players are Immich-unaware).

## How it reaches a Node

The package is not installed on the Node. `debian-packaging/build-root.sh --package photo-wall-player` runs `mmdebstrap` from the Debian snapshot pin and the run's local repo, seals the root and writes it as a squashfs image: the **app root**. The release writer ships that image, with the package's `.deb` as `app.deb`, in the node component set; Central stages the image to the Node, and PID1 mounts it read-only ([Player architecture](player-architecture.md#release-roots-as-images-e2c)). The app root's size is held to its memory line (`appliance/kernel/capacity.py`, `app-image`) by the release writer.

The wall e2e runs the Player from the same app root: `scripts/demo_wall.py --app-image` imports it into the Player containers ([wall demo](module-wall-demo.md)).

## Release sourcing

Central's worker polls the project's GitHub Releases and records them for the node release catalog ([decision 0010](decisions/0010-github-release-sourcing.md); [runbook](runbook.md#release-sourcing-from-github-0010)). The operator selects a release in the console; the V1 `.deb` promotion routes and `GET /v1/app/*` are removed.

## Qualification

The package's imports, files, Depends and installability are proved on the built `.deb`s by `tests/debs/test_context_packages.py` and `tests/debs/test_local_repo.py`; the sealed app root by the release writer's checks and by the [PID1 scenarios](evidence/player-node-handoff-support/node-lifecycle-qualification.md); the Player on a headless Weston by the display harness. None of these establishes Pi boot, HDMI output or physical timing.
