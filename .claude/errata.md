# Errata

Append-only. Read with `grep -a`.

## m1-central-lifecycle (central unbind + pending queue)

- **"bumps generation (and whatever epoch `configuration_in` fences on)" conflates two
  distinct mechanisms.** `configuration_in` (central/registry.py:300) fences execution
  bindings on the *player's* `authority_epoch`, which only changes on retire/re-enroll —
  not on the Frame's `generation`, which is a separate optimistic-concurrency counter
  bumped by `bind`/`retire`/now `unbind`. Deleting the `bindings` row (frame_id is the
  bindings PK) is what withdraws the player's execution binding; the generation bump on
  the Frame is for `bind`/`calibrate` optimistic-concurrency parity with `bind`/`retire`,
  not part of `configuration_in`'s fencing. `unbind` does not touch `authority_epoch`, and
  should not — the player's token/session must remain valid after an unbind (only `retire`
  invalidates it). Implemented per the literal instruction (mirror bind/retire's generation
  bump) but flagging that the epoch clause, read literally, does not apply here.

- **Extending `InstallationInventory`/`PlayerInventory` with a required field breaks
  existing consumers.** Adding `PlayerInventory.is_bound: bool` (no default) broke
  `tests/test_installation_models.py`, `tests/test_appliance_media.py`, and
  `tests/test_vm_media_probe.py`, which construct/decode inventory-shaped payloads by
  hand (fixture `Operator.request` in tests/test_appliance_media.py:19, and
  `InstallationInventory.model_validate_json` in scripts/vm_inventory_probe.py) without
  the new field. Resolved by defaulting `is_bound = False` rather than requiring it. This
  is the pragmatic non-breaking choice per the bead's "do not break existing inventory
  consumers/tests" constraint, but it does mean a hand-built or older-schema inventory
  payload silently reads as "not bound" rather than raising — worth noting since it's a
  divergence from this codebase's general preference for `Field(strict=True)`/required
  fields over silent defaults on wire models.

## m3-hardware-serial (flashed device_id) — DESIGN FACT CORRECTION

- **The 0008 "Verified facts" table is wrong that serial enroll is standalone/already-built.**
  `Registry.enroll` (central/registry.py:134) unconditionally calls `bind_session_in`
  (central/releases.py:264) -> `_device()` (central/releases.py:168), which raises
  `device_not_found` (404) when there is no `appliance_devices` row. That row is created
  ONLY by the netboot boot-ticket path (`select_boot`). So the as-built enroll is COUPLED
  to netboot: a flashed (D0) player presenting a placeholder ticket_id/release_id is
  rejected before any player record is created. Reproduced against real Postgres by the
  m3-hardware-serial verifier.
- **Consequence:** the 0008 baseline "flash and go" tracer (line 146: "enrolls by serial ...
  with no pre-registration") is NOT deliverable with player-only changes. It requires a
  NET-NEW central bead -- `m3-central-d0-enroll` -- that lets a ticketless serial enrollment
  succeed and auto-creates an UNBOUND (pending) equipment record. Additive within the
  accepted three-ladder architecture (does not change the frame); the design INTENT
  ("no pre-registration") already dictates the behavior. Re-cut: added m3-central-d0-enroll
  before m3-flash-image.
- The player bead (serial->device_id derivation) is correct in isolation (risks A/B passed);
  its docstring claim that "central records but does not verify" is FALSE and was corrected.

## p2-release-workflow (signed GitHub Release, netboot + flash artifacts)

- **The spec's literal steps 1-3 ("reuse the existing base/builder/os-base preparation
  approach from appliance.yml ... duplicate minimally") contradict an existing, tested
  architectural invariant.** `tests/test_service_workflows.py::test_only_appliance_workflow_assembles_pi_os_and_smoke_skips_media`
  asserts that no workflow file OTHER than `appliance.yml` contains the literal strings
  `scripts.os_base`, `scripts.build_ci_image`, or `scripts.ci_images builder` — i.e. "only
  appliance.yml constructs the Pi OS and signed appliance" (`docs/module-appliance-ci.md`)
  is enforced by a grep-style test, not just documented. A first draft of `release.yml` that
  called those scripts directly (as the spec's context bullets literally describe) fails that
  test. Resolved by FACTORING rather than duplicating: added a `workflow_call` trigger and a
  new `release-artifacts` job to `appliance.yml` itself (the one file the invariant permits)
  that reassembles/signs with the persistent key and builds the flash image, gated
  `if: github.event_name == 'workflow_call'` so none of appliance.yml's existing
  push/pull_request/workflow_dispatch jobs change behavior. `release.yml` calls it via
  `uses: ./.github/workflows/appliance.yml`, downloads its uploaded build-output artifact,
  and only does tag validation, the fail-closed signing-secret check, packaging, and
  `gh release create` — none of which touch the forbidden substrings. This is the
  convention-consistent reading of "you may factor shared setup," made mandatory rather
  than optional by the existing test.
- **`scripts/build_ci_flash_image.py` has no prepared-OS-base input mode** (unlike
  `scripts/build_ci_image.py`'s `--os-base`/`--player-package`/`--os-base-builder-image`
  triple) — every flash-image build cold-fetches and re-installs the Ubuntu base root from
  scratch, duplicating work `build_ci_image.py` just did with a published, cached OS base.
  Out of scope here (its own docstring forbids modifying `build_ci_image.py` or anything it
  calls, and adding a symmetrical prepared-input mode is a real design change to that
  script, not a workflow change) — flagged as a follow-up bead: teach
  `build_ci_flash_image.py` to accept the same prepared `--os-base`/`--player-package`
  inputs so a release run doesn't pay for two independent OS-base assemblies.

## p3-central-app-service (0009 slice 1 — central app package service)

- **The 0009 endpoint table's literal wording for `POST /v1/operator/app`
  ("upload a `.deb`, stored by sha256") contradicts how the existing, reviewed
  release-registration pattern actually works.** `POST /v1/operator/releases`
  (`central/app.py` `register_release`) never uploads rootfs bytes through the
  request body — it registers a signed JSON manifest naming
  `rootfs_sha256`/`rootfs_size`, and the actual squashfs bytes are staged
  out-of-band under `PHOTO_WALL_RELEASE_ROOT` before or after that call;
  existence/size is checked lazily, only at GET-artifact time
  (`central/app.py` `release_image`, 503 `release_artifact_unavailable`/
  `_invalid` if missing/wrong-sized). The task brief explicitly asked to
  "decide upload-vs-stage-by-reference consistent with how releases are
  registered today," so `POST /v1/operator/app` was implemented as
  **stage-by-reference**: it takes `{version, sha256, size}` JSON metadata
  only (no request-body file bytes), mirroring `register_release`/`set_default`
  exactly, and the operator stages the actual `.deb` bytes at
  `PHOTO_WALL_APP_ROOT/app-{sha256}.deb` out of band. This is a deliberate
  divergence from the endpoint table's literal word "upload," made for
  consistency with the mirrored pattern per the task's own instruction — not
  an oversight. If a literal multipart/binary upload endpoint is wanted
  instead, that is a new pattern (this codebase has no existing convention
  for streaming request-body file uploads) and should be a separate,
  explicitly-scoped decision.

## p3-base-bootstrapper (0009 slice 2 -- `appliance/provision.py`)

- **The design's literal "reuse `appliance/bootstrap.py`'s `Fetcher`" does not
  fit an mDNS-discovered origin as written.** `Fetcher.__init__` takes a
  `BootConfig` (`appliance/bootstrap.py:92`), whose `__post_init__`
  hard-requires `release_origin` to be `https://` and pins a 64-hex-char
  `boot_abi`. The 0009 bootstrapper's origin comes from
  `MdnsCentralDiscovery`, which legitimately returns `http://` on the T0
  home-LAN baseline (`player/mdns_discovery.py:35`) and has no OS-ABI to pin
  (it fetches an app `.deb`, not a signed rootfs). Constructing `BootConfig`
  with a synthetic https-only wrapper to satisfy the constructor would be
  worse than the alternative taken: `appliance/provision.py` reimplements the
  same *discipline* (one deadline for the whole acquisition, no
  proxies/redirects, exact `Content-Length` bound enforced while streaming,
  `Content-Encoding` pinned to identity) as a small standalone `AppFetcher`
  class scoped to `http://`-or-`https://` origins with no ABI pinning. This is
  a reuse-the-pattern, not reuse-the-class, reading of the design doc's "the
  bounded ... `Fetcher` ... reused for the manifest + `.deb`" language
  (0009-minimal-base-and-app-package.md, "The seam") -- flagged because a
  literal read could be taken as "import `Fetcher` directly," which does not
  typecheck against a plain-HTTP discovered origin.
- **Corollary the design doc does not spell out: a plain-HTTP origin handoff
  needs `allow_http: true` alongside it.** `PlayerConfig`'s explicit-origin
  validator (`player/service.py` `_validate_origin`, exercised by
  `test_config_rejects_untrusted_or_non_origin_urls`,
  `tests/test_player_service.py:118`) rejects an *explicit* `central_origin`
  with scheme `http` unless `allow_http=True` is also set -- `resolve_origin`
  only forces `allow_http=True` for a *discovered* origin
  (`player/service.py:423`), never for the config's own field. Since the
  design's origin handoff ("The origin must be handed forward, not
  re-discovered") turns a discovered origin into an explicit one, a bare
  `{"central_origin": "http://..."}` handoff on the common home-LAN
  (plain-HTTP) case would make the Player's own `PlayerConfig` fail to
  validate at boot -- the opposite of the intended fix.
  `appliance.provision.write_public_config` sets `allow_http: true` whenever
  the resolved origin's scheme is `http`, so the handoff cannot self-defeat.
  This is a necessary consequence of 0009's home-LAN ruling, not a new
  decision, but the design doc's origin-handoff section does not mention it.
## p4-rpi-image-gen-spike (0009 Phase 4 -- rpi-image-gen base OS spike)

- **The reused `photo-wall-provision.service`'s hardcoded `/usr/bin/python3.12`
  ExecStart does not exist on a Debian trixie base, contradicting a literal
  read of "reuse the existing unit file" against "select a Debian arm64
  base."** The unit (`appliance/systemd/photo-wall-provision.service`) was
  written against the **existing, Ubuntu-24.04-based**
  `appliance/os_definition.json` path, where Ubuntu Noble co-installs
  `python3.12` alongside its default `python3`. Debian trixie carries no
  `python3.12` package at all -- confirmed against packages.debian.org
  (`trixie/python3.12` returns "Package not available in this suite");
  trixie's own default `python3` is 3.13. Reusing the unit **verbatim** (as
  instructed) against a plain Debian trixie rootfs (as instructed) would
  therefore make the unit fail to start with no compensation. Resolved by
  adding a `ln -sf python3 "$1/usr/bin/python3.12"` compatibility symlink in
  `appliance/rpi_image_gen/layer/photo-wall-bootstrapper.yaml`'s
  customize-hooks, rather than editing the unit (out of scope) or silently
  shipping a base that cannot boot the unit. This is a deliberate,
  documented compensation for a genuine cross-distro mismatch between the
  two OS bases this repo now targets (Ubuntu for the existing
  `scripts.os_base`/`build_ci_base_image` path, Debian trixie for this
  spike) -- not a fix to the underlying mismatch, and worth resolving for
  real (either always installing a fixed Python minor version explicitly on
  both bases, or making the unit's ExecStart reference `python3` generically)
  before this spike's approach is taken past the spike stage.
- **rpi-image-gen ships no literal `--verbose`/`--debug` CLI flag** (checked
  the root `rpi-image-gen` wrapper and `bin/ig`'s argument parsing -- there
  is none). The task's "make the run output verbose" is satisfied by simply
  not redirecting/suppressing the tool's own output (it is already quite
  verbose by default -- `msg()` stage headers, a full resolved-ENV dump, and
  `mmdebstrap`/`apt`/`genimage` output all stream to stdout with no existing
  redirection) plus `set -x` in the workflow's own steps. Flagging this in
  case a literal `--verbose`/`--debug` flag was expected to exist and be
  passed.
- **Scope note, not a contradiction:** the design's "The seam" section lists
  `contracts.equipment.equipment_device_id` as one of exactly three things the
  bootstrapper imports, implying it also produces the boot-context file
  (`/run/photo-wall/boot.json`, device_id/ticket_id/persistence) that
  replaces `appliance/bootstrap.py:428`'s `persistence="volatile"`+ticket
  production (migration edit B). The task brief scoping this slice lists only
  discover/fetch/verify/install/origin-handoff/start -- no boot-context or
  enroll concern -- and says explicitly "it does NOT enroll." `provision.py`
  therefore does not import `contracts.equipment` or touch
  `/run/photo-wall/boot.json`; that production (edit B) is left for whichever
  slice replaces `appliance/bootstrap.py`'s netboot path.

- **p4-boot-chain s2b OPEN-item resolution + assumptions to confirm in CI.**
  The Pi 5 (BCM2712) network-boot filename set was confirmed against RPi docs
  (raspberrypi.com network-booting + rpi-eeprom firmware-2712 notes): the Pi 5
  boots from its SPI EEPROM and needs NO `start*.elf`/`fixup*.dat`; `config.txt`
  is MANDATORY (its presence marks a TFTP prefix bootable); kernel image is
  `kernel_2712.img` (16K-page rpi-2712 kernel; firmware default on Pi 5); DTB is
  `bcm2712-rpi-5-b.dtb`; initramfs is pulled via `initramfs initrd.img
  followkernel` in config.txt. TWO items still need a GREEN arm64 CI run to
  confirm (no hardware nails them): (1) `linux-image-rpi-2712` + `raspi-firmware`
  are pulled from `archive.raspberrypi.com/debian trixie main` -- the `trixie`
  suite is ASSUMED published there; the workflow's `apt-get update` fails loudly
  with captured diagnostics if not (fallback would be the `bookworm` suite).
  (2) The kernel image / DTB / overlays install paths under the scratch root are
  discovered at build time via `find` (evidence logged to the diag artifact),
  not hardcoded, because the exact raspi-firmware `/boot` vs `/boot/firmware`
  layout is not verifiable off-hardware. **Also:** the Pi firmware passes
  `cmdline.txt` verbatim to the kernel and does NOT support `#` comments, yet the
  bundle layout calls for a "cmdline.txt template + a comment" -- resolved by
  shipping the explanatory comment as leading `#` lines the operator MUST delete
  (the `@@...@@` placeholders already make the file un-bootable unedited). Flag
  for review: a stricter design would move the comment to a sidecar README.

## 2026-09-12 — s2b: verify_netboot_initrd required a `_socket*.so` that does not exist on Debian
- **Where:** scripts/verify_netboot_initrd.py REQUIRED_GLOBS; tests/test_verify_netboot_initrd.py GOLDEN fixture.
- **Spec-vs-reality:** the s2a contract listed "_ssl / _hashlib / _socket lib-dynload extension modules" as required initrd files. On Debian trixie (python3.13) `_socket` (and array/math/select/_struct/binascii/zlib/…) are BUILT INTO libpython3.13.so (statically linked), so there is NO `lib-dynload/_socket*.so` file. `copy_exec python3` pulls libpython (with built-in _socket) in, so `import socket`/`import ssl` work at runtime — but the file-existence check failed CI (run 34712822314). The local unit test passed only because the synthetic GOLDEN fixture invented a `_socket.so` that real Debian never produces (fixture-vs-reality theater).
- **Fix:** require the stdlib `socket.py` (`*lib/python3*/socket.py`) instead of `_socket*.so`. `_ssl.so` (present; links openssl) + `socket.py` (stdlib tree present) is the honest file-level proxy for "the initrd can do TCP+TLS". Fixture made realistic (dropped the fake `_socket.so`/`array.so`, added `socket.py`).
- **Lesson:** initrd content-verify fixtures must mirror a REAL `lsinitramfs` listing from the target distro, not an idealized one; built-in vs shared extension split is distro/build-specific.
