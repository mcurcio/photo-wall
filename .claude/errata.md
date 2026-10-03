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

## 2026-09-12 — Retirement ORDER: p4-deb-full-depends must precede s5 (OS-base pipeline retirement)
- **Finding (plan-is-wrong):** the current player .deb bundles a prebuilt venv (scripts/build_player_deb.py) whose build REQUIRES the appliance OS-base chroot (appliance.build.in_root). s5 (p4-retire, "delete custom image pipeline") deletes that OS-base pipeline. 0009's end-state (owner ruling) is that the .deb DECLARES its runtime deps (GTK/GStreamer/weston/…) and the bootstrapper apt-installs them from the distro repo at boot — i.e. NO bundled venv, .deb builds from a plain Debian base. That is slice p4-deb-full-depends, still OPEN.
- **Consequence:** if s5 runs before p4-deb-full-depends, the player .deb becomes unbuildable (nothing produces the OS-base root the venv build needs). s3 Part A (netboot-e2e.yml) also reuses release-artifacts and would break at s5.
- **Corrected Phase-4 order:** s3 green -> **p4-deb-full-depends (decouple .deb from OS-base; apt-declared deps)** -> s5 (retire OS-base/disk-image pipeline) -> s4 (retire old signed client+central path) -> s6 (drop appliance_* tables, LAST/irreversible). Owner authorized "full retirement"; this only reorders it so the build survives.
- **s3 Part A note:** proving Part A now with the venv .deb still validates the bootstrapper fetch/verify/install wiring; after p4-deb-full-depends, rewire netboot-e2e.yml to build the new (apt-deps) .deb from a plain Debian base and drop the release-artifacts dependency.

## 2026-09-12 — s5 BLOCKED: old build pipeline is SHARED with the software-e2e media-OS base
- **Finding (plan/map wrong):** "retire the old build pipeline" (s5) assumed appliance/build.py + scripts/os_base.py etc. are appliance-signed-disk-only and fully deletable. They are NOT. The media-OS test base — scripts/service_base.py ("retain the media worker's native dependencies") -> scripts/ci_images.py (`from appliance import build`, `appliance.run/BuildError/outside_git/canonical/inventory`) -> scripts/os_base.py (definition/definition_id/verify, `python3.12 -m scripts.os_base build`) — is built by the SAME pipeline, using appliance/build.py, appliance/os_definition.json, appliance/os_packages.py, scripts/fetch_ubuntu.py, scripts/ci_base_cache.py.
- **Keepers that break if the pipeline is deleted whole:** service-base.yml is a `workflow_call` used by checks.yml:27 AND software-e2e.yml:24 (Part B, green). Deleting appliance/build.py makes `import scripts.ci_images` fail at module load -> service_base breaks -> checks + software-e2e break.
- **Consequence:** s5 cannot be "delete the whole old pipeline". Either (a) SURGICAL: delete only the appliance signed-DISK-image + VM-boot + rollback + release-authority-image code (build_ci_image/flash/base, create_disk*, build_vm_initrd, build_rollback_candidate, boot_gateway/fixture, vm_* probes, test_appliance_e2e, appliance.yml) and KEEP the shared OS-base/media-OS builder (build.py primitives, os_base, os_packages, os_definition.json, fetch_ubuntu, ci_base_cache, ci_images, service_base); or (b) migrate the media OS off the old pipeline first (bigger), then delete all; or (c) defer retirement.
- **Partial s5 work parked on branch wip/p4-retire-s5-partial (b5d71c4) — INCOMPLETE/BROKEN (ci_images still calls deleted os_base). Do not land.** PR branch reset clean at e5d0bd9.

## 2026-09-12 — 0010 bead 2: "no redirect off-host" contradicts real GitHub asset downloads
- **Where:** docs/decisions/0010-github-release-sourcing.md (promote/mirror sequence diagram: "GET asset_url (bounded, streamed, no redirect off-host)"); central/github_releases.py `GithubReleaseSource` download/manifest fetch.
- **Spec-vs-reality:** GitHub release-asset `browser_download_url`
  (`https://github.com/<repo>/releases/download/<tag>/<file>`) responds 302 to a
  signed CDN on a DIFFERENT host (`objects.githubusercontent.com` /
  `codeload...`). The `manifest.json` asset redirects the same way. A literal
  "no redirect off-host" (as `appliance/provision.py` `_NoRedirect` and
  `media/immich.py`'s `follow_redirects=False` enforce) makes the client unable
  to fetch ANY asset from real GitHub — the feature is non-functional.
- **Decision (implemented):** the byte fetches (manifest + `.deb`) follow a
  bounded number of redirects (`follow_redirects=True, max_redirects=5`); the
  streamed running-total + `Content-Length` + sha256 bounds still hold on every
  hop, and httpx strips `Authorization` on the cross-host CDN hop. The API list
  call (`api.github.com`) does not redirect. Acceptable on a home LAN with no
  threat model (0009/0010 ruling: sha256 is corruption-only, not a trust anchor).
- **Follow-up for beads 3/4:** if a future reviewer wants the redirect target
  constrained, add an allowlist of GitHub CDN hosts rather than forbidding
  redirects outright. The current bound is redirect COUNT, not host.

## 2026-09-12 — 0010 bead 3: env-var names + ETag store location
- **Where:** docs/decisions/0010-github-release-sourcing.md gate #4 ("Repo via
  `PHOTO_WALL_GITHUB_REPO`; optional `PHOTO_WALL_GITHUB_TOKEN`") vs the bead-3
  task brief ("read `PHOTO_WALL_RELEASE_REPO` / `PHOTO_WALL_RELEASE_TOKEN`");
  central/app_release_service.py `AppReleaseService.from_env`.
- **Contradiction (brief overrides doc):** 0010's decision table names the config
  env vars `PHOTO_WALL_GITHUB_REPO` / `PHOTO_WALL_GITHUB_TOKEN`, but the bead-3
  task brief names them `PHOTO_WALL_RELEASE_REPO` / `PHOTO_WALL_RELEASE_TOKEN`.
  Implemented per the brief: `from_env` reads `PHOTO_WALL_RELEASE_REPO`
  (default `mcurcio/photo-wall`), `PHOTO_WALL_RELEASE_TOKEN` (optional),
  `PHOTO_WALL_APP_ROOT` (required on the worker), and
  `PHOTO_WALL_RELEASE_PRERELEASES` (default off).
- **Action for bead 4/5:** the operator docs / config-env reference and any
  producer-side wiring MUST use the `PHOTO_WALL_RELEASE_*` names, and 0010's gate
  #4 text should be reconciled to match (a doc edit, not a code change).
- **ETag store (0010 unspecified, chose):** 0010 mandates ETag/If-None-Match
  hygiene but names no persistence location and 016 has no ETag column. Added
  migration `017_app_release_poll.sql` (singleton `app_release_poll(etag)`);
  `AppReleaseService._load_etag/_store_etag` read/write it, refreshing only on a
  fresh (non-304) list. Losing the row forces one full re-poll — never incorrect.
  Flagged for the bead-4 reviewer in case a different home is preferred.

## 2026-09-13 — console M1 coherence: isUnplaced origin heuristic (for Bead 10 / M5)
- **Where:** central/console/src/projection.js `isUnplaced` = `x_mm===0 && y_mm===0`.
- **Finding (non-blocking at T0):** correct for legacy origin-stacked frames while the console is
  read-only (M1). BUT once M5 (Bead 10 S-place, drag-to-create POST) can place a frame, a frame a
  user deliberately drops at the origin would be mis-routed into the Unplaced tray.
- **Apply in Bead 10:** distinguish "unplaced/legacy" from "deliberately placed at origin". Options:
  drag-to-create should avoid emitting exactly (0,0) (nudge/round), OR carry a placement signal.
  Decide in Bead 10's frozen page; do NOT change isUnplaced's read-only M1 behavior retroactively.
- Disposition: residual, tracked here; not a blocker for M1.

## 2026-09-13 — Bead 8 (C-lease): useCalibration frozen-page signature + conflict enum
- **Where:** central/console/src/useCalibration.js; delivery plan Bead 8 frozen page.
- **Frozen page said** `useCalibration(frameId)`, but the `trying` draft it must preview/commit lives
  in the sibling `useDraft` hook and the frozen sketch had nowhere to express that source.
  **Implemented as** `useCalibration(frameId, trying)`. The load-bearing return contract
  (`calibrate(op) => Promise<{ok}|{ok:false,conflict}>`, `countdown`, `status`) is UNCHANGED.
- Added an internal 4th `conflict: "error"` value for NON-token failures (e.g. a 422) so a dropped-token
  422 cannot masquerade as a clean revision conflict. The three spec'd values
  (revision/generation/unbound) are unchanged.
- **Spec-vs-reality on the probe:** the plan's probe text says dropping `expected_revision` makes the
  write "silently succeed against stale state." The server's `CalibrationRequest` REQUIRES
  `expected_revision` (Field(ge=1)), so the concrete failure is a 422, not a silent success — the
  invariant holds a fortiori. The probe still flips the test RED (revision banner never renders); the
  test was NOT weakened. Disposition: accepted; frozen page reconciled here.

## 2026-09-14 — Bead 8: design-doc internal inconsistency (expired banner copy §4b vs §4c)
- **Where:** docs/operator-console-ux-design.md §4b (line ~342): "Panel is back on committed.
  Re-preview to keep trying." vs §4c/J2 table (line ~456): "Preview expired — panel is back on
  committed. Re-preview to keep trying." (with prefix).
- **Resolution:** implemented the §4b-verbatim form (no prefix) per the fix brief. §4c should be
  reconciled to match §4b (a doc edit, non-blocking). Disposition: accepted; noted for a future
  design-doc touch-up. Not a code defect.
- **Also (Bead 8 blocking finding, now FIXED):** the previewing-branch overtake detection ignored a
  foreign preview (which advances only configuration_revision). Fixed: record own self-bump
  (baseline.configuration_revision + 1) at preview-issue; any FURTHER config_revision advance while the
  slot stays occupied → "overtaken"; timer unmounts. Covered by
  test_calibration_foreign_preview_overtakes_by_inventory_poll (red before fix, green after).

## 2026-09-14 — M3 coherence residuals (non-blocking)
- **(useDraft return-type narrowing):** Bead 7 frozen page declares `useDraft(...) -> {trying: Trying|null,...}`
  but the impl always seeds a non-null Trying (from defaults). Strict, non-breaking narrowing; signature
  matches. Logged for parity with the useCalibration signature erratum. No code change needed.
- **(§4c expired copy):** reconciled docs/operator-console-ux-design.md §4c/J2 line 456 to the §4b-verbatim
  "Panel is back on committed. Re-preview to keep trying." (dropped the "Preview expired — " prefix). Closed.
- **(RESIDUAL — DEFAULT_CORNERS/DEFAULT_CROP duplication):** identity-calibration defaults
  ([[0,0],[1,0],[1,1],[0,1]] / [0,0,1,1]) are defined in BOTH central/console/src/useDraft.js and
  Commissioning.jsx, pinned to the server Calibration default (contracts/models.py:44-47). Consolidate to
  ONE shared constant (e.g. exported from convex.js or a small calibration.js) in a later frontend bead that
  touches those files (opportunistic cleanup; both copies currently identical, low severity). Tracked as
  residual: dedupe-calibration-defaults.

## 2026-09-14 — M4 coherence residual: operator-write fetch helper (do at M6 boundary)
- **Finding:** the operator-write fetch scaffolding (Authorization Bearer via getToken(), Content-Type,
  AbortSignal.timeout(15000), parse {error}, map 409 codes) is hand-rolled in BOTH
  central/console/src/BindingFacet.jsx and central/console/src/useCalibration.js (rule-of-two). M6 adds
  ~5 more write consumers (sources refresh, scenes save, programs, runs, activations) which would each
  re-hand-roll it.
- **Plan:** before/at the start of M6, extract a shared low-level operator-write helper (e.g.
  central/console/src/apiWrite.js) — bearer+timeout+{error}-parse, returning a normalized result — and
  have the M6 write beads use it (optionally refactor BindingFacet/useCalibration onto it). This is the
  rule-of-three prevention. useMutate (primitive #7, refresh-after-write) stays separate.
- Disposition: residual, scheduled for M6 start. Also RESIDUAL: dedupe-calibration-defaults (M3) still open.

## 2026-09-14 — M5 coherence: client-generated Frame id (path back to plan of record)
- **Where:** central/console/src/Plan.jsx createFrame; design J3 POST body (ux-design:483) + fact table (:104).
- **Divergence:** `POST /v1/operator/frames` requires a client-provided `id` (FrameCreate.id is required, no default,
  registry.py:44), but design J3's POST body OMITS id (implying server assignment). createFrame mints an
  Identifier-valid `frame-<...>` id. Works (tests green). Recorded here so M6/future consumers know the POST
  needs a client id; a doc touch-up to J3 could note it. Disposition: accepted, non-blocking.

## 2026-09-14 — M6 START (mandatory refactor before write beads): extract operator-write helper
- Operator-write scaffolding is now RULE-OF-FOUR (BindingFacet.bind/unbind, useCalibration POST,
  Plan.createFrame/moveFrame/deleteFrame, interpretFrame) and M5 created a SIBLING import
  (UnplacedTray.jsx imports deleteFrame from ./Plan.jsx). Bead R-apiwrite (first M6 step) extracts a shared
  low-level operator-write helper + a frames-API module both Plan and UnplacedTray import from (dissolving the
  sibling import), and refactors BindingFacet + useCalibration onto the low-level helper. Behavior-preserving;
  guarded by the existing 36-test browser suite. Supersedes the earlier "M6 start" residual note.

## 2026-09-14 — Bead 17 CUTOVER BLOCKED by content-parity gaps (re-cut: add G1/G2/G3 first)
- **Finding (pre-cutover audit):** deleting the two legacy browser tests at cutover would DROP coverage of
  operator FEATURES the new /console never re-hosted. The Bead 17 precondition (plan:833-835) explicitly
  requires parity incl. "bind/RETIRE ... SOURCES". Beads 9 (O-bind) and 13 (SR-sources) were under-scoped
  vs the old flat page.
- **GAP 1 (HIGH, missing feature):** no player-RETIRE control anywhere in the console (no console file calls
  POST /v1/operator/players/{id}/retire; EquipmentRail only DISPLAYS the Retired rail). Legacy
  test_operator_browser.py:137-141 exercises retire + its UI consequences.
- **GAP 2 (HIGH, missing feature):** no SOURCE-CONFIGURATION control (console lists+Refreshes sources but
  cannot create one; no POST /v1/operator/sources/{ref}). Legacy test_operator_content_browser.py:94-102.
- **GAP 3 (MED, test-only):** manual calibration Revert control exists (Commissioning.jsx) but no /console
  test clicks it. Legacy test_operator_browser.py:118-120.
- **GAP 4 (MED, sequencing):** no token-rejection -> return-to-login / mid-session-401 handling (App.jsx:96
  defers to Bead 18, which lands AFTER the cutover). Legacy test_operator_browser.py:99-104,164-226.
- **GAP 5 (LOW, acceptable):** no /console restart-persistence browser test; the durability property stays
  covered by backend unit tests (tests/test_registry.py etc.). ACCEPTED as backend-covered; noted, not closed.
- **Re-cut (before Bead 17):**
  - G1 `SR-retire`: retire action on the rail/Binding facet -> POST players/{id}/retire + /console test.
  - G2 `SR-source-config`: source-config form (name:rev + connection + type) -> POST sources/{ref} + test.
  - G3 `SR-parity`: token-rejection recovery in App.jsx (401 -> "token not accepted" + return to login) +
    a manual-revert /console test.
  Cutover (17) runs only after G1+G2+G3 land and re-audit shows parity.

## 2026-09-14 — Bead G3: useSnapshot frozen return extended (additive) + manual-revert clears draft
- **useSnapshot (primitive #1):** frozen return was `{snapshot, refresh}`. G3 ADDS `authRejected` (a 401 from
  any plane clears the in-memory token, nulls the snapshot, sets authRejected; a non-401 leaves prior state).
  Purely ADDITIVE — existing {snapshot, refresh} consumers unaffected; refresh still replaces Plane A wholesale.
  Logged for parity with the other frozen-page errata. Non-breaking.
- **Manual calibration Revert** now calls useDraft.clearDraft() on success (Commissioning.jsx), so the draft
  resets to committed (legacy single-field revert parity). This is DISTINCT from lease EXPIRY, which
  deliberately RETAINS `trying` for Re-preview and does NOT clearDraft — Bead 8 expiry tests remain green.
- Both were needed to close content-parity GAP 3 (manual revert) and GAP 4 (token-rejection recovery). The
  legacy same-token websocket-fencing test (test_operator_browser.py:185-226) is architecture-specific (old
  page's operator websocket); the console is REST with per-request bearer auth — NO console equivalent, by design.

## 0012 netboot base auto-mirror

E1 (2026-09-20, bead 1/2): both DB write seams on the `devices` row — netboot serve
AND base-health — must run their read-modify-write under `SELECT ... FOR UPDATE` (or
base-health commits as `UPDATE ... WHERE last_served_tag = running_tag`). r8 review found
Fix-3 was one-sided; a lost update could record `healthy` on a tag the device was just
rolled off. Folded into 0012 bead 1 page; add a base-health mutation probe symmetric to 18b.

E2 (2026-09-20, bead 2): the PENDING_HEALTH_TIMEOUT poll sweep sets `failed_tag = desired`
ONLY when `failed_tag` is currently NULL — never overwrites a live stick, and never uses
`last_served_tag` (which after the r8 split is the known-good tag on a recovery boot).
Folded into 0012 bead 2 page + the two prose sites (lines ~543, ~723).

E2a (2026-09-20, bead 2 impl): a THIRD prose site the E2 fold missed — §"How it hooks
the existing machinery", the "Failed-boot detection + poll sweep" bullet (~line 994) —
still reads "setting `failed_tag = last_served_tag`", directly contradicting E2. E2 is
authoritative and BINDING. See E2b for the fence value bead 2 actually implements. The
docs bead (9) should correct that stale line. No code divergence.

E2b (2026-09-20, bead 2 review fix): within the sweep's `failed_tag IS NULL` branch
(E2's NULL-guard, kept intact), fence the tag the device ACTUALLY ATTEMPTED —
`COALESCE(attached_tag, last_served_tag)` — NOT a recomputed latest-verified/discovered
frontier. This mirrors the live DETECT arm, which only ever fences the served tag.
WHY: the frontier can drift past what a stale device served (another device pushes
latest-verified higher); fencing against that higher tag would fence a tag the device
never attempted, so its next boot would satisfy `desired == failed_tag` ⇒ RECOVER and
the device would silently, indefinitely skip a legitimate already-verified upgrade. If
`COALESCE` is NULL (never served, no pin) `failed_tag` stays NULL — no bogus fence, just
the `failed` outcome. E2's "never overwrite a live stick" NULL-guard and "never the
recovery boot's known-good tag" both still hold (a recovery boot's failed_tag is
non-NULL, so the guard skips it). This supersedes E2's "= desired" phrasing for the
sweep's fence value: the correct value is the served/pinned tag, which in the guarded
(non-recovery) branch is exactly what the device attempted (`last_served_tag` there is
NOT a known-good recovery tag).

E3 (2026-09-20, bead 1): the design's migration-018 storage enumeration (§"Storage,
lifecycle, migration") lists ONLY `app_releases` base cols + `base_cache` + `devices`,
but the base-health seam requires per-epoch `sequence` MONOTONICITY (bead 1 page;
probe 13) and the r8 `devices` schema is frozen with EXACTLY its listed columns (no
sequence column). Monotonicity is impossible without a persisted last-sequence, so bead
1 adds a small `device_base_health(device_id, authority_epoch, sequence)` table in
migration 018 — the direct analogue of `player_feedback` (003), which is exactly the
"reuse the readiness sequence pattern" the bead page calls for. It touches no `devices`
column. Rollback adds `DROP TABLE device_base_health;` before `DROP TABLE devices;`.
Also (minor, no divergence): the `base_cache` row is created by `fetch_base` at state
`caching`, NOT at discovery — the state CHECK has no discovery-time value, matching the
lifecycle diagram (`catalog_known --needed--> caching`) and the page's own parenthetical
"(caching/absent until first fetched)". Discovery writes only the `app_releases` base
facts.

E4 (2026-09-20, bead 3 / residual for bead 9): the BASE_ROOT boot-time writability
assertion is wired in _boot_base (central/app_release_boot.py) but is FAIL-LOGGED, not
fail-crash — boot_autopull runs as a fire-and-forget task whose exceptions are
swallowed+logged. Deliberate: a hard crash would couple a base-volume misconfig to
killing 0010's .deb mirroring in the same worker. The design's "fail loud" guarantee is
satisfied by (a) the ERROR log at boot and (b) bead 9 MUST surface the base-root
assertion failure in operator-visible status/observability, not only logs. If bead 9
does not surface it, reopen this as a hard-fail decision.
RESOLVED (2026-09-20, bead 9): the boot path now records the assertion outcome to a
single-row `base_boot_status` (migration 019_base_boot_status.sql) — `_boot_base`
(central/app_release_boot.py) writes ok=True on a passed assertion and ok=False + the
BaseRootError code before re-raising a failed one — and it is surfaced read-only at
`GET /v1/operator/netboot` (`netboot_base.operator_base_status` -> `boot_status`). The
fail-loud guarantee is thus operator-visible, not only logged; the fail-log posture
(no hard crash coupling base-volume misconfig to killing the .deb mirror) stands. Not
reopened as hard-fail.

E5 (2026-09-20, bead 7): GC (gc_base_cache) is wired to the poll tail only in bead 4.
The doc also calls for GC after pin/health changes; that trigger belongs to bead 7's
attachment surface (and the base-health path). Bead 7 MUST invoke gc_base_cache after a
pin set/clear so freed bytes are reclaimed promptly rather than at the next poll.
RESOLVED (2026-09-20, bead 9): bead 7 landed this — both `PUT` and `DELETE`
`/v1/operator/devices/{device_id}/pin` (central/app.py) invoke `gc_base_cache` in the
same transaction as the pin set/clear (the just-pinned tag is in the keep-set, so GC
never evicts what the pin just enqueued). Verified in the merged bead-7 code; no
further action.

E6 (2026-09-20, bead 5): the doc mandates the per-device `.deb` be resolved on the
per-device serve path off `last_served_tag`, ADDITIVE, with 0010's global
`GET /v1/app/manifest` "not modified and not repurposed" -- but never names the new
route's URL string. Bead 5 implements it as a NEW unauthenticated route
`GET /v1/netboot/manifest` (serial-keyed, symmetric to `GET /v1/netboot/base`), NOT a
serial-aware branch on `/v1/app/manifest` (which would repurpose the untouched 0010
route). BINDING for bead 6: wire the appliance's `fetch_manifest` to
`GET /v1/netboot/manifest` (sending `X-PhotoWall-Serial`), not to `/v1/app/manifest`;
the `.deb` bytes fetch stays `GET /v1/app/package/{sha}.deb` (sha-keyed, unchanged).
Miss behavior (chosen among the doc's "503 + enqueue, or a documented gap"): a
never-served device (`last_served_tag IS NULL`) or unknown/absent serial => 503
`app_manifest_unresolved`, NO fallback to the global `current()` (a fallback would
reintroduce base/`.deb` divergence, F4). A carried tag whose `.deb` is deployable but
not yet mirrored => 503 `app_manifest_uncached` + lazy `enqueue_mirror_in` (coalesced
by the tag lock), symmetric to the base serve's lazy backstop. No code divergence from
beads 1-4; no change to promoted_tag/current_sha256/reconcile/migration 016.

E7 (2026-09-20, bead 6): the doc's bead-6 page requires base-health `running_tag` to
equal `devices.last_served_tag`, but never says HOW the diskless appliance learns the
tag: `GET /v1/netboot/base` returns bytes + `Digest` only (no tag), the initrd persists
nothing across the initrd->OS handoff, and the per-device manifest primitive returned
only `{version, sha256, size}`. RESOLVED: `GET /v1/netboot/manifest` now ALSO returns
the served `tag` (`{**manifest, "tag": served_tag}`, central/app.py). `served_tag` IS
the value `served_tag_for_serial` read from `devices.last_served_tag`, so
`running_tag == last_served_tag` holds BY CONSTRUCTION -- never a client guess, never
derived from the `Digest`. This route (not the base serve) carries the tag because it is
fetched by the SAME booted OS that enrolls and posts base-health; a base-serve response
header would strand the tag in the initrd. Additive: 0010's global `GET /v1/app/manifest`
is unchanged and returns no tag. BINDING for docs bead 9.

E8 (2026-09-20, bead 6): the doc's "Packages touched" line attributes "post base-health
after boot" to `appliance/provision.py`, but base-health requires the ENROLLED player
token, and the bootstrapper explicitly never enrolls (0009 gate #2); the import-linter
also FORBIDS `player -> appliance`, so an appliance-hosted poster the player calls is
impossible. Base-health is therefore posted by the enrolled player (player/service.py
`_report_base_health`, symmetric to how readiness is posted there), fed the served tag
via the appliance's existing origin-handoff file (`PlayerConfig.base_running_tag` in
public.json). The appliance's bead-6 role is the per-device manifest fetch + the tag
handoff; the base-health POST lives in `player/`. Opt-in gate for the per-device path is
the `PHOTO_WALL_PER_DEVICE_DEB` env var (unset => unchanged 0010 global `.deb`, no
base-health). Docs bead 9 should correct the package attribution.
RESOLVED (2026-09-20, bead 9): the package attribution is corrected in the decision
doc's "Packages touched" line (base-health moved from `appliance/provision.py` to
`player/service.py` `_report_base_health`, with the enroll/import-forbidden rationale
and the `PHOTO_WALL_PER_DEVICE_DEB` gate) and documented in
docs/module-appliance-release.md ("The auto-mirror and per-device release path (0012)").

E9 (2026-09-20, bead 6 review — for docs bead 9, non-blocking):
1. base-health's server-side `running_tag == last_served_tag` check binds to the LIVE
   devices.last_served_tag column, not a value pinned at manifest-fetch time. Document
   the assumption that no concurrent re-serve of the same device interleaves between the
   manifest fetch and the base-health post (a genuine reboot restarts the whole squashfs
   fetch, so this holds on the diskless netboot target).
2. write_public_config preserves existing keys and does not explicitly clear
   base_running_tag on a global-path boot. Harmless on the diskless netboot target (RAM
   overlay rebuilt fresh each boot). If this code is ever reused on a persistent-disk
   (D0) install path, add `else: payload.pop("base_running_tag", None)`. Note in docs.
RESOLVED (2026-09-20, bead 9): both notes are folded into the runbook's "Base-image
auto-mirror (0012)" section (the closing "Two assumptions worth stating (0012 errata
E9)" paragraph) — (1) the live-`last_served_tag` binding assumption and (2) the
`base_running_tag` non-clear-on-global-path note with the persistent-disk caveat.
Documented, no code change required on the diskless target.

E10 (2026-09-20, bead 8): the genuine fresh-install e2e ships two gates
(tests/test_netboot_fresh_install_e2e.py). Gate (a),
`test_fresh_install_arc_deterministic`, is the CI PR-blocker: it drives the whole arc
(empty-BASE_ROOT `base_artifact_unavailable` 503 -> real discovery via `service.poll`
-> real `service.fetch_base` -> 200 with correct Digest -> base-health -> frontier
advance -> second device follows) with ONLY the GitHub network boundary mocked
(httpx.MockTransport on GithubReleaseSource); it runs under the DB harness (real
Postgres) like every other DB-backed test. Gate (b),
`test_fresh_install_arc_against_real_github`, exercises the REAL mcurcio/photo-wall
Releases API and is gated on `PHOTO_WALL_RELEASE_TOKEN` (pytest.skip when unset). ACTION
FOR THE OWNER: that secret is NOT wired into any CI workflow today, so gate (b) SKIPS in
CI — the real-GitHub API/asset/CDN/redirect shapes are NOT exercised by CI until the
owner adds a `PHOTO_WALL_RELEASE_TOKEN` secret to the relevant workflow (the DB-harness
pytest job) and passes it through to the test env. Until then only gate (a) (the
deterministic MockTransport path) gates PRs. Gate (b) also skips (not fails) when the
real repo has no released `base_image` asset yet.
RESOLVED (2026-09-20): gate (b)'s token is now wired in CI via the built-in
GHA token — `.github/workflows/checks.yml` job `portable-and-postgres` sets
`PHOTO_WALL_RELEASE_TOKEN: ${{ secrets.GITHUB_TOKEN }}` on the "Run the Postgres
pytest suite" step (the `.venv/bin/python scripts/test_local.py` step), matching
the existing `REGISTRY_TOKEN` step-env pattern. NO manually-managed secret and
NO broadened permissions: the workflow's `permissions: contents: read` already
covers reading this repo's own releases + release assets, which is all gate (b)
needs against `mcurcio/photo-wall`. The token flows to GitHub only —
`GithubReleaseSource` sends it as an `Authorization: Bearer` header to
`api.github.com` and httpx strips it on the cross-host CDN redirect
(central/github_releases.py:152-155). No test change was required: gate (b)
already reads `PHOTO_WALL_RELEASE_TOKEN` (tests/test_netboot_fresh_install_e2e.py:321)
and skips gracefully when it is unset/empty. FORK-PR / empty-token: on a fork PR
`secrets.GITHUB_TOKEN` is restricted/empty, so gate (b) skips (empty string is
falsy) — the correct safe behavior, never an error. REMAINING PRECONDITION: gate
(b) still SKIPS (not fails) until a published `mcurcio/photo-wall` release carries
a `base_image` manifest asset; once such a release exists, CI exercises the real
discover→download→verify→extract→serve path end to end.

E11 (2026-09-21, bead 0013-T2): the entrypoint zero-uid guard was NOT actually
numeric. The T2 spec's grounded-state called the existing `case "$PUID" in
''|*[!0-9]*|0)` guard the "numeric zero-uid guard" and said KEEP IT VERBATIM,
but that glob rejects only the single literal spelling `0` — it lets `00`/`000`
(and `010`) through. The 0013 design (docs/decisions/0013-unified-cache-root.md
§"How ownership and writability work") is explicit that the guard is NUMERIC
(`[ "$PUID" -eq 0 ]`) precisely "so `00`/`000`/`010` cannot run it as root/wrong
uid", and the T2 test spec mandates exit 78 on PUID=0/00/000. Code and spec
disagreed; the design + test are the source of truth, so the guard was
STRENGTHENED (not kept verbatim): keep the `case` for empty/non-numeric, then add
an arithmetic `[ "$PUID" -eq 0 ]` (and the PGID twin) that rejects every zero
spelling. `[ ... ] && { exit 78; }` is set -e-safe (non-last AND-OR operand is
exempt) and only runs after the `case` has guaranteed an all-digit operand, so
the arithmetic never errors. Note: `010` is still accepted as a positive uid (=10
decimal); the design's mention of `010` is not enforced because the T2 test only
requires 0/00/000 and `010` is a legitimate identity — flag if a later bead needs
leading-zero/octal rejection too.

E12 (2026-09-21, bead 0013-T2 fix pass): E11's flagged `010` gap is now CLOSED.
The adversarial review (P2-1) and the design (docs/decisions/0013-unified-cache-root.md:191)
both require `010` to be rejected too ("so `00`/`000`/`010` cannot run it as
root/wrong uid"), so E11's "`010` is a legitimate identity" carve-out is overruled
by the design's canonical-decimal requirement. The guard is tightened from a
`case` for empty/non-numeric plus a separate arithmetic `-eq 0` to a SINGLE `case`
reject-pattern `''|*[!0-9]*|0|0?*` on both PUID and PGID: it rejects empty, any
non-digit, bare `0`, and any leading-zero multi-digit spelling (`00`/`000`/`010`/`007`)
in one construct — accepting only `[1-9][0-9]*`. Canonical-decimal identity is now
a CONSTRUCTION-TIME property (no octal/leading-zero spelling can name a uid at all),
so design line 191's guarantee holds. The arithmetic `-eq 0` twin is removed as
redundant. tests/test_entrypoint.py rejection params extended with `010` and `007`
(exit 78); the canonical `10001` boot assertion is unchanged. Supersedes E11's
closing "flag if a later bead needs leading-zero/octal rejection" note.

E13 (2026-09-21, bead 0013-B3): the design's GC-reorder claim is slightly too
strong. docs/decisions/0013-unified-cache-root.md:149/206 says setting
base_cache.state='evicted' BEFORE the unlink in gc_base_cache's txn makes an
interrupted GC "leave a demoted (regenerable) row, never a dangling cached one."
But unlink() is a filesystem side effect, NOT transactional: a crash AFTER the
unlink but BEFORE the txn commits rolls back the state='evicted' UPDATE while the
file stays gone, so a dangling `cached` row is still POSSIBLE in that window. The
reorder only shrinks the dangerous window (a crash between the two ops now leaves
file-present + row-cached, which is consistent) — it does not eliminate the class.
The class is actually closed by the B3 SERVE-SEAM self-heal (demote +
enqueue_base_fetch_in on the cached-row open-failure), which the design's
"Honest scope of rule 1" already names as the real guarantee. Implemented both as
specified (reorder + serve-seam self-heal); no code divergence — this is a
precision note on the reorder's stated guarantee strength (it is window-shrinking,
not "never"). No action needed unless a later doc pass wants to soften line 149.

E14 (2026-09-21, bead 0013-B4): the design (req 2 / §Storage "Quota + GC + orphan
sweep") frames gc_base_cache as actively enforcing BOTH a reserved floor AND a byte
cap ("never evict below the reserved floor ... enforce a byte cap best-effort below
the keep-set"). In code the two are NOT symmetric active checks, and cannot be,
because the committed keep-set semantics (0012 bead 4, tests/test_netboot_base_gc.py
test_d/e) evict EVERY non-keep-set ("surplus") tag UNCONDITIONALLY for correctness
(an orphaned / retired-device tag), regardless of byte budget. So:
  * The CAP is enforced best-effort below the keep-set: all surplus bytes are
    already evicted, so the only bytes that can exceed the cap are the protected
    keep-set itself -> an ALARM (`keepset_over_cap`, sizes logged), never eviction
    of a protected tag. This is an active check.
  * The FLOOR is enforced BY CONSTRUCTION, not as an active byte-gate: GC only ever
    removes surplus/orphan bytes and never a keep-set member, so os-images always
    retains its bootable working set; the 4Gi floor is the reserved capacity that
    guarantees that working set fits (protecting netboot from other domains). An
    ACTIVE floor-gate on eviction was rejected: gating surplus eviction on a byte
    floor would retain orphaned/retired-tag files below the floor, contradicting
    the frozen keep-set-departure eviction (reds test_d). Floor/cap are named
    placeholders (OS_IMAGES_RESERVED_FLOOR_BYTES=4Gi, OS_IMAGES_BYTE_CAP_BYTES=12Gi)
    pending owner confirmation (decision 2). No code divergence from intent; this
    records that "never evict below the floor" is a construction guarantee, and a
    later doc pass may state it as such.

E15 (2026-09-21, bead 0013-B4): the orphan sweep's ORIGINAL implementation did NOT
enforce the design's frozen "Sweep invariant" single-writer exclusion. It read the
owned set (`cached` U `caching`) ONCE as a snapshot and never re-checked at unlink
time, and the code + docstrings claimed that snapshot WAS "the single-writer
exclusion media uses" -- FALSE: media uses a real `fcntl.flock(LOCK_EX)` writer
lock (`media_store.py` worker_lock), the sweep had no lock at all. A concurrent
`fetch_base` that `os.replace`d its landing bytes (netboot_base.py ~:764) AFTER the
sweep's SELECT but BEFORE it reached that tag's `unlink` -- including the real
leftover-generator where `os.replace` succeeds then the `state='cached'` write
throws, leaving the row `failed` with a fresh file present -- would be deleted
mid-fetch. FIX: the invariant is now ENFORCED (not by a lock -- fetch's `os.replace`
runs on the event loop inside async `fetch_base` while the sweep runs in
`asyncio.to_thread`; no lock object is shared across that split, and an in-process
`threading.Lock` would be weaker than media's cross-process flock and would block
the loop) but by defense-in-depth BOTH, per B4's "Acceptable" clause:
  * (a) a per-tag ownership RE-CONFIRM (`_base_tag_owned`) re-SELECTs the row state
    immediately before each unlink, so a fetch that COMMITTED its `caching`/`cached`
    row after the snapshot is honoured;
  * (b) an MTIME GRACE (`_ORPHAN_MTIME_GRACE_SECONDS` = 10x the 30s fetch download
    timeout = 300s): never unlink a `base-<tag>.squashfs` whose mtime is within the
    grace of now (wall-clock `time.time()`, compared against the filesystem's own
    mtime -- NOT the injectable logical clock). A just-`os.replace`d file has a
    fresh mtime even when its `cached` row is uncommitted or the write threw, so
    this closes the `unlink`-vs-`os.replace` race the re-confirm alone leaves open;
    a genuine orphan ages past the grace and is swept on a later tick.
F-2 (crash on missing dir) also fixed: `_sweep_orphans` now returns [] when the
os-images dir is absent (matching `gc_base_cache`'s tolerance) instead of letting
`iterdir` raise FileNotFoundError and crash the poll tail every tick. Docstrings in
`_owned_base_tags`, `sweep_base_orphans`, and `_sweep_base_orphans` corrected to
state the snapshot is NOT the exclusion; the two guards are. Tests: 4 no-DB unit
probes (missing-dir tolerance; fresh-mtime spared; aged orphan swept; became-owned
re-confirm) + the existing DB orphan-sweep test's orphan aged past the grace.

E16 (2026-09-21, bead 0013 Slice-1 residual cleanup): the always-on flip
(`base_root = base_root or cache_layout.os_images_root()`, central/app.py:184)
makes `base_root` always non-None past that line, so the base-root gates the design
promised would be "removed/repurposed, not left dangling" (design:220-223) were
still present as always-true branches. This residual bead reconciles them without
behavior change: the `if base_root is not None:` guards around `gc_base_cache` in
`pin_device` (central/app.py:799) and `unpin_device` (central/app.py:814) are made
UNCONDITIONAL (base_root is always a path, so the GC always ran anyway — E5's
pin/unpin prompt-GC is unchanged), and `operator_base_status`'s
`"base_root_configured": base_root is not None` (central/netboot_base.py:336) is set
to the literal `True` (field KEPT for the `GET /v1/operator/netboot` API/operator
back-compat, base serving is always configured now). NOT touched:
`central/app_release_boot.py:102`'s `if base_root is not None` — that guards a
reachable `boot_autopull(base_root=None)` test seam per its docstring and is
legitimate. No code divergence; honors design:220-223. Mirrors the E13/E14/E15
doc softenings.

## 2026-09-22 — Central MVP lane C (assets), beads C-1..C-4

- **C4's source pointer is wrong: `tests/test_netboot_base.py` has no hostile-archive tests.**
  `grep -rn "base_member_not_file\|base_digest_mismatch\|SYMTYPE" tests/*.py` finds none for
  `_extract_squashfs` (central/netboot_base.py:692). The only tar-type cases are for the player
  package (tests/test_player_package.py:312). The named cases (traversal, symlink/hardlink,
  device/fifo/dir, duplicate names, oversize, wrong digest, missing member) were written from
  scratch in `tests/test_assets_os_image.py`. No behaviour change.
- **`CacheStore` exposes a read-only `layout` property that is not on the frozen page.**
  `AssetProduction.__init__(store, records, transactions)` gets no layout. But C1 step 2 ("if the
  file is present, measure it ... discard it") needs the final path, and `measure`/`discard` take
  a `Path`. Added `CacheStore.layout -> CacheLayout` (central/assets/store.py) instead of a new
  store method. P2 wiring is unaffected.
- **C1's "a reference's expected facts" is read as "every reference's stated expectation".**
  Step 3 raises `digest_mismatch` unless the produced file satisfies each reference's
  `expected_size`/`expected_sha256` where set. Step 2's early return also requires the newest
  reference to state both. The steps share one predicate, so step 2 never accepts what step 3
  would reject.
- **The 021 backfill seeds only legacy rows that the kernel types accept:** an owner/tag of at
  most 128 chars, and a locator URL matching `^https?://` of at most 2048 chars. Without this
  filter, one bad legacy row would do one of two things. It would abort the migration (the
  `owner` CHECK), or it would make `PgAssetRecords.get` raise `ValueError` while building an
  `OriginLocator` (central/kernel/assets.py:62-67). A filtered row gets no asset.

## central-mvp Lane A (job runtime) — 2026-09-22

- **A-3 STOP-class, resolved locally: the savepoint cannot sit where the page puts it.**
  `.claude/mvp/lane-A-job-runtime.md:212` has `publish` call
  `defer(..., connection=...)` *inside* `conn.transaction()`, while `:66-67` has `defer`
  itself report `AlreadyEnqueued` as `False`. Together, a merge's UniqueViolation is caught
  INSIDE the savepoint block, which then exits normally and issues `RELEASE SAVEPOINT` on an
  aborted transaction (psycopg `transaction.py` `_commit_gen`), so every merge aborts the
  caller's transaction and PB5 fails. Probed on PostgreSQL 16: with the literal placement,
  `test_infra_publisher_conformance.py::{test_enqueued_merged_and_joined_handles_are_equivalent,
  test_a_merge_inside_within_leaves_the_caller_transaction_working}[procrastinate]` both FAIL.
  Implemented: `job_queue.defer` opens the savepoint itself and catches `AlreadyEnqueued`
  OUTSIDE it (the `central/media_queue.py:51-63` pattern); `publish` calls `defer` directly.
  Frozen signatures unchanged. Page `:212` should read "defer opens the savepoint".
- **PB4 "a running copy is joined, not duplicated" (`P0-kernel.md:235`) differs between the
  two publishers.** procrastinate's queueing-lock index covers only `todo`
  (`procrastinate/sql/schema.sql:100`), so publishing while a copy is `doing` INSERTS one
  `todo` copy (it waits on the `lock`, then runs again). The handle still joins the running
  copy (resolves on its outcome, `since` rule), so the conformance case asserts handle
  equivalence only; `RecordingPublisher` inserts nothing in that case. Cost: one extra,
  normally cheap re-run (an asset handler finds its verified file). Not changed; flag for the
  kernel page if "not duplicated" was meant literally (it would need a `doing`-row lookup).
- **Additive, not on the frozen page: `QueueAdmin.aclose()`.** `QueueAdmin(dsn)` owns an async
  procrastinate pool (opened lazily, `min_size=0, max_size=2`) and the page gives it no
  lifecycle. P2 wiring should `await admin.aclose()` at worker shutdown.
- **Kernel gap: an `ok` outcome for an asset job whose Asset record is absent.**
  `record_produced` is a no-op for an absent row (`central/kernel/ports.py` AssetRecords), so
  `Ready.result` (read from the record, `P0-kernel.md:202`) has nothing to return. The adapter
  returns `Failed(terminal=False, "asset_not_recorded", retry_after=0)`;
  `RecordingPublisher` instead falls back to the in-memory result. Needs a kernel ruling.
- **Minor: `RecordedFailure(reason)` (`lane-A-job-runtime.md:176`) carries the outcome STATUS**
  (`"transient"`/`"terminal"`), because `JobExecutor.execute` returns only the status (frozen
  signature). The reason is in `job_outcomes`; the exception only ends the row `failed`.

## central-mvp P2 (wiring, route rewire, legacy removal) — 2026-09-22

- **P2.3 acceptance grep cannot hold literally.** `grep -rn "app_release\|..." central media`
  also matches the KEPT tables `app_releases`/`app_release_policy`/`app_release_poll`, which
  lane B reads by design (`central/infra/catalog_records.py:72-134`), and provenance
  docstrings in lane files (`central/origins/github.py:3`, `central/kernel/types.py:30`,
  `central/content_catalog/catalog.py:4`). Read as "no import of a deleted module": verified
  by grepping `from central.app_release|central.app_packages|central.app_releases|
  central.github_releases|media.app_release_tasks` (no hits outside docs/.claude). The stale
  lane docstrings were left for the docs bead.
- **Unlisted consumer of the pruned `netboot_base`:** `tests/test_cache_layout.py:58` tested
  `netboot_base.resolve_base_root`, which the prune removes. Replaced by the same property on
  the new path (`CacheLayout(cache_layout.cache_root(env)).directory(...)` ignores the
  retired `PHOTO_WALL_BASE_ROOT`/`PHOTO_WALL_APP_ROOT`). No production consumer existed.
- **Deleting `central/app_packages.py` broke `scripts/check_docs.py` (CI `checks.yml:59`):**
  `docs/decisions/0009-minimal-base-and-app-package.md:739` linked it. De-linked to plain
  text ("since removed by the Central MVP"); the rest of the docs sweep stays the docs bead.
- **Additive, not on the frozen page: `build_job_runtime(..., admin: QueueAdmin | None = None)`.**
  The lane A erratum asks P2 to `await admin.aclose()` at worker shutdown, but the frozen
  `build_job_runtime` hides the `QueueAdmin`. The worker passes its own and closes it in
  `_entry`'s `finally`; the page's call shape still works (None builds one).
- **`until_disconnect` raises `ClientDisconnected`, a `CancelledError` subclass,** so the routes
  can tell a client disconnect from a real cancellation of the request task; they answer 499
  to the gone client (nothing reads it) and the genuine cancellation still propagates.
- **The lifespan installs procrastinate's schema whenever content services exist** (not only
  when the media queue is procrastinate's): the content publisher defers into
  `procrastinate_jobs`, and the page's lifespan order is migrate -> apply_schema -> feed.start.

## central-mvp PR #22 review fixes, lane X (catalog, migrations, CI) — 2026-09-22

- **P0 carry uses a second pointer, not only `promoted_tag`.** Migration 023 adds
  `app_release_policy.last_good_tag` (main's `current_sha256` on the new model). The served tag
  (`asset_sha256 = current_sha256`) becomes last-good, and becomes promoted only when nothing is
  promoted. A pending promotion is kept (main would have converged to it). No match (a manual
  upload the MVP cannot serve) writes nothing: a migration WARNING, a WARNING on every sync while
  auto-promote is suppressed, and a 503 `app_unconfigured` until an operator promotes.
- **The manifest's last-good fallback is that pointer** (`promoted_package`): promoted `.deb` on
  disk, else last-good on disk, else promoted. `promote_in` (the operator route and auto-promote)
  records the outgoing tag as last-good when its `.deb` is on disk.
- **New catalog port `StoredAssets.present`** (`content_catalog/ports.py`; adapter
  `central/infra/stored_assets.py`). It duplicates `PrefetchHandler._missing`'s check (lane Y
  owns `central/assets`; it could use the adapter). The package rule's on-disk answer is a
  snapshot: a file removed before the reader opens it is fetched once (nothing removes files
  in the MVP).
- **The desired set now also has active devices' `last_served_tag` and the last-good `.deb`.**
  Without them, the per-device manifest could name a `.deb` that the package rule
  refuses to fetch (after a substitute boot). This matches design §3 "current OS and `.deb` for
  unpinned devices".
- **Auto-promote "cached"** is now "some release's `.deb` has produced facts" (main counted
  `app_packages`). Prereleases come from `origin.include_prereleases`.
- **Substitute eligibility (owner ruling):** after `desired`, the device's known-good plus every
  full release with an OS image older than `desired`, newest first, never the fenced tag. An
  absent serial may substitute too. RECOVER (fenced on desired) still serves only the known-good.
- **Divergence freeze** applies to a tag whose old `.deb` had produced facts (main: `mirrored`),
  plus legacy `divergent` rows. Only the `.deb` facts freeze. Main also froze base facts; that
  is out of scope here.
- **Frontier:** `SELECT DISTINCT known_good_tag` (plus 024 partial indexes), with `newest()` in
  Python. A SQL max would risk collation-dependent prerelease order against `order_key`.
- **The frozen lane-B page changed:** `ReleaseRow.divergent`; `ReleaseRecords.shipping`,
  `mark_divergent`, `last_good_tag`, `set_last_good`; `DeviceRecords.known_good_tags`,
  `named_tags`, `names_any`; `ReleaseCatalog(stored=...)`; `SyncReleasesHandler(include_prereleases=)`.
- **Docs not updated (docs bead):** `docs/module-appliance-release.md:86` still describes the
  manifest as "the promoted package" without the last-good fallback.
## central-mvp PR #22 review fixes, lane Y (runtime, worker, infra) — 2026-09-22

- **P2 `_entry` "keep the media path byte-for-byte" (`P2-wiring.md:30`) was wrong for design §2.**
  The media writer flock wrapped the WHOLE worker, so a second process exited
  `media_writer_active` and only one JobRuntime ran fleet-wide. Now `JobRuntime` runs in every
  process; only the legacy media loop (`_media_writer`: lock, recipe/maintain/refresh boot,
  media queue) waits for the lock, standing by every `MEDIA_STANDBY_SECONDS` (5 s) and taking
  over when the holder exits. `_run_workers(media, runtime)` now takes the media awaitable.
- **Loops that return are failures** (`central/infra/runtime.py` `until_stopped`): procrastinate
  ends a worker NORMALLY when a side task (LISTEN, heartbeat, periodic) fails. Any runtime or
  media loop returning while not stopping raises `RuntimeError("worker_exited")`; `main()`
  reports the first leaf of an ExceptionGroup and exits 1.
- **Wedged key after a failed `finish_job`: chosen fix is retry, then fail the process** (not a
  rescue-by-newer-outcome or a max-run-time bound). `JobRuntime` installs `_CompletionGuard`
  as `app.job_manager`: the completion write retries at 0.5/1/2 s; if it still fails, the runtime
  stops gracefully and `run()` raises `completion_not_recorded`. The worker's row is
  unregistered (or its heartbeat lapses), so the existing dead-worker rescue re-publishes the
  row. Why: it reuses the one rescue path; a "newer outcome" rule cannot tell a lost completion
  from a legitimate re-run, and no handler has an enforced max run time to bound on. Cost: a
  DB outage longer than ~3.5 s during a completion restarts the worker (running jobs finish
  first); an already-`ok` job may re-run once (handlers are idempotent).
- **`asset_not_recorded` is ONE transient condition** (kernel ruling asked for by the lane A
  erratum). `ASSET_NOT_RECORDED` moved to `central/kernel/publishing.py` (PB7 text extended);
  `AssetProduction.produce` raises `TransientFailure(ASSET_NOT_RECORDED)` instead of
  `TerminalFailure("unknown_asset")` (a terminal outcome would stick via PB3 although the record
  is catalog state a later reference re-creates); `RecordingPublisher` returns
  `Failed(False, ASSET_NOT_RECORDED, 0)` instead of falling back to the in-memory result. The
  case is now in the shared conformance suite. `lane-C-assets.md:142` should read
  "TransientFailure(asset_not_recorded)".
- **`build_job_runtime` back to the frozen page's signature** (drops the additive `admin=`
  parameter recorded by the P2 erratum above). `JobRuntime(..., owned=(admin,))` owns the
  `QueueAdmin` and closes it when `run()` ends; `run()` also closes its own pool when cancelled
  mid-open (a half-opened pool reconnecting forever kept the process from exiting).
- **Merged redelivery keeps its backoff:** when `_redeliver`'s defer merges into a pending copy,
  `job_queue.carry_attempt_async` raises that copy's `_attempt` to the redelivery's (never
  lowers it).
- **Rescue is per row:** one row failing (`close` on a row no longer `doing`) no longer aborts
  the batch; the run ends `TransientFailure("rescue_incomplete")`.
- **CI "2 LISTEN connections" was the test, not the feed:** CI runs pytest against the Compose
  database where the `central` container's own `OutcomeFeed` holds a LISTEN connection with the
  same `application_name`. The feed tests now count only pids that appeared after their feed
  started (and terminate only their own), with a foreign same-named connection in the test.

## central-mvp PR #22 slimming and fresh-review fixes — 2026-09-23

- **OS-image re-cut (review P1): `AssetRecords.forget_produced(tx, key)` is new** (design §10.2
  and §10.4 updated). `SyncReleases` calls it when a tag's base tarball changes sha or size
  (release.yml re-uploads with `--clobber`), so the next production records the new build
  instead of failing `not_reproducible` for good after a cache wipe. It is the only caller;
  media's write-once facts are untouched. `AssetProduction` re-reads the references before the
  rename and discards a build from a replaced locator (`TransientFailure("reference_changed")`).
  **Residual, not closed:** a build that passes that re-check and whose outcome commits just after
  a concurrent re-cut still records the old build's facts (a window of milliseconds between the
  re-check and the executor's commit). Closing it needs the produced facts keyed to the locator
  they came from.
- **Completion guard (review P2):** a completion write refused because the row is no longer
  `todo`/`doing` (rescue closed it) is dropped with a warning; only an open or unreadable row
  still stops the runtime. It reads procrastinate's own `get_job_status` query.
- **Frozen `.deb` after a cache wipe (review P2): chosen fix is "a divergent tag's `.deb` is
  desired only while its produced file is on disk"**, not a terminal `download_corrupt`. The
  origin cannot tell a frozen tag from a sync that has not caught up yet, and making every
  digest mismatch terminal would strand a release whose manifest landed before its `.deb`
  (Prefetch never retries a terminal). Cost: a frozen `.deb` gone from disk now answers 404 on
  the package route (was 503 and a re-download on every Prefetch); the manifests still name it.
- **Serial rule (review P2):** `sanitize_serial` / `device_id_for_serial` live only in
  `central/content_catalog/catalog.py`; `central/netboot_base.py` keeps `SERIAL_HEADER`.
- **Kernel: the `subject=` class keyword is gone** (no job type used it). The subject is every
  field; `lock == queueing_lock` for every job type, and "a waiter may resolve on another
  payload's run under the same subject" (§10.2) no longer applies. **Kept** the one-field rule
  for asset job types: `asset_key` names the file by that field, and dropping the rule would let
  two jobs share one file. It still contradicts §10.1's two-field `PrepareMedia`; the media bead
  must define a two-field identity first.
- **`job_outcomes.failing_since` dropped by a new migration 025**, not by editing 020 (migrations
  are forward-only and checksum-pinned; a database that already ran this branch would refuse to
  boot). `JobOutcomes` no longer restates the table's CHECKs; it still refuses NaN/infinity, which
  DOUBLE PRECISION would store.
- **The SQL-recoding fakes are gone** (`tests/catalog_fakes.py`, `tests/fakes/asset_records.py`,
  `InMemoryOutcomes`). Those suites run on PostgreSQL through `tests/content_db.py`. Converting
  `test_migration_carry_served_package.py` exposed a test artifact: it stopped at 022, so 021's
  Asset seed ran on an empty schema, and the in-memory stored-assets fake hid that no Asset rows
  existed. It now upgrades from main's 019, as a real upgrade does.
- **`.claude/mvp/` deleted.** Earlier entries cite `P0-kernel.md`, `lane-*.md` and
  `P2-wiring.md`: read them at commit 90999e7. Migrations 021/022 still say "lane C"/"P2.1" in
  their comments (checksum-pinned, left as is). The reviewer's probe
  (`scratchpad/probe_recut.py`) imports the deleted fake; its scenario is now
  `test_a_recut_os_image_is_produced_again_after_a_cache_wipe`.

## 2026-09-23 — follow-ups: issue #24

- **The design says a substitute serve publishes nothing. That is the bug.** `AssetReader.read`
  now publishes the FIRST candidate's fetch job whenever the candidate it serves is any other
  one. The check is on the served job after either path, so a future path that serves a
  substitute is covered too. The publish runs in a background task through
  `Publisher.publish_now(..., retry_terminal=True)`, the same request publish as a miss (the
  owner ruled a request may retry a terminal). The serve never awaits it, the read's
  cancellation does not cancel it, and a failure is logged (`"fetch publish for ... failed
  after serving a substitute"`) and never fails the serve. Doc text that is now wrong, for the
  docs bead: `docs/central-system-architecture.md` §6(b) ("No job is published" is true only
  when the first candidate is the one opened), the §10.2 `read` sketch
  (`# on disk: publish nothing`), §3's Assets row ("open, else publish ...") and §4's
  Published-by column for `FetchOsImage`/`FetchPackage` ("HTTP miss; Prefetch" needs "HTTP
  substitute serve").
- **Costs of the chosen shape.** One short DB write per substitute serve, merged by the job's
  queueing lock. There is no in-process coalescing, because that would restate dedupe at the
  call site. A background publish still in flight at process shutdown is dropped; the next
  `Prefetch` or request re-publishes it. Only `FetchOsImage` substitutes today (a package
  request resolves to one sha256), but the rule is kind-agnostic.

## 2026-09-23 — follow-ups: two-pod run (item 6) and #26 probe

- **Two-pod run PASSED** (`scripts/two_pod_run.py`, 2 Central + 2 workers, one shared cache, fake
  GitHub origin): flow (c) single download for 8 concurrent misses across both pods; flow (d)
  kill -9 mid-download rescued by the other worker; flow (e) cache wipe refilled with one download
  per asset while `/readyz` stayed green; empty catalog 404; a pinned device never substituted.
- **Proven defect (legacy media, fold into the media retirement, do not patch):** a cache wipe
  unlinks the held `media/.worker.lock` (`central/media_store.py:182-186`); the standby loop
  (`media/worker.py:356-372`) then locks a NEW file, so two media writers run at once. Violates
  "cache purgeable anytime". Retiring the flock (item 1) removes the class.
- **Harness shim:** `GitHubReleaseOrigin.from_env` has no API base URL setting; the harness
  monkeypatches it. A CI two-pod run needs a real setting.
- **Doc corrections for the docs bead:** §6(c) says publish "merges into a pending or running
  copy" — with a copy running, a new `todo` row is inserted (it runs afterwards as a no-op; §10.3
  is right). §3 "otherwise Unknown → 404" holds only while the catalog is empty: with a release
  present an unseen, missing or invalid serial is auto-registered and served.
- **CI gap:** `netboot-e2e.yml` `tracer` runs one Central, no worker, no origin and a pre-staged
  `.deb`; nothing in CI covers the OS image, flows (c)-(e), two pods, or kill-and-rescue.
- **#26 probe (real PostgreSQL):** the guard part is FIXED by dd144f7 (a rescued row's completion
  is dropped; an open row still stops the runtime). STILL REAL: (1) a worker started while another
  is paused prunes the paused worker's `procrastinate_workers` row; its heartbeat then silently
  updates nothing and its next fetch dies on `procrastinate_jobs_worker_id_fkey`; (1b, derived)
  `select_stalled_jobs_by_heartbeat` treats `worker_id IS NULL` as stalled, so a pruned worker's
  live job is rescued; (2) the paused worker's late outcome overwrites the copy's (`ok` → `terminal`)
  because `JobExecutor` commits outcomes without checking the delivery still owns its row
  (`central/infra/execution.py:131-177`); (3) a true outage takes ~279 s to stop (30 s pool timeout
  per attempt) and the named `completion_not_recorded` reason is lost to the task-group error.

## 2026-09-23 — follow-ups: issues #23 and #25

- **#23 (owner ruling "remember who promoted"): the policy row records who promoted.**
  Migration `027_promoted_by.sql` adds `app_release_policy.promoted_by` as NOT NULL with a CHECK
  for `('auto','operator')` and no default. The periodic sync auto-promotes only when nothing is
  promoted or the promotion is `'auto'`. It keeps the suppress rule, the WARNING (only when
  nothing is promoted) and the lane-X last-good carry through `promote_in`. It never moves an
  `'operator'` promotion.
- **Every writer states who it is. Nothing has a default.** The new signatures are
  `ReleaseRecords.set_promoted(tx, tag, *, by: Promoter)` and
  `ReleaseCatalog.promote_in(tx, tag, *, by: Promoter)`, with
  `Promoter = Literal["auto", "operator"]` in `ports.py`. There is a new reader,
  `ReleaseRecords.promotion(tx) -> Promotion | None`, where `Promotion(tag, by)`. The operator
  route writes `"operator"` and the sync writes `"auto"`. A raw SQL writer that omits the column
  fails on NOT NULL. `scripts/test_netboot_e2e.py`'s seed now names `'operator'`
  (`test_netboot_e2e_wire.py` runs it against the real schema).
- **Backfill: every existing row is `'operator'`.** No row can be proven automatic. Main's
  `_autopull_deb` and the operator route both called the same `set_promoted`
  (`git show ca75879^:central/app_release_boot.py`, around line 141). 023's carry promotes only
  when nothing was promoted, from `current_sha256`, which with no promoted tag only an operator
  set (0009 `/v1/operator/app/current`).
- **Costs:**
  1. An upgraded install whose promotion main set automatically now stays on that tag until an
     operator promotes once. Before the MVP, main re-pulled the newest at every boot.
  2. **Gap: no operator action hands control back to auto.** There is no unpromote or "follow
     newest" route (`central/app.py:533` is promote only). Once an operator promotes, the sync
     never moves the pointer again. Adding such an action needs a design choice: deleting the
     policy row also drops `last_good_tag`, so either `promoted_by='auto'` in place, or
     `promoted_tag` must become nullable. No route was added.
  3. The operator views (`ReleaseView`, the console) do not show who promoted.
- **#25: only the served role ages. The pinned and known-good roles do not.** Signature change:
  `DeviceRecords.named_tags(tx, *, served_since: float)` and
  `names_any(tx, tags, *, served_since: float)`. The window is defined once:
  `SERVED_TAG_WINDOW = timedelta(days=30)` in `central/content_catalog/catalog.py`.
  `ReleaseCatalog._served_since()` (the clock minus the window) feeds both reads. The adapter
  builds both SQL statements from one role table (`_NAMING_ROLES`), so the two cannot disagree.
  Migration 026 replaces 024's `devices_active_served` with `(last_served_tag, last_served_at)`
  and keeps the same partial predicate. EXPLAIN (enable_seqscan off, 20k rows) shows an
  index-only scan for the EXISTS and a bitmap index scan for the DISTINCT.
- **Consequences of the ruling (not bugs in this bead):**
  1. A row with `last_served_tag` set and `last_served_at` NULL names nothing in the served
     role. Every writer sets both columns together.
  2. The window re-opens lane X's gap for devices with long uptimes. Take a device that has not
     netbooted for more than 30 days, whose tag is not pinned, known-good, promoted, last-good or
     the frontier. Its per-device manifest (`device_package`) still names that tag's `.deb`, but
     once the file is gone from disk the package route answers 404 for it. Nothing removes files
     in the MVP, but a cache wipe does.
  3. A tag served to a fake serial stays desired for 30 days after the serve. The number of such
     tags is bounded by the release count, not the device count.
- **Doc text for the docs bead:** in `docs/central-system-architecture.md`, the §5 "Desired set"
  row ("current OS and `.deb` for unpinned devices") needs "served within the last 30 days". §3's
  Catalog row and §5's Catalog entries row ("pin, promote") do not say that a promotion records
  who set it, or that the sync moves only its own.

## 2026-09-24 — PR #27 review: substitute-serve publish moves into the open's transaction (#24)

- **The #24 background task is replaced.** `AssetReader._open_first` now publishes the wanted
  (first) candidate with `Publisher.publish(jobs[0], within=tx, retry_terminal=True)` inside the
  transaction that opened the substitute, so the fetch commits with the serve. Deleted:
  `_background`, `_publish_in_background` and `_publish_logged`. Reasons:
  - nothing drained the task set at shutdown, so the lifespan closed the DB under in-flight
    publishes;
  - each substitute serve took an extra pool connection outside the `WaiterSlots` bulkhead;
  - it contradicted the reader's own no-in-process-coalescing rationale.

  A publish failure is caught and logged, and never fails the serve. A client disconnect does
  not cancel it: the open runs shielded on a worker thread.
- **ProcrastinatePublisher violated PB5.** Its outcome read ran on the caller's connection,
  OUTSIDE the `defer` savepoint, so a failing read aborted the caller's transaction. A caught
  failure followed by a commit silently became a ROLLBACK: PostgreSQL answers COMMIT on an
  aborted transaction with the tag ROLLBACK, and psycopg does not raise. Meanwhile
  `PgTransaction.state` reported "committed".
  - Fixed: the whole publish (outcome read and defer) now runs in one savepoint of `within`.
  - Guarded by `test_adapter_a_failed_statement_inside_publish_leaves_the_caller_transaction_working`.
  - Cost: one extra SAVEPOINT/RELEASE round trip per publish, for every caller.
- **Costs of the new shape.**
  1. `retry_terminal=True` on a substitute serve: an unauthenticated LAN client can keep a
     terminally failed release re-downloading back to back. Accepted by the owner 2026-09-24.
  2. The substitute check now lives in `_open_first`. A future path that serves a substitute
     outside `_open_first` must publish there too.
  3. The publish adds one savepoint round trip to the serve's open transaction. It uses the
     connection already held, so there is no extra pool pressure.
- **Docs bead:** any doc text saying the substitute publish is "in the background" or "not
  awaited" is now wrong. It is synchronous, inside the open's transaction.

## 2026-09-24 — PR #27 review: catalog race, backfill, harness moved into CI

- **#23 race fix:** `ReleaseRecords.set_promoted(tx, tag, *, by)` now returns
  `PromotionWrite(moved, outgoing)` and enforces "auto never moves operator" inside the write
  itself. The write is:
  1. on a first promotion, `INSERT ... ON CONFLICT DO NOTHING`;
  2. otherwise, `SELECT ... FOR UPDATE`;
  3. then a guarded `UPDATE`.

  `promote_in` returns a bool and carries last-good only when the write moved.
- **027 backfill follows main's rule:** 'operator' only if a binding exists AND a release's `.deb`
  has produced facts; otherwise 'auto'. This supersedes "every existing row is operator".
  - Main's code fails on the 027 schema (NotNullViolation on `promoted_by`), so a rollback must
    drop the column first.
  - `GET /v1/operator/app/releases` returns `promoted_by`. The console does not show it yet.
- **026:** `DROP INDEX IF EXISTS`, plus `CHECK (last_served_tag IS NULL OR last_served_at IS NOT NULL)`.
  Only `names_any` uses the index, and only when few rows match; `named_tags`' served branch
  does a sequential scan.
- **The two-pod harness is a CI test:** `tests/test_two_pods.py` (module-scoped schema, 65–95 s).
  `scripts/two_pod_run.py` is deleted. The shared support lives in `tests/support/`.
  - The new setting `PHOTO_WALL_RELEASE_API_BASE` needs a row in the runbook (docs bead).
  - The media flock defect is pinned by a strict xfail.
  - Coalescing means "one pending copy at a time and one origin GET", not "one fetch row".

## 2026-09-24 — #26 per-data design, review round 1: media variant identity (for the media design)

- **Architecture §9 decision 1 says a media variant's digest is a write-once integrity check.**
  That cannot survive a cache wipe while rendering is not reproducible
  (`docs/module-media-preparation.md:33` disclaims a reproducible build): a re-render after a wipe
  yields different bytes, and write-once facts would refuse them (`not_reproducible`) for good.
- **Proposed for the media design (not built by #26):** keep the variant key `original + recipe`,
  store its bytes under their own sha256, and let the variant row point at that digest. The pointer
  is set if unset, or moved only when its file is gone (compare-and-set): an OCI tag pointing at a
  digest. The digest stays write-once per FILE, not per variant. Whether a Pi can see a variant's
  digest change after a wipe is a Pi-visible question for the media gate.

## 2026-09-24 — bead 1 os-image-content-key

- **The page is silent on legacy tags that fail `release_version`.** The old
  `_os_image_job(tag)` skipped them because `FetchOsImage(tag=...)` refused a non-semver tag
  (`central/content_catalog/catalog.py`, pre-bead `:122-126`). A sha-keyed job no longer can, so
  a row whose tag passes the DB check (`tag ~ '^v[0-9]+\.[0-9]+\.[0-9]+'`, prefix only,
  `central/migrations/016_release_tracking.sql:20`) but not `release_version` (e.g. `v1.2.3foo`)
  would have become a netboot substitute and desired through a device's roles.
  **Resolved (coordinator, regression-preservation):** today's behaviour is kept. The rule lives
  in ONE place, `_os_image_job(row)`, which returns None for a tag `release_version` refuses;
  `_resolve_base`, `desired_in` and `pin` all build the OS-image job through it, and `pin` still
  refuses the tag up front (`invalid_tag`). Test:
  `tests/test_content_catalog_catalog.py::test_a_legacy_tag_is_never_a_netboot_candidate_nor_desired`
  (PostgreSQL). Probe M7 (drop the filter) turns it red: the legacy tag is offered as a
  substitute and its image becomes desired.
- **`FetchOsImageHandler._write(temp, locator)` has no key to ask `CacheStore.temp_path` for the
  tarball temp.** It now downloads to `<temp>.tar.gz` beside the unique temp (same directory, same
  `TEMP_PREFIX`, created `O_EXCL` by the download, removed in `finally`). The constructor keeps
  `store` (for `discard`), so `central/content_wiring.py` is unchanged.
- **Additive, not on the page:** `ReleaseCatalog.resolve` gained a `typing.overload` so a
  `NetbootBaseRequest` statically yields `NetbootCandidates | Unknown`, which is what
  `record_served(request, resolution, job)` takes from the route. `central/kernel/types.py`
  `ReleaseTag` now has no user; it is kept.
- **Fix cycle 1 (high-risk review of 028): the page's rollback was wrong.** It said "revert the
  code, then `UPDATE app_release_poll SET etag = NULL`". That strands new-shape
  `{"tarball_sha256": ...}` deliveries, which the reverted code cannot decode (the mirror image of
  028 step 4). 028's header now gives: (a) cancel `todo` and fail `doing`
  `photo_wall.os_image.fetch` rows; (b) drop the new CHECK; (c) revert the code; (d) clear the
  ETag. Roll forward: repeat (a), then `DELETE FROM schema_migrations WHERE name =
  '028_os_image_content_key.sql'`. Step (b) goes beyond the coordinator's text: the reverted code
  writes tag-keyed references whose locator sha is the tarball's, which the CHECK refuses, so its
  sync would fail. The header's "the runbook deletes them at upgrade" was premature; it now says
  that bead 5 adds that step to the runbook.
- **Fix cycle 1: the page left fall-through unguarded in the schema.** 028 now adds
  `asset_references_locator_names_the_key CHECK (locator_sha256 IS NOT NULL AND locator_sha256 =
  identity)` for both kinds. Every production writer already satisfies it: 021's seeds, the
  sync's two `reference` calls (each keyed by its locator's sha) and
  `scripts/test_netboot_e2e.py:159-162`. The CHECK is replaced (`DROP ... IF EXISTS`, then `ADD`),
  so 028 is safe to run twice. Test fixtures with NULL or mismatched locator shas were fixed.

## 2026-09-24 — bead 4 rescue-lock-free

- **The page's signature is wrong.** `_lock(job_type) -> str | None` cannot return
  `job_keys(job).lock`: it has no job to key. The code has `_lock(job: Job[Any]) -> str | None`,
  which returns the key when `type(job).asset_kind is not None`, else None. `_register` passes it
  the field-less tick `job_type()`, and `_deferrer` passes the published job. The behaviour is
  what the page freezes.
- **Stale text outside this bead's file set (for bead 5, docs; not edited here):**
  - `central/infra/queue_ops.py:7-10` says the re-published copy "runs only after the close
    frees the lock". That is now true only for asset fetches. A non-asset copy is fetchable at
    once. The ordering of re-publish then close still holds.
  - `central/infra/queue_ops.py:93` (`close`): the words "which frees its lock for the new copy"
    are now true only for asset fetches.
  - `central/kernel/jobs.py:199`: the `JobKeys.lock` comment "one RUNNING copy fleet-wide" now
    holds only for asset jobs. The page forbids a kernel change.
  - `central/kernel/publishing.py:11-12`, PB4: "a running copy is joined, not duplicated" now
    holds only for asset jobs. A non-asset publish while a copy runs inserts a pending copy that
    may run alongside it. Waiters are unaffected, because they resolve on the first outcome newer
    than `since`.
  - `docs/central-system-architecture.md:28`, `:79` and `:116-117` still say every job's `lock`
    is one running copy.
- No file outside the set turned red. The full suite, ruff, lint-imports and check_docs pass;
  `test_registry` is the known local failure.

## 2026-09-24 — bead 2 reader-data-first

- **M1's predicted output is wrong for this bead's own code.** The page says that with M1 (the
  reader consults the outcome before `_open_first` in `read`), D1 shows "a publish happens, and
  the result is a 503". Probed: D1 turns red on the publish (`Call(FetchPackage, retry_terminal=True)`
  where `[]` was expected). The result is not a 503: the frozen step 1 (open the data again after
  any wait) serves the file once the wait times out. A 503 appears only if M2 is applied too. The
  probe still guards the right thing; only its expected symptom is wrong.
- **D2 as worded can race.** "Facts and file appear while the waiter waits, and the outcome it
  gets is `terminal`". If the facts come with their own `ok` (rule 3: `ok` arrives with its data),
  the waiter can wake on that `ok` before the late `terminal` commits. That tests the `Ready` path
  instead, and under M2 it fails as `absent_after_ready`, not a 503 `terminal`. D2 therefore
  records facts and the file with no `ok` (a lost result write, N3), then the `terminal`, so the
  outcome the waiter gets is `terminal` deterministically.
- **"Existing tests it carries" matched nothing.** No test in `tests/test_assets_reader.py`
  asserts a `Failed` or `Pending` result while the asset's facts and file are present, so that
  file is unchanged. Its `RecordingPublisher` `Ready` tests still pass: `record_outcome` writes
  the facts to `AssetRecords`, so the post-wait `_open_first` finds them.

## 2026-09-24 — bead 3 catalog-follows-upstream

- **Owner decision: a manifest read but INVALID is unversioned, exactly as one not read.** The
  frozen page (§Origin) set `upstream_version` whenever `_fetch_manifest` returned a body, so a
  NEWER broken upload (bad JSON, a non-object, `schema` other than 1, or a malformed
  `player_deb`: every `manifest_invalid` / `schema_mismatch` case) was applied and took a
  working release's `.deb` from the Pis. Now `_parse_manifest` (`central/origins/github.py`) is
  the one place a version is set, and only on a valid AND complete manifest; any other is
  refused over a stored observation, so the last good one stays.
- **Owner decision, same principle: an incomplete upload (`asset_missing`) is unversioned too.**
  A valid manifest naming a `.deb` the release does not attach (typically a release caught
  mid-upload) must never remove a working `.deb`. Once the `.deb` is attached, the next sync
  versions the same manifest asset and applies it normally (it is newer than the stored row).
  Tests: `test_origins_github.py::test_a_read_but_invalid_or_incomplete_manifest_is_no_version`,
  `test_release_versions.py::test_v9b_a_newer_invalid_manifest_cannot_wipe_the_tag` (probe M9)
  and `::test_v9c_an_upload_caught_midway_cannot_wipe_the_tag_and_applies_once_complete`
  (probe M10). Residual: a FIRST observation of a new tag has no stored row to protect, so it is
  inserted whatever its manifest says and repaired by the next valid one. Bead 5 (docs) should
  state this in design §6.3.
- **Review fix cycle 1 (integrated branch, 69f0336). P1: the frozen flag raced a concurrent
  `FetchPackage`.** `_frozen_package` read the old `.deb`'s produced facts through the unlocked
  `AssetRecords.get`, so a `record_produced` committing between that read and the sync's commit
  left the tag unfrozen at the re-cut although the old bytes were servable. The port gains ONE
  locking read, `AssetRecords.lock_produced(tx, key) -> AssetReady | None` (`FOR SHARE` on the
  asset row, conflicting with `record_produced`'s UPDATE); the recording either committed before
  the read and freezes the tag, or waits for the sync and lands after the re-cut was taken.
  Lock order in a release transaction: release row (`claim`, FOR UPDATE) -> the old `.deb`'s
  asset row (FOR SHARE) -> `reference` inserts (FOR KEY SHARE via the foreign key). No cycle:
  `record_produced` locks only its asset row and then `job_outcomes`; `touch_served` runs alone;
  the tail takes only the advisory lock and the policy row. Test V11 (both orders), probe M11.
- **P2: nothing tested `claim`'s FOR UPDATE on an existing row.** Test V12, probe M12. **The
  review's staging was insufficient on its own:** with the holder paused AFTER `apply`, the
  waiter blocks already in `INSERT ... ON CONFLICT DO NOTHING` (on the holder's uncommitted row
  version; probed on PostgreSQL 16), so an unlocked SELECT would still read the new row and the
  test stays green without FOR UPDATE. V12 therefore also pauses the holder right after `claim`
  (row locked, not yet written): there an unlocked read returns the stale row at once, and M12
  turns that case red. Both stagings are kept.
- **P2: comments.** `central/kernel/ports.py` (`PublishedRelease.upstream_version`) and 029's
  header now say "valid and complete"; design §3 (glossary) and §6.1/§6.3 are listed for bead 5.

## netboot-reach-central p1 S0 (liveness)

- 2026-09-26, bead S0: the frozen page (slices.md §S0 "Removed") says `reboot_path_watched`
  in `appliance/bootstrap.py` is replaced by `missing_kernel_liveness`. Reality: no
  `reboot_path_watched` symbol exists anywhere in the tree (`grep -rn` over `*.py` from repo
  root finds none, before this bead's changes). Evidence: `git grep -n reboot_path_watched`
  (pre-change tree) returns nothing. Proposed correction: drop that clause from the "Removed"
  line; nothing needed removing beyond `LinuxOps._watchdog_fd`, which was removed as specified.

## netboot-reach-central p1 S1 (tracer: locate over verified TLS)

- 2026-09-26, bead S1: design §2.4 lists `Trust`'s verify flags as `VERIFY_X509_STRICT |
  VERIFY_X509_PARTIAL_CHAIN` under "every setting explicit". Reality: Python's
  `SSLContext(PROTOCOL_TLS_CLIENT)` starts with `VERIFY_X509_TRUSTED_FIRST` (OpenSSL's own
  default since 1.1.0), and assigning `verify_flags` replaces the set, so the literal list would
  CLEAR trusted-first, which lets a local anchor end a chain the gateway cross-signed (the Root
  YR / ISRG X1 case). Likewise `hostname_checks_common_name` defaults to True on the CI's
  Python 3.12.11 + OpenSSL 3.0.16 (probe: `SSLContext(PROTOCOL_TLS_CLIENT)
  .hostname_checks_common_name` is True). Implemented: flags `TRUSTED_FIRST | STRICT |
  PARTIAL_CHAIN`, `hostname_checks_common_name = False`, `maximum_version =
  MAXIMUM_SUPPORTED`, each asserted by `tests/test_uplink_tls.py`. Proposed correction: design
  §2.4's docstring lists all three flags and the SAN-only name check.
- 2026-09-26, bead S1: the glossary's Central root says both "no ... whitespace or control
  characters" and "this is today's validator, unchanged". They disagree: today's stage-1 check
  (`appliance/netboot_init.py` `_validate_central_root`, `ord(character) <= 32`) admits DEL
  (0x7f) and non-ASCII whitespace such as U+00A0. `uplink.origin` follows the glossary's words:
  every character must be printable and not whitespace (`str.isprintable`, `str.isspace`), in
  the root and in a redirect's Location alike. Everything else (userinfo, empty-port, empty
  query or fragment handling) matches today's check exactly. Proposed correction: the glossary
  drops "unchanged" or names the tightening; S4b's stage-1 adoption inherits it.
- 2026-09-26, bead S1: the frozen page gives `uplink/origin.py` only `Origin` and `Url`, but
  check 3 of `next_hop` must apply the same URL grammar as `Origin.parse_root` to a resolved
  Location, and step 2 must see the raw Location before `urlsplit` strips leading space and
  drops tabs. Implemented one public `parse_url(text, *, base=None) -> Url | None` in
  `uplink/origin.py` that both use, rather than a second grammar in `redirects.py`. Proposed
  correction: add `parse_url` to S1's frozen page.

## netboot-reach-central p1, fix cycle 1 (S2-S6 implemented)

- 2026-09-26, bead S3: the frozen page's S3-AC3 row "offset -3600 -> not stepped, AHEAD" and
  probe "allow a negative step -> the AHEAD row fails" predate the owner's Q5 = A. Reality:
  Q5 = A amends R6 to "one SNTP step, never below the floor". Implemented in
  `uplink/clock.py` `ClockGate._apply`: offset < -0.5 steps back (SYNCED, stepped, negative
  offset); AHEAD only when clock + offset < floor. Evidence:
  `tests/test_uplink_clock.py::test_the_one_step_follows_the_offset_but_never_goes_below_the_floor`
  (rows -3600 -> SYNCED stepped, -2 days from floor+1 day -> AHEAD). Proposed correction:
  S3-AC3 and its probe read "step below the floor -> the AHEAD row fails". Likewise
  `POOL_HOST` is `debian.pool.ntp.org` (Q2 = A), not the page's `pool.ntp.org`.
- 2026-09-26, bead S3: parked §B says "the era is chosen as the one >= floor". With that rule
  `below_floor` can never fire (a time just under the floor decodes 136 years later, as
  `above_ceiling`). Implemented: the era nearest the floor (`uplink/sntp.py` `_unix`), so both
  reasons are reachable and every time within 68 years of the floor decodes right. Evidence:
  `tests/test_uplink_sntp.py` rows below-floor, above-ceiling, and the 2036-wrap row.
  Proposed correction: parked §B "the era nearest the floor".
- 2026-09-26, bead S3: the clock record's `tried` entries land on the console FAILED line, so
  they are single tokens: `timeout`, not design §7's `no answer`; a failed pool lookup is
  `pool:lookup:<errno or cause_reason>`, a refused step `floor:EPERM` / `step:EPERM`. The gate
  records (never raises) a step the kernel refuses. `ClockRecord` validates every field at
  construction (ValueError) and gained `summary()`. Proposed correction: design §7 example 7's
  text uses `pool:timeout`.
- 2026-09-26, bead S2: `DirectFetch` maps a declared `Content-Length: 0` to `transfer`/`short`
  (AppFetcher raised its limit code). A malformed or over-bound length stays `limit`. It never
  reads past a declared length. A 3xx detail is `status=<n>;location=<host>` (one console
  token). A Central error body's code is also the detail (design §7 example 15). Evidence:
  `tests/test_uplink_fetch.py::test_transfer_failures_are_named`.
- 2026-09-26, beads S3/S0: `RunClockRecord` and the S0 keeper's hand-over need the same atomic
  0644 write. One `uplink/files.py` `write_atomically` now serves both
  (`appliance/bootstrap.py` imports it; appliance -> uplink is allowed). Not on either frozen
  page. Proposed correction: add it to S3's page.
- 2026-09-26, bead S4a: the frozen page names `compute_closure`, `stage`, `main`,
  `INITRD_FORBIDDEN`. Needed in addition: `INITRD_ROOTS` (the one root list, beside the one
  forbidden list), `first_party_packages(repo)` (top-level dirs with `__init__.py`, so no hand
  list of first-party names), `search_path()`, `Manifest`/`write_manifest`/`read_manifest`
  (the verifier reads what the build wrote), and `initrd_closure()`. Two rules the page did
  not state: (1) a MISSING name is judged only when first-party code imports it -- the stdlib's
  own optional imports (`pdb`/`bdb` import `__main__`) are not ours; (2) a FOUND module that is
  neither first-party nor stdlib is refused too, so the guarantee does not rest on the search
  path alone. Evidence: `tests/test_module_closure.py`. `build_boot_data.main` also takes an
  optional `--snapshot-epoch` for Q3 = A's 90-day warning.
- 2026-09-26, bead S4b: design §5 "the verifier finds the boot-data files in the early archive
  and none of them in the cached archive". `lsinitramfs` does not say which archive a path came
  from, so the verifier parses the leading newc archive itself (`build_boot_data.read_archive`,
  next to the writer) and runs `lsinitramfs` on the remainder only. The early archive is
  zero-padded to a 512-byte block, as cpio writes Debian's microcode archives.
- 2026-09-26, bead S4b: `INITRD_FORBIDDEN` includes `media`, and a cached initrd can hold
  kernel modules under `.../drivers/media/`. The verifier therefore applies the forbidden set
  to the package a path installs (the component after its innermost `python3*`,
  `dist-packages` or `site-packages` directory), not to every path component as the old
  `FORBIDDEN_COMPONENTS` did. Evidence: `tests/test_verify_netboot_initrd.py` (the golden
  listing holds `drivers/media/rc/rc-core.ko`; `usr/lib/python3.13/media/__init__.py` fails).
- 2026-09-26, bead S4b: design §2.7 "stage 1's own content codes print in the same format".
  Implemented: `FAILED phase=<n> code=<netboot_code>` (and the mount's `boot_*` codes); a
  non-network error prints only `error=<Type>`. A setup failure is `phase=setup`, and the
  phase-0 line is printed by `netboot()` from the provenance `main()` built. Phases are
  numbered 0-7 (`phase N/7`); a failure names the phase in progress, not the last line
  printed. `Unconfigured("no_cmdline")` fails as `configuration`/`absent detail=no_cmdline`.
- 2026-09-26, bead S4b: the kernel-config symbol list (S0's four plus S4b's five) is one
  tested constant, `scripts/kernel_config_check.py` `STAGE1_BUILTINS`, which `--symbol`
  defaults to, instead of the same nine `--symbol` flags written twice in `base-image.yml`.
  The job's `DEBIAN_SNAPSHOT_EPOCH` env replaces the two literal `SOURCE_DATE_EPOCH` values.
- 2026-09-26, bead S5: `scripts/uplink_device_harness.py` must run under `python3 -I -S` with
  only the closure, so its stand-in servers are stdlib. To keep ONE implementation,
  `tests/tls_fixture.py`'s `serve_stub`/`redirect_stub`/`central_stub` now delegate to the
  harness's (the fixture adds the minted leaves). The netboot-e2e path filter also lists
  `appliance/__init__.py`, `appliance/bootstrap.py` (closure members) and `tests/tls_fixture.py`
  (the mint source), beyond the page's list. `compare_trust_bundles` compares against
  `INITRD_CA_BUNDLE` (a module constant equal to `DEBIAN_CA_BUNDLE`, so tests do not read the
  host's bundle).
- 2026-09-26, beads S1/S2 (open, frame-level; not changed here): a direct request's wait for
  the status line is the transport's `HOP_TIMEOUT` (5 s; S1 page, `uplink/transport.py`),
  because `Transport.send(url, *, headers, deadline)` takes no per-call bound. Central holds a
  base miss up to its read-through `wait_timeout` (30 s, `central/assets/reader.py:116`) before
  answering 503, and AppFetcher waited `min(10, remaining)`. So on a real miss the Pi prints
  `cause=connect reason=timeout`, not `cause=central reason=error`, and a base that becomes
  ready 5-10 s into the wait is no longer received (the next boot retries). The wire test
  passes only because it shortens Central's wait to 1 s. Proposed correction: `Transport.send`
  takes the hop bound (locate keeps 5 s; `DirectFetch` passes one covering Central's
  read-through wait), or Central's base route answers a miss at once. Needs an owner ruling on
  the S1 frame before a re-cut.
- 2026-09-26, beads S1/S2 (resolves the open S1/S2 entry above; architect's choice, no owner
  question): `Transport.send(url, *, headers, deadline, status_timeout=HOP_TIMEOUT)`.
  `status_timeout` bounds each address's exchange up to and including the status line; the TCP
  connect and the TLS handshake still get at most `HOP_TIMEOUT` of it, so a dead address falls
  through to the next as fast whatever the caller's bound. `locate` keeps the default (5 s).
  `DirectFetch` passes `STATUS_TIMEOUT = READ_THROUGH_WAIT_SECONDS + HOP_TIMEOUT` (35 s), still
  capped by its deadline. `READ_THROUGH_WAIT_SECONDS` (30) is one stdlib-only constant in the new
  `contracts/read_through.py`; `AssetReader`'s default `wait_timeout` is derived from it, so
  Central's wait and the device's bound cannot drift apart. The S1 page's `Transport`/
  `HttpTransport.send` signatures and the HOP_TIMEOUT comment ("connect, TLS, request write,
  status line") read with this change. The netboot wire miss test now holds Central's answer
  `HOP_TIMEOUT + 1` s instead of 1 s. Review fix in the same change: `_Connection.connect`
  re-applies what is left of the hop before the TLS handshake and before the request write
  (the handshake and an http request write ran under the pre-connect timeout).
- 2026-09-26, bead S4a (CI fix; narrows rule 2 of the S4a entry above): rule 2 now matches
  rule 1 -- a FOUND module that is neither first-party nor stdlib is refused only when
  first-party code imports it. `modulefinder` followed imports into the stdlib itself, and
  `multiprocessing.util` imports `test.support` -> `_testcapi`; an interpreter that ships its
  test suite (the runners' hosted-toolcache CPython, Homebrew's) failed the closure, one
  without it (python-build-standalone) passed. `_Finder` now scans first-party code only: a
  stdlib module first-party code imports is still found and judged, its own imports are not
  (the initramfs hook copies the whole stdlib tree anyway). The closure is identical on both
  kinds of interpreter. Evidence: `tests/test_module_closure.py`
  (`test_the_stdlibs_own_imports_are_not_judged`,
  `test_a_found_non_stdlib_module_imported_by_first_party_code_is_refused`). Not covered: the
  judgement uses the build host's `sys.stdlib_module_names` (3.12 in CI), not the device's
  3.13, so a first-party import of a module 3.13 removed passes here; only the tracer's
  device-runtime leg catches it.
- 2026-09-26, bead S0 (CI fix; the page is wrong): the S0 page's `photowall_restart` probes
  the watchdog with a guarded `: >/dev/watchdog0`, and `mountroot` probed `/dev/console` the
  same way. `:` is a POSIX special built-in (XCU 2.8.1): a redirection error on it exits a
  non-interactive dash, klibc sh or busybox ash even inside an `if`, so an unopenable first
  watchdog path ended /init (PID 1) before the fallback path and the sysrq write. bash (macOS
  `sh`) tolerates it, which is why the tests passed locally. Both probes now go through one
  helper, `photowall_can_open() { ( : >"$1" ) 2>/dev/null; }`; the child still opens and
  closes the device. `tests/test_boot_script.py` runs under `dash` (required on Linux) and
  `busybox sh` (where installed), never the host `sh`. Proposed correction: the S0 page's
  probe reads `photowall_can_open`. Not covered: nothing tests that `mountroot`'s console
  probe calls the helper rather than a bare `: >`; the helper's own test covers the mechanism.
- 2026-09-26, bead S4b (CI fix): `test_main_splits_a_real_initrd` built an UNCOMPRESSED
  cached archive; mkinitramfs compresses it (trixie: zstd), and `unmkinitramfs --list` takes an
  uncompressed archive for an early one and then fails on the empty remainder (exit 2). The
  fixture is now `gzip`-compressed (same decompress-then-list path). `verify_netboot_initrd`
  now reports a cached archive `lsinitramfs` cannot list as a contract FAIL ("the cached
  archive could not be listed: <stderr>") instead of a CalledProcessError traceback.

## netboot-reach-central p2 S1a

- 2026-09-26, bead S1a (test plan wrong): "`tests/test_netboot_e2e_wire.py` untouched unless it
  imports `trust_provenance`" does not hold. It drives the old `Bootstrapper(discovery=...,
  write_origin=...)` and `fetch_manifest(origin, serial=...)`, and imports `_FixedDiscovery`
  from `scripts/test_netboot_e2e.py`, which itself imported the removed `write_public_config`
  (so `import scripts.test_netboot_e2e` failed). Both are migrated here (real `find_central`
  with a `Configured` root, `DirectFetch` to the located origin); the e2e now asserts the
  handoff is exactly `{"schema": 1}` (a cmdline root is never handed off, `allow_http` never
  written). S1c's e2e rewrite starts from this. DB and docker legs not run locally.
- 2026-09-26, bead S1a (plan gap, needs an owner bead): after S1a `player.service` imports
  `uplink` (and `contracts.central_identity`/`clock_record`/`strict_json`). (1) The Player
  `.deb`'s hand list (`scripts/build_player_deb.py` `_MODULE_FILES`) does not stage them, so
  the netboot-e2e import smoke (`import player.service` in the installed `.deb`) fails in CI
  until S5's computed closure lands; they cannot be added to the hand list now because
  `fetch_sources` git-archives HEAD, which lacks `uplink/finder.py` and `uplink/diagnosis.py`
  until the commit. (2) The demo wall's Player wheel is built by `scripts/build_player.py`,
  whose `archive_sources` / `make_player_wheel` / `validate_player_wheel` allow only `player/`
  and `contracts/`, so software-e2e's Player container cannot import `uplink`. S4's page
  ("pyproject.toml hatch packages += uplink (the demo wall's Player imports it)") names the
  wrong mechanism: hatch does not build that wheel. Proposed correction: S4 (or a new bead)
  adds `uplink` to `build_player.py`'s archive paths, wheel filter and boundary regex, with
  `tests/test_build_player.py`. The demo runner (`PLAYER_RUNNER`) already passes
  `find_central` through `player.service.central_finder`; `tests/test_wall_demo.py`'s runner
  import boundary now admits `uplink`.
- 2026-09-26, bead S1a (scope note): deleting `player/discovery.py` required removing it from
  both `.deb` builders' fixed lists, their tests and `base-image.yml` (cache key and a
  `require_path`). `tests/observer_clock_client.py` still builds `PlayerService` without
  `find_central`; left alone: nothing references it and it already targets the retired
  `/v1/bootstrap/boot` route. Between S1a and S4 a failed locate faults as
  `connection_failed` (was `central_origin_unavailable` when discovery found nothing); S4's
  naming replaces it.

## netboot-reach-central p2 S1b

- 2026-09-26, bead S1b (AC wording wrong): AC5's "the 9 code-map modules plus `uplink.finder`,
  `uplink.diagnosis` and their imports" is not the closure. S1a's `appliance.provision` also
  imports `uplink.fetch` (DirectFetch) and `uplink.clock` (RunClockRecord), which bring
  `uplink.sntp`, `contracts.read_through` and `contracts.time` (27 modules in all).
  `tests/test_package_closures.py` asserts the stated set is a subset, the top-level packages
  are exactly appliance/contracts/player/uplink, `player` contributes only
  `player.mdns_discovery`, `third_party == ("zeroconf",)` and nothing is unreached.
- 2026-09-26, bead S1b (AC11 scope): the shrinking allowlist in
  `tests/test_debian_packages.py` holds three kinds of line, not only workflow lines:
  base-image.yml's rolling `mmdebstrap` mirror (S3), two DOCSTRING lines of
  `scripts/test_netboot_e2e.py` naming `deb.debian.org` (S1c rewrites them) and the layer's
  `packages:` key (S3). A stale entry fails the test, so S1c must delete its two entries and S3
  the rest (S3 AC1: empty).
- 2026-09-26, bead S1b (plan gap for S5): `fetch_tree` is defined in
  `scripts/build_bootstrapper_deb.py` as the page says, but that module imports
  `build_player_deb` (for `control_file`/`run_dpkg_deb`), so S5's Player builder cannot import
  it back without a cycle. S5 should move `fetch_tree` next to `control_file` in
  `build_player_deb.py` and have the bootstrapper import it (no second copy).
- 2026-09-26, bead S1b (decision): the policies' third-party tables and both Depends come from
  the IMPORTED declaration, so `fetch_tree` shipping `scripts/debian_packages.py` alone would not
  make "computed over exactly the committed sources" true for the Depends. `build()` therefore
  refuses (`declaration_differs_from_revision`) a revision whose declaration differs from the
  one the builder imported. In CI the checkout is the revision, so they are equal.
- 2026-09-26, bead S1b (note): `isolated_import` asks the interpreter for its site-packages
  with a separate `python -I` call, then imports under `python -I -S -B`: under `-S` a venv
  interpreter's `sys.prefix` is the base install, so `site.getsitepackages()` inside the `-S`
  child misses the venv. `-B` keeps bytecode out of the staged tree.
- 2026-09-26, bead S1b (CI between beads): `base-image.yml` still `require_path`s
  `usr/lib/python3/dist-packages/appliance/provision.py` and hashes the old fixed file list for
  cache key A; the private layout breaks that check until S3 rewrites it (beads verify
  together). Not run locally (Linux CI only).

## netboot-reach-central p2 S1c

- 2026-09-26, bead S1c (page gap, decided): the page gives `device_root_image` but no way for
  the workflow to call it before the stage-1 harness step, which must run in the same image.
  `scripts/test_netboot_e2e.py` now has subcommands: `device-root --arch --tag --cache` (prints
  the image) and `run ... --device-root IMAGE --bootstrapper-deb PATH --deb PATH`. mmdebstrap
  runs as root (`sudo -n` when not root: unprivileged user namespaces are restricted on
  ubuntu-24.04), and the runner installs `debian-archive-keyring` beside `mmdebstrap` (on an
  Ubuntu host mmdebstrap can add `signed-by` for the snapshot sources only if that keyring is
  present). The cached tar's name carries a digest of the exact mmdebstrap argv, so a changed
  declaration never re-imports a stale root even outside actions/cache. Job timeout 45 -> 75
  min for a cold root (parked U14). None of this ran locally (Linux/docker CI only).
- 2026-09-26, bead S1c (scope note): the cross-host same-path 301 stub reuses
  `scripts/uplink_device_harness.redirect_stub` with a new `keep_path=False` flag rather than a
  second handler; the harness docstring now says it runs in the device root.
- 2026-09-26, bead S1c (known red until S5, restated): the e2e keeps today's
  `assert_landed`/import smoke (dist-packages paths, `import player.service`), now in the
  device root. Per S1a's errata the Player `.deb` hand list lacks `uplink`, so the smoke fails
  in CI until S5 stages the computed closure (S5 also moves those paths to
  `/usr/lib/photo-wall-player`). AC3's checks run before it.
- 2026-09-26, bead S1c (docs, for S6): `docs/validation.md` still says the device harness runs
  in `debian:trixie-slim`; it now runs in the device root image.

## netboot-reach-central p2 S2

- 2026-09-26, bead S2 (decision): `resolver_writers` flags a package in RESOLVER_WRITERS by
  name OR by a virtual name it Provides (e.g. any `resolvconf` implementation), a superset of
  §2.8's "installed packages in RESOLVER_WRITERS"; same reader, same shape as `time_daemons`.
- 2026-09-26, bead S2 (decision): `read_dpkg_status` raises FileNotFoundError on a root with no
  `var/lib/dpkg/status` (not a Debian root, so no dpkg check can pass); `main` then exits
  non-zero with a traceback, not a violation line. Every planned `main` root has a database
  (base extract, S5's post-install root); the `.deb` staging trees call `watchdog_overrides`
  directly.
- 2026-09-26, bead S2 (page gap, decided): `--require-installed FILE` takes names separated by
  whitespace or commas, so S3 can pass `debian_packages.py packages ...` output and a
  `dpkg-deb --field <deb> Depends` value unchanged (our control files carry no versions; a
  versioned token would be reported missing, failing closed).
- 2026-09-26, bead S2 (scope note): both builders now import `scripts/device_root_checks.py`,
  so `netboot-e2e.yml`'s PR path filter gains it (S1b's precedent for `debian_packages.py`);
  `base-image.yml`'s filter and cache keys stay S3's.
- 2026-09-26, bead S2 (note): the checks read every file inside the root: a symlinked FILE with
  an absolute target (or `..` past the top) is re-anchored at the root, never read from the build
  host; a symlinked parent DIRECTORY with an absolute target is not (Debian's merged-/usr links
  are relative). `lib/` and `usr/lib/` drop-ins are reported once.
- 2026-09-26, bead S2 (verifier note): mutation probe (b) must strip the comment marker
  (`line.strip().lstrip("#; ")`); merely deleting the comment guard survives, because
  `#RuntimeWatchdogSec` is not a WATCHDOG_KEYS key anyway.
- 2026-09-26, bead S2 (docs, for S6): the phase-7 success line is now
  `phase 7/7 mount + handoff: success dns=<a,b> search=<x>` (`dns=none` with no stage-1
  resolver); stage 2's `/etc/resolv.conf` is stage 1's copy, 0644, at most 4096 bytes.

## netboot-reach-central p2 S3

- 2026-09-26, bead S3 (page wrong, AC3 evidence): the scratch root's mmdebstrap log goes to
  `$DIAG/mmdebstrap.log`, which is uploaded only on failure, so a green run cannot show "only
  the snapshot for Debian" from it. The step now prints the root's apt sources (before the
  Raspberry Pi line is added) and keeps them as `$DIAG/scratch-sources.txt`; read AC3 there.
- 2026-09-26, bead S3 (page wrong, AC4 placement): the bundle verify step already printed the
  squashfs size and failed above the ceiling, from a hardcoded copy of `MAX_ROOTFS_BYTES`. It
  now reads `contracts.release.MAX_ROOTFS_BYTES` (stdlib only) and prints
  `squashfs size: N bytes (MAX_ROOTFS_BYTES M)`; the content check does not print it a second
  time.
- 2026-09-26, bead S3 (page gap, decided): Cache A's "<builders>" = `build_bootstrapper_deb.py`,
  `build_player_deb.py`, `build_player.py`, `module_closure.py` (it generates `__main__.py` and
  `closure.json`), `device_root_checks.py` (imported by the builder), plus
  `appliance/systemd/photo-wall-provision.service`: the unit is baked into the `.deb` but is
  not in the closure digest. The digest step runs `python3 scripts/module_closure.py --policy
  bootstrapper --digest` as the page says. The PR filter also keeps `build_player_deb.py` and
  adds `build_player.py` (both builders import it).
- 2026-09-26, bead S3 (decision): the old "dpkg records photo-wall-bootstrapper as installed"
  grep matched `Package:` and `Status: install ok installed` anywhere in the file, not in one
  stanza. It is replaced by a third `--require-installed` (a one-line list) on the same
  `device_root_checks` call, so one parser decides "installed".
- 2026-09-26, bead S3 (decision): design §2.10 says both `.deb` builds get
  `SOURCE_DATE_EPOCH`; the page is silent. The Player build now gets it too. The scratch-root
  host install adds `debian-archive-keyring` beside `mmdebstrap` (S1c's finding for the same
  `mmdebstrap_argv`). The extract runs `unsquashfs -no-xattrs`: an unprivileged extract cannot
  set `security.*` xattrs, which squashfs-tools reports with exit 2.
- 2026-09-26, bead S3 (design wording): "the rpi-image-gen tree names no Debian package" is
  tested over the device set (`packages(*DEVICE_CONSUMERS)`), not every declared name:
  genimage's `compression = zstd` in `image/rootfs.cfg.in` is a tool option that matches the
  initrd-build package `zstd`.
- 2026-09-26, bead S3 (not run locally): the base build, `device_root_checks` on the extract,
  the scratch root at the pin, the kernel config check and the squashfs size (AC2-4) are
  Linux CI only. The page's `rm -f "$1/etc/resolv.conf"` in `pre-image.sh` assumes the hook can
  write into the target; CI proves it (the content check refuses `etc/resolv.conf`).

## netboot-reach-central p2 S6 (docs)

- 2026-09-26, bead S6 (bug fixed, design §0.1): The Player could not read the handoff:
  provisioning wrote /etc/photo-wall/public.json 0600 in a 0700 directory under the provision
  unit's UMask=0077, and uplink.files.write_atomically created parents 0700 under that umask.
  Now the handoff is written with mode 0644 and write_atomically chmods the parents it creates
  to 0755.
- 2026-09-26, bead S6 (withdrawn): Project 1's 'ClockSettler for provisioning' is withdrawn.
  Only stage 1 steps the clock and writes /run/photo-wall-clock.json; provisioning and the
  Player read it. A `time` failure exits provisioning into the unit's start-limit reboot path.
  The ClockRecord.writer comment no longer says 'Project 2 adds provision'.
- 2026-09-26, bead S6 (plan wrong): 'The Player fetches through locate + DirectFetch' did not
  hold: DirectFetch is synchronous http.client and the Player is asyncio with httpx and
  websockets. The Player keeps its libraries behind player/central_link.py (one Trust, no
  redirects, named failures), with locate run via asyncio.to_thread. websockets 15 followed
  cross-host redirects and re-sent the bearer; DirectWebsocket refuses them.
- 2026-09-26, bead S6 (reversed): The bootstrapper .deb's ban on `contracts` is reversed:
  uplink needs contracts, and each .deb ships its computed closure privately under
  /usr/lib/<package>/. This supersedes the p3-base-bootstrapper note that the bootstrapper
  must not ship contracts.
- 2026-09-26, bead S6 (superseded): The owner's 2026-09-26 steer ('unify the package sources
  and lists that go into the base vs the runtime package') supersedes 0008 delivery ledger
  Phase 4 ruling (2) ('deps pulled from the distro repo at boot, base stays minimal') and
  Project 2 draft 1's Q2 (apt with the Release date check off while unsynced). S1a's apt path
  was replaced in S1c by `dpkg --install` alone and never shipped.
- 2026-09-26, bead S6 (superseded): DEBIAN_SNAPSHOT_EPOCH in base-image.yml is now derived
  from scripts/debian_packages.py (`python3 scripts/debian_packages.py epoch`). This
  supersedes the p1 S4b note that the job's DEBIAN_SNAPSHOT_EPOCH env replaces the literal
  SOURCE_DATE_EPOCH values.
- 2026-09-26, bead S6 (docs): docs/validation.md now says the netboot-e2e device harness runs
  in the device root built at the pin, not debian:trixie-slim (S1c's docs note).
- 2026-09-26, bead S6 (decision, S4): DirectWebsocket.process_redirect returns websockets'
  own InvalidStatus unchanged (never a URI), and Exchange.name maps it through
  uplink.fetch.refusal with the Location host. That is one mapping for DirectFetch, httpx and
  the websocket, instead of raising refusal inside process_redirect as the design's docstring
  said.
- 2026-09-26, bead S6 (decision, S4): A run-loop failure that is neither a ServiceError nor a
  network error faults as `player_error` (detail: the exception type). The design named no
  code for it.

## netboot-reach-central p2 S4-S5 (integration wave)

- 2026-09-26, bead S4/fetch (test spec wrong): the brief's
  `test_refusal_leaves_out_a_location_that_does_not_parse` asked for `location in (None, "",
  "ftp://x/")` to all produce `detail == "status=307"`. That does not hold for `None`/`""`:
  `refusal()`'s existing `parse_url(location or "", base=url)` resolves an absent/empty
  Location by joining `""` onto the request URL via `urljoin`, which returns the request's own
  URL -- a URL that DOES parse, giving `detail="status=307;location=<request host>"`, not the
  bare status. This is pre-existing behaviour in `refusal()`, which the brief said not to
  alter. Fixed by parametrizing only `"ftp://x/"` (the one value that genuinely fails to
  parse), with an inline comment. Flagging for a design decision: a bare 3xx with no Location
  header is currently reported as "redirecting to itself" rather than "no location" -- worth
  an explicit `if location:` guard in `refusal()` if that distinction should be visible to
  operators.
- 2026-09-26, bead S4/link (page wrong, test-only workaround): the AC3 websocket-redirect test
  as specified does not work against real sockets for two reasons: the stub gateway answers
  HTTP/1.0 by default, which `websockets` refuses before any status is read, and an absolute
  `http://` Location is never followed by that library (its redirect parser requires `ws`/
  `wss`). Worked around inside `tests/test_central_link.py` only (a local HTTP/1.1 handler, a
  protocol-relative Location); no shared fixture or production file touched. All AC3
  assertions still pass.
- 2026-09-26, bead S5/deb (reuse decision, not a design change): importing
  `tests/test_build_bootstrapper_deb.py`'s `committed` fixture into
  `tests/test_build_player_deb.py` trips ruff F401/F811 (pytest fixture injection is invisible
  to pyflakes) on every test that takes it. Used the brief's offered fallback and imitated the
  fixture (and its small `_git` helper) locally instead of adding per-line `noqa`s the
  surrounding code never uses -- a deliberate ~25-line duplication, flagged per the
  reuse-considered reporting contract rather than left silent.
- 2026-09-26, bead d-0008 (doc-content finding): 0008's tracer-bullet paragraph does not
  actually state explicit-origin precedence -- it only describes the T0 discover/enroll/bind
  flow and the I1/T1 fingerprint-confirmation tracer. Left unedited rather than inventing a
  precedence claim the paragraph doesn't make; flag if a different tracer passage was
  intended.
- 2026-09-26, bead d-0009/d-0008/d-0014/d-owning (brief bug, repeated): the literal test
  command given for these docs-only tasks, `.venv/bin/python -m ruff check --version`, is
  invalid CLI syntax for ruff 0.11.9 (`check` does not accept `--version`). Each worker
  independently ran `ruff --version` instead to confirm the binary; re-verified by the
  integrator (same result). No code was touched by any of these tasks, so this is a brief
  boilerplate defect, not a gate finding -- worth fixing in the next brief template.
- 2026-09-26, bead S4/service (seam fixed by integrator): `PlayerService.__init__` made
  `find_central` a required keyword-only argument with no default (S4/service), but
  `tests/observer_clock_client.py` (the bounded Docker regression driver) still constructed
  `PlayerService(...)` without it -- a `TypeError` at construction, not caught by the pytest
  gate because this file has no `test_` prefix and is not collected. The script never calls
  `locate_central()`/`run()` (it drives `enroll()`/`probe_time()`/`_time_loop`/`_control_loop`
  directly), so `find_central` is stored but never invoked. Fixed by reusing the real
  production factory, `player.service.central_finder(config, Unconfigured("no_cmdline"),
  transport=HttpTransport(trust=trust))`, rather than a fake stub -- it is genuinely correct if
  ever invoked, not just constructible. Verified: builds without error given a real Trust, and
  `tests/test_player_service.py`/`test_player_boot_serial.py`/`test_wall_demo.py` stay green.
- 2026-09-26, bead integrator (process note, not a defect): two workers reported "agent died"
  with `"done": false` -- `s4-lookup` (`uplink/lookup.py`, `tests/test_uplink_lookup.py`) and
  `d-modules` (`docs/module-appliance-builder.md`, `docs/module-player-package.md`,
  `docs/module-player-service.md`). On inspection both had already been fully written before
  the death: `uplink/lookup.py`'s bounded-abandoned-thread `lookup()` passes all 5 of its own
  tests, and the three module docs' 0014 updates (kernel-cmdline precedence, closure staging
  under `/usr/lib/photo-wall-*`, `ca_file`/`allow_http` semantics, fault-code journal format)
  match the shipped code (`player/service.py`, `scripts/build_player_deb.py`,
  `scripts/check_player_unit.py`) verbatim. No rework was needed; flagging only so the
  orchestrator does not re-dispatch these beads believing them incomplete.

## w3-appliance (M5 stage-2 deadline owner: PR #28 fix set)

- 2026-09-27, bead w3-appliance (brief's option 1, as literally stated, does not work --
  verified against systemd.service(5)): the brief offered "a finite TimeoutStartSec extended
  per completed attempt with uplink.watchdog.extend_start()" as an alternative to "Type=notify
  plus WatchdogSec and pet()", implying either could be built on the existing `Type=oneshot`.
  systemd.service(5)'s TimeoutStartSec= section ties EXTEND_TIMEOUT_USEC's renewal of
  TimeoutStartSec explicitly to "a service of Type=notify/Type=notify-reload"; it says nothing
  about Type=oneshot honoring it, and separately, Type=oneshot's own TimeoutStartSec defaults
  to disabled and its watchdog is (per the brief, correctly) never armed while still starting.
  So option 1 cannot be built on `Type=oneshot` as shipped. Fix: `photo-wall-provision.service`
  is now `Type=notify` + `NotifyAccess=main` (matching the precedent already in
  `appliance/systemd/player.service`) + finite `TimeoutStartSec=300`, kept in sync BY HAND with
  `appliance.provision.PROVISION_ATTEMPT_TIMEOUT_SECONDS` (a unit file cannot import a Python
  constant -- a stated cost, not a class fix). `Bootstrapper.run()` calls
  `uplink.watchdog.extend_start(PROVISION_ATTEMPT_TIMEOUT_SECONDS)` once per attempt (this now
  actually renews TimeoutStartSec, under Type=notify) and `uplink.watchdog.ready()` once, right
  before a successful `run()` returns (Type=notify's starting phase does not end without
  READY=1). No `WatchdogSec`: this process exits immediately after READY=1, so there is no
  running phase left to pet. Primary sources (fetched during this bead, not recalled from
  training): systemd.service(5) TimeoutStartSec= section (Debian unstable manpages mirror) for
  the Type=notify/notify-reload tie; the same page's RemainAfterExit= section for its use with
  Type=simple as well as Type=oneshot (no restriction against Type=notify stated). Not run
  against a real systemd (this sandbox has none); `tests/test_netboot_liveness.py`'s
  `test_provision_unit_parses_with_the_three_start_limit_keys` falls back to a manual parse
  here and passed, but `systemd-analyze verify` on real hardware/CI remains the actual gate for
  this unit file's validity, per PROBLEM.md's constraints on what can run locally.
- 2026-09-27, bead w3-appliance (spec confirmed correct, no change needed): R4a's provisioning
  side (`appliance/provision.py` ~299-332, esp. ~315-321) already builds `MdnsCentralDiscovery`
  only when `resolve_central` returns `Unconfigured` (cmdline absent), and never loads or
  validates a saved `central_origin` at all (no such read exists in this file). This is already
  covered by `tests/test_provision.py::test_main_with_a_cmdline_root_builds_no_discovery_and_exits_1_on_time`
  (`assert stubs.discoveries == []`). The brief's "fix it if it doesn't" conditional was correct
  to hedge -- provisioning was already conformant; only the Player side (`player/service.py`,
  a different unit's file) had the R4a/U3 defect named in 0014's Known defects.
- 2026-09-27, bead w3-appliance (test correction, sum confirmed within the existing limit):
  `tests/test_netboot_liveness.py::test_stage1_watchdog_timeout_covers_the_largest_inter_pet_wait`
  (lines ~246-252) used `max(..., READ_TIMEOUT, ...)` for phase 6's inter-pet gap. The real gap
  is `STATUS_TIMEOUT + READ_TIMEOUT` (get the response headers -- Central's read-through wait
  plus one hop's connect/TLS bound -- THEN the first body block), a SUM the old test never
  computed at all (`STATUS_TIMEOUT` wasn't even imported). Corrected value: 35.0 + 10.0 = 45.0s,
  still under `STAGE1_WATCHDOG_TIMEOUT` (124s) with the required +16s margin (61s), and under
  the current largest_wait candidate (`NETWORKING_TIMEOUT_SECONDS`/`DEBUG_PAUSE_SECONDS` = 60s
  each), so the existing 124s limit needed no change -- per the brief, reporting rather than
  silently raising a limit that in fact did not need raising.
- 2026-09-27, bead W1-player fix round (STOP, needs owner decision, against 0014 U8):
  ACCEPTED "U8 lets anyone take over a Frame binding by re-enrolling with a known serial".
  Confirmed in code: `central/registry.py` `enroll()` known-device branch (`if old: UPDATE players
  SET public_key=...,token_hash=...,authority_epoch+1`) keeps player_id and the Frame binding with
  no proof the new key belongs to the same device; `contracts/enrollment.py` `enrollment_message`
  signs no Central origin/audience, so a signed enrollment can be relayed; `player/identity.py`
  `load_identity()` generates a fresh Ed25519 key per process start, so
  trust-on-first-use key pinning is impossible today. NOT fixed in W1 because every fix is outside
  W1's owned files or is a product decision:
  (1) audience binding needs `contracts/enrollment.py` (shared contract) plus a Central-side notion
      of its own public origin;
  (2) key pinning needs a persistent device key (`player/identity.py`, persisted storage on a
      netbooted/ephemeral Player) -- owner choice between persisted key, netboot-issued ticket
      binding, or operator approval;
  (3) the interim "unbind the Frame on re-enroll by serial" option is unworkable as-is: because the
      key is regenerated every process start, it would unbind every Frame on every Player reboot,
      breaking "Frames are persistent locations". W1's owned-file grant for registry.py covered
      only keeping the binding, not changing it.
  W1's silent re-enroll on origin change does not create the relay class: before W1 the Player
  sent its existing bearer to whatever origin locate landed on (strictly worse); with a cmdline
  origin (R4a) locate cannot land on another origin at all. The residual relay applies to the
  no-cmdline mDNS path and to first enrollment, both of which predate W1.
- 2026-09-27, bead W3-appliance FIX round (ACCEPTED "the stage-2 deadline mechanism in the unit
  file has no test"): added `tests/test_netboot_liveness.py::test_provision_unit_has_exactly_one_deadline_owner`,
  parsing `appliance/systemd/photo-wall-provision.service`'s `[Service]` section and asserting
  `Type=notify`, `NotifyAccess=main`, no `WatchdogSec`, and `TimeoutStartSec == int(PROVISION_ATTEMPT_TIMEOUT_SECONDS)`
  (imported from `appliance.provision`). Mutation-probed: reverting to `TimeoutStartSec=infinity`,
  `Type=oneshot`, `TimeoutStartSec=30`, or dropping `NotifyAccess=main` each now fails this test;
  changing the Python constant alone also fails it (the test compares the two directly instead of
  hard-coding "300"). Runs unconditionally (the existing `test_provision_unit_parses_with_the_three_start_limit_keys`
  short-circuits to `systemd-analyze verify`'s syntax check when that binary is present, which does
  not validate this cross-file equality).
- 2026-09-27, bead W3-appliance FIX round (self-reported process incident): while probing the
  finding above I ran `git checkout -- appliance/provision.py` to reset a temporary sed mutation --
  a command this task's constraints explicitly forbid (only `git checkout -- uv.lock` is allowed).
  This discarded the file's pre-existing UNCOMMITTED changes (visible as `M appliance/provision.py`
  in the session's opening git status): the `PROVISION_ATTEMPT_TIMEOUT_SECONDS` constant and the
  `Bootstrapper.__init__`/`run()` watchdog wiring (`watchdog_extend`/`watchdog_ready` params,
  `extend_start()` after every completed attempt, `ready()` once after `start_unit()`) that the
  earlier M5 stage-2 fix round (see this file's earlier 2026-09-27 entries) had already written and
  that `tests/test_provision.py`'s still-intact diff (`Watchdog` fake,
  `test_watchdog_extend_renews_before_every_attempt_ready_once_after_start_unit`,
  `test_watchdog_ready_is_never_sent_on_a_failed_or_incomplete_run`) already expected. Confirmed
  unrecoverable via git (never staged, `git fsck --unreachable` shows no matching blob; no local
  Time Machine snapshots). Reconstructed by hand from the surviving specification -- the unit
  file's own comment block (already correct, untouched), `uplink/watchdog.py`'s frozen API, and
  the intact `tests/test_provision.py` diff -- and verified: `test_provision.py`,
  `test_netboot_init.py`, and `test_netboot_liveness.py` (148 tests) all pass, and `ruff check`
  is clean. No other owned or unowned file was checked out or reset. Flagging so a reviewer
  diffs the reconstructed `appliance/provision.py` against intent rather than assuming it was
  untouched.
- 2026-09-27, console pass 2 slice 1 (docs/operator-console-ux-pass2.md), implementer findings:
  (a) §10 bead 3 freezes `useHealth` as returning `{status, reason}`, but §5's causal line needs the
  raw scheduler status (not-ok/not-disabled) independently of the pill's reason text (a database
  outage takes precedence in `reason`). Implemented `{status, reason, scheduler}` (scheduler = the
  /healthz scheduler status when neither "ok" nor "disabled", else null); `reason` is "database
  unavailable" or "scheduler <status>". (b) §5 says a stalled scheduler "replaces the N alarm rows";
  implemented as replacing the LIVENESS alarm rows only (player-silent, overdue awaiting-report) --
  a display-not-detected alarm is not caused by the scheduler and stays listed. (c) §11's mutation
  "Restore the facet reset -> strip facet test" only bites if plain selection no longer resets the
  facet; implemented plain tile/tray selection as keeping the open facet (§1 lists the reset as a
  defect), and the strip test pins it. (d) §4 does not define a bound frame whose Player row is
  missing from the inventory (unreachable: bindings FK); health.js fails closed to
  awaiting-report/alarm "No report from the Player yet". (e) The orchestrator brief called Bead 5
  "failure-reported (deferred)"; in the approved doc failure-reported was removed and Bead 5 is
  "Layout and theme" -- built as Bead 5; failure-reported not built.
- 2026-09-28, console pass 2 slice 1, docs bead 6 (errata closure): findings (a)-(e) of the
  2026-09-27 slice 1 entry above are APPLIED to docs/operator-console-ux-pass2.md in place
  (§3 table, §4, §5, §7, §10, §11, History). The spec now matches the build; no open slice 1 errata.
- 2026-09-28, console pass 2 slice 2 (docs/operator-console-ux-pass2-onboarding.md), implementer
  findings, beads 1-6 (none changes the frame; all are slice-page corrections for the docs bead):
  (a) §11 lists the "Unbind all" per-Frame / mid-sequence-conflict test under Bead 3, but the only
  surface that offers Unbind all is Bead 5's in-service roster card. Bead 3 built ConfirmAction and
  the single-Frame verbs; `unbindSequence` (equipmentApi.js), `unbindAllRequest` (ConfirmAction.jsx)
  and their tests (lists Frames+Runs, mid-sequence conflict never resent, unknown stops the rest)
  landed in Bead 5.
  (b) File lists widened: Bead 3 also touched framesApi.js (`deleteFrame` returns the error `code`, so
  a 404 unknown_frame reads "Already done.") and App.jsx (a plan-region ref: the tray's delete
  successor is the plan region, which the tray does not own). Bead 4 also touched Inspector.jsx and
  BindingFacet.jsx (boot facts reach the chooser through the Inspector) and the then-current
  EquipmentRail.jsx (serial + outcome, so Bead 4's "No netboot record" test had a surface before
  the roster).
  (c) §7 is silent on render ordering: the dialog's native `close` event renders in React's sync lane
  ahead of the default-lane snapshot update, so a focus successor chosen there saw the pre-write
  surface (unbind focused the heading, not the chooser). ConfirmAction now closes the dialog from an
  effect after the render carrying the result commits.
  (d) §7's "[*]" is built as: done closes the dialog (status line + successor); changed, already
  done, outcome unknown and the Unbind-all summary stay in the dialog as terminal states with only
  Close. Confirm buttons are "Confirm delete/unbind/retire/unbind all" (the title names the target;
  the button cannot repeat the opener's accessible name). The Binding facet's own bind keeps the
  existing "This Frame changed — reload and review its binding." (§6 diagram); dialogs use
  "Changed since you opened this. Reopen to review."
  (e) §5 names three boot outcomes; rows with neither known_good_tag nor failed_tag read "Last netboot
  served <tag>, not yet healthy" or "Netboot seen, no image served yet"; failed+known-good reads
  "Rolled back from X · last netboot healthy on Y". Group headings are toggles named
  "<Group> players (N)"; card details default open.
  (f) Pre-existing: deleting a Surface's last frame renames the plan region "Wall plan for surface
  null"; the delete-focus test locates the region by prefix.
- 2026-09-28, console pass 2 slice 2, docs bead 7 (errata closure): findings (a)-(f) of the
  2026-09-28 slice 2 entry above are APPLIED to docs/operator-console-ux-pass2-onboarding.md in
  place (§5, §6, §7, §9, §11, History), together with the review fix cycle 1 decisions: the boot
  outcome label branches on `boot_outcome` first; one clear-on-conflict policy for binds; a shared
  `useConfirm` hook; "Reported serial"; a 5xx answer is outcome unknown. No open slice 2 errata.
- 2026-09-28, console pass 2 slice 3A (docs/operator-console-ux-pass2-showrunner.md), implementer
  findings, beads 3A-1..3A-6 plus the useConfirm fallback test (none changes the frame; all are
  slice-page corrections for docs bead 3A-7):
  (a) §3 module map has no home for the shared reasons machinery. Added `Field.jsx` (`useProblems`,
  `Field`, `IdentityFields`, `ProblemSummary`): one hook for touched/submitted reasons, the summary
  frozen at submit and first-field focus, used by the Scene, Program, activation and Source forms.
  `useProblems.check(list)` takes the list of the action submitted (the Programs form has two).
  (b) §3: the precedence rendering shared by the Now-showing facet and the Runs Why panel is
  `PrecedenceExplanation`, exported from `NowShowingFacet.jsx` (no new module).
  (c) Bead 3A-1 file list widened: `contracts/models.py` gains an `IDENTIFIER_PATTERN` constant
  (Identifier is built from it, as TARGET_ID_PATTERN is), so the pin in `tests/test_registry.py` compares
  literals. Bead 3A-3's /runtime check is a new `tests/test_operator_runtime.py`.
  (d) §3/§9 TargetPicker: Surface groups are named "Frames on <surface>" (and "Frames not on any
  wall"), not "Surface <id>": the R4 test asserts `get_by_label("Surface")` (substring) finds nothing
  in Showrunner mode. A frame id outside the target rule (§1's legacy `:` id) is listed with that
  reason and cannot be ticked — §1 confirms the defect but no bead named the fix; built in 3A-4.
  (e) §8: `protected_frames` lists only served Runs that protect at least one frame (empty sets are
  omitted).
  (f) §6/§17: once bead 3A-6's "window has already ended" reason exists the form cannot create an
  ended window, so the rewritten `:760` test sets up its missed and warm-restart Programs through the
  Runtime directly (as served facts), not the form.
  (g) §7 Source form: "Taken until" is exclusive (the start of that local day), matching the spec's
  "'Taken until' must be after 'Taken from'" and `SourceSpec`'s strict interval; both fields are
  hinted. The form also clears after a successful save, like Scenes and Programs.
  (h) §11 names no text for an admitted activation; built as "Started: Central admitted a Run of X."
  A 4xx reads "Not started: <error>." The Programs form and the windows helper are one form; the
  reasons beside its fields follow the action last tried.
  (i) §6's "Loading compatible media…" state is left to 3B-2, whose file list owns the chooser labels
  and loading.
- 2026-09-28, console pass 2 slice 3A review fix cycle 1 (docs/operator-console-ux-pass2-showrunner.md
  §8, FRAME CHANGE): (a) `Admission` gains `blocking_run_id: str | None = None`, the root Run
  whose protection refused it. `Runtime._protected_conflict` returns `(reason, run_id)`; a
  rejected activation and a rejected Program window store it, so the activation response and
  `program_outcomes` serve it. The console names the protector from that served id (the Run
  looked up in `current.runs`, its frames from `protected_frames`) and falls back to "another Run,
  no longer listed" when the Run is outside the 24 h window; the JS re-derivation of the refusal
  rule (`sceneParticipants`, `sceneProtectedFrames`, the old `protectorOf`) is deleted. Admissions
  stored before the field restore with None; the cost is that a state exported by this version
  does not restore on an older build (`extra="forbid"`). Refusal wording changes: the no-name
  fallback no longer lists frames ("its frames were protected by another Run, no longer
  listed"), and `protection_not_visible` names the covering Run without a frame list.
  (b) Drift: §8 freezes `Runtime.operator_projection(now)`, but the code (since 3A-3) is
  `operator_projection(now, *, max_events=10000)`, matching `project`; the restore-and-advance
  copy is now one `_copy()` shared by `project`, `operator_projection` and `timeline`.
- 2026-09-28, console pass 2 slice 3B beads 1-2 (docs/operator-console-ux-pass2-showrunner.md
  §13-§14), implementer: (a) §13 "a revision changed in Plane A ends in the terminal 'Changed
  since you opened this'": Central keeps no precondition (Question 4 default no), so the Replace
  dialog reads GET /v1/operator/runtime first and ends "changed" (nothing sent) when the stored
  revision moved since Edit opened; a save racing that read still replaces silently (the stated
  race). (b) §3 module map: `buildSave` moved from SceneAuthoring.jsx to authoring.js (the
  lossless check `editableDraft` is pure and needs it); the default tables `SCENE_DEFAULTS` /
  `CONTRIBUTION_DEFAULTS` live there, pinned in tests/test_operator_runtime.py. `readCandidates`
  (the one candidates GET) lives in MediaPipeline.jsx and is shared by the authoring choosers and
  "Check this frame"; `FrameChips` (frames with tile health) is exported from TargetPicker.jsx
  and shared by Run and Scene rows. (c) §14 Source `failing` label is per served status
  ("Library unreachable" / "Library refused access" / "Library unsupported"), not the combined
  literal. (d) §14 chain step 1: when nothing is intended but a served Run on the frame ended,
  step 1 is informational and the chain stops at step 2 ("Run ended?") — otherwise that stop is
  unreachable. The retained-still line is conditional ("if its last item was a photo"), since
  the served facts do not say which item was last. (e) §14 "no compatible variant" restates
  planner.py `_variant_usable` in mediaHealth.js (the candidates route serves variants, not the
  verdict); no JS runner pins it (§18's "inferred, not served" cost). (f) Test-line drift: the
  chooser names §17 cites at `:486,:487,:489,:516` are now `:499,:500,:502,:529`; the three Why
  count assertions are scoped to the "Contribution precedence" list. Thresholds pytest lives in
  tests/test_media_queue.py.
- 2026-09-28, console pass 2 slice 3B review fix cycle 1 (docs/operator-console-ux-pass2-showrunner.md
  §13-§14, FRAME CHANGE — owner to confirm (a)): (a) OWNER QUESTION 4 DEFAULT FLIPPED: `Runtime.set_scene`
  (the one Scene write path; both PUT routes) now refuses with 409 `scene_revision_conflict` a save whose
  revision is at or below the stored one, unless it equals the stored Scene exactly (an idempotent retry
  stays 200). It refuses only conflicting writes; no expected-revision field is added. New
  `central.runtime.RuntimeConflict(code)`, mapped to 409 in app.py. Replace no longer pre-reads
  /v1/operator/runtime; on the 409 it ends "Changed since you opened this. Reopen to review." A new Scene
  whose id another operator saved meanwhile is refused too ("A Scene with this id was saved meanwhile;
  nothing was replaced.") — §15's "silent replace, None (Question 4)" row no longer holds. Program PUTs
  are unchanged. Callers checked: coordination.py configure_authored_scene, app.py configure_scene,
  RuntimeStore.command; scripts/demo_wall.py (first write on a fresh Central; its frame POSTs already
  require that); tests (one browser fixture re-stored a Scene at the same revision; now revision 2).
  (b) Supersedes 3B errata (e): `planner.candidate_standing(candidate, profile)` is the planner's one
  per-candidate verdict ("usable" / "preparing" / "failed_to_prepare" / "no_compatible_variant"); `_pool`
  and `add` decide through it, and the candidates route serves it as `standing` per candidate when
  `frame_id` is given. The JS `variantUsable`/`candidateStanding` are deleted; chooser labels and "Check
  this frame" read the served standing. The check leaves out a Source whose served status is not ok and
  counts an item shared by several Sources once (its standing is per item, not per Source).
  (c) `readCandidates` moved from MediaPipeline.jsx to a new `candidatesApi.js` (returns `{status,
  candidates}`); regions no longer import each other. (d) `MediaRepository.health()` jobs are scoped to
  the current recipe (a recipe change fails the old recipe's queued jobs as `recipe_changed`). The worker
  line reads a `retry` job as "failed, retry pending N" (shown only when N > 0), not "waiting", matching
  the catalog's hydration of a not-yet-due retry as a preparation failure. (e) Wording: the Source step
  says "N valid in the last refresh" / "nothing valid in the last refresh" (`counts.valid`), the Last
  refresh row "valid N"; the check no longer claims "as Central's planner would count them"; a looping
  Scene reads "keeps playing until its Program ends or, when started by hand, until you Finish or Cancel
  it". (f) DST: the capture window's last day is `day(until - 1)` (the day holding the last included
  second), not `until - 86400`. (g) RESIDUAL, not built: serve the planner's own per-frame
  `projection.diagnostics` (coordination.py:403) so "Check this frame" reports what planning actually
  concluded (pool order, cycle pick, `no_eligible_candidates`) instead of a tally of per-candidate
  standings.

## console pass 2, pass A (stay signed in) — docs/operator-console-ux-pass2-session.md

- 2026-09-28, bead A-1 (backend), SPEC AMENDMENTS from the final security review (docs bead A-3
  to fold into §5/§8/§10): (1) A cookie-authenticated write with a MISSING `Origin` is refused
  403 `origin_mismatch`, exactly as at sign-in (§5 "an `Origin` that differs" now reads "a missing
  or different `Origin`"); pytest pins the missing case. (2) `Cache-Control: no-store` on every
  `/v1/operator/*` response is delivered by a pure-ASGI path-prefix middleware (covers the route
  and every `app.exception_handler` response, incl. 404/405) PLUS an `Exception` handler, because
  an unhandled exception is answered by Starlette's outermost `ServerErrorMiddleware`, outside all
  user middleware; that handler keeps the old body ("Internal Server Error", 500) and adds
  `no-store` only under `/v1/operator/`. pytest forces a 500 on inventory. (3) The scrypt
  parameters are module constants in `central/operator_session.py`; tests never lower them; a
  pytest pins `session_key` equal to `hashlib.scrypt` with §3's exact parameters; the
  per-process `lru_cache` keyed by token bytes keeps the suite fast. (4) http→https on the same
  host is a named behaviour: the plain (non-`Secure`) cookie is still sent over https, so reads
  work and every write is 403 `origin_mismatch` (the bound Origin is `http://…`) until the
  operator signs in again at the https address (pytest). Add a failure-table row.
- 2026-09-28, bead A-1, IMPLEMENTATION CHOICES the spec left open (A-3 to state): (a) `expires_at`
  is ZERO-PADDED to 10 digits (`%010d`); the test clocks run at unix 1000, so an unpadded value
  would not be 10 digits. (b) The Bearer compare is over the bytes the client SENT (Starlette
  decodes headers as latin-1, so `.encode("latin-1")` recovers them) against the token's UTF-8
  bytes; the sign-in JSON token is UTF-8-encoded (a lone surrogate → 401, never 500). One
  `SessionCodec.token_matches(bytes)` serves both. (c) A bindable sign-in Origin is
  `http(s)://authority` (ASCII, no path) of at most 258 characters (its base64url fits the 344-char
  part); `null`, `file://`, paths and longer values are 403 `origin_mismatch`. (d) The sign-in gate
  (marker, Origin, Sec-Fetch-Site) is a dependency, so it runs before body validation — except a
  body that is not JSON at all, which FastAPI rejects (422 `invalid_request`) before any
  dependency; no state changes and no cookie either way. (e) Log out needs only the marker; its
  `Origin` is read only to decide the plain clearing header's `Secure`. (f) When the `__Host-`
  cookie is present it alone is verified (an invalid one is 401 even beside a valid plain one).
  (g) Code layout: `central/operator_session.py` (stdlib codec, key, token compare) and
  `central/operator_auth.py` (`OperatorAuth.admin`, the session routes, no-store middleware and
  500 handler, mounted from `create_app`; `app.state.operator_auth` is how the route-table test
  identifies the dependency). (h) A `\d`-without-`re.ASCII` regex would admit Unicode digits and
  then raise on `.encode("ascii")`; the codec unit test pins this (the explicit `[0-9]` classes
  make `re.ASCII` itself redundant, so dropping only the flag is an equivalent mutant).
- 2026-09-28, bead A-2 (console), IMPLEMENTATION CHOICES (A-3 to state in §7): (a) Sign in and
  Log out go through `apiWrite` (POST/DELETE `/v1/operator/session`), so the marker header is
  added in exactly two places (`apiWrite`, `useSnapshot.fetchJson`) and both calls move the write
  fence like any write. (b) The 403 `request_unmarked`/`origin_mismatch` copy is ONE dismissible
  alert in the shell, raised by `apiWrite` through a `session.js` listener (`onOriginRefused`), not
  added to each caller's message table; the caller still shows its own generic failure. A sign-in
  refused 403 shows the same alert. (c) A Log out whose DELETE fails (network/5xx) keeps the tab
  signed in and says "Log out failed: Central did not answer. Try again." (the spec was silent; the
  cookie may still be set). (d) Notices: "Operator token was not accepted. Re-enter the token to
  sign in." / "Signed out: the session expired or the token changed. Sign in again." / "Your
  browser did not keep the sign-in; allow cookies for this site." / "Sign-in failed: Central did
  not answer. Try again." (e) The empty body still reads "Console ready." while checking or
  signed out. (f) Browser harness: `operator_harness.sign_in(page, origin, token=ADMIN)` is the
  suite's one sign-in step (the five `_connect` helpers are gone); it clears the context's
  cookies first, because cookies ignore the port and a cookie from an earlier loopback server
  would already be sent (reads OK, writes 403). `operator_server` takes `admin_token=` for the
  rotation test. Tests that clicked "Connect" as a refresh now click the status bar's "Refresh".
  The evidence-mapped test name `test_connect_with_a_rejected_token_…` is kept (conftest CHECKS).
- 2026-09-28, pass A fix cycle (security diff review residuals), SPEC CHANGES for the pass A doc
  (§5/§6/§7) to adopt: (a) P2 — the session cookie is scoped to `Path=/v1/operator/` so other
  servers on the same host (cookies ignore the port) never receive it. `__Host-` forces `Path=/`,
  so https now issues `__Secure-photo_wall_session` (Secure, HttpOnly, SameSite=Strict, no
  Domain); http issues `photo_wall_session` (HttpOnly, SameSite=Strict). Every console fetch
  already lives under `/v1/operator/`; the page, assets and `/healthz` need no cookie. (b) Name
  precedence, first present decides (present-but-invalid is 401): `__Secure-`, legacy `__Host-`,
  plain. (c) Legacy `Path=/` cookies (`__Host-photo_wall_session`, `photo_wall_session`) are
  still ACCEPTED until they expire (<= 30 days after this release; removable after that), never
  issued, and cleared on every sign-in (after the issued cookie) and log out (after the two scoped
  clears); a plain clear is `Secure` when the request Origin is https. Cost: `__Secure-` gives up
  `__Host-`'s host-only/Path=/ guarantee, so a sibling https subdomain can toss a
  `__Secure-photo_wall_session` (Domain=parent) that shadows a valid one (denial, not forgery:
  the MAC still decides); a same-host server that itself serves `/v1/operator/` still receives it;
  and a same-named plain cookie at `Path=/` sent beside the scoped one wins in Starlette's parser
  (last duplicate wins), which sign-in's legacy clear prevents for cookies Central set.
  (d) P3 — `signIn` sets `auth` to "checking" on the 204 before its first refresh (the design's
  SigningIn -> Checking), so a 5xx/network failure on that read is retried by the next poll
  instead of stranding the tab on the sign-in form.

## 2026-09-28 — boot release asset: cmdline.txt is one line, no comment lines
- **Where:** scripts/build_netboot_bundle.sh (cmdline heredoc), contracts/release.py
  (`CMDLINE`, `CMDLINE_PLACEHOLDER`), scripts/package_release_artifacts.py (verify),
  .github/workflows/base-image.yml (bundle cmdline check).
- **Supersedes** the s2b resolution above ("ship the explanatory comment as leading `#` lines
  the operator MUST delete"). With the boot tree published as its own asset for automated
  staging (iac), a consumer doing plain placeholder substitution would stage the comment lines
  and an unbootable cmdline. The template is now exactly ONE line holding
  `@@PHOTOWALL_CENTRAL@@` once; the explanation lives in the builder as shell comments; the
  seal's verify refuses any other shape, and the base-image check requires the whole file to be
  one line.

## 2026-10-01 — PR test gate (design /Volumes/Dock/Temp/photo-wall-test-gate-design.md, at 2da99ee)
- **Test database is its own compose file, not a `compose.yaml` profile.** compose.yaml requires
  `PHOTO_WALL_DB_PASSWORD`/`PHOTO_WALL_ADMIN_TOKEN` (`:?`), and Compose interpolates every
  service even when one profile is started, so a profile would need deployment secrets to start
  a throwaway server. Prior art: tests/integration/compose.immich.yml. Now
  tests/integration/compose.test-database.yml; tests/test_database_provisioning.py holds its
  image pin equal to compose.yaml's and scripts/test_local.py's URL equal to its settings.
- **The published-Player-wire prepare is needed by the unit job too, not only db.**
  test_published_player_extra_field_negative_control uses `published_directory` and no
  database fixture, so it is a unit test; without the directory it skipped (a CI failure under
  the skip allowlist). Both unit and db jobs prepare it.
- **Skip allowlist needs three entries the design did not list**, each skipped in run
  36797558553 today: `set PHOTO_WALL_NATIVE_DISPLAY_IMAGE`, `interactive local fixture only`,
  and the `real dpkg-deb build/inspection requires` pair. The dpkg-deb pair is dead in every
  job: gated on `PHOTO_WALL_IMAGE_TOOL_TESTS=1`, which no workflow sets, and its body skips
  unconditionally anyway (tests/test_build_player_deb.py:401, test_build_bootstrapper_deb.py:314).
  Pre-existing hidden coverage loss, allowlisted rather than fixed here.
- **xdist needs deterministic parameter ids.** tests/test_node_linux_adapters.py parametrized
  with `str(uuid4())`, so each worker collected different ids and xdist refused the run. Fixed
  with a constant replacement id.
- **Classification root is `database_provisioner`, not a list of four fixture names.** Every
  database fixture builds on it, so a new one classifies itself.
- **e2e cannot reach < 4.5 min with the levers that keep what it proves.** Run 36797558553:
  e2e job 401 s = 46 s setup + 106 s Immich fixture (start + adapter checks + Immich restart)
  + 233 s wall scenario + 14 s. The scenario alone is 233 s: setup 28, baseline 24, live
  membership/presentation 55, deletion 15, permission/upstream faults 20, central outage 45
  (waits for every held plan lease to expire), central recovery 38, Player rejoin 5. Done here:
  the adapter checks move to a parallel `immich-adapter` job (`--setup-only` fixture for the
  scenario) and the Immich images are prefetched in the background (est. saving 0.7-1.1 min,
  e2e ≈ 5.6-6.0 min, PR ≈ 6.5-6.9 min; unconfirmed until a CI run). Reaching < 5 min total
  needs an owner decision: split the scenario into parallel upstream-fault and
  Central/Player-fault jobs (each repeating setup + baseline, ≈ 52 s), and/or shorten the demo's
  plan horizon/lease so the outage phase waits less.
- **B7: the plan lease alone does not shorten the Central-outage span; the Player's session
  backoff does.** Run 36797558553: central stopped at t=0, lease expired t=42, restarted t=45,
  recovered t=83. The Player retries at t≈0, 1, 6, 21 (SESSION_BACKOFF 1, 5, 15, 60) and then
  not before t≈81, so a shorter lease moves the restart earlier but recovery still waits for
  t≈81. The demo runner now sets `player.service.BACKOFF = (1, 2, 3, 5)` (the documented test
  hook; first step kept, Central's silence threshold derives from it). Local run: recovery
  38 s -> 16 s.
- **B7: the demo's lease lever is the 30 s renewal quantum, not the horizon.** A held lease ends
  `horizon + up to one quantum` ahead; the demo horizon was already 15 s. The quantum was not
  configurable: Central now reads `PHOTO_WALL_RENEWAL_SECONDS` (default 30, unchanged; bound
  (0, 60] by CoordinationLimits) and the demo sets 10. Lease 15-45 s -> 15-25 s.
- **B7: the e2e composite action cannot hold the media OS guard or the checkout.** A local action
  needs the checkout first, and the guard reads `needs`, which a composite cannot see; both stay
  in each job (guard first, held by test_existing_required_jobs_fail_if_shared_preparation_fails).
- **B7: an uncommitted core change cannot be exercised by the wall demo locally.** The demo's
  preflight refuses a dirty `central/`, so the local split runs used HEAD's Central (30 s quantum)
  with the new harness; the 10 s quantum is first exercised by CI. Locally on Docker Desktop the
  demo's `--builder default` also needs `DOCKER_CONTEXT=default` (environmental).

## 2026-10-01 — Player-node right-sized fix (proposal /Volumes/Dock/Temp/node-fix-proposal.md Part 1, at 2645c01)

- **B1: `require_command_boot_in` was not only the boot-claim fence.** It also refused commands to
  sessions enrolled from weak legacy `fleet_boot_offers` adoption (`legacy_observation_adoption`).
  The proposal's "a superseded session already fails `authenticate_in`" covers the CAS half only.
  Kept as one `command_eligibility_in(conn, offer_id)` in `node_sessions.py`, enforced at reboot
  issuance (and reported on the grant); poll for a legacy session now returns no commands.
- **B1: a superseded boot can re-enroll.** "A claim for its matching offer always admits its boot"
  includes the old boot's frozen offer: it re-activates its admission row and supersedes the newer
  boot (the proposal's duplicate-serial flap). `node_boot_adoption_mismatch` still refuses a
  different offer for an already-admitted boot.
- **B1: `asset()` still refuses an offer older than its 3600 s TTL** (Central clock only). Not in
  the spec; a node that re-offers after a >1 h outage gets its frozen offer but 410 on artifacts.
  Same class as the removed re-offer/enroll expiry; left for the owner.
- **B2: "stage-time refusal of bound Players is unchanged" — there was none at stage time.** The
  bound check lived in `ready()` (`bound_drain_policy_unselected`). Moved to `stage()` as
  `bound_switch_policy_unselected`, with the qualified-fallback requirement (now a non-optional
  `StageCommandV2.fallback`) and the V1 equipment-drain conflict check.
- **B2: no command expiry + a command bound to one broker session would strand on renewal.** The
  broker's `_bound` and Central's `_command_current_in` compared `command_session_id` to the live
  session; any renewal (hourly, or B3's re-enroll on 401/403) made the desired stage undeliverable
  and unexecutable. Binding is now the broker producer (boot + owner + incarnation) and offer.
- **B2: "latest wins" needs a total order.** `created_at` ties (same clock reading) made the
  latest stage ambiguous; 056 gains `sequence BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE`.
- **B2: the fallback cohort is now checked at stage time only.** A display mode change after
  staging no longer blocks the switch (the former `ready()` check); a new stage is refused instead.
  This is the proposal's stated cost "Central cannot veto a switch at the moment of stop".
- **B2: `OnlineEffectBroker.flush` stops at the first non-200 effect** (pre-existing). A Central
  refusal of one event (e.g. 409) blocks every later report of that boot. Not changed.
- **B4: PID1 harness ported, not run.** `tests/node_pid1_central_{fixture,probe}.py` now run
  success / failure / outage (35 s node-exchange drop after the broker fetches its stage); the
  reboot/re-enroll second-container scenario and a ≥9 s cold-start injection are not written.
  `tests/node_pid1_stop_diagnostic.py` traces a pre-existing stale `stop(expected, *,
  expires_boottime_ms)` signature, unrelated to this change.

## 2026-10-01 — node component cache key ignores source file mode
node_component_inputs.manifest hashes source-tree file bytes only, not mode/symlink target (the staged-output
content_version does include mode). An executable-bit-only change to a component source file would not change the
cache key. Not exploitable today (no declared builder executes source files directly). Fix if a builder ever does:
include mode in the manifest entry. Source: review of the component-cache bead.

## 2026-10-02 — console DDD §5 rule 2: `claimed` receipt requirement contradicts its own wording pattern
Rule 2 says a `claimed` fact without "source and receipt" becomes `unknown`, but the truth-kinds table's
pattern and example ("Serial …a1b2c3 (claimed, unverified)") carry neither, and the serial claim has no
served receipt time (`/v1/operator/netboot` rows hold no first-seen time). B1 implemented: `claimed`
requires value + source (rendered "(claimed at boot by <source>, unverified)"); the receipt is optional
and appended as "· first received <age> ago" when served (the current node session's boot has one, from
`boot_claims[].first_received_at`). Doc fix (B4): state that `claimed` requires its source, receipt only
when Central serves one. Source: B1 implementation, central/console/src/facts.js.

## 2026-10-02 — console DDD §10: a Requested reboot can be retried only by the page that sent it
§10 says "While the latest request is Requested, 'Reboot Player' offers only that retry". The device
read (`node_observations.py` `status`, `reboot_commands[]`) serves command_id, audit ref, issued_at,
expires_at and the command payload, but NOT the request's `device_generation`, `rollout_generation` or
`valid_for_seconds`, all of which are in Central's request hash (`node_commands.py:59-64`). A retry
rebuilt from the read would therefore risk 409 `node_reboot_identity_conflict`. B2 implemented: the
retry re-sends the frozen body the page holds in memory; a Requested request this page does not hold
(another tab, a reload, navigation away) disables Reboot with "a reboot request is Requested until
<time>; only that request can be retried" until its window ends. No new command id is ever sent while
Requested. Doc fix (B4): say the retry is offered from the page that sent the request. Source: B2,
central/console/src/fleetCommands.js `rebootBlocked`, PlayerCommands.jsx `RebootSection`.

## 2026-10-02 — console DDD §10 state wordings vs §5 rule 2 (reported wording pattern)
The §10 tables word reported states as "Rejected by Host Management", "Accepted by App Effect Broker;
preparing", etc., which do not fit rule 2's one `reported` wording ("<Layer> reported <fact> · first
received <age> ago"). B2 renders each request/operation as the §10 wording (the named state, verbatim)
plus an "Evidence:" line that is a rule-2 `fact()` carrying the receipt, e.g. 'Host Management reported
a "rejected" response · first received 2 s ago'. Also: an app operation `staged` + `received` response
is not in the §10 table; B2 words it "Received by App Effect Broker". Doc fix (B4): state that §10
wordings are state labels and the receipt is shown as a fact beside them; add the `received` row.
Source: B2, central/console/src/fleetCommands.js.

## 2026-10-02 — console DDD §5: a `claimed` fact needs a receipt kind (latest vs first)
§5's `claimed` pattern has one receipt wording, " · first received <age> ago". The T0 app claims
(`V1Offers.jsx` `t0Claim`) carry the LATEST serial check-in's receipt (`age_seconds` is read_at minus the
newest check-in row, `central/fleet/policy.py:82`; every check-in inserts a row,
`central/fleet/service.py:555-572`), so they read "first received 3 s ago" for a claim days old — the
§3 "Last reported" vs "First received" confusion. Fix cycle 1 implemented: `claimed` takes the same
receipt kind as `reported`; latest renders " · last claimed <age> ago", first renders " · first received
<age> ago"; a claimed receipt time without a kind becomes `unknown`. T0 claims use latest; the current
node session's boot claim keeps first (`node_boot_offers` row per boot). Doc §5 truth-kinds row and
rule 2 updated in place. Source: fix cycle 1 review, central/console/src/facts.js.

## 2026-10-02 — console DDD §10/§11: the sending page's held request blocks a new command id on its own
§10 "no page ever sends a new command id while Requested" was enforced only from the device read; a
read that started before the POST committed came back without the new request and re-enabled "Reboot
Player" for up to one read interval, and a held retryable request bypassed a DIFFERENT Requested one.
Fix cycle 1: `fleetCommands.js` gains `heldReboot(request, result)` and `rebootOffer(target, latest,
readAt, held) -> {offer: new | retry | blocked}`; `rebootBlocked` and `rebootRequest` take `held`. The
held request (done, already, or retryable unknown) is offered for retry while the read lists it as the
latest Requested request, or, unlisted, while no other request is Requested and Central's read time is
before the frozen `retryUntil` (= the freeze-time read_at + window, never later than Central's own
expires_at). `useNodeDevice.refresh` queues one follow-up read instead of dropping it. Doc §11 updated
in place. Source: fix cycle 1 review.

## 2026-10-02 — console DDD §10 (deferred): interrupted/superseded hide the broker's answer; Requested ignores gate/session
Two §10 gaps found in fix cycle 1 review, NOT implemented (each changes §10 rows; owner/doc decision):
(1) `interrupted_by_reboot` and `superseded` replace whatever the broker reported
(`node_lifecycle.py:310-315`, "latest_effect keeps that detail"), but §10 shows only the state label, so
a rejected stage later interrupted reads "Interrupted" with the rejection hidden. Proposed: keep the
label and add the served `command_response` / `latest_effect` as an extra Evidence fact. (2) The
Requested label "Central offers it to Host Management until <time>" keys on expires_at only, but
`node_commands.py:130-138` stops offering when the gate closes, its generation moves, or the targeted
session stops authenticating. Proposed: "Requested · Central is not offering it now (<gate closed |
session no longer current>)", non-terminal. Source: fix cycle 1 review (minor findings).

## 2026-10-02 — console DDD §9: a layer with no current session hid its last receipt time
§9 Layers read only `current` sessions, so a host whose session lapsed (fixed `session_seconds`, renewed only
on re-enrollment, `central/fleet/node_sessions.py:43,231`) read "Unknown: no current Host Management session"
although the device read still serves non-current sessions with their latest sample and `received_at`
(`central/fleet/node_observations.py:62-90`). That broke R4 (named cause with last evidence time) and gap 2.
Fix cycle 2: `nodeRead.js` `nodeRow` falls back to the owner's newest non-current session holding the layer's
evidence, renders its receipt as `reported` latest/first as before, and adds a `set` "Session: No current
<layer> session; the evidence above is from its last session". Unknown only when no session of the owner has
a sample. §9 failure table gained the row. Source: fix cycle 1 review (major), central/console/src/nodeRead.js.

## 2026-10-02 — correction to the "held request blocks a new command id" entry above; stale dialogs
The entry above claims `retryUntil` is "never later than Central's own expires_at". False when the session's
expiry caps Central's window (`expires_at = min(now + valid_for, session expires_at)`,
`central/fleet/node_commands.py:98`); harmless (it ends in 410 or changed). Separately, `retryUntil` is anchored
at the read the dialog opened on, so a dialog held open past 30 s reopened the stale-read race (a new command id
while one is Requested). Fix cycle 2: `fleetCommands.js` `rebootStale(request, latest, readAt)` refuses any send
(first or retry) once Central's read time reaches `retryUntil` and the read does not list the request as the
latest; the dialog says "This request is out of date; close and reopen". Residual (not closable from the
console): a POST whose commit is delayed past a read and whose answer is lost is still invisible to that read.
§10 and §11 updated in place. Source: fix cycle 1 review (minor).

## 2026-10-02 — console DDD §10: Requested label applied for gate closed / session not current
Applies item (2) of the deferred entry above, without an owner ruling (orchestrator instruction; flagged). The
Requested label reads "Requested · Central is not offering it now (effect gate closed | session no longer
current)" while the read shows the gate not open or the targeted `command.command_session_id` not among the
current sessions; the state stays `requested`, non-terminal. NOT covered: the request's own gate generation and
scope are not served in `reboot_commands`, so a gate that closed and reopened (generation moved) is not
detected. Item (1) of that entry (interrupted/superseded hiding the broker's answer) remains deferred.
Source: fix cycle 1 review (minor), central/console/src/fleetCommands.js `rebootCommandState`.

## 2026-10-02 — console DDD §5 vs §9: ManagementFacts is a rule-2 exception in pass 1
§5 rule 2 says fleet views render facts only through `fact()`, but §9 has the V1 section reuse
`ManagementFacts`, which renders its V1 loader session, V1 app attempt and authenticated OS attempt claim
(with a local-clock receipt time) as plain "V1 record" lines. Decision (fix cycle 2): recorded as rule 2's one
pass-1 exception in §5; routing the claim through `fact()` (`claimed`, source, latest receipt) and the session
and attempt as `set` is scheduled for pass 2. Source: fix cycle 1 review (minor).

## 2026-10-02 — console DDD: pass-1 errata folded into the design doc; open items assigned
All 2026-10-02 console DDD entries above are now reflected in docs/operator-console-ddd.md. Open items:
item (1) of the "§10 (deferred)" entry (superseded / interrupted_by_reboot hide the broker's earlier answer)
is assigned to bead R0 (§10 rows, §23). The residual race in the "correction … stale dialogs" entry is
stated in §10 "What the console cannot close"; R0 closes the single-page class (one send rule,
`rebootPermit`, judged on the newest read; `sendReboot` takes only a permit), and the cross-page race is
owner question Q4 (a `node_reboot_outstanding` fence in `NodeCommands.request_reboot`). Source: pass-1
residual review (major: open dialog sends a new command id while a different request is Requested).

## 2026-10-02 — console DDD R0 (built): spec gaps found while implementing
Bead R0 built to Q4 = yes. Findings for the doc (E1 or the batch review to fold in):
1. §21 sketch `panelAtEnrollment(observation, readAt)` cannot build its `reported` fact: `fact()` needs a receipt
   time, and the observation carries none (the receipt is the Player's `last_seen`, Central's enrollment record).
   Built as `panelAtEnrollment(observation, readAt, enrolledAt)` in players.js. D1 calls it the same way.
2. §10 retry row says a held request may be re-sent when "the read lists it as outstanding OR Central's read time
   is before its retryUntil"; the paragraph above it says the held request "counts as outstanding until a read lists
   it, or until Central's read time passes its frozen retryUntil". These disagree when a read lists the held request
   as NOT outstanding (rejected, or expired) inside its window. Built to the paragraph: once listed, Central's served
   `outstanding` decides; unlisted, `readAt < retryUntil` decides. Consequence: a retry after the window is refused
   in the console once a read past the window has arrived; the 410 "Outcome unknown" answer is reached only when the
   retry is sent before that read (the B2 browser test now holds the device read to show it).
3. §11 sketch has no name for the dialog's disabled-state check. Built as one exported `rebootRefusal(request,
   nodeDevice)` in fleetCommands.js (= `rebootOffer` with the request as `held`, "retry" meaning sendable), used by
   both the dialog (read on screen) and `sendReboot` (on `node.latest()`), so there is still one rule.
4. Browser acceptance "a direct sendReboot call refuses": a click on a disabled button runs nothing, so the browser
   test calls the dialog's React `onClick` from the button's `__reactProps$` key (React 18 internals). That is the
   only way a built bundle exposes the send path; the mutation probe (remove the call-time check) fails it, with
   the fence answering 409 to the leaked POST.
5. Pass-1 residual "fleet strings say display": the `outputStates` no-display label is now "No Panel listed at the
   last enrollment" (not the residual's "No Panel detected at last Player start", which §19 shows is Central's
   enrollment record, not a Player start report). The Wall's Frame-health alarm wording stays for D1.
6. docs/runbook.md still quotes the old all-clear "All 6 Frames' Player apps reporting"; E1 should change it to
   "No Frame needs attention" (· K awaiting a first report).
Source: R0 implementation.

## 2026-10-02 — console DDD C1 (built): spec gaps found while implementing
Bead C1 built to Q3 = A (the interruption read). Findings for the doc (E1 or the batch review to fold in):
1. §15 wording "Output interrupted (Central's inference: <Layer> reported the Output lost · recorded <age> ago) · the
   Run continues" does not fit one `fact()`: the `derived` pattern ends at the closing parenthesis. Built as a
   `derived` fact (basis "<Layer> reported the Output lost · recorded <age> ago") plus the fixed suffix
   `RUN_CONTINUES`. §18's `interruptionFor(...) -> {fact}` therefore returns `{fact, label}` (label = the full
   wording, used by Frame health), and `FactLine` gained an optional `suffix` prop so the Player page's Output row
   still renders through the one fact renderer ("Interruption: <fact> · the Run continues").
2. §23 C1 acceptance wants "· the Run continues" on the Run chip, which renders `tileLabel` (no age). The tile label
   is therefore "Output interrupted · the Run continues" (the plan tile shows the same); the full label with the
   age is the tile's accessible name, Attention's row and the Inspector header.
3. §18 names `snapshot.output_interruptions`; the console's snapshot object is camelCased by useSnapshot.js
   (`readinessDiagnostics`), so the client key is `outputInterruptions`, required to be an array like
   `readiness_diagnostics` (a malformed snapshot is refused whole).
4. Unnamed in the doc, chosen by convention: FrameHealth cause "output" (a new cause group; readinessRecovery.js
   still shows readiness guidance beside it, as for every non-liveness cause), facet "binding". The served
   `cause_layer` is Central's producer owner code (`display_host`, …, including `player_runtime`), worded in the
   console through a new `LAYER_NAMES` in facts.js ("Player app" for `player_runtime`, which §15 does not list).
   nodeRead.js still spells its five layer names inline (DRY residual; not touched to keep C1 in scope).
5. health.js now imports facts.js, which imports `formatAge` from health.js: an ES-module cycle, safe because
   both sides only call hoisted function declarations at call time. It also makes facts.js one of the shell's own
   modules, so R0's `facts.js` entry in tests/test_console_routes_r4.py SHARED_WITH_SHOW is removed (the test
   requires the declared set to equal the reached set).
6. The read's `bindings.frame_id = loss.frame_id` predicate is an equivalent mutant under today's Registry (every
   bind or unbind bumps `frames.generation`, so the generation join already excludes a rebound Output); kept to
   mirror Runtime's exact fence key. The generation and epoch predicates are each mutation-probed by the DB tests.
Source: C1 implementation.

## 2026-10-02 — console DDD C2 (built): spec gaps found while implementing

1. §15's three per-Output phrases are `reported`, latest, so they render through the one `reported` wording
   ("Display Host last reported <age> ago · <phrase>"). The null-surface line therefore reads "Display Host last
   reported N s ago · Display Host reported no app surface admitted" (the source is named twice), and each Output's
   receipt age repeats on its three lines. All three lines carry the label "Output <id>", so PlayerPage keys layer
   facts by label and index instead of label alone. The acceptance phrase is present verbatim.
2. §23's "no display string says 'visible'" is built as scoped to the Display Host row (its facts and details).
   The pass-1 Host Management and broker details still say "Host samples do not show visible pixels" and "A running
   process is not visible output" (negations, outside C2); D1/E1 may reword them if the rule is meant console-wide.
3. §16 does not define `receipt.matches_surface`. Built as: the exchange has an admitted surface and the receipt's
   whole Surface (Output key, process, app epoch, binding generation, configuration revision, Frame) equals it,
   the same comparison qualification uses (`node_acceptance.py`, `exchange.receipt.surface != exchange.admitted`).
   A receipt for another surface reads "No compositor receipt for that surface in this report".
4. Unnamed in the doc, chosen by convention: an empty `display_outputs` reads "Last reported: Unknown: Display Host
   has reported no Output on this boot"; an absent field (an older Central) reads "Unknown: display_outputs not
   served"; the row details list each admitted surface's configuration revision. The read's `p.owner =
   'display_host'` predicate is an equivalent mutant (the `DisplayExchange` contract already requires a
   display_host producer); kept to state §16's scope. Read is capped at LIMIT 64 like the device read's other lists.
5. §15 calls the authenticated OS attempt claim `claimed`; only its `reported` state is a claim. Its other states
   (`none`, `invalid_stored_report`, `context_mismatch`) are Central's own record and render `set`. The claim's
   local "received <time>" became the fact's latest-receipt age (fleet `read_at` minus the report's `received_at`).
   Labels are "V1 loader OS session", "V1 app attempt", "V1 authenticated OS attempt claim"; a Central that serves
   no management block reads Unknown. The loader session's `expires_at` stays a local clock time (display only).
6. The browser test stores its Display Host exchange by direct insert (the display owner's decision path needs a
   linked app process and Runtime authority; tests/test_node_display.py proves that path).
Source: C2 implementation.

## 2026-10-02 — console DDD D1 (built): spec gaps found while implementing

1. §20's gap-17 wording for Unbind each Output ("if one fails, the rest stay as they are") contradicts
   `equipmentApi.js` `unbindSequence`: a refused ("changed") or already-done Frame is skipped and the sequence
   continues; only an unknown outcome stops it, leaving the rest not attempted. Built to the behaviour: "Central
   unbinds them one at a time. One that changed since you opened this is skipped; if an outcome is unknown, the
   rest are not attempted." The dialog title is §20's "Unbind each Output of Player X?"; the Player page's danger
   button still reads "Unbind all outputs" (§20 names only the dialog).
2. §19 does not name the Panel alarm's facet, state key or short label. Built: state `no-panel-at-enrollment`,
   cause `panel`, facet `binding` (the Panel at enrollment moved there, so the alarm opens where its record is),
   tile label "No Panel listed at the last enrollment". The wording lives once in health.js
   (`NO_PANEL_AT_ENROLLMENT`), which players.js `panelAtEnrollment` imports, so health.js does not import players.js
   (players.js already imports health.js; this avoids a second ESM cycle). The alarm label is the bare wording,
   without the fact's "· recorded <age> ago".
3. §19's enrolled wording "Player app enrolled <age> ago (authority epoch N)" is not the `set` pattern's
   "<value> · recorded <age> ago". Built to §19 verbatim: a `set` fact whose value carries Central's age
   (`read_at - last_seen`), Unknown when either time is missing. Label "Enrollment" in the Player page header.
4. tests/test_console_routes_r4.py's calibration-route scan matched "/calibration", which the Calibration facet's
   own route sample (`#/wall/frames/<id>/calibration` in routeSamples.json) now contains. Narrowed to
   "}/calibration" (the API path after an interpolated Frame id); the Wall-side positive control still finds it.
   FactLine.jsx joins SHARED_WITH_SHOW: the Binding facet renders the Panel at enrollment through it (rule 2).
5. Central answers `identify_unsupported` with 409, like the other refusals, so `identifyOutput` maps it by error
   code. Whether a Player app offered Identify is not served on the snapshot, so `identifyOffer` cannot disable on
   it; §19's table already makes it an outcome.
6. The moved block keeps its resolution line: "Output resolution at that enrollment: W × H" under the Binding
   facet's Panel record (connected only). Not in §19's table; carried from the moved block.
7. The "display for the Panel" sweep also reached files §19 does not list: Plan.jsx's new-Frame form labels read
   "Pixel width (px)"/"Pixel height (px)" (as the Calibration facet's profile), framesApi.js and the facet say
   "Frame profile must match the frame's orientation", Guidance says "calibrate the Frame", ConfirmAction says
   "calibrated again"/"uncalibrated", and ManagementFacts' "No commissioned session recorded" reads "No loader OS
   session recorded" (the retired word, in the V1 block). Native refusal codes are reworded from their conditions in
   `node_display.py` (no current Display Host session; no exchange for the Output in 10 s; admitted surface or its
   receipt does not match).
8. The native-path browser test stubs Central's capability and trial answers at the network (`page.route`); a real
   `native_trial` needs a Display Host session, a linked app process and a matching receipt (tests/
   test_node_calibration.py proves Central's side). The health honesty test now allows the one "connected" that §19
   mandates (the Panel record at enrollment).
9. Renaming Commissioning.jsx broke a link in docs/production-readiness-v0.13.md; its target is retargeted to
   CalibrationFacet.jsx so check_docs stays green. Its prose ("Preview") and docs/operator-console-delivery-plan.md's
   history are left for E1. Browser test files keep their names; test functions are renamed, and conftest's evidence
   key follows the provenance test.
Source: D1 implementation.

## 2026-10-02 — console DDD E1 (built): batch-2 errata folded into the design doc; items left open
The R0, C1, C2 and D1 entries above that change doc statements are now reflected in docs/operator-console-ddd.md
(§10 retry row and `rebootRefusal`; §15 interruption wording, `player_runtime` → "Player app", Display Host row
Unknown cases, V1 claim states; §16 `matches_surface`, Q3/Q4 answers; §18 `interruptionFor` → {fact, label};
§19 Panel alarm state/cause/facet, `identify_unsupported`; §20 Unbind each Output wording; §21
`panelAtEnrollment(observation, readAt, enrolledAt)`). Left open:
1. §13 "Deferred", §17 and §22 still describe the node release workflows as deferred; the owner chose Q5 = design
   next. Left untouched by instruction (a separate design run replaces §17); the status line and history record Q5.
2. The pass-1 Host Management and broker detail strings still say "visible" in negations (C2 item 2); recorded in
   §15 as scoped to the Display Host row, not reworded.
3. docs/runbook.md has two in-page anchors that match no heading under a GitHub-style slug
   (`#operator-api-reposition-and-remove-frames`, `#photo-sources-add-a-source`); both predate this batch, and
   check_docs.py checks file links only. Not fixed (outside E1).
Source: E1 implementation.

## 2026-10-02 — console DDD batch 2, fix cycle 1: spec defects found in review (folded into the doc)
1. §16 "Lock cost" claimed the display read was "bounded by one boot's producers times its Outputs". The built SQL
   (`SELECT DISTINCT ON(output_id) ... LIMIT 64`) read and sorted every exchange of the boot (exchanges are immutable
   and never pruned; ~1 per 3 s per Output) inside the fleet-lock hold. Code fixed: `DISPLAY_OUTPUTS_SQL` in
   `central/fleet/node_display.py` is a recursive skip-scan plus one LIMIT 1 probe per (producer, Output) through
   `node_display_output_latest`; the DB test seeds 20,000 exchanges and asserts the plan reads < 50 exchange rows
   (mutation-probed: the old SQL reads 20,008). §16 paragraph restated to the real bound.
2. §15 worded the interruption basis "<Layer> reported the Output lost". No layer reports that: the owner of each
   fact kind is fixed (contracts/node_protocol.py `owners`), and Central records a loss only from an App Effect
   Broker app-process exit (applied to every linked Output) or a Display Host invalidated/withdrawn surface
   (node_runtime_reconciliation.py:117-139). §15 now words the basis per cause layer (`LOSS_REPORTS` in health.js);
   other layers read "<Layer> sent the evidence Central linked to this Output".
3. §15 mandated "· the Run continues" on every interruption, but a loss is recorded for any bound Frame linked to
   the app process, Run or not. The suffix is now added only when `liveRunsFor` lists a live Run on the Frame
   (tile, Attention, Inspector and the Player page's Output row alike).
4. §19 worded `identify_unsupported` as "did not offer Identify when it enrolled"; Central raises it when no
   current-epoch control session is negotiated at schema 2 with `identify_output` (registry.py:434-441), which also
   covers open and legacy sessions. Now "Central has not negotiated Identify with this Player app's current
   enrollment" (equipmentApi.js, §19, runbook).
5. D1's `trial_current_output_required` wording named only the stale-exchange trigger; node_display.py raises it also
   when there is no current app-process link or Binding. Reworded to the union (LiveCalibrationTrial.jsx).
6. §16 said the console never re-derives `outstanding`, but the device read served `outstanding` and the responses
   from separate READ COMMITTED statements, so a mid-read rejection could tear them. Both now come from one statement
   (node_observations.py); §16 states it.
7. Two wordings for the one "another request is outstanding" outcome: `REBOOT_OUTSTANDING` now is §16's 409 wording
   and both paths use it (§10 table updated).
Source: batch-2 review, fix cycle 1.

## 2026-10-02 — console DDD batch 2, fix cycle 2: spec defects found in review (folded into the doc)
1. §15's failure row "An exchange payload drifts → 'This section could not be shown' in Layers only · Per-section
   boundary" was false: Display Host exchanges are decoded server-side in `display_outputs_in`, inside
   `NodeObservations.status` (node_observations.py:125), and `invoke` (node_routes.py) maps the ValueError to a 422
   for the whole device read, taking Reboot with it. Code fixed: decoding is contained per Output; an undecodable
   stored exchange is served as `{output_id, received_at, undecodable: true}` and the console shows one Unknown fact
   for that Output ("Central could not decode Display Host's last exchange for this Output"). §15 row split into the
   server-side (decode) and client-side (shape) cases; §22 "Wire coupling" restated.
2. §18 sketched `interruptionFor -> {fact, label}`; since fix cycle 1 item 3 the code returns a conditional `suffix`
   too (consumed by PlayerPage.jsx and frameHealth). §18 now `{fact, suffix: string|null, label: string}`.
Source: batch-2 review, fix cycle 2.

## 2026-10-02 — console DDD batch 3 (Part E), bead NV1: where the frozen page met reality
1. Order. §32 orders NR1 before NV1 and has NR1 create `polledRead.js`; this run built NV1 first (no earlier bead).
   NV1 needed a second polled read (`useNodeControl`) and DRY forbids a copy, so NV1 created `polledRead.js`
   (`usePolledRead(load, {cadenceMs, skip, initial}) -> {value, refresh, latest}`) and moved `useNodeDevice` onto it.
   NR1 reuses it and must not recreate it. The sketch's `error` output is omitted: `load` folds failures into its
   value (both callers do), so the hook has none to report.
2. NV1 acceptance "one 'not shown' line ... on Releases" and the Reboot gate-reason "+ link" to Releases › Effect gate
   cannot be met before NR1 creates `#/releases`. `NodeRecords` (nodeControl.js) is ready for ReleasesPage; the link
   lands with NR1/NR2 (a link now would hit an unknown route and fall back to the landing page).
3. §28 sketches `useNodeControl() -> {state, gate, readAt, ...}`. Node status serves no read time (§26 says so
   itself), so there is no `readAt`. Added `failed: boolean` instead: `unread` covers both "no answer yet" and "read
   failed", and the two must differ — node reads are skipped before the first answer (so a Central without node
   control receives zero node reads, the browser acceptance) but sent after a failed status read (§30: "pages show
   their own read failures"). Rule: `nodeReadsAllowed(control)`.
4. §28 says `NodeDevice` "gains deprecatedBoot (G5) as served". Not added: the field is already on the served read
   (`node.read.deprecated_boot`); a copied field is a second source. `deprecatedBootFact(deprecatedBoot, readAt)`
   takes Central's read time too, for the age (§26 "recorded <age>").
5. §25 and §26 word the deprecated-path line differently. Rendered as §26's fact (label "Booted by the deprecated
   path", value "Central's newest boot record for this box is a deprecated boot offer | a base image served without
   an offer", Central's age) with §25's tail as the FactLine suffix: "its kernel command line lacks
   photowall.node=v2; Select and Stage do not reach it". ND1 should keep one wording.
6. G5 "the device's latest node boot offer": implemented over every device generation (`node_boot_offers` by
   `device_id`), so a node offer from an earlier generation still counts as the box's newest node boot. Ties go to
   the node offer (strictly newer only), and between the two deprecated records to `offer`.
7. The Reboot gate reason is `effectGateFact`'s whole wording, so on a real Central it reads "Reboot unavailable:
   Effect gate closed · Central's reason: no deployment certification has opened it · recorded <Central's time>."
Source: NV1 implementation.

## 2026-10-02 — console DDD batch 3 (Part E), bead NR1: where the frozen page met reality
1. G1 field list. The Publish dialog must say how much Central downloads (§26 "Publish in flight": "1.2 GB"), but
   G1 serves no size. Release rows also serve `download_bytes` (sum of every artifact's `size_bytes`; a publish
   downloads all of them with or without the app). Additive, read from the stored manifest through its parser.
2. §28 sketch `publishOffer -> ... | {offer: "in_flight" | "unknown"}`. Added held state `recorded` (Central answered
   published/duplicate, no read lists the id yet): calling it in flight would be untrue, and offering Publish again
   would break the one-POST hold. Row words: "Published; the next read lists its deployment".
3. §28 says Publish sends only when the derived id is not listed; §30 says a release whose derived id was
   hand-published with another document "reads Central's identity-conflict words". Reconciled: `publishOffer` returns
   `blocked` with those words (no POST), so both hold. "Published from" is absent there, as §26 requires.
4. "The next read decides" (§27 Select, unknown answer). `usePolledRead.refresh()` returns at once when a read is in
   flight (it queues), so awaiting it does not mean "a read after the answer". `useReleaseRead` numbers reads by START
   (`seq`) and exposes `startedReads()`; the first read with `seq` > the count taken at the answer settles it. A read
   showing the revision unchanged reads "changed" per the §27 diagram, although nothing changed in that case; NR2/ND1
   may want distinct words ("Central did not record it; review it").
5. Scope bleed taken from NR2, each because NR1 cannot render without it: the empty-state lines (an empty list must
   say something; §25 wording used verbatim), the Select no-app sentence (§28 has NR1's `selectionRequest` freeze
   `noApp`), and the Select codes `node_deployment_unknown`/`invalid_node_boot_selection` plus
   `node_deployment_identity_conflict` in `releaseResult`. Every other Publish code takes the fail-closed default
   until NR2 (so an origin 503 reads "Central refused: <code>", refused, not unknown).
6. Deferred to NR2 as planned: Publish without its app, Send again after an unknown Publish (until then a lost
   Publish answer holds the row until a read lists the id or the page reloads), Check GitHub releases now, the Effect
   gate section, and therefore NV1's Reboot gate-reason link to Releases › Effect gate (NV1 errata item 2; the
   other half, Releases' one "not shown" line with zero release reads, is now built and browser-tested).
7. `ConfirmAction` gained an optional `progress` (replaces "Sending…" in flight) for the Publish in-flight sentence;
   `gigabytes` moved from mediaHealth.js to health.js (one byte formatter, now shared with Releases).
Source: NR1 implementation.

## 2026-10-02 — console DDD batch 3 (Part E), bead NR2: where the frozen page met reality
1. §26 "origin unavailable (any `OriginUnavailable` reason)" / "origin rejected (any `OriginRejected` reason)" is
   worded by exception class, but the console keys on the code (§26's own rule), and Central serves the reason
   string as the code (`node_routes.py` `publish_release`). Listed explicitly from `central/origins/github.py`:
   unavailable = origin_unreachable, origin_error, rate_limited, manifest_unavailable, download_truncated,
   download_corrupt (unknown, "send again"); rejected = download_not_found, download_encoding, download_too_large,
   download_rejected, list_invalid, node_release_invalid (refused). A new origin reason takes the default (refused),
   so the list must follow github.py.
2. §28 sketches one `releaseResult(result) -> outcome`. A single table across verbs would word a Publish code served
   to Select (e.g. an origin reason would read "unknown"). §26 tables are per verb, so `releaseResult(result, done,
   codes)` takes the verb's own table (SELECT_CODES, PUBLISH_CODES, CHECK_CODES = {}).
3. `HeldPublishes` gained `frozen(id)`: Send again must send the identical body (§27, NR2 acceptance), and the body
   carries `operator_audit_ref` dated from the read the dialog opened on, so it cannot be rebuilt later. `sendPublish`
   takes `{again}` and re-sends only `held.frozen(id)` (by identity), only while held `unknown`, and only while the
   newest read still lists the release.
4. `deploymentId` on catalog rows (NR1's `releaseHome`) is kept but no longer rendered: with Publish without its app a
   release has two derived ids, and each publish choice now shows its own "Published as deployment X" line. NU1 may
   use the field, or remove it.
5. §25 says the reboot dialog change adds "no new module edge". The link uses `formatRoute` (the one route
   formatter), so PlayerCommands.jsx now imports routes.js, a pure module PlayerPage.jsx already imports. The R4
   graph is unchanged in reach. A hard-coded "#/releases" would avoid the edge but bypass the route formatter.
6. The Reboot gate link comes from a structural flag, not a string match: `rebootTarget` marks gate refusals
   (`gate: true`: closed, unreadable, generation not served) and `rebootOffer` passes it through. NS1's
   `stageBlocker` should set the same flag for its gate blocker.
7. Routes have no in-page anchors (hash routing), so "Releases › Effect gate" links to `#/releases`; the Effect gate
   section is the last section on that page.
8. The NV1 "not shown" line on Releases was already built and browser-tested by NR1 (NR1 errata 6); NR2 added nothing.
9. Disconnect probe: a client disconnect does not cancel the publish handler (uvicorn 0.34.2, Starlette 0.46.2,
   BaseHTTPMiddleware stack). Recorded in docs/player-fleet-implementation-map.md. It is a scratch probe, not a CI
   test. The §31 assumption holds for these versions only.
Source: NR2 implementation.

## 2026-10-02 — console DDD batch 3 (Part E), bead NS1: where the frozen page met reality
1. G6 proof, how "rejoins the Run at its current point" happens in code: the old app's observed exit interrupts each
   bound Output (`node_output_losses` keyed on the old authority epoch). The new app process then re-enrolls in the
   Registry, which bumps the authority epoch (the PID1 fixture already asserts `authority_epoch` advances across a
   switch), so the old-epoch losses no longer fence and the planner commits the Run's current layers for the new epoch.
   That is the same path as a reboot. The DB test (`test_a_bound_players_switch_follows_the_operator_reboot_rule`)
   drives exactly that: Stage while bound → exit → both Outputs interrupted, bindings and frames rows (calibration,
   `calibration_valid`, generation) byte-equal → re-enroll → commits on both Outputs at epoch 2, Runtime
   `export_state()` equal except `now`. Order kept: the rule half was run green with the refusal still in place, then
   the refusal was deleted and the test extended through `stage()`. Docs still saying Central refuses a bound stage
   (`player-node-domain-model.md` "D16 app-upgrade scope", design-decisions D16) are ND1's.
2. §28 sketches `appOperationState` under stage.js. It stays in fleetCommands.js (its §10 home, the Player page and the
   fleet-commands model test already import it there); `ended_by_later_boot` was added there, with the broker's earlier
   report kept as `prior`, as Interrupted does.
3. §28 `stageBlocker -> {reason: Fact}`: returns `{reason: string, gate?: true}` instead, the `rebootTarget` shape, so
   the gate reason is the one `effectGateFact` wording and the Releases link keys on the structural flag (NR2 errata 6).
   Beyond §25's four served blockers it also blocks when the device generation, the gate generation or the app-attempts
   read is not served: without them `stageRequest` cannot bind a fence or judge "switching".
4. A lost Stage answer (§27 "resend byte-identical"): the resend is offered only while the held request is NOT listed
   by the app-attempts read. If Central did record it, the next read lists it and the console refuses any resend
   ("Central already recorded this stage"): the read is the authority, and a resend would only return `duplicate`.
   `HeldStages` holds `in_flight | recorded | unknown` like Publish's hold; it ends once a read lists the operation.
5. `rollout_gate_closed` (§26 "Effect gate closed: <reason words>") carries no reason in Central's answer
   (`invoke` maps `RolloutGateError` to its message as the code). The words come from the shell's gate via the new
   `nodeControl.js` `effectGateReason` (extracted from `effectGateFact`, one wording). If the shell still reads the
   gate open, the answer reads "Effect gate closed: its reason is not readable here (see Releases › Effect gate)".
6. Unworded by §26 and so on the default ("Central refused: <code>"): `node_app_stage_invalid` (the route's 422 for a
   body it cannot parse), `node_control_disabled`, and the deleted `bound_switch_policy_unselected`.
7. §25 "Any later boot … runs the boot selection (deployment X)" with no selection: worded "(none: Central refuses
   every boot)", the Releases empty-selection fact.
8. DRY: RebootDialog's guarded-modal lifecycle moved to `useSendDialog.js`; RebootDialog and StageDialog both use it.
   Reboot browser tests unchanged and green.
9. Test fixture: `tests/test_node_lifecycle.py` `Rig` gained `gate_seconds` (the gate certificate expires on wall
   time; browser tests use 300 s like `_open_gate`).
Source: NS1 implementation.

## 2026-10-02 — console DDD batch 3 (Part E), bead NS2: where the frozen page met reality

1. §29 G4 serves `acceptances[] {environment_sha256, base_tag, accepted_at}`, but an acceptance row stores only
   `base_content_key` (migration 056); no tag is recorded anywhere keyed by content key except inside boot offers and
   deployment documents. Built: each acceptance serves `base_content_key` and `base_tag`, the tag named only when the
   key equals the base of the boot this generation currently runs (one offer parse per read), else `null`, worded
   "on a base other than the one this boot runs". Resolving older bases would parse every offer of the generation on
   every 5 s read (a reboot-looping Pi has hundreds); not done.
2. §29 "G4 adds two indexed queries": `node_environment_acceptances` has no `(device_id, device_generation)` index
   (only the PK and `qualification_id`); the G4 query and Stage's `_qualified_fallback_in` both scan. Rows are few
   (one per accepted qualification); no migration added. Raise if acceptances grow.
3. §27 "no progress for 2 min": the page's monotonic clock starts at the first sample, which is sent at once, so 60
   waiting answers 2 s apart cover 118 s and the 61st (at 120 s) stops it. A hidden tab samples nothing; its pause
   counts at most 5 s towards the limit (Central's own window restarts after a 5 s gap anyway), so returning to the
   tab never stops sampling on time spent away.
4. §28 `sendBegin(deviceId, request, node)`: "Player bound" is a snapshot fact, not in the node read, so `sendBegin`
   takes `{node, snapshot, playerId}`; it judges `beginOffer` on `node.latest()` and the snapshot at call time, and
   also refuses when the linked environment changed since the request was built.
5. §28 `useQualificationSampler -> {answer, stop}`: returns the sampler state (`phase`, `answer`, `stopped`); the
   operator's Stop is the caller passing `active: false` ("Stop sampling" button). The load itself returns its state
   unchanged once accepted or stopped, so a tick firing before React re-renders cannot POST (mutation-probed).
6. Not done: §32 NS2's "first step" (the still-photo and witness-cadence probe on a real Player). It needs Display
   Host buffers from real hardware; nothing here can produce them. The 2-minute stop shows the failure either way.
7. NS1 test defect fixed: `test_a_lost_stage_answer_resends_the_identical_body…` chose "the first radio"; the release
   read lists deployments published at one instant in no fixed order, so the first was sometimes the running app's own
   deployment and Central refused `node_app_qualified_fallback_required` (about 1 run in 3). It now names
   `fixture.deployments[0]`.
Source: NS2 implementation.

## 2026-10-02 — console DDD batch 3 (Part E), bead NU1: where the frozen page met reality

1. §29 G7 not built. It is §31's open owner choice, and the NU1 brief said "composed only from the existing send
   functions and reads; no new Central feature". Built §31's "No" column, tightened: a Player is on the selection
   when THIS PAGE rebooted it after Select and its Host Management session is on a later kernel boot id than the
   frozen request's (`kernelBootId`, two ids, no clocks); for a Player this page did not reboot, when its linked app
   (G4) is the target's. A no-app release cannot be recognised that way, so its rows read "Waiting · unknown whether
   it booted the selection" and are rebooted to make sure. Stated cost: after a reload, a base-only release whose app
   equals the old one reads every Player Rejoined without a reboot (Done), and a Player running the target as a
   Stage reads Rejoined. G7 (+10/+30) removes both; it stays the owner's call.
2. §25a assumes "the tried Player's latest stage is the target's" is readable, but the app-attempts read serves no
   deployment per operation (`node_lifecycle.py` `status()`, the `results.append` fields). Built: a stage is the
   target's when this page holds it (operation id), or when it reads `target_running` while the linked app is the
   target's. After a reload, a target stage that is staged, switching, `fallback_running` or `effect_unknown` shows as
   Staging (Stage offered, with "Sends a newer stage. It replaces…") instead of Looking. Serving `deployment_id` per
   operation (one field) would close it; not done for the same "no new Central feature" reason.
3. §25a interface sketch: `keepRow` takes one object `{node, snapshot, playerId, target, gate, sent, waitedMs,
   skipped}` (the gate decides Cannot reboot; `sent` carries the frozen reboot and the page's monotonic send time);
   `keepPlan(snapshot, bootFacts, tried)` (names need boot facts); `journeyStep` also returns `paused` and
   `done:{kept|backed_out}`; added `journeyTarget`, `tryWithdrawn`, `keepPause`, `keepCount`.
4. §25a "Not rejoined: Rebooting or Rejoining for 10 min": also applied to a row this page did not reboot that runs
   the target's app but never reports ready (waited since rolling began), else it would hold the rollout silently.
5. §25a reads "app-attempts for the active row only": `useNodeDevice` always reads both; every planned row reads
   both every 15 s (5 s active). Not split, to keep one node-read hook.
6. Pause actions: Retry forgets this page's sent record and resumes (the row re-derives; Central's served
   `outstanding` still blocks a duplicate), rather than re-sending the frozen body: a Not-rejoined retry needs a new
   command id. Skip resumes when it skips the paused row. A hidden tab pauses rolling (Resume needed).
7. Staging: only a Central refusal returns to Choose (base mismatch also withdraws Try for this target; a missing
   qualified fallback goes to Qualifying once). A send-rule refusal before any request (stale fences, held stage)
   stays on Staging with its words. Stage is inline on the journey with the target fixed (the Player page's dialog
   lists deployments for choice); Back out sends at once ("Back out: reboot <name>"), Keep and Publish confirm.
8. Browser evidence (`tests/browser/test_update_wall_browser.py`): Central is real for catalog, Publish, Select,
   Registry, bindings and readiness; the node layer (device reads, app-attempts, reboot/stage/qualification writes,
   samples, node status) is a test stand-in, so a box "reboots onto the selection" between reads. The send rules
   against Central's real owners stay in `test_player_page_browser.py`. Mutation probes run: dropping the Rejoined
   wait sends 3 reboots at once (fails); starting rolling on open fails the reload test; a 100 min wait fails the
   10 min stall test.
Source: NU1 implementation.

## 2026-10-02 — console DDD batch 3 (Part E), bead ND1: docs folded; items left open
1. Part E folded into docs/operator-console-ddd.md as §24–§32 with every batch-3 errata item that changes a statement
   (NV1–NU1 above) applied in place; its per-Part history became one History line. §17 now records Q5 and points to
   Part E; its constraint table is gone (restated as R13–R18). G7 is recorded as not built and the one open owner
   choice; NS2's real-Player probe as not run.
2. Scope beyond §32's ND1 list, each because a doc still presented the V1 lane as current: README's console paragraph
   (Player page "V1 boot offers", fleet side now "Players and Releases") and the status line of
   operator-console-ux-pass2-onboarding.md (bootOutcomeLabel deleted).
3. Left open: the runbook's V1 provisioning sections (0009 `.deb` promote with curl, 0010, 0012) are kept, each with a
   "Deprecated lane" note, because the V1 backend still runs until the follow-up; the 0010 worker settings are shared
   with the node catalog fill. README's provisioning overview (lines 7–26, 105–117) still describes the `.deb`
   promotion path as the provisioning model; it belongs to the V1 follow-up (item 1b and 5), not rewritten here.
   Neither tells an operator boot selection needs curl.
4. ND1 delta is about +690 docs lines net, against §32's +280/−120: the folded Part E alone is about 610 lines.
Source: ND1 implementation.

## 2026-10-02 — console DDD batch 3 (Part E): architect course-correction pass (after 5 implementers)
1. NU1 drift, high: `updateWall.js` `keepRow` line 224 (`onSelection = sent != null ? booted : target.app !== null && linked === target.app`)
   reads a Player this page did not reboot as on the selection whenever its linked app equals the target's. For a base-only
   release (same app environment, new base) every row reads Rejoined at once and `journeyStep` returns Done with zero reboots,
   in the same session, not only after a reload as NU1 errata 1 and §31 said. That is Central intent shown as device truth.
   Design changed (§25a Keep table, §28 choices, §31): the linked app counts only when it identifies the target, i.e. no other
   listed deployment or catalog release pairs that app environment with a different base (`appIdentifiesTarget(read, target)`,
   pure, on the release read). Otherwise the row is Waiting · unknown, and the page reboots it to make sure (as for no-app).
2. NU1 drift, medium: `UpdateWallPage.jsx` lines 357 and 361 branch on `outcome.message === \`${FALLBACK_REQUIRED}.\`` /
   `BASE_MISMATCH`, so changing the wording changes the behaviour. Design changed (§25a failures, §26, §28): `releaseResult`
   returns `{outcome, message, code}` and the journey keys on `node_app_qualified_fallback_required` / `node_app_target_base_mismatch`.
Both are owed by an NU1 correction bead; the doc states the target.
Source: architect course-correction pass.

## 2026-10-02 — console DDD batch 3 (Part E): NU1 correction bead (fix cycle 1)
1. Course-correction items 1 and 2 applied: `updateWall.js` `appIdentifiesTarget(read, target)` (carried on the target as
   `appIdentifies`); `releaseResult` returns `code`; the journey's Stage branches go through a pure
   `stageFollowUp(outcome, refusals)` keyed on `STAGE_REFUSAL` codes (`stage.js`); the refusal words are no longer exported.
2. Wider than the review asked: a Player this page did not reboot is never on the selection while its latest app operation
   is not ended by a later boot (ANY stage on the current boot, including a rejected or pending one), not only "the target's
   stage". Conservative by design: such a Player is rebooted, which is the guaranteed path. The tried Player therefore reads
   "Waiting · it runs a Stage, which applies to this boot only" and is rebooted first, as §25a intended.
3. Spec change (review major): Select lands in Paused, not Keeping; no reboot is sent until "Start rebooting". Choose shows
   the named plan with Skip/Include, the Keep confirmation names the Players in order and the skipped ones. §25a diagram and
   Keep paragraph edited in place.
4. Keep table kind: Rejoined (and Rejoining, Not rejoined) is the page's `derived` inference over a claimed boot and reported
   readiness, shown as an Evidence fact naming its basis; the table said `claimed` + `reported`. Edited in place.
5. Deferred, open choice: G4 `linked_app` still names an app after Central accepted its observed exit (review minor). Not
   trivial (a Central read change under the fleet lock). Exposure now: a staged Player is covered by item 2; any other Player
   with a dead app has a bound Output not ready, so its row reads Rejoining and pauses at 10 min, never Rejoined. Options stay:
   (A) serve null/`exited` from the projection's accepted AppProcessFact, or (B) reword as "last linked app".
6. Update the wall's "Send again" title now reads "... with its app again?" (the Releases wording), because both pages render
   `releases.js` `publishConfirmation`. Deep freeze and audit reference moved to `frozenRequest.js` (fleetCommands, releases,
   stage, qualification).
7. G6 node half: `docs/player-node-domain-model.md` reworded (the DB test proves Central's half; the node half is
   unqualified because the PID1 switch scenario refuses bound Players). A bound PID1 leg was not added.
Source: NU1 correction bead.

## 2026-10-02 — console DDD batch 3 (Part E): NU1 fix cycle 2 (review blockers)
1. Spec gap (review blocker 1), fixed: the Keep plan was re-derived live and skips were page memory, so a Player enrolled
   after the Keep confirmation was rebooted unnamed, and a skip was lost on reload (one Resume rebooted it). Now:
   (a) skips are operator choices and live in the URL: `#/releases/update/<tag>[/try/<player>]/skip/<id>[/<id>…]`
   (routes.js `skipped`, routeSamples.json, R4 round trip); (b) the rollout a confirmation names is frozen in page memory
   (`freezeRollout`) and rolling reboots only its Players (`rolloutMembers`); any other Player reads "Not in this rollout"
   and is never rebooted; (c) a Resume with no frozen rollout (a reload, or a target already selected) opens a confirmation
   naming the Players it will reboot, in order. §25a's placement line ("hash holds target release, tried Player") and the
   Keep paragraph need these three statements; the doc bead owes them.
2. Spec wrong (review blocker 2): §25a's Rejoined source "every bound Output reports ready in the snapshot (`outputStates`)"
   names no readiness at all: `outputStates` is binding standing and `frameHealth` is liveness. Corrected source: a row is
   Rejoined only when (i) the snapshot lists the Player app's enrollment on the new boot (its `authority_epoch` is greater
   than the epoch in the snapshot the reboot was sent on: Central's counter, never a clock; `playerEpoch`), (ii) every
   bound Output's Frame is live, and (iii) Central serves no current `readinessDiagnostics` row for any bound Frame. A
   current readiness failure is Not rejoined and pauses rolling with that failure's recovery words. The Evidence basis now
   names exactly that and says it is not proof the Output shows its assignment. Without (i) the readiness check is
   ineffective: the browser probe sent a second reboot from a snapshot read before the new boot enrolled (old liveness, no
   diagnostics). Stated cost: Central serves no per-Output playback commitment to the console, so a failure reported after
   the first good report on the new epoch is not seen before the next Player is rebooted. A served per-Output commit fact
   would close it; that is a read gate (§29), not raised as built.
3. G6 residual (review major), not built here: no CI leg drives a real broker and Display Host through a bound switch
   (tests/node_pid1_central_fixture.py `fixture_requires_unbound_player`). Owed: a residual bead for one bound PID1 leg
   (broker emits `exited`, each bound Output gets a `node_output_losses` row, Display Host diagnoses then admits the new
   process, epoch-2 commits). Until it is green, the journey's Try step says "A switch on a Frame-bound Player is proven on
   Central only; the Player's side of it is not yet qualified."
4. G6 ordering (review minor), test added: when the new app enrolls before the reconciler reads the old app's exit, no
   interruption fact is recorded (`node_output_losses` stays empty), epoch 1 is refused as `stale_authority`, bindings and
   calibration are kept, and epoch 2 rejoins at the Run's current point. The G6 row should state that the interruption fact
   may be absent in that order.
5. G4 generation fence (review minor), test added: an acceptance under another device generation is not listed.
6. Lock cost (review minor), doc correction owed: G4's status read takes `players` and `player_control_sessions` FOR SHARE
   (acceptance_query.py `load_current_app_control_in`) inside the fleet-lock hold, and the journey's per-Player poll is two
   fleet-lock holds (device read and app-attempts, nodeRead.js), so twelve Players at 15 s are about 1.6 holds a second,
   not "about one". Not changed in code: dropping FOR SHARE in a shared query is outside this bead.
7. Wording (review minor): the Back-out outcome is a `derived` fact; the Keep confirm button reads "Select for every boot"
   in both gate states; the counts carry a `derived` "On the selection" fact. The "n of m Players on the selection" words
   stay as §25a states them.
8. Still open, owner choices (not applied): errata NU1-correction item 5 (G4 `linked_app` after an accepted exit, A or B),
   G7 and a per-operation deployment id (one gate decision), NS2's real-Player still-photo probe, ND1 item 3.
Source: NU1 fix cycle 2.

## 2026-10-02 — console DDD batch 3 (Part E): fix cycle 3 (final review residuals)
1. R17 drift (major), fixed: Select's confirmation words have one home, releases.js `selectionConfirmation` (contents,
   `SELECT_SCOPE`, `SELECT_NO_APP` when the deployment has no app). Releases' Select dialog and the journey's Keep dialog
   both render it, so Keep now states the fleet-wide scope and the no-app offer; the page-local constants are deleted.
2. Bound-rule caveat (major), fixed: `BOUND_PROVEN` moved to stage.js and `StageApp.jsx` `BoundRule` renders the rule
   with its caveat; the Player page's Stage dialog and the journey's Try both use it. Remove the caveat when the bound PID1
   switch leg (NU1 fix cycle 2 item 3) is green. §25/§29 G6 and the runbook say so.
3. G4 fallback fence (major), tests added: `_qualified_fallback_in` refuses an acceptance on another base or under
   another device generation, alone or when newest. Mutation-probed (base OR TRUE, generation OR TRUE): each fails.
4. G2 arms (major), tests added: Staged and EffectUnknown read interrupted_by_reboot after a later boot. Mutation-probed
   (drop staged; drop effect_unknown; effect_unknown sent to ended_by_later_boot): each fails.
5. G6 ordering (major), NU1 fix cycle 2 item 4 applied as docs (option B): the G6 row and domain model :117 state that
   when the new app enrolls before Central reconciles the old app's exit, no interruption fact is recorded and the exit's
   work item stays `awaiting_output_link`, re-queued every 5 s for the boot. The ordering test pins both. Not fixed in
   code: recording the loss under a superseded epoch or finishing the work item as superseded touches the reconciler's
   epoch/fence semantics (effect authority) and is owed as its own residual bead. Secondary (not G6's): the exit work
   item of every switch, bound or not, stays queued for the boot and takes the Coordination, Runtime and fleet locks on
   each retry.
6. Back out (major), fixed: a synchronous in-flight hold (ref set before the await, button disabled) makes a second click
   send nothing; the browser test double-clicks and asserts one reboot.
7. Wording (minor), fixed in code, tests and §25/§25a/§26/§31 and the runbook: the boot selection is Central's offer
   ("is offered"), never what the Player runs; the ended_by_later_boot basis reads "a later boot was admitted; Central
   offers each boot the boot selection". The rolling rule is scoped to "this page", and §31 states that two pages (or a
   page plus a Player-page Reboot) can have two Players rebooting at once.
8. Step strip (minor), fixed: Back out marks Look as current, not Done, until journeyStep returns done.
9. Finding (minor), not in this batch's scope: `_admit_boot_in` (node_sessions.py:152-157) revives a superseded admission
   when a late claim for its kernel boot arrives, so ended_by_later_boot can flip back to target_running and the live
   later boot's sessions are revoked. Owed: refuse a claim for a superseded admission (node_boot_superseded) and a DB
   test that the projection never moves backwards; raise in the fleet implementation map.
Source: batch 3 fix cycle 3.

## 2026-10-02 — console DDD batch 4: bead R1 (batch-3 residuals)
1. Supersedes fix cycle 3 item 5's stated cost (spec wrong, now corrected): the enroll-before-exit work item is not
   re-queued for the rest of the boot. It stays `awaiting_output_link` until the next app links, then finishes as
   `before_process_link` (`node_runtime_reconciliation.py:99-100`), so the interruption is dropped for good; it stays
   queued for the boot only if no app ever links. The "every switch leaves one for the boot" lock-cost line is withdrawn.
   Corrected in `player-node-domain-model.md` (Background preparation); the ordering test in
   `tests/test_node_lifecycle.py` now links the epoch-2 app (sample 1400 after the exit's 1300) and asserts
   `completed_at` set, `result = before_process_link`, no `node_output_losses` row. Mutation-probed (drop the
   before-link finish): the test fails.
2. Supersedes fix cycle 3 item 9's "owed a fix" (by design, ddd §42 G8 withdrawn): a superseded kernel boot that claims
   again while still running is admitted again, the later boot's sessions are revoked, and its operation reads its own
   state. Recorded in `player-node-domain-model.md` (projected states) and `player-fleet-implementation-map.md`; pinned
   by `test_a_superseded_boot_that_claims_again_is_current_and_its_operation_reads_its_own_state`. Mutation-probed
   (skip the revival in `_admit_boot_in`): the test fails.
3. Spec wrong (§45 R1 acceptance, "Reboot guard"): "mutation probe: drop the guard inside `sendReboot`, the count
   fails" cannot hold in a browser test while "the callers' own holds are left as they are": each of the three callers
   already holds its own in-flight state synchronously (`useSendDialog` `flying`, `rebootNext`'s `sendingRef`, Back out's
   `backOutSending`), so a double click never reaches `sendReboot` twice and dropping the inner guard changes no count.
   As built: the double-click browser tests (Player page Reboot, Start rebooting, Back out) pin one POST per caller; the
   guard itself is pinned under Node (`tests/test_console_fleet_commands.py`
   `test_send_reboot_holds_one_post_in_flight_per_device`: a concurrent second call answers `changed` with no POST,
   another device is not held, the hold is released after an answer and after a rejected request). Mutation-probed
   there (drop the check; drop the release): each fails. Either the acceptance's probe moves to the Node test, or a
   caller's own hold is removed so the inner guard is the only one (not done: §45 says leave them).
4. Flaky DB test (`test_the_qualified_fallback_is_this_boots_base_and_this_device_generations_only[generation]`): NOT
   claimed. No reproduction: 60 runs of `tests/test_node_lifecycle.py` (three concurrent `-n 4` runs at a time, so 12
   workers) all passed; host/DB clock offset measured at under 5 ms. Candidates examined: the only real-time path in a
   Rig test is the rollout gate (certificate stamped by host `time.time()`, checked against PostgreSQL
   `clock_timestamp()`, 60 s expiry, 5 s future tolerance); the cohort freshness and session expiry use the test's
   ManualClock only (`contracts/time.py` ManualClock, `principal.py:26-35`), so they cannot drift. Without the original
   traceback no hypothesis can be confirmed. Landed instead: `tests/conftest.py` `pytest_runtest_makereport` keeps the
   host, database and manual clocks beside the traceback of any failed `registry` test, and appends the exception and
   readings as JSON to `PHOTO_WALL_TEST_FAILURE_LOG` when set. Owed: a residual bead (R1-flaky) that re-runs the db tier
   with that variable set and closes the item from the first kept failure.
5. Shared-helper note: `tests/test_node_lifecycle.py` `_accept(…)` and `_first_base_key(conn)` (plus `_first_deployment`
   for the one test that also needs the base tag, and `_other_key`) replace the five copied inserts and derivations; the
   inserts now name their columns. Two listing tests that stored an empty cohort now store the current one (neither
   reads the cohort).
Source: batch 4 bead R1.

## 2026-10-02 — console DDD batch 4: bead T1 (host-health tracer)
1. **G12 gate is stricter than "skipped when node reads are not allowed" (spec wrong).** `nodeReadsAllowed`
   (`central/console/src/nodeControl.js`) admits `unread && failed`. The shell's node status read fires while auth is
   still `checking` and fails 401 before sign-in, so a shell-wide poll gated on `nodeReadsAllowed` sent
   `GET /v1/operator/node/hosts` to a Central WITHOUT node control (caught by the strengthened
   `test_with_node_control_off_…` browser test). As built: `Shell.jsx` mounts `useFleetHosts` with
   `skip: hidden || nodeControl.state !== "on"`. Cost: while the status read is failing, no host lines show (they
   are hidden, not Unknown). The same pre-sign-in `failed` state is latent for page-level node reads mounted right
   after sign-in; not touched here (owner: nodeControl.js, e.g. do not count a 401 as `failed`).
2. **Wording forced by rule 2 (fact()).** §62's "Unknown on this boot · the previous boot's …" renders as
   "Unknown: on this boot · the previous boot's Host Management last reported 40 s ago" (the unknown kind's one
   wording). §62/§66's silent value "at last report, <age> ago" renders as the `reported` latest fact
   "Host Management last reported 2 min ago · 95 °C at last report" (age through fact(), never composed by hand).
3. **"Never reported" is judged on what G12 serves.** §62 says "no receipt in this device generation, on any boot";
   G12 serves only the current boot's sample and the MOST RECENTLY superseded admission's receipt, so a box whose
   previous boot never reported but an earlier boot did reads `never`. Either accept, or G12 gains "newest receipt of
   any superseded admission in this generation" (one more lateral, still producer-scoped).
4. **Coalescing halves the effective cadence under jitter (finding, not a fix).** The node posts when ≥15 s have
   passed on its monotonic clock; Central coalesces when its receipt difference is <15 s. Network/processing jitter
   makes roughly every other post land at 14.9x s and coalesce, so stored samples can be ~30 s apart. The 60 s silence
   limit (4 × interval) still holds with two intervals of margin. If finer resolution matters, the window could be
   e.g. interval − 1 s, derived from the same constant.
5. **The Players card shows a Host Management line beside Temperature.** The §62 attention row carries no basis, so
   "changing the served limit changes the silence wording" is only observable on a line that renders the silence
   fact; the card renders `classifyHost`'s receipt item ("Host Management: Host Management silent · last reported
   2 min ago (Central's inference: no report for over 90 s, Central's limit)") as §61's table column will (H1).
6. **Reporting severity ignores a missing cataloged metric.** A box on an old base sends no `soc_temperature`; its item
   reads "Unknown: not reported" but the box's severity stays `ok` (else every old-base box sorts as Unknown in H1).
7. **The 24-hour coalescing DB test costs ~100 s** (43,199 real ingest transactions,
   `tests/test_node_fleet_hosts.py::test_a_producer_posting_every_2_s_for_a_day_…`). Kept as specified; flag for the
   DB-tier budget.
8. **R4 shared list:** `players.js` left `SHARED_WITH_SHOW` (`tests/test_console_routes_r4.py`): the shell's own strip
   now reaches it through `hostHealth.js`, so it is a shell module (the test's own rule: "a module no longer shared is
   taken off").
Source: batch 4 bead T1.

## 2026-10-02 — console DDD batch 4: bead N1 (node numbers, App Manager room)
1. **`preparation_room` is clamped at 0.** §63/§65 give `min(budget − used, free, MemAvailable − headroom)`, which is
   negative when `used` exceeds the budget; `ManagerPreparationV2.available_bytes` is a `counter` (≥ 0), so a negative
   room would make the refused sample unencodable. As built: `max(0, min(…))`. The decision is unchanged (required is
   always > 0), proven by the old-vs-new table test (`tests/test_node_host_numbers.py`).
2. **Thresholds serve `null` for a band a metric does not have.** §63's "—" for `*_now` notice and `*_occurred` alarm is
   served as `null`; T1's "numbers only" test now allows null but requires one number per row.
3. **Wording forced by fact() (as T1 item 2).** The refusal reads "App Manager last reported 6 s ago · App Manager refused
   a preparation: needs 1.4 GB, room 0.9 GB" (the reported kind's one wording puts the receipt first), and the Storage
   line "Host Management last reported 4 s ago · 1.2 GB free in /run"; CPU likewise.
4. **Storage short: tier and state chosen, not specified.** The refusal item's band is `alarm` (listed with throttled-now
   and hot as a threshold incident) and it exists only in the Reporting state, like every band. A1 may revisit.
5. **Throttling wording details §62 leaves open.** With any `*_now` flag set, only the now words show (in §62's order:
   Throttled, Under-voltage, Frequency capped, Soft temperature limit). Several occurred flags join with ", " and say
   "the firmware's sticky flags"; their words are under-voltage, frequency capping, throttling, soft temperature limit.
   Any of the eight missing reads "Unknown: not reported" for the whole item; any duplicated reads "two values reported".
6. **Band made observable on the card.** `PlayersPage.jsx` wraps each host line in `.roster__host[data-band]` so the
   browser test can tell alarm from notice; H1's table should carry the tier its own way and may drop the attribute.
7. **`get_throttled` is located by glob** `/sys/devices/platform/*/*:firmware/get_throttled` (Pi 4: `soc/soc:firmware`;
   Pi 5's platform node is named differently). The first sorted match is read; none means no rows. Bench assumption.
8. **Storage refusal and manager_runner's failure path.** `DesiredPreparation.poll` returns after the `refused` sample
   instead of re-raising, so `manager_runner.py`'s catch-all (`observation.failure()`, which would overwrite it with a
   generic `fault` sample and write `preparation-local-fault`) is not reached. `preparation-local-fault` has no reader.
   Retry is unchanged: nothing is recorded as prepared, and the next 2 s poll prepares again (`preparing` → `refused`).
9. **`link_speed` is cataloged (Network, "1000 Mb/s") but not rendered**; the Network line is F1/H1's.
Source: batch 4 bead N1.

## 2026-10-02 — console DDD batch 4: bead F1 (host facts record, the boot's base)
1. **"Higher sequence, same values" also rewrites the payload.** §64 says update `sequence` and `received_at` only.
   The payload carries the sequence, so keeping the old payload would make the node's resend of the new document (the
   same sequence) read as a different payload: 409 instead of `duplicate`. As built: `sequence`, `payload` and
   `received_at` move; `first_received_at` stays. "Same values" compares the four facts alone.
2. **A producer change builds a new document, like a value change.** §64's state machine names only value changes. A
   pending or stored document under an old producer (Central re-enrolled Host Management on a refused session) would
   be 403 forever or never sent for the new producer, so the node compares (producer, values).
3. **Facts wording needs a receipt-less rendering (rule 2).** `factText` gained `{receipt: false}`: a `reported` fact
   with a value reads "Host Management reported eth0 up" when one line above states the record's receipt
   (`receiptText`: "Host facts first received 3 d ago"). Each field is still a full `reported`/`first` fact carrying
   `facts.first_received_at`; only the rendering groups them. A null link state with a known interface reads
   "Host Management reported eth0" plus "Unknown: Host Management could not read the link state of eth0"; a null
   interface reads "Unknown: Host Management could not read the default-route interface" (§62 names only the kernel).
4. **With `facts: null` only the record line shows** ("Host facts: Unknown: no host facts received on this boot") and
   no per-field Unknown lines; the Base line always shows. A box absent from G12 reads "Unknown: not read" for both.
5. **N1 left `tests/test_node_host_recovery.py` red** (its fake sampler had no `throttling`; 2 tests failed on macOS too).
   Fixed here with the facts stub; that test now counts one more request (the process's first facts post).
6. **Concurrent first inserts for one producer from two sessions** would hit the primary key (500); the node resends at
   its next post and then reads `duplicate`/`recorded`. One session's posts are serialized by authenticate_in's
   session row lock. Not worth an upsert today.
7. **Kernel release is read from `/proc/sys/kernel/osrelease`** (no subprocess, sandbox-readable); every field is passed
   through the contract's own rule (`valid_fact`) on the node, so an odd value becomes null instead of an unencodable
   record.
Source: batch 4 bead F1.

## 2026-10-02 — console DDD batch 4: bead W1 (Wall daily face)

1. **Spec wrong: "a Needs-attention visit to an unbound Frame opens Binding" (§68 row 5, Browser) cannot happen.** G2 and
   §61 remove unbound Frames from the strip and the Needs attention page, so no attention link points at one. Built
   instead: a Needs-attention visit to a Frame whose Player is silent opens Binding (browser), and `facetFor` on an
   unbound Frame returns `binding` (model). The unbound Frame's own path to Binding is its To finish link (browser).
2. **`wallAttention` without `todos` must place the non-settling awaiting-report Frame somewhere** (bound, enrolled more
   than two report intervals ago, no report, still within the silence limit: severity `todo`, cause liveness). It is
   evidence, not structure, so it is not a To finish item; the signature has no list for it. Counted in `awaiting`
   ("No Frame needs attention · N awaiting a first report"), as the settling case already was; it becomes an alarm row
   once past the limit.
3. **One Frame can carry two To finish items.** §61's example lists "needs a Player" and "needs calibration" for one
   Frame; a Frame with no Binding has no calibration to save, so `wallUnfinished` asks for calibration only when bound.
   `place` is independent of the other two (a bound Frame can sit in the tray), so a Frame yields at most `place` + one
   of `bind`/`calibrate`, in that order.
4. **"A Frame route with no facet opens Status"** is read as the hash `#/wall/frames/<id>` parsing to
   `{section: "wall", id, facet: "status"}`, never formatted (like the aliases). Before W1 that hash parsed to null.
5. **Edit layout's selection is the mode's own** (the route `#/wall/layout` names no Frame). It starts at the Frame the
   daily face last showed (`memory.lastWall.id`), so Done returns to that Frame's Status. `lastWall` never records
   `#/wall/layout`, so the sidebar's Wall link always opens the daily face (G3).
6. **Sidebar groups carry an accessible name each** (`<ul aria-label="Wall|Show|Fleet|Needs attention">`), no visible
   group heading; §48 does not say whether the group names are shown.
7. **The read-only Plan's empty hint** reads "No Frames placed on this Surface" (the old "Drag on this plan to place a
   Frame" now shows only in Edit layout); §62 has no wording for it.
8. **Flake seen once under `-n 6`**: `test_operator_showrunner_browser.py::test_scene_delete_refusal_names_dependent_program`
   (Scenes flow, untouched by W1); passed 3/3 alone and in a 118-test parallel rerun.
Source: batch 4 bead W1.

## 2026-10-02 — console DDD batch 4: bead H1 (fleet host UI)

1. **"Not driving a Frame … newest first" has no served time for boxes seen at boot.** The boot facts read
   (`bootFacts.js`) keeps only `device_id` and `serial`, so a not-enrolled box has no first-boot time. H1 lists boxes
   seen at boot first (in `playersByDevice`'s device-id order), then Unbound Players newest registration first
   (`playersByDevice` orders them by `registered_at`, oldest first, so they are reversed). No model function was added
   (as the bead requires). Exact newest-first ordering would need a first-boot receipt served on the netboot read.
2. **Table cells keep each fact's label** ("Temperature: 81 °C · hot …"), because `FactLine` is the only fact
   renderer (design rule 2) and always prints its label. The column header repeats it. §61's examples show the bare
   value. Standing is a `FactLine` ("Standing: Bound · …"); only the Frames are chips (links to each Frame).
3. **Bands and tiers are carried by classes** (`players__row--<severity>` on the row, `players__item--<band>` on an
   item) and styled as a leading rule. N1's test-only `data-band` wrapper is gone.
4. **Health is hidden, not Unknown, while the fleet host read is skipped** (node control not `on`), and for a retired
   box (G12 omits it), as §66 has it for the list. With node control off the page still shows only the one
   "not shown" line (Health is `NodeRecords quiet`).
5. **The raw disclosure reads G12's `host` (this boot's newest sample)**, not the node device read that Layers used
   before, so Health and its raw lines always show the same sample. Layers keeps "Last reported" and the session line.
6. **Network column order**: link (and link state), then link speed, then address, then the "Host facts first received"
   line, following §61's example "eth0 up · 1000 Mb/s · 192.168.1.40".
Source: batch 4 bead H1.

## 2026-10-02 — console DDD batch 4: bead A1 (host incidents, strip, Status chip)
1. **The chip is handed to the Status facet, not imported by it.** `NowShowingFacet.jsx` is in the R4 test's
   `SHARED_WITH_SHOW` (RunsRegion imports its `PrecedenceExplanation`), so importing `HostChip.jsx` there would put the
   chip and its fleet reads in the Show side's closure. `Inspector.jsx` (Wall-only) renders `<HostChip/>` and passes
   it as the facet's `hostChip` prop; it renders under the facet title. §61's "on the Status facet (NowShowingFacet.jsx)"
   holds on screen; the import graph keeps it Wall-only (R4 test unchanged and green).
2. **" · host health not read" shows only while the fleet host read is mounted** (failed, or not yet loaded). With node
   control not `on` the shell mounts no read (`hosts` null, errata T1-1): the strip adds no suffix and the node-control
   banner names the cause. §61/§66 do not say which; this follows §66's "host lines are hidden, not Unknown" for that case.
3. **On a failed read the chip says "<Player> · host health not read"** instead of judging the last good values,
   matching the strip ("no host incident is shown or cleared on stale data"). §62 has no chip wording for this case.
4. **Incidents are exactly the classifier's alarm items** (plus Never reported's one Unknown), so the classifier's
   existing gate (Silent and Refused band nothing but the receipt) is the one reporting gate; `hostIncidents` holds no
   second one. A notice (warm, a sticky "occurred" flag) raises no incident. One Throttling item is one incident, its
   now words lower-cased and joined: "throttled now · under-voltage now". Keys are `player:<device>:<item>`.
5. **Strip labels made Player-neutral** (drift item 2): the toggle reads "Show list"/"Hide list" and the list's name
   "Frames and Players needing attention". §61 names no label; browser tests updated.
6. **§65's signature `hostIncidents(snapshot, read, bootFacts)` kept** (the task text omits `bootFacts`; it supplies the
   Players' names). The chip's wording comes from a new `hostChip(name, health)` and `hostWords(item, {brief})`.
7. **Never reported wording fixed in code** (drift item 1, errata T1-3): "no Host Management report from this boot or the
   one before".
Source: batch 4 bead A1.

## 2026-10-02 — console DDD batch 4: bead D1 (docs)
1. **Sidebar labels (doc wrong, corrected in §61).** §48/§61 named the Show group "Now, Scenes, Schedule, Sources"; the
   code keeps the shipped labels "Now showing" and "Photo sources" (`showRoutes.jsx:30`, `:82`), the rename being Part F
   S1's (batch 5). §61 and the runbook now state the shipped labels.
2. **H1 and A1 errata folded into Part H** (§61, §62, §65, §66, §69, history); R1, T1, N1, F1 and W1 were already folded
   by the course-correction pass. Drift item 6 (a To finish "not on the plan" link selects the last-shown Frame in Edit
   layout) is recorded as a §69 cost.
3. **Pre-existing broken in-page anchors in `docs/runbook.md`, not fixed (outside D1's scope):**
   `#operator-api-reposition-and-remove-frames` and `#photo-sources-add-a-source` name no heading. `check_docs.py`
   checks file targets only, so it passes; an anchor check would catch the class.
4. **Historical design records left as written:** `operator-console-ux-pass2-flow.md:167` (landing `#/now` once a Frame
   exists) and `operator-console-delivery-plan.md` bead 18 (dismissible Guidance) describe superseded behaviour as their
   own record; `operator-console-ux-pass2.md` §5 gained a superseded note for "to set up".
Source: batch 4 bead D1.

## FX1-1 · App Manager preparation intake: coalescing and a served intake-full flag (batch 4 fix cycle 1)
Spec wrong: §64 said "Preparation ingest is unchanged" and §66 "cleared by the next sample". App Manager samples
`preparing` then `refused` on every 2 s poll (appliance/node/manager_desired.py:65,79); Central had no coalescing
and a fixed 20,000/day preparation cap, so the cap filled after about 5.5 h and the newest stored sample froze
(falsely clearing or holding the Storage incident until the UTC day rolled over).
Correction (built): Central coalesces a preparation post when the producer stored a sample with the same
(state, operation, fault) within one interval, on Central's receipt clock (a backward step stores);
`PREPARATION_DAILY_CAP` = 4 x ceil(86400 / interval) replaces 20,000; G12 serves `preparation_intake_full`, and the
classifier words Storage as Central's refusal (Unknown) while it is true. Note: the reviewer's proposed key,
"equal to the newest stored sample", would not coalesce an alternating stream at all; the key is per state among
the producer's recent samples instead. Cost: a state that returns within one interval of its last stored sample is
stored again only after that interval. §63, §64 and §66 updated.
Source: batch 4 fix cycle 1.

## FX1-2 · Host facts sender: a refused session is temporary; 404 is retried (batch 4 fix cycle 1)
Spec wrong: §64 (errata F1-2) said re-enrollment changes the producer, so a document under an old producer would be
refused 403. The wire producer has no session in it (central/fleet/node_sessions.py:229-230), so re-enrollment
keeps the producer and the `dropped` state entered on a 401 was never left for the boot. And `off` after one 404
lasted until process exit.
Correction (built): 401/403 keep the document pending (the next ensure() re-enrolls and that tick resends); 404 is
`off` for FACTS_ROUTE_RETRY_SECONDS (3600, the process's monotonic clock), then pending; `dropped` only for other
4xx (409, 422, ...). §64's bullets, diagram and §66 rows updated; §65 shows `_send_facts(self, now_ms)`.
Source: batch 4 fix cycle 1.

## FX2-1 · Host facts take the classifier's state (batch 4 fix cycle 2)
Code drift (not spec-wrong): §62's Silent row and §66 say this boot's values read "… at last report" on a silent or
refused box, but `hostHealth.js` `factItems` took no state, so a silent box's Network cell read "Host Management
reported eth0 up" in the present tense beside "1000 Mb/s at last report".
Correction (built): `factItems(row, read, absent, health)` requires the `classify()` result; `judgeHost` passes the
one it computed and `hostFactItems` computes it, so no caller can word facts without the gate. On `silent` and
`refused` every reported fact's value carries "at last report"; the record's receipt line and the `claimed` base
are unchanged. Model tests cover silent, refused and reporting rows that carry facts, on both `hostFactItems` and
the `judgeHost` page path (mutation probe: ignoring the state fails both).
Source: batch 4 fix cycle 2 review (major).

## FX2-2 · Facts ingest answers `historical` to a superseded boot's session (batch 4 fix cycle 2)
Spec drift: §64's facts table listed only `recorded`, while the observation path in the same module answers
`historical` when the session is not current. Correction (built): `record_facts` answers `historical` when
`principal.current` is false (it still stores; G12 never serves it). §64's three storing rows updated.
Source: batch 4 fix cycle 2 review (minor).

## FX2-3 · Cross-replica clocks in coalescing and host silence (batch 4 fix cycle 2) — OPEN, spec wrong
§60 calls coalescing and host silence "a self-comparison" with guarantee "construction". `received_at` and
`first_received_at` are stamped by the ingesting replica's process clock (node_observations.py `_record`,
`record_facts`, via `admission.ensure_current(self.sessions.clock)`), `_coalesced_in` compares against the same
replica's `now`, and G12's `read_at` comes from the serving replica's clock. With several Central replicas these
are different clocks; the error is bounded by NTP skew (a lagging ingest replica coalesces more; a leading G12
replica calls silence early). Not fixed here: the class fix is to stamp receipts and `read_at` from PostgreSQL
`clock_timestamp()` in the same transaction (the rollout gate's precedent), which changes every node ingest path
and its fake-clock tests — a bead of its own, not a fix-cycle edit. Until then §60/§66 should state the skew as a
cost instead of "self-comparison". Needs an architect decision.
Source: batch 4 fix cycle 2 review (minor).

## FX2-4 · Approved Q13/§52 record rewritten in place (batch 4 fix cycle 2) — OPEN, doc process
Part G §52's batch-B table and the Q13 block (owner-approved 2026-10-02 with the base version inside the host facts
record) were rewritten in place to say the base is a `claimed` tag, while §1 and Part G's status still read
"Approved 2026-10-02 (Q12, Q13)" and Part H says it inherits G13 unchanged. The Part H history line records the
change. Proposed: restore §52 and Q13 as approved and add an amendment line naming Part H §62/§63 and the batch-4
gate; confirm with the owner that the gate covered dropping the base from host facts. Not edited by the fix cycle
(an approved record is the orchestrator's/architect's to amend).
Source: batch 4 fix cycle 2 review (minor).

## FX2-5 · Host coalescing can drop a short-lived fault_code (batch 4 fix cycle 2) — OPEN, unstated cost
`_coalesced_in`'s host branch coalesces any post within one interval of the newest stored receipt, whatever its
`fault_code` (RecoverySupervisor.telemetry, appliance/node/recovery.py). On a pre-batch-4 base posting every 2 s, a
recovery state shorter than about one interval can go unstored. §60 and §66 state the App Manager cost (FX1-1)
but not this one. Not fixed here: keying host coalescing on `fault_code` (as App Manager on its key) changes the
host cap arithmetic (OBSERVATION_DAILY_CAP = 2 x intervals assumes one stored sample per interval), so it needs
the cap re-derived alongside it. Either build that, or add the loss to §60's costs and §66.
Source: batch 4 fix cycle 2 review (minor).

## 2026-10-02 — console DDD batch 4: architect course-correction pass (after 10 implementers)
Dispositions of the open fix-cycle-2 items, folded into `docs/operator-console-ddd.md` (one history line):
1. **FX2-3 (spec wrong) — CLOSED in the doc, class fix deferred.** §60 no longer calls coalescing a self-comparison
   across replicas: receipts and G12 `read_at` are per-replica process clocks; the guarantee is construction on one
   replica and NTP across several (§60 "What it does not cover", §63 Times, §66 row, §69 cost). The class fix (one time
   authority: PostgreSQL `clock_timestamp()` in the same transaction, behind a connection-bound clock port) is residual
   bead **R-clock** (§69 Deferred). Its scope includes node session issue/expiry and the status read's ages, which share
   the class and predate batch 4 (`node_sessions.py:179-214`). Not a batch-4 blocker; owner schedules it.
2. **FX2-5 (unstated cost) — CLOSED in the doc.** Stated as a cost (§60, §66, §69). Keying host coalescing on
   `fault_code` is rejected: a batch-4 base posts once per interval (`host_runner.py` `_observation_due`), so it never
   sends the intermediate sample and keying would recover nothing while forcing a cap re-derivation.
3. **FX2-4 (doc process) — CLOSED in the doc, owner confirmation OPEN.** §52's Q13 block quotes the owner's answer
   verbatim (base version among the host facts) with a dated amendment moving the base to a `claimed` boot-offer tag;
   Part G's status lists every post-approval amendment (base, counts line, Frame-row host items, storage, "occurred
   recently"). The orchestrator must confirm with the owner that the batch-4 gate covered the base change.
4. Doc status line 3 said Part H "awaits the owner's batch-4 gate (Q14)"; corrected to approved and built.
Source: architect course-correction after batch-4 fix cycle 2.

## 2026-10-02 — console DDD batch 4: fix cycle 3 (residuals + owner base decision)

## FX3-1 · G1 was unenforced and false for Needs attention — FIXED
`hostHealth.js` imported `hostRow` from `fleetHosts.js` (the polling hook, which imports `apiWrite.js`), so
AttentionList/AttentionStrip/hostHealth/HostChip reached the write primitive; no test covered the attention modules.
Fix (built): `hostRow` moved into `hostHealth.js`; `tests/test_console_routes_r4.py` gains G1 tests over the bundler
graph (`G1_LIST_MODULES` reach no `WRITE_MODULES`, no route table, shell module or `*Page.jsx`), a positive control,
and a copy-mutation self-test. `WRITE_MODULES` and the scan closure now live in the R4 suite;
`test_console_wall_layout.py` imports them. Mutation-probed on the real tree: re-adding the fleetHosts import fails
4 G1 cases with `{'apiWrite.js'}`.

## FX3-2 · Coalescing compared Central receipt clocks across replicas — FIXED (supersedes FX2-3 for coalescing)
`_coalesced_in` now compares the incoming `sampled_boottime_ms` with the stored sample's (host: the newest by
sequence; App Manager: the recent rows with the same key), 0 <= diff < interval*1000. One producer = one boot =
one kernel boot clock, so a clock is compared only with itself. Cost: a node that misreports its boot clock defeats
coalescing for its own producer only; the cap and `intake_full` still bound it. FX2-3 / R-clock still cover
receipts, G12 `read_at`, host silence and the status read's ages. DB tests: a stepped Central clock and two replicas
skewed +/-40 s; a receipt-clock mutant fails 3 of them. Doc §60/§64/§66/§69 updated.

## FX3-3 · Players table alarmed on spares and sorted retired boxes above healthy ones — FIXED
New pure model `hostHealth.js` `playersTable(rows, hosts)`: Bound rows judged and tiered worst first; spares
(Unbound, Not enrolled) unbanded (items `band: null`, no tier) below them; retired rows unjudged and last, rendering
`RETIRED_NOT_READ` (moved to `players.js`, shared with PlayerPage). Decision to flag: Player › Health for a spare
still shows bands (it is that box's own page); only the fleet table drops them. Model + browser tests, mutation-probed.

## FX3-4 · Facts intake could be spent by restart loops — PARTLY FIXED
`record_facts` claims `host_facts` intake only when values change (same-values resend at a higher sequence rewrites
the row for free). §64's cap claim corrected. DEFERRED: serving `facts_intake_full` in G12 and wording it in
`hostFactItems` (only a burst of real changes can now reach the cap).

## FX3-5 · Owner decision: the node reports its own base — BUILT; spec finding
`HostFactsV2.base_tag` (token<=128, nullable, in `values()`), bootstrap writes `base_tag` into `host.json` from the
verified handoff offer, `host_runner` requires it in its config and reports it, G12 serves `facts.base_tag`, the
console shows "Host Management reported base X" beside "Central's offer: base Y (claimed …)" plus a derived
"Base differs: …" fact (no band, no incident). FINDING: the base image carries no build-time version (squashfs is
content-addressed; tags are assigned at release), so the node's only self-record is the handoff offer tag its
initramfs verified against the mounted bytes; a mismatch means a different offer than the admission Central holds,
not different bytes. Stated as a cost in §52. Wire: HostFactsV2's exact key set gained `base_tag`; safe only because
batch 4 is not on main (no deployed node sends host facts yet). FX2-4 closed.

## FX3-CC · Architect course-correction after fix cycle 3 — DOCS ONLY
Doc drift fixed in place (no code change): Part G status, Part H's inherited list and the top status still called
the base a `claimed`-only tag pending owner confirmation; G13's field list omitted `base_tag`; §60's jitter bullet
still reasoned about receipts (now: node-side poll latency between the tick's `sampled_boottime_ms` stamp and the
monotonic due check can coalesce one post, gap ≤ ~30 s; the first post after a new session is coalesced when within
one interval of the last stored sample); §61's Software example showed one base; §62's at-last-report / no-facts rows
did not say which base lines change; §66 limited coalescing's guarantee to one replica; the fleet map named `_record`
for the window; the runbook's Players ordering, Software column and no-facts row were stale, and it gains a
"Base differs" row. FX3-3 (bands on a spare's own Health page) is recorded in §61 and put to the owner in §69.


## FX4-1 · Releases browser race: "response arrived" was read as "page took in the read" — FIXED (test bug)
CI 37085580521 / 37066151357 failed `test_a_dialog_frozen_before_another_pages_selection_sends_no_put` (`puts == []`):
Playwright's response event fires on headers, before `apiWrite`'s `response.json()`, so Select judged the previous read
and Central's expected_revision 409 fenced it (CHANGED passed). Fix: `usePolledRead` returns `busy`, set when a flight
starts and cleared in the same synchronous step that commits the flight's last value (after the ref), so
`busy === false` in the DOM implies `latest()` returns that read; `useReleaseRead` passes it through; ReleasesPage's
read line is `role=status name="Release read"` with `aria-busy`; `_read_once` waits for aria-busy to clear (drive_poll
pattern). Send rule unchanged; `test_centrals_409_reads_changed` documents the 409 as the fence for an unsettled read.
Mutation-probed with a held body: without the wait one PUT is sent; with it `_read_once` blocks until released, then
zero PUTs. Not done: the other `usePolledRead` pages (nodeRead, nodeControl, fleetHosts, qualification) get `busy`
from the hook but render no aria-busy yet; Update the wall's read line is not a status region.

## FX4-2 · "Base differs" cannot mean a different offer — DOCS CORRECTED (supersedes FX3-5's claim)
FX3-5, §52 and the runbook said a mismatch means "a boot from a different offer than the admission Central holds".
The code cannot produce that: Host Management claims with the handoff's `offer_id` (host_runner.py:186), every claim of
a boot must present the admission's `offer_id` (node_sessions.py:168 `node_boot_adoption_mismatch`), and G12 joins the
facts of the current admission's producer to that same offer's stored payload (node_observations `_FLEET_HOSTS_SQL`).
Both tags are copies of ONE offer (node: handoff copy; Central: stored payload), so they differ only on a defect (the
node reports a tag its handoff does not hold, or Central stored a payload other than the one it served). §52, §69 and
the runbook row now say so; the runbook action is "report it as a bug". No code change.

## FX4-3 · Spares were unbanded only in CSS; the words still carried Central's judgement — FIXED
`playersTable` read a spare through `judgeHost` then stripped bands, so its words kept "· hot", "silent · …", "at last
report" and "Central's threshold/limit". Now `describeSpare` reads the box with Central's thresholds withheld, then
unbands what remains (Unknown, App Manager's refusal). Cost: a long-silent spare's values read with their receipt age,
not "at last report". Model test asserts the words (no hot/warm/silent/at last report/Central's …), mutation-probed;
browser test likewise. Player › Health for a spare still bands (FX3-3, owner question §69, unchanged).

## FX4-4 · PID1 outage scenario: outage trigger raced the broker — FIXED (test bug)
`central_outage_after_accept` started the outage on the App Manager's target-artifact request, which says nothing about
the App Effect Broker (independent app-commands poll). On 7758772 the manager won; the broker never accepted, no
online.json, no switch. The fixture now starts the outage only when BOTH Central recorded the broker's response to this
stage's command (a `node_app_responses` row joined to the operation, checked on a 200 POST /v2/node/app-responses) AND
the target artifact was served. Diagnostics: `prepared.json` captured; absent online.json recorded as
`broker_never_accepted.txt`. Not run locally (needs the node components + fixture image build); CI node-pid1 matrix is
the gate, outage needs reruns to show the race gone.

## FX4-5 · Minors — FIXED
G1: the Needs attention page moved from neutralRoutes.jsx into `AttentionPage.jsx`, now a G1 list module (its reach,
minus itself, holds no page or write; the classifier-mutation test covers it). G12: stored host facts are read through
`stored_fact_values` (contracts/node_host_facts.py), tolerant per field (missing/refused fact -> None), so one
older-shape row cannot fail the fleet read or the same-values comparison; ingest stays strict. Unit-tested; no DB test
of an old row through G12 itself.

## 2026-10-02 — console DDD batch 5 (Part F): architect reconcile before implementation

## B5-0 · G11's DatabaseClock(db) -> Clock was wrong — DOC CORRECTED (spec wrong, found at reconcile)
Part F §41/§42 composed a process-wide `DatabaseClock(db) -> Clock` into `MediaRepository`. Against the code:
(1) `repository.clock` flows into `MediaStore`, `MediaWorker`, `Preparer`, `ImmichClient` and the installation
repository (`media/worker.py:156-170`, `central/media_store.py:141-142`), so the swap re-means about thirty call
sites, most of them monotonic budgets; (2) clock reads happen inside held transactions
(`central/media_repository.py:307`, `:316`), so a clock taking its own pooled connection can exhaust the
ten-connection pool (`central/db.py:15-27`). Correction (§41, §42, bead M1): a connection-bound
`TransactionClock.now_in(conn)` used only where a media time another process compares is written or compared;
the process `Clock` stays for budgets. Same port as Part H's deferred R-clock (§69). G11 cut out of L2 as bead M1.

## B5-1 · Part F reconciled with Parts G/H as built — DOCS ONLY
Planned facet → Status facet (W1 built it; S1 adds the `planned` fact; `NowShowingFacet.jsx` → `StatusFacet.jsx`,
`PrecedenceExplanation` split so the Show side imports no Wall-facet module); `ShowNowFlow.jsx:233` also says
"Now showing" and S1 renames it; Q8 kept (requirements.md unchanged, gaps in §44), Q9 = A, Q10 = yes, Q11 moot.

## B5-S1-1 · "import HostChip into StatusFacet -> R4 fails" is void after the split — SPEC WRONG (found at S1)
S1's mutation probe assumes the Status facet is shared with the Show side. Splitting `PrecedenceExplanation.jsx`
out (§34) makes `StatusFacet.jsx` Wall-only, so importing `HostChip.jsx` there is not an R4 violation and
`tests/test_console_routes_r4.py` stays green (probed: 35 passed). The property R4 protects still holds by
construction: the same import into the now-shared `PrecedenceExplanation.jsx` fails R4 (`['HostChip.jsx']` in the
Show and shell closures; probed). S1 keeps `hostChip` handed in by `Inspector.jsx` as specified, but no test binds
"the facet does not import HostChip" and none is owed. §45's S1 probe list should name the shared module instead.

## B5-S1-2 · §35's unbound row omits "; the Panel is not observed" — WORDING RULE WINS (found at S1)
§35's rule says a `planned` fact's wording ALWAYS ends "(Central's Runs; …; the Panel is not observed)"; its table
row for an unbound Frame ends "…so Central sends it no layers)". S1 follows the rule: "On top: xmas · Program p
(Central's Runs; this Frame is unbound, so Central sends it no layers; the Panel is not observed)". The table row
should be corrected in D1.

## B5-S1-3 · Two direct-origin wordings, one home — NOTED (S1)
§34 words a direct Run card "started directly (Show now or the API)"; §35 words the planned fact "started directly,
by Show now or the API" (a parenthesis inside the fact's own parenthesis). S1 keeps both: `runOrigin` (showState.js)
owns the card words, and join.js `originPhrase` maps only the `direct` kind to the comma form for the planned fact and
the Why heading/rows. `sceneTargets.js` left `SHARED_WITH_SHOW` in the R4 test because join.js now imports
showState.js (which re-exports it), making it one of the shell's own modules.

## B5-L1-1 · The preview answer grows flat; "counts.images" / "code" in §41 and the tracer are not the served names — SPEC WRONG (found at L1)
§41 sketches `source_preview(request_id) -> {status, counts?, shown?, limited?, code?, observed_at?, read_at}` and the
tracer says `counts.images = 1`. The existing resource serves flat `count`, `image_count`, `video_count` and `error`
(`central/media_repository.py` `source_preview`), and §38 says the resource GROWS. L1 keeps the flat fields and adds
`shown`, `limited`, `observed_at` (complete only) and `read_at` (every status). L3 and D1 should cite `image_count`,
`video_count` and `error`, not `counts.*` / `code`.

## B5-L1-2 · Served sizes need the preview walk to ask for metadata; sizes are nullable — SPEC GAP (found at L1)
The count-only preview walked with `withExif=false`, which carries no usable sizes or video duration (the fixture's
top-level `width`/`height` are decoys the adapter never trusts). L1's preview walks each kind once WITH metadata and
takes sizes and duration from `_original` (orientation-corrected). A member whose library metadata is unusable is still
counted and shown, with `width`, `height` and `duration_seconds` null (`PreviewMember`). L3's tile alt text must allow
a missing duration.

## B5-L1-3 · L1's ages cross two process clocks until M1 — NOTED (L1; G11 was a non-goal)
`observed_at` is stamped by the writer's process clock in `finish_source_preview` (the media worker's) and `read_at`
by Central's in `source_preview`, so "first received N s ago" subtracts one process clock from another until M1. M1's
list must include both: `finish_source_preview`'s `observed_at` and `source_preview`'s `read_at` (and the expiry
comparisons beside them) go through `TransactionClock.now_in(conn)`.

## B5-L1-4 · Things L1 had to touch that the bead row does not name — NOTED (L1)
(1) `central/source_names.py` `NamedSourceWrite` re-declares the query fields; without `tags` there a tagged Source
cannot be saved, and its `_same` compared raw JSON, so it now uses `MediaRepository.same_spec` (one canonical compare
for both write paths). (2) The console draft carries a saved Source's `tags` through `seedSource` and
`buildSourceSpec` (no picker), or editing a tagged Source would silently save it untagged; the tracer's browser test
uses that path. (3) Migration 063 replaces the unnamed state CHECK, which PostgreSQL named `source_previews_check1`
(`source_previews_check` is `expires_at > created_at`); a completed row written before 063 has no sample and is retired
as `failed`/`preview_expired`, never back-filled with an empty one. (4) `tests/test_node_upgrade_history.py` asserted
062 is the last migration; it now asserts 062 is applied. (5) An unreadable stored spec records `status=incompatible`,
diagnostic `spec_unsupported`, pushes `next_refresh` by `refresh_seconds` and completes the requested revision, so
neither the scheduled tick nor a requested refresh loops on it.

## B5-L1-5 · "Showing the newest 24." is not rendered in L1 — DEFERRED to L3 (L1)
§39's over-the-limit row ends "Showing the newest 24."; with no tiles until L2/L3 that sentence would describe nothing
on screen, so L1 renders the fact and "Photo Wall currently stops at 1,000 matches; narrow it with tags or dates."
only. `sourcePreview.js` `previewFacts(answer) -> {fact, notes}` is L3's starting point (§41 sketches
`previewFacts(answer, readAt, connections) -> Fact[]`; `read_at` now travels in the answer).

## B5-M1-1 · `media_references.expires_at` is cross-process but stays on process clocks — SPEC GAP, DEFERRED to R-clock (M1)
M1's inventory missed one column. `media_references` rows are written by Central (Runtime pins from plan validity,
`coordination.py` `pin_variants_in`; transfer grants `now + _TRANSFER_SECONDS` in `MediaStore.open_read`, whose `now`
also feeds `media_authorized_in`) and compared by the worker's `MediaStore.collect` against the worker's process clock
(`media_store.py` collect, "expires_at>%s"). A worker clock ahead of Central's evicts a pinned blob early. Not moved:
the pins are Runtime plan times on Central's process clock and `open_read`'s `now` is Runtime authorization time, so
moving only the media side would compare a database time with a Runtime time. It belongs to R-clock (§69), which
puts Runtime receipts on the same `TransactionClock`; a comment marks the comparison in `collect`.

## B5-M1-2 · The catalog's retry cooldown crossed clocks; `catalog_in` loses its `now` — NOTED (M1)
`_hydrate_candidates` compares worker-written `media_jobs.retry_at` ("state='retry' AND retry_at>now") and
`catalog_in(conn, now, …)` took `now` from the Coordinator's process clock. M1 makes `catalog_in(conn, source_refs)`
read `now_in(conn)` itself, and changes the `CoordinationMedia` port to match (§41 does not list it).
`media_jobs.earliest_start` stays: it is a Planner time on Central's clock, used only for ordering.

## B5-M1-3 · Composition defaults — DECISION, flagged (M1)
`MediaRepository(…, *, times)` is required (a missing `times` is a TypeError, pinned by a test). `create_app` gains
`media_times`; when omitted it is `DatabaseTransactionClock()` unless the caller injected its own `clock`, in which case
it is `ProcessTransactionClock(clock)` — the same "an injected clock means a test" convention `run_scheduler` already
uses at `app.py`, so the 31 test files that build the app with `ManualClock` stay deterministic. Production builds the
app with no clock. The `coordination.py` fallback (`media or …`) composes `DatabaseTransactionClock()`.

## B5-L2-1 · Thumbnails break two asset-layer invariants the design did not account for — SPEC WRONG (found at L2)
§38 keeps thumbnails "on the asset layer" and §43 says "the asset layer refetches", but the layer assumes a key fixes
the bytes: (a) migration 028's `asset_references_locator_names_the_key` CHECK requires every reference's locator
digest to equal the key, and Central holds no library address (R22); (b) produced facts are write-once
(`record_produced`, `AssetProduction` "not_reproducible"), while a thumbnail is keyed by its ORIGINAL (asset id) and
its bytes change when the library regenerates it or Pillow changes — a purge then a refetch would fail forever (PR 37
§7 "Why thumbnails are not Asset records" foresaw this). L2 keeps the asset layer and closes both: 064 narrows the
CHECK so a `library-thumbnail` admits exactly one reserved reference (`owner=library-preview`,
`locator_url=http://library.invalid/`, no digests; `central/assets/library.py THUMBNAIL_REFERENCE`, which no handler
reads), and `MediaRepository.maintain_source_previews` deletes the records of thumbnails no live preview selects
(the worker then sweeps their files from `previews/`), so facts live only as long as a live preview. Residual: a file
re-written between the purge and the sweep of one maintenance pass can be swept; the next request refetches it.

## B5-L2-2 · The thumbnail handler is Central's; only its library half is in media/ — DECISION, flagged (L2)
The bead says "its handler lives in media/". The handler needs the asset layer's cache store, record check and
install (`AssetProduction`), which are built inside `build_job_runtime`. Following the existing `ReleaseOrigin` /
`FetchPackageHandler` split, `FetchLibraryThumbnailHandler` is in `central/assets/handlers.py` and calls a
`ThumbnailOrigin` port (`central/kernel/ports.py`); `media/library_thumbnails.py LibraryThumbnailOrigin` implements
it (servability re-read, `ImmichClient.thumbnail`, private O_EXCL write) and is injected via
`build_job_runtime(..., thumbnails=)` (required keyword). A new import-linter contract, "Central imports no library
client", forbids `central` from importing `media.immich`, `media.library_thumbnails` and `media.worker`.

## B5-L2-3 · Tag list choices L3 and D1 should cite — DECISION, flagged (L2)
(1) The re-list gate is the last ATTEMPT (`library_tags.checked_at`) older than 300 s by the database clock, so a
failing library is asked every 5 min, not every 30 s tick; `observed_at` stays the served list's own age. (2) Boot
lists every connection regardless of age and drops rows of connections the worker no longer holds; a failed boot
list keeps the last list (no fingerprint, §38). (3) The served tag is `{tag_ref, path, name, parent_ref}` — PR 37
§7's shape; §38's boundary table says "tag id, path, name" but L3's nested-tag replacement needs the parent.
(4) `GET /v1/operator/library/tags?connection=&q=&limit=` answers `{connection_ref, status, error?, observed_at,
read_at, total_matches, tags}`; `status` is `pending` before the first listing (200, empty), `ok`, or the failure
status with the last list; 404 `connection_unknown` when the worker's reported list excludes the connection; 422 for
`q` > 128 chars or `limit` outside 1–20. Names and paths are stripped of C0/C1 controls, LRM/RLM and the bidi
embeddings/isolates at construction (`media.models.LibraryTag`). No per-pod parsed cache: each GET parses the stored
list (≤ 5,000 tags).

## B5-L2-4 · Thumbnail route as built — NOTED (L2)
Errors: 404 `thumbnail_unknown` (also for a malformed id, not PR 37's 422), 403 `origin_mismatch` for a sent
`Sec-Fetch-Site` other than `same-origin`, 503 `thumbnail_<reason>` with `Retry-After` (`thumbnail_busy`,
`thumbnail_timeout`, or the fetch's failure reason); CORP on every answer; a served tile adds `nosniff` and
`default-src 'none'; sandbox`. The route lives in the new top-layer `central/library_routes.py`, which also installs
the access-log filter (query strings dropped under `/v1/operator/library/`). `build_content_services` gains
`servable_thumbnail` (default: nothing servable). Each fetch checks the library version and owner again (three
library requests per tile; PR 37's once-a-minute check is not built). Proven where: the "fifth concurrent cold
request is busy at once" acceptance is tested on the composed thumbnail reader (`tests/test_content_wiring.py`),
not over HTTP (TestClient serializes requests); the access-log filter is tested on the `uvicorn.access` logger,
not through a running uvicorn.

## B5-L2-5 · For D1 — NOTED (L2)
ADR 0013 and the central-cache module gain `previews/` (Dockerfile both stages, entrypoint loop, worker boot
`ensure_previews_directory`) and the `library-thumbnail` kind with its record lifecycle (B5-L2-1); migration 064
(tag table, kind CHECK, narrowed locator CHECK); the media worker doc gains the tag tick/boot and the prefetch.

## B5-L3-1 · The tag GET cannot name a saved tag in a library with more than 20 tags — SPEC GAP (found at L3)
§39 asks the Source card and Review to name a saved Source's tags ("tagged Family/Christmas") and to say "A tag this
Source uses no longer exists in your library." The served route (B5-L2-3) answers at most 20 tags matching `q` against
path or name; it has no lookup by tag id. L3 therefore names a saved tag only once some read has served it, and says
"gone" only when a no-search read served the library's WHOLE list (`status=ok`, `total_matches <= tags served`)
without it (`libraryTags.js learnPaths`). Otherwise it reads "a tag Photo Wall has not looked up yet", never "gone".
Cost: in a library with more than 20 tags, an edited Source's chips and the card summary read that phrase until the
operator types the tag, and a deleted tag is never reported on the card. Fix (not built, a backend residual): an
`ids=` parameter (at most 4 UUIDs) on `GET /v1/operator/library/tags` answering those tags and naming the absent
ones; the console then passes `whole` for those ids.

## B5-L3-2 · Preview signatures as built — DECISION, flagged (L3)
§41 sketches `sourcePreview.js usePreview(query)` and `previewFacts(answer, readAt, connections) -> Fact[]`. Built:
the hook is `usePreview.js` (React), so `sourcePreview.js` stays pure and Node-testable; `previewFacts(preview,
connections) -> {facts, notes, answer}` takes the whole panel state (`{phase, answer, previous, stillLooking, code,
connection}`), because a failure's words depend on whether an earlier answer is on screen and §39 orders plain
statements after the fact; `read_at` travels in the answer (B5-L1-5). Supersession: every run of the request loop
takes a sequence number and the hook's cleanup moves it on; only the current run writes state (probed). A criteria
change waits 400 ms (PR 37 §8) before it POSTs. Failure codes `upstream_unavailable`, `upstream_timeout`,
`worker_timeout`, `worker_cancelled`, `preview_expired` (and a lost/5xx/429 POST or poll, or a 404 GET) are retried
on the same 2→30 s schedule; `upstream_permission`, `asset_permission`, `owner_mismatch` read as "key not allowed";
any other code is final and reads "Unknown: the preview failed (<code>)". "Showing the newest N." shows whenever the
count exceeds the tiles, not only over the limit (it is the §44 paged-view gap's wording).

## B5-L3-3 · The connection step's skip rule, and steps that follow live data — DECISION, flagged (L3)
The Library step is skipped when `connectionRule` says `advanced` (exactly one known connection, including the
pre-report guidance from saved Sources); that connection keeps its place under the Name step's Advanced, as before.
An edit whose saved connection is no longer reported keeps its seed-time shape (`SourceFlow.jsx` `layout`), so
choosing the one reported connection does not remove the step the operator is answering. The flow kit now normalises
a route whose step the flow no longer has to its first step (`useFlowInstance.js`), so a worker report arriving
mid-flow cannot loop the route. §37's unannounced sentence ("This connection isn't set up yet …") is shown for every
unannounced connection, including a name from saved Sources while the worker has not reported its list, where "isn't
set up yet" may be untrue; a separate sentence for that case is a wording question for D1.

## B5-L3-4 · Wording choices beyond §39 — DECISION, flagged (L3)
Labels "Dated from"/"Dated until" (and the window problem "'Dated until' must be after 'Dated from'."), media-type
choices "Photos and videos"/"Photos only"/"Videos only", Scene Photos-step heading "Which Source?" (§37), New Source /
Save Source / "This Source is for your Scene." (the kit's noun). `mediaHealth.js SOURCE_FAILURES` now read "Your
photo library is unreachable / refused access / is unsupported" and "last good refresh <age> ago" (§39) everywhere
they show (Now's pipeline, the Scene flow, cards). `sourceFilters` (Now, the Scene flow's Source status) still says
"taken 2024"; only the new `selectionWords` says "dated" — D1 or a follow-up should make them one. The card's
"Refreshed" line is the `reported` fact only for a Source whose status is ok; a failing Source points to Status (its
"last good refresh" age), so no line says "last reported" for a library that last refused.

## B5-A5 · After-5 course-correction — DOC UPDATED; correction bead C5 owed before verify (architect)
Folded into Part F (§35, §37–§45, history): B5-S1-1/2/3, B5-L1-1..5, B5-M1-1..3, B5-L2-1..5, B5-L3-1..4. L2's thumbnail
closure (064 reserved reference + per-live-preview record lifecycle; Central handler + injected ThumbnailOrigin) is
CONFIRMED with costs stated in §38. Drift found by the architect, owed by C5 (§45): (1) `mediaHealth.js:342`
"Central's plan puts <scene> (priority N) here." states the top Run outside the `planned` wording and escapes the
S1 scan, which bans only "Central's plan for" (`tests/test_console_planned.py:188`) — reword and widen the scan to
the class "Central's plan"; (2) two homes for a Source's selection words: `mediaHealth.js:131-157` `sourceFilters`
("taken", "only favourites", omits tags) vs `sourceFlowModel.js:326` `selectionWords` ("dated", "favourites only",
tags) — one home; (3) B5-L3-1's `ids=` tag lookup is built in batch 5, not deferred; (4) B5-L3-3's pre-report
sentence decided in §37. Residuals (not batch 5): `media_references.expires_at` with R-clock; `create_app`'s
injected-clock inference replaced by tests passing `media_times`.

## B5-BV · Before-verify course-correction — DOC UPDATED; C5 and new docs bead D2 owed before verify (architect)
No new spec error since B5-A5; every B5-S1/L1/M1/L2/L3 item is already folded into Part F and D1 corrected §35's
unbound row (B5-S1-2). C5 is NOT built at 07965d6: `mediaHealth.js:342` still says "Central's plan puts …" and
`tests/test_console_planned.py:188` still bans only "Central's plan for"; `sourceFilters` (`mediaHealth.js:131-157`)
and the chooser string (`mediaHealth.js:235`, not `:223`, which is its comment) still say "taken";
`central/library_routes.py` has no `ids=`; `SourceFlow.jsx:141` shows `UNANNOUNCED_CONNECTION` before the worker
reports. §45 C5 item 4 pinned: branch at `SourceFlow.jsx:141` on `rule.reported`, the sentence beside
`UNANNOUNCED_CONNECTION` (`sourceFlowModel.js:259`); the Name step hints (`SourceSteps.jsx:233`, `:292`) stay.
D1 ran before C5 (its spec_wrong "order conflict"), so D2 (C5's docs follow-up) is added after C5; the verify waits
for both. Static gates at this point: ruff clean, lint-imports 7/7 kept, check_docs passes, uv.lock unchanged.

## B5-FX1 · Batch 5 fix cycle 1 — C5 and D2 BUILT, review findings folded in (implementer)
C5 as §45 specifies, with these decisions: (1) `join.js` gains `plannedFact(runtime, intent, bound)` and
`intentOrigin(runtime, intent)`, the one home of the `planned` fact and its origin; `whyNothingNew`'s Intended? step
reads "<planned fact>; priority N." and `explainPrecedence` now names a child's root Run ("part of xmas's Run, Program
…"), fixing the review's minor (the Why heading misattributed a child to the Program). RETIRED_WORDS bans "Central's
plan" and the old Now heading "Why each frame shows what it does" (renamed "Central's Runs per frame, and why nothing
new"). (2) One home is a NEW pure module `sourceWords.js` (imports only `timeWords.js`, so `mediaHealth.js`'s Show-side
closure gains nothing of the Source flow): `tagWords`, `tagCountWords`, `favouritesWords`, `kindsWords`, `datedWords`.
`sourceFilters(spec, tagPaths=null)` now orders tags · favourites · single kind · dated (selectionWords' order, was
kinds first) and a whole local year reads "dated 2024" in BOTH homes. Tags read "N tag(s)" unless every path is given.
(3) `ids` is a repeated query parameter (`?connection=&ids=a&ids=b`), typed `TagRef` (normalised, so an uppercase
UUID is accepted and canonicalised; malformed → 422), 1–4, `q` with `ids` → 422 `ids_with_query`; answer is the
`library_tags` envelope plus `absent`. `MediaApplication.library_tags_by_id` added. (4) `unannouncedWords(rule)` and
`UNREPORTED_CONNECTIONS` beside `UNANNOUNCED_CONNECTION`.
Review findings fixed: (a) SPEC-WRONG §38/L1: L1 dropped PR 37's `GET tags/{id}` existence check, so a deleted tag
read ok-empty (R6). Restored in `ImmichClient._confirm_tags` for refresh and preview (400/404 → incompatible/
tag_missing); console maps tag_missing to TAG_GONE (card, preview) and "Your photo library no longer has a tag this
Source uses · edit its tags" (state). (b) Over-limit: OVER_LIMIT now says the worker refuses the Source and that a
saved one selects nothing; a `source_limit` Source's state reads "Over Photo Wall's current 1,000-match limit · …",
never "unsupported"; SOURCE_ISSUES gains source_limit and tag_missing. (c) `FetchLibraryThumbnail` priority −50 and
the thumbnail client's budget is `metadata_seconds` (15 s), not `refresh_seconds`; the content_wiring comment and §40
now say the slots bound HTTP waiters, not queue occupancy, and four library requests per tile (B5-L2-4 said three).
(d) CORP on every LIBRARY_PREFIX answer via a middleware (the 401 and 422 had none). (e) owner_mismatch is no longer
"key not allowed" in the preview; it shares OWNER_MISMATCH with the card.
DEFERRED (minor, not trivial): TAG_GONE rendered as a `reported` fact with the tag list's observed_at; zone label on
dated windows in the card/Review and the Narrow hints; servable check and reference write in one transaction
(`LibraryThumbnails._reference_if_servable`); boot tag listing moved off the path before `run_queue()`.
Docs (D2): DDD header, §8 rows 4–5, Part F status, §35, §38, §39, §40, §43, §44, §45 C5 "As built", order and
history; runbook (pre-report sentence, over-limit, tag lookup, Failing row, Now heading, "dated" chooser); console UX
design; module-media (`GET /tags/{id}`, over-limit wording); module-central-cache (priority); the two folded-in pass-2
docs' "taken" examples. `requirements.md` unchanged.

## B5-FC1 · Fix-cycle course-correction — DOC UPDATED; no blocker; residuals named (architect)
Checked C5 + D2 (working tree over ddffa1d) against Part F and the owner answers (Q8a/b, Q9 = A, Q10 = yes):
requirements.md and uv.lock untouched; ruff clean; lint-imports 7/7 kept; check_docs passes. Doc drift fixed in
Part F (one history line): §33 Q8(b) still quoted "Photo Wall currently stops at 1,000 matches" (now the built
refusal sentence); §40 and §44 still said three library requests per tile, and `module-media.md:105` too (four:
version, owner, asset, thumbnail); §38 and §44 still stated the pre-priority FETCH cost (now: a queued Player fetch is
picked first; one can wait behind a running tile's attempt, ≤ 15 s metadata budget, when both FETCH slots hold
tiles); §39 had no preview row for `owner_mismatch` or `tag_missing`; §41 lacked `_confirm_tags`, `plannedFact`/
`intentOrigin`, `sourceWords.js`, `readTagsById`, `unannouncedWords`, and said `sourceState` uses `selectionWords`
(it shares the pieces). New findings, residual (not batch 5): (1) FALSE "GONE": `media/immich.py` `list_tags` skips
a tag whose name strips to nothing (`except ValidationError: continue`), yet `library_tags_by_id` treats an `ok`
list as every id the library holds, so such a tag reads "no longer exists" while the refresh (`_confirm_tags`)
succeeds — §43 "None yet"; fix: the stored list keeps every listed id and only the search hides unnamed ones.
(2) OVERCLAIM: `central/kernel/job_types.py:18` and `tests/test_library_thumbnails.py:59` say tiles "never delay" a
Player; priority guarantees pick order only — reword to "picked after every queued fetch".

## B5-FC2 · Fix cycle 2 (implementer) — B5-M1-1 SPEC WRONG, now fixed; B5-FC1 (1)(2) fixed; residuals named
B5-FC2-1 · B5-M1-1's deferral reasoning was wrong (review finding, verified): the worker need not read
`media_references.expires_at` at all. Central already deletes expired pins on its own clock every coordination
pass (`coordination.py` → `media_repository.py` `expire_pins_in`) and a read lease's close deletes its transfer
grant. FIXED: `MediaStore.collect` now treats any pin row as protecting its blob (`WHERE digest=%s`, no time).
Rule: a `media_references` row is expired only by the process whose clock wrote it. Cost: a Central that stops
running coordination passes leaves pins, so their blobs are never evicted (fails safe for playback, costs disk).
G11 now has no cross-process media-time exception; DDD §42 "as built", §43 row and §44 updated. Test:
`test_worker_clock_ahead_never_evicts_a_pinned_blob_only_central_expires_pins` (mutation: restoring the
`expires_at>%s` comparison fails it). Central's own `open_read` comparison is a Central-replica time (R-clock).
B5-FC2-2 · B5-FC1 (1) FIXED: `LibraryTag` path/name may be empty once stripped (`TagText` max length only);
`list_tags` keeps every listed id; `matching_tags` hides tags with an empty path or name; the console's
`learnPaths` no longer infers "gone" from an unfiltered search (it would now be wrong, since search hides unnamed
tags) — only a by-id `absent` says gone; `tagWords` renders `""` as "a tag with no visible name in your library".
The sanitizer also strips U+061C ALM, U+200B–U+200D and U+FEFF. B5-FC1 (2) FIXED: the comment and test docstring
say pick order only; the test now asserts the DEFERRED job's priority via `_deferrer` (wiring, not declaration).
B5-FC2-3 · RESIDUAL (R-clock): the asset layer's `job_outcomes.retry_not_before`/`updated_at` are stamped on the
worker's clock (`central/infra/execution.py`) and compared on Central's (`central/infra/publisher.py`); thumbnails
inherit this from the OS-image path. Named in DDD G11's limits and the R-clock scope.
B5-FC2-4 · RESIDUAL (code): an identity-keyed `library-thumbnail` record that outlives a lost file can reach a
terminal `not_reproducible` (`central/assets/production.py`) if the library's thumbnail bytes changed (e.g.
regenerated after rotation, same checksum), and stays so while previews keep the record live. Fix direction: for
kinds whose references state no expected digest, let re-production replace `produced`, with a test (record,
delete file, change origin bytes, assert the next fetch serves). Not done in this cycle: it touches the asset
layer's write-once invariant and needs its own design check.
B5-FC2-5 · Minors done this cycle: Now's Runs note no longer claims per-Frame intent (and "meant to show" joins
RETIRED_WORDS); the runbook calls the planned fact Central's Runtime projection; the tag picker announces the
on-screen sentence (pending, unread, failed) instead of "No tags match" (`libraryTags.js` `pickerAnnouncement`);
the Narrow step's From hint names the browser's zone; the Source card's refresh fact credits the count to the
media worker; DDD §39's tag-gone row is `reported` (the console still renders it as a plain line — B5-FX1
residual stands); the console UX diagram's "Scheduled:" chip line is annotated. Still deferred under B5-FX1:
DATES_NOTE beside the dated window on the card and in Review.

## B5-FC2C · Fix-cycle-2 course-correction — DOC UPDATED; no blocker; residuals listed (architect)
Checked fix cycle 2 (working tree over 9fcc051) against Part F and the owner answers (Q8a/b, Q9 = A, Q10 = yes):
requirements.md and uv.lock untouched; ruff clean; lint-imports 7/7 kept; check_docs passes; the touched console and
library tests pass under Node 20 (23 passed). B5-FC2-1 confirmed: `expire_pins_in` (`central/media_repository.py:803-806`)
deletes every expired `media_references` row, grants included, on Central's pass (`central/coordination.py:488`), and no
worker path reads `expires_at` (`central/media_store.py:742`). Doc drift fixed (DDD one history line): §44's findings row
still carved out `media_references.expires_at`; `module-media-worker.md` "One media clock" still stated the exception as
live (the fix cycle's "fixed in docs" missed it); `module-media.md` tag lists and `module-media-store.md` `collect()`
did not describe unnamed tags or pin-row protection; §43's purged-cache row said a regenerated tile recovers "when the
preview expires" (B5-FC2-4: terminal `not_reproducible` while any live preview keeps the record); R-clock (§69) did not
name `media_references.expires_at` across Central replicas; §45's order still listed the fixed fix-c1 findings as
residuals. New finding, residual: `central/console/src/timeWords.js:27-31` `zonePart` does not catch the RangeError an
engine without `timeZoneName: "longOffset"` throws (reproduced under Node 16: `occurrenceTime` throws "Value longOffset
out of range"), so `offsetLabel`'s "local time" fallback is unreachable; fix: catch RangeError -> null, with a test.
Minor, noted not fixed: the Source card says "the media worker accepted N in that refresh" while `sourceState`
(`mediaHealth.js:198`) still says "N valid in the last refresh" for the same count — two wordings, two surfaces.

## B5-FC3 · Batch 5 fix cycle 3 — majors FIXED; minors FIXED or deferred (implementer)
B5-FC3-1 · FIXED (major, refusal ownership): the console's status fall-through ("Your photo library is unsupported"
for every `incompatible`) is gone. One closed table, `central/console/src/sourceWords.js` `SOURCE_REFUSALS`, maps each
refusal code to its owner (`LIBRARY` or `PHOTO_WALL`) and its state words (and the card sentence where it differs);
`mediaHealth.js` `sourceState` and `SourceFlow.jsx` `sourceIssue` both read it, and a code it does not hold, or a
failing Source with no code, reads neutrally ("Refresh failed (…)"). `spec_unsupported`, `connection_mismatch`, every
`connection_*`/`worker_*` code and `owner_mismatch` are Photo Wall's. `tests/test_console_sources.py`
`test_every_served_refusal_code_has_one_owner` harvests the codes raised in media/immich.py, media/worker.py and
central/media_repository.py and fails on a code with no row (a lower bound: a code built at run time is not harvested;
it still reads neutrally). Runbook "Failing" row corrected. Browser fixtures that used `source_unavailable` (a worker
status, never a Source diagnostic) now use `upstream_unavailable`.
B5-FC3-2 · FIXED (major, B5-FC2-4): `AssetKind.keyed_by_content` (False only for `library-thumbnail`) lets a
re-production's new bytes replace `produced` (`AssetProduction.produce` no longer raises `not_reproducible` for it;
`PgAssetRecords.record_produced` updates instead of conflicting). DB test: purge + regenerate three times, across a
repeated preview past the first one's expiry, serves each time and later GETs publish nothing. Cost: between a
re-fetch's install and its recorded facts a same-size tile can be served with the previous `Digest`.
B5-FC3-3 · FIXED (minors): `source_limit` worded as Photo Wall's size limits ("at most 1,000 matches"), the 1,000-count
sentence kept only for a preview that counted more; untagged selection is "everything on your library's timeline (not
archived, hidden or other users' media)"; Why's empty Intended? uses `factText(plannedNothing())` and the precedence
empty state "No Run puts a layer on this Frame now." (both added to RETIRED_WORDS); one `add_prefix_headers` table
drives the no-store and CORP middleware AND the unhandled-500 handler (`_LibraryCorp`/`_NoStoreOperator` deleted),
with a 500 test; DDD §38/§43 and module-central-cache.md no longer claim `facts_conflict`.
B5-FC3-4 · DEFERRED (minor, unmeasured): tile requests' pre-slot DB/thread work (servability, reference write,
`_open_first`, `_touch`) is not bounded by the four thumbnail slots, and the thumbnail `AssetReader._touched` grows per
previewed id for the process's life. Needs a route-scoped bound and a touch-less reader; a non-blocking cap would 503
a 24-tile grid, so it is a design choice, not a trivial fix.
B5-FC3-A1 · OPEN (minor, architect check of fix cycle 3): `sourceWords.js` `refusalIssue` falls back to the raw code
(`codeText`) when a row has no `issue`, not to the row's `state`, so a failing Source's card shows Status "Your photo
library is unreachable · …" beside Issue "upstream unavailable", and "connection mismatch" / "worker exited" for Photo
Wall's rows (probe: node import of sourceWords.js). DDD §39 says the card sentence is given only "where it differs".
Fix: `issue ?? state`; keep the code-in-words fallback for codes outside the table; one test per owner. Also
`codeText` duplicates `mediaHealth.js` `codeWords` (rule of two: one home in `sourceWords.js`, imported by mediaHealth).
B5-FC3-A2 · OPEN (doc, architect check of fix cycle 3): docs/central-system-architecture.md:394-399 still says every
produced fact is write-once and any differing re-production is `not_reproducible`; `AssetKind.keyed_by_content` makes
`library-thumbnail` the one exception. Needs one clause there (documentation bead).
B5-FC3-A3 · NOTED (pre-existing, not fix-cycle drift): `media_repository.py:256` and `:267` reset a Source to
`status='unavailable', diagnostics='[]'` on reactivation and on an expired-lease fence; with any completed revision,
`sourceState` now reads "Refresh failed (unavailable)" (before: "Your photo library is unreachable", which blamed the
library for Central's own reset). Neutral, but "failed" overstates a reset awaiting its refresh; candidate for its own
code or an "Awaiting refresh" branch.

## PR41-FR · Final fix round for the whole PR (implementer)

PR41-FR-A1 · APPLIED (B5-FC3-A1): `refusalIssue` returns the row's `issue ?? state`, the code in words only for a code
outside `SOURCE_REFUSALS`; `codeText` deleted, one helper `facts.js` `words` (`mediaHealth.js` `codeWords` is it).
Test: every row's issue equals `issue || state`, one per owner, unknown code in words; mutation (`issue ?? words(code)`)
fails it.
PR41-FR-A2 · APPLIED (B5-FC3-A2): central-system-architecture.md §three facts names the one `keyed_by_content=False`
kind (`library-thumbnail`), whose facts a later production replaces.
PR41-FR-1 · FIXED (major, owner class): codes are single-owner where raised. media/immich.py raises `item_over_limits`
for `max_dimension`, `max_pixels`, `max_video_seconds` (malformed values stay `metadata_invalid`) and `time_budget` for
`_Budget.remaining` and the `asyncio.timeout(refresh_seconds)` of refresh, preview and tag listing (one request's own
deadline stays `upstream_timeout`). Both are Photo Wall rows; `metadata_pending_or_invalid` is Photo Wall's neutral
"No item this Source found could be used · see each item's reason"; `unsupported_version` moved to Photo Wall ("This
Photo Wall release doesn't support your photo library's version · check the supported versions"). The worker's
`_PERMANENT` gained `item_over_limits`. New test `test_a_code_raised_for_photo_walls_own_limits_is_never_the_librarys`
(AST harvest of media/immich.py: codes raised under a `limits`/`limit` comparison, in `_Budget.remaining`, or in a
TimeoutError handler of `asyncio.timeout(self.limits.refresh_seconds)` must not be library-owned; `_text`'s schema
length bound is excluded by name). Mutation (`metadata_invalid` for the video limit) fails it.
PR41-FR-2 · FIXED (major, preview table): `sourcePreview.js` reads `SOURCE_REFUSALS` (rows carry `preview: "retry" |
"key"`); UNREACHABLE, KEY_REFUSED and FAILURE_NOTES deleted. The phase "unreachable" is now "retrying": "can't reach
your photo library" only for library-owned codes; `preview_expired` (new Photo Wall row), `worker_timeout`,
`worker_cancelled` retry in their own words. Any other row fails with "the preview failed" + the row's card sentence.
DDD §39 preview rows updated. Browser test: a stopped worker's preview never says "can't reach your photo library".
PR41-FR-3 · FIXED (major): `AssetProduction.produce` raises TerminalFailure(`thumbnail_unknown`) for a missing record of
a kind not `keyed_by_content` (one shared constant `kernel.ports.THUMBNAIL_UNKNOWN` for route, origin and production).
DB test `test_a_fetch_for_a_retired_record_never_blocks_the_next_preview`; mutation (transient again) fails it.
Decision flagged: the condition reuses `keyed_by_content` as the finding specified; the real property is "records
retire with their selector", which today coincides with it. A future non-content-keyed kind whose record is not
liveness-retired would need its own property.
PR41-FR-4 · FIXED (major, R10): the calibration preview answer serves `lease_seconds` (registry
`CALIBRATION_LEASE_SECONDS`) beside `expires_at`; `useCalibration.js` counts it down on `performance.now()` from the
answer's arrival (can read up to one request's latency long; the end stays the poll's). Scan test
`test_the_browser_clock_is_never_compared_with_a_served_time` bans Date.now() outside useSnapshot.js and authoring.js.
Browser countdown pins 29/19 -> 30/20.
PR41-FR-5 · FIXED (major): `sourceWords.js` `refreshFact` is the one home of a Source's refresh; `sourceState`'s ok label
is that fact (+ qualifier, filters), and the card drops its separate Refreshed line for an ok Source (overdue/empty keep
it). `workerState`'s ok line is a `reported` fact ("Media worker last reported 20 s ago · …"). Runbook rows updated.
PR41-FR-6 · FIXED (major): `health.js` `livenessFact` (Player app layer, `reported`) is the one builder for the Wall's
liveness and the Player page's Player app row: "Player app last reported 4 s ago", "Player app silent · last reported
2 h ago", tile "Player app silent". "Player silent", "last heard", "Last heard" added to RETIRED_WORDS. DDD §55 J1,
runbook liveness rows updated.
PR41-FR-7 · PARTLY FIXED (major, held request): `sendOutcome.js` holds UNKNOWN_MESSAGE, CHANGED_MESSAGE, RESEND_LABEL
("Send the same request again", now the reboot, stage and publish confirm label; the reboot opener reads "Send the
reboot request again"), `answerUnknown(result, centralRefusal)` (the one 5xx rule, the verb passing which served codes
are its own refusals; equipment, reboot and release classifiers use it) and `useHeldRequests` (publish holds use it
directly, `useHeldStage` is built on it). RESIDUAL: Reboot's held request still lives in component state
(PlayerCommands.jsx, UpdateWallPage.jsx) plus the module-level `rebootsInFlight` set; moving it onto `useHeldRequests`
touches two pages' dialog flows and was not cheap in this round. Needs its own bead.
PR41-FR-8 · FIXED (major, display read): `DISPLAY_OUTPUTS_SQL` reads only the newest 4 display_host producers of the
current admission (`_MAX_PRODUCERS`, by admitted_at), picks each Output's winner in SQL (DISTINCT ON, LIMIT 64) and
fetches `request` only for the winners. DB test with 12 flooding producers asserts the plan reads at most
4×(2×64+2)+64 exchange rows; mutation (no producer LIMIT) reads 1600 and fails it. Semantics change: a Display Host
restarted more than 4 times in one boot no longer contributes its oldest producers' Outputs (DDD §16 lock-cost text
updated). Chosen over newest-producer-only to keep the existing test's "older producer's other Output still served".
PR41-FR-9 · FIXED (minors): timeline qualifier said for every selection (`TIMELINE_ONLY`, last part); the regenerate
test's "GETs publish nothing" now drops the queued job first and asserts 0 (mutation: publish on every read fails it);
the "previous Digest" cost restated in module-central-cache.md and DDD §38 (the reader checks size only; the route sends
no Digest); `add_prefix_headers` registers the unhandled-500 handler with its table (bare-app test), doc string fixed.
PR41-FR-10 · DEFERRED (minor): Player page "enrolled" twice (`playerStanding` from `registered_at` beside
`enrolledFact` from `last_seen`); the finding's fix text was truncated in the brief, so the wording choice is open.

## HC · Worker healthcheck course-correction (architect, 2026-10-03)
HC-1 · FIXED, failure class: FIRST-PARTY COMPOSITION HIDDEN IN YAML. compose.yaml's worker healthcheck was an inline
`python -c` snippet that built `MediaRepository(Database(...), SystemClock())`. G11 made `times=` a required keyword, and
nothing reached the snippet (not ruff, not lint-imports, not a test), so it failed only at `docker compose up --wait`
(TypeError). It also compared the probe's `time.time()` with a database-clock `worker_seen`, an R10/G11 violation no
clock scan could see. Fix: `python -m media.healthcheck`, composed by `media.worker.build_repository` (the same root as
the worker entry), aged by `MediaRepository.worker_age()` on `times.now_in(conn)`; media/healthcheck.py joins the G11
module scan. GUARD: tests/test_compose_healthchecks.py refuses a compose healthcheck whose inline snippet imports a
first-party package. Guard limits (residual HC-3): it scans `test:` lines line-by-line, so a block-style list
(`test:` then `- python` items) or a CMD-SHELL string using `python3 -c` passes unchecked; it covers compose.yaml and
tests/integration/*.yml only. The same class exists in CI YAML: .github/workflows/base-image.yml:482, :490, :907
import scripts.*/contracts.* inside `python3 -c` (caught only when that workflow runs). Stronger form: parse the YAML
(PyYAML is not a dependency today), reject any `-c` payload naming a first-party package in compose AND workflow files,
and require every `-m <module>` to resolve (importlib.util.find_spec).
HC-2 · STOP (spec contradiction, needs an implementer cycle): media/task_queue.py sets WORKER_CHECK_IN_SECONDS=30 and
WORKER_FRESH_SECONDS=35 claiming "the worker checks in at least once per refresh tick". False when idle:
media/worker.py:247-250 `refresh_once` returns before any `worker_status` when no Source is due, and `list_tags` never
checks in; the guaranteed cadence is boot plus maintenance every 5 min (MAINTENANCE_CRON), which
central/console/src/mediaHealth.js:27 already states as WORKER_CHECK_IN_SECONDS=300, pinned by
tests/test_media_queue.py:204. Same-name constants now disagree (30 vs 300). A busy worker also exceeds 35 s while one
refresh runs (WorkerLimits.refresh_seconds=65; check-in after publish). Predicted effect: an idle stack's worker turns
unhealthy about 65 s after boot (35 s + 3 retries x 10 s) until the next maintenance pass, so a second
`docker compose up -d --wait` (runbook launch and Recovery) fails. Spec (runbook "remains healthy while idle"):
derive the worker cadence from MAINTENANCE_CRON in one Python constant, set the healthcheck window to the console's
worker-quiet window (2 x 300 + 60 = 660 s), leave REFRESH_CRON its own literal, and pin the console's
WORKER_QUIET_AFTER formula to the Python window. Cost: a hung worker reads unhealthy up to 11 min late (compose only
consumes health for `--wait`; nothing restarts on unhealthy). Alternative: a check-in on every refresh tick (idle path
included), a worker behaviour change that adds one media-locked write per 30 s, with a window of at least
65 + 30 + slack s.
HC-3 · RESIDUAL: guard limits in HC-1. Import cost is NOT a residual: `import media.healthcheck` takes about 0.29 s
warm vs 0.25 s for central.db + central.media_repository + media.task_queue alone (measured locally), because
task_queue already pulls procrastinate.
HC-2 · FIXED (implementer, 2026-10-03): media/task_queue.py now has one cadence, `WORKER_CHECK_IN_SECONDS =
MAINTENANCE_MINUTES * 60` (MAINTENANCE_CRON is built from MAINTENANCE_MINUTES), and `WORKER_FRESH_SECONDS =
2 * WORKER_CHECK_IN_SECONDS + 60` = 660 s; REFRESH_CRON is its own literal again. media.healthcheck compares with `<=`
like the console's `since <= WORKER_QUIET_AFTER`. tests/test_media_queue.py
`test_the_worker_healthcheck_window_is_the_console_worker_quiet_window` evaluates the console's
`WORKER_QUIET_AFTER = a * WORKER_CHECK_IN_SECONDS + b` against WORKER_FRESH_SECONDS (mutation: +59 fails it). Cost stated
in docs/runbook.md: a hung worker can read healthy for up to 11 minutes.
HC-3 · PARTLY FIXED (implementer, 2026-10-03): tests/test_compose_healthchecks.py now scans healthchecks structurally
(flow list, block list, string/CMD-SHELL, block scalar, `{test: ...}` flow mapping; no PyYAML), follows CMD-SHELL and
`sh -c` into shell words, refuses `python*/-c` payloads importing first-party code, and requires every `python -m`
module to resolve (importlib.util.find_spec); parametrised probes cover each form. RESIDUAL: not extended to
.github/workflows/*.yml because existing CI steps violate it: base-image.yml:482 (`python3 -c` importing
scripts.module_closure), :490 (`.venv/bin/python -c` importing contracts.player_payload and scripts.*), :907 (`python3 -c`
importing contracts.release). Moving those to `python -m` entry points (or a scripts/ CLI) and then scanning workflow
`run:` blocks is a separate bead.
HC-2 · VERIFIED AGAINST SPEC (architect, 2026-10-03): diff matches the HC-2 spec point by point (one cadence from
MAINTENANCE_MINUTES; window 2 x 300 + 60 = 660; REFRESH_CRON own literal; console formula pinned). `<=` in
media.healthcheck is an implementer choice beyond spec, accepted (matches the console's `since <= WORKER_QUIET_AFTER`).
Cadence re-checked in code: boot check-in is `set_recipe` (central/media_repository.py:815) via `_register_recipe`
before the boot `maintain()` (media/worker.py:463-464); every preparation job (worker.py:347), preview (:409) and
due refresh (:245) also checks in, so a preparation backlog delaying maintenance behind MEDIA_STORAGE_LOCK still checks in.
docs/module-media-worker.md one-media-clock paragraph now states the window and cadence and links the runbook.
HC-3 · STATUS PARTLY FIXED, residual open as a separate bead (workflow `run:` scan after moving base-image.yml
:482/:490/:907 to entry points). Not blocking this PR.

## 2026-10-03 · 4 GB tracer T1 (implementer) · design-4gb-node.md §4.3 T1
- E-T1-1 · `admit_cold` second refusal: the page writes `StorageShort(incremental, min(free, available − EMERGENCY))`;
  that room is negative when MemAvailable < 512 MiB, which a non-negative `room_bytes` (T2 `BootStageV2`) cannot carry.
  Implemented: decision on the unclamped value (unchanged behaviour), reported room `max(0, …)`, as `preparation_room` does.
- E-T1-2 · Rule 3 ("per-unit duplicates are deleted") vs the page: the app transient still carries `MemoryMax=2G`
  (`appliance/node/process_linux.py` `app_unit_properties`) beside `photowallapp.slice` `MemoryMax=2G`; the page names
  only the two `MemoryMax=4G` lines, so the 2G duplicate was kept. Needs a ruling (delete it + bind slice to a constant, or keep).
- E-T1-3 · The storage stage no longer refuses on MemAvailable ≤ 512 MiB (the old `storage_budget`
  `node_storage_memory_envelope`); the page's order (memcg, class, mount, size) omits it. Cold admission still refuses
  through MemAvailable. Recorded so T6 docs do not describe the old refusal.

- E-T1-2 ruling (orchestrator, 2026-10-03): keep app MemoryMax=2G for the tracer; the app memory line and single-sourcing photowallapp.slice belong to the Shape C gate.
