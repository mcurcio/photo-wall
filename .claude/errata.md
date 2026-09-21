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
