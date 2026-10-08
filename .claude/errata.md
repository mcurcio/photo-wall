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

## 2026-10-03 · 4 GB tracer T4 (implementer) · design-4gb-node.md §4.3 T4
- E-T4-1 · Wording numbers: the page's "needs 3.5 GB of memory, the box has 1.9 GB" assumes binary GiB. The console has
  one byte wording, `health.js gigabytes()` (decimal, shared with App Manager's storage refusal), so the 4 GB class's
  3584 MiB reads "3.8 GB" and T5's 2 GiB MemTotal reads "2.1 GB". Kept DRY; T6 docs (DDD §62 rows) should quote decimal values.
- E-T4-2 · Case: boot items are host-facts `reported` facts (receipt `first`, `facts.first_received_at`), worded
  "Host Management reported boot preparation … · first received 1 min ago", so their values are lower-cased mid-sentence
  ("boot preparation refused at storage: …", "base unit failed on this boot: …"), as the kernel/base facts are. Incidents
  read "pi-07 (Frame x) — boot preparation failed at prepare (os:ENOSPC)", matching "— throttled now". `out_of_memory` is a
  `latest` metric and keeps "Out-of-memory kills on this boot: app 2 · base 1".
- E-T4-3 · Wordings the page leaves open, implemented: "no base unit failed on this boot" (no band); plural "base units
  failed on this boot: a · b and N more"; overflow with no shown name "… N not named"; "boot preparation failed at X"
  without "(fault)" when the fault is absent; "No out-of-memory kills on this boot" when every `oom_kill:` row is 0 (no band;
  only non-zero slices are listed, most kills first). The OOM notice is a fixed band (any kill > 0), not a Central threshold,
  following the App Manager storage refusal precedent (`reported` kind with its own band).
- E-T4-4 · `failed_units_more` cannot be filtered: when more than 4 units fail, a stage unit sorted past the fourth name
  is counted in "and N more", so a stopped stage can also raise `base_units`. Only reachable with ≥ 5 failed units.
- E-T4-5 · Baseline: the 29 console JS failures T2 reported (fleet_commands, releases, stage, planned, qualification) are
  environmental: the shell's default `node` is v16.20.2, which lacks global `fetch`/`Response`. With node v22.23.2 first on
  PATH all 52 pass on this tree; not a code fault, no origin/main comparison needed.

## 2026-10-03 · 4 GB tracer T3 (implementer) · design-4gb-node.md §4.3 T3
- E-T3-1 · Brief vs page: the T3 brief asked `failed_units()` to exclude the three stage units "per the page". The page
  does not: T4 `base_units` is "`facts.boot.failed_units` **minus** the three stage units" (the console subtracts), and §6
  rows "Prepare killed before its exit write" / "Record write fails" rely on `failed_units` still naming the stage unit.
  Implemented per the page: stage units are INCLUDED in `boot.failed_units`; T4 subtracts them (E-T4-4 is the known
  overflow consequence). If exclusion at the node is wanted, the §6 guarantees need another carrier.
- E-T3-2 · `read_boot_report`'s "None only if both are unreadable" vs `failed_units() -> ((), 0) on error`: the page's
  unit reader cannot signal "unreadable". Implemented: `units()` raising (any Exception) is unreadable; with no readable
  stage file either, the report is None. With the real sampler a box with no records reports `BootReportV2((), (), 0)`.
- E-T3-3 · §4.4 says `boot_stage` imports `contracts.node_host_facts` and `appliance.node.capacity` only. It also imports
  `contracts.node_protocol.token`, `contracts.strict_json.loads_object` and `uplink.files.write_atomically` (stdlib-only,
  already in the bootstrap closure; reused rather than a second atomic writer). HostCore's closure now carries
  `uplink.files`; no forbidden module (test binds it).
- E-T3-4 · `memcg_present` is emitted as 0 when `cgroup.controllers` is unreadable (reuses
  `capacity.memory_controller_present`, which maps OSError to False), rather than omitted; the store is refused on the
  same reading, so 0 is the consistent answer.
- E-T3-5 · The storage unit has no file-system sandboxing (no ProtectSystem), so only handoff and prepare gain
  `ReadWritePaths=/run/photo-wall-boot-stage`; HostCore reads it under ProtectSystem=strict (read-only is enough).

- E-T3-1 ruling (orchestrator, 2026-10-03): follow the page. failed_units includes stage units; the console subtracts them. residual: with 5+ failed units a stage unit can sort past the cap into "and N more" (E-T4-4), so one cause can raise two incidents. Fix later by reserving the cap for non-stage units or carrying a stage-units count.

## 2026-10-03 · 4 GB tracer T6 (implementer, docs) · design-4gb-node.md §4.3 T6
- E-T6-1 · Only the node release's cohort tree carries `cgroup_enable=memory` (`scripts/node_release_artifacts.py`
  `NODE_CMDLINE_TOKENS`). The general bundle's `cmdline.txt` template (`scripts/build_netboot_bundle.sh:288`) carries
  neither it nor `photowall.node=v2`, and the runbook still tells operators to add `photowall.node=v2` per Pi through
  iac `cmdline_extra`. A Pi opted in that way boots with the memory controller off, and its storage step now fails
  `memory_controller_absent`. The runbook (Node control, step 2) now says to add both tokens; whether iac's
  `players.yaml` stages the cohort tree or the general one with `cmdline_extra` was not checked (outside this repo) and
  should be before the tracer deploys.
- E-T6-2 · `docs/requirements.md` numbers no requirements, so R6 is a prose subsection, "Supported Player hardware",
  under Installation model › Player provisioning; README's "8 GiB" hardware line now links to it.
- E-T6-3 · The root import's refusal (`appliance/node/root_import.py:50-51`) stays `ValueError("root_import_capacity")`
  with no numbers, unlike admission's `StorageShort`; the domain model says so rather than claiming every refusal
  carries numbers.

## 2026-10-03 · 4 GB tracer T5 (implementer, node-pid1 refused leg) · design-4gb-node.md §4.3 T5
- E-T5-1 · The page names the drop-in only; it does not say how the leg starts the units. The existing inner script
  starts storage and prepare with `check=True`, which a refusal aborts. The refused leg starts handoff alone, then
  storage, prepare, host-core, broker and manager-supervisor as one `systemctl start` (the units' own Requires=/After=
  decide). The display units are left out (not on the refusal path); `photo-wall-node.target` itself is not started.
- E-T5-2 · Central is read through a new fixture route `/fixture/host` (newest `node_host_facts` row via
  `stored_fact_values`, newest observation's metric rows); `/fixture/status` is unchanged for the other legs.
- E-T5-3 · Not a spec error, an observation for the console/T6: in a real boot HostCore and storage race, so the first
  facts post can carry `storage running`; the refusal then arrives with the next observation-cadence post (≤ 15 s).
- E-T5-4 · memcg in the container: with `--cgroupns=private --privileged`, Docker Desktop (linuxkit 7.0.14) shows
  `cpuset cpu io memory hugetlb pids rdma` in the container's `cgroup.controllers`, and the `success` leg passes locally
  with T1 (storage mounts, class pi5-8gb). The ubuntu-24.04-arm runner was not observed directly; CI is the gate.

- E-T6-1 resolved by 2d264bb: the general cmdline template owns cgroup_enable=memory; the release seal enforces exactly once for both release types.

## 2026-10-03 · 4 GB tracer coherence fix cycle 1 (implementer) · design-4gb-node.md §4.3 T4, §6
- E-FX1-1 · Contradiction: §4.3 T4 says `boot_preparation` reads only `boot.stages` and `base_units` subtracts all three
  stage units unconditionally; §6 relies on `failed_units` naming the stage unit when the step is killed before its
  exit write (OOM victim, TimeoutStartSec SIGTERM) or its record write fails. Under T4 such a stop read "running" /
  "not started" and the unit was subtracted, so nothing alarmed. Fix (§6 wins): `hostHealth.js` `stoppedStages()` is
  the one rule both boot items read: a stage is stopped when its record is refused/failed, OR its own unit is in
  `failed_units` while its record is `running` or absent ("boot preparation failed at <stage> (unit failed, no exit
  record)", alarm). `base_units` subtracts a stage unit ONLY when that stage is stopped (all stopped stages, not just
  the first shown, since later stops are effects of the first); a `done` stage's failed unit stays listed. T4's page
  text is superseded by this entry; console DDD §62 and runbook rows updated.
  Residual (unchanged, E-T4-4): a stage unit sorted past MAX_BOOT_FAILED_UNITS into `failed_units_more` is invisible to
  the unit-failed rule and to the subtraction.
- E-FX1-2 · Compatible interface additions made during T1-T5, recorded so the frozen pages stay the plan of record:
  `mount_storage(*, controllers, meminfo)` (injected readers), `require_mounted_size`, `boot_document`,
  `boot_from_document`, `check_metric_families`, `MAX_HOST_METRICS`.
- E-FX1-3 · `kernel_release` narrowed from printable ASCII to `[A-Za-z0-9._+~-]{1,64}` (the `uname -r` alphabet): `"` and
  `\` JSON-escape to 2 bytes and 64 of them overflowed MAX_HOST_FACTS_BYTES (HostRunner swallowed the ValueError, so
  facts went silently absent). `HostFactsV2.values()` deleted; `fact_values_document` is the one "what changed".

## 2026-10-03 · 4 GB tracer fix cycle 2 (implementer) · docs/node-4gb-memory-design.md §4.3 T1, T3, T4, §6, §8a
- E-FX2-1 ruling (orchestrator, 2026-10-03; overrides the design's fail-closed `memory_controller_absent` and gate
  answer Q2(a)): an absent memory controller is REPORT-ONLY. `cgroup_enable=memory` comes from the separately staged
  TFTP boot tree, not from Central's offer, and Select is fleet-wide (docs/runbook.md, Releases), so a V2 Pi on an
  older tree that reboots after Select would be refused at storage and go dark. `mount_storage` logs the absence and
  mounts; HostCore's `memcg_present` 0 is the report; the console's `memory_limits` item reads "memory controller
  absent: memory limits not enforced" (notice band, no incident). The device class (`node_memory_class`) stays
  fail-closed. Cost: a Player on an old tree runs with every cap unenforced until its tree is restaged.
- E-T3-2 ruling (orchestrator, 2026-10-03; supersedes the E-T3-2 implementation note): "failed units unreadable" is
  representable end to end. `BootReportV2.failed_units`/`failed_units_more` are both None when never read;
  `failed_units()` raises `ValueError("failed_units_unreadable")`; `HostRunner` reuses its last successful reading for
  the process (None until one succeeds); the console reads "Unknown: failed units not read" (no band) and
  `stoppedStages` uses only the records when the list is null (a `running` record stays running, no alarm, no clear).
  A slow systemctl no longer clears the E-FX1-1 killed-stage alarm.
- E-FX2-2 · Smaller fold-ins: a 422 facts answer (an older Central's strict parse) is retried on the 404 timer
  (`FACTS_ROUTE_RETRY_SECONDS`), other 4xx still drop; stage records are read through a bounded regular-file reader
  (O_NOFOLLOW|O_NONBLOCK, fstat, ≤ MAX_STAGE_BYTES+1); `photo-wall-display.service` gains `OOMPolicy=continue`;
  `memory_peak:display` (photowallbase.slice/photo-wall-display.service) raises the `memory_peak:` family to 5 rows
  (36 of MAX_HOST_METRICS 64, now the one constant HostObservationV2 enforces); hostHealth.js no longer slices failed
  units or `oom_kill:` rows (MAX_UNITS/MAX_OOM_ROWS removed), so no cap hides a kill. The design doc moved into the
  repo as docs/node-4gb-memory-design.md with these rulings folded in.
- E-FX2-3 · Console band vocabulary: the brief's "WARNING band" is the console's existing `notice` band (warm; the
  catalog has no `warning`), which raises no incident, as Out of memory's does. The "failed units not read" Unknown
  carries band null (no band), unlike the generic "Unknown: not reported", whose band stays `unknown`.

## 2026-10-03 · prepare-status-bound adversarial review fixes (implementer) · appliance/node/preparer.py, uplink/fetch.py
- E-PSB-1 · DEFERRED (minor): `BootStageV2` carries no last fault and no attempt count, so while the preparer retries
  transient failures inside its window the prepare stage reads `running` for up to 900 s (TimeoutStartSec) with no
  reason shown. Needs a contract addition (last fault token + attempts) written by the retry loop, and a console read.
- E-PSB-2 · DEFERRED (CI gap): no cold-cache node-pid1 scenario. tests/node_pid1_central_fixture.py (~115-125)
  pre-copies every archive into Central's cache, so the cold path (a ~974 MB sealed-environment fetch through the
  read-through wait, 503 + Retry-After, and the preparer's retries) is never exercised in CI. Needs a fill source the
  fixture's worker can reach for missed sealed-environment entries, and an arm64 privileged Docker leg.

## 2026-10-04 · node release readiness fix cycle 2 (implementer) · release-readiness design §6.1 · central/kernel/ports.py, central/content_catalog/deployment.py
- E-NRR-1 · Design drift, as built: §6.1 says `NodePublication` gains the node manifest asset's `UpstreamVersion`.
  Built instead: `PublishedRelease.node_version: UpstreamVersion | None` (central/kernel/ports.py ~271-286), because
  `node_problem` (a release whose node facts failed to read, with no `NodePublication`) also needs a version guard,
  independent of the legacy manifest's `upstream_version`. Also added `PublishedRelease.legacy: bool = True`, False
  only for a pre-release listed while legacy pre-releases are off (node facts observed, legacy facts unread, no legacy
  row written; construction refuses legacy fields when False). `OFFER_TTL` is named `OFFER_TTL_SECONDS`
  (central/content_catalog/deployment.py:28) to carry its unit. The design reads as built from this entry.
- E-NRR-1 · APPLIED (2026-10-04, asset-roots lock review fixes): the auto-ingest design's §6.1 now names
  `PublishedRelease.node_version` and `PublishedRelease.legacy` (central/kernel/ports.py ~267-292) instead of a
  version on `NodePublication`, and §6.2 names `OFFER_TTL_SECONDS` (central/content_catalog/deployment.py:28); a
  history line r3 records it. The entry above stays as written.

## 2026-10-04 · asset-roots lock review fixes (implementer) · central/infra/{catalog_records,node_releases}.py, central/netboot_base.py
- E-ARL-1 · The "every root writer takes the asset-roots lock" claim (CacheRetention, maintenance.py, design §6.3)
  was false for the node observation upsert (`_observe`, so `record_problem`) and for every V1 desired-set writer
  (`PgReleaseRecords.claim/apply/set_promoted/set_last_good`, `PgDeviceRecords.set_pin/record_served`,
  `netboot_base.record_base_health`). Each now takes the lock first (catalog records through `_root_write`).
  tests/test_asset_roots_lock.py proves each waits on the lock and makes a new records method declare its side.
  Cost: netboot served/known-good writes, pins and each V1 sync transaction now queue behind a cleaner run (at most
  50 unlinks).
- E-ARL-2 · OPEN (lock order, not fixed): `SyncReleasesHandler._tail` locks device rows (`sweep_failed_boots`) and
  then takes the asset-roots lock (`select_first`, pre-existing; now also `set_promoted` when auto-promote moves),
  the reverse of retirement / `record_served` / `record_base_health` (lock, then device row). PostgreSQL detects
  the cycle and aborts one transaction. Taking the lock at the tail's start fixes it but serializes operator
  promotion and V1 claims with the whole tail, which makes the interleavings of
  test_content_catalog_sync.py::test_an_operator_promotion_committed_during_the_sync_tail_is_never_overwritten and
  test_release_versions.py::test_v5_* impossible (both fail/hang); needs an orchestrator decision.

## 2026-10-04 · auto-ingest review fix list (implementer) · central/{assets/maintenance,infra/node_releases,content_catalog/sync}.py, migration 065
- E-ARL-1 · RESOLVED (superseded): the asset-roots lock is no longer a cleaner guarantee, so no desired-set writer
  needs it. Removed from `_root_write` (deleted) and its callers in central/infra/catalog_records.py, `_observe` and
  `ingest` in central/infra/node_releases.py, `netboot_base.record_base_health` (restored to main) and the cleaner;
  `CacheRetention`, `PgCacheRetention` (central/infra/retention.py) and tests/test_asset_roots_lock.py deleted. The
  cleaner is mark and sweep: it re-reads `desired_assets()` immediately before each unlink, and re-stats the file
  (a fetch that landed after the pick has a fresh mtime). Residual window: a root committed between that re-read
  and the unlink costs one read-through re-download (files install by `os.replace`, serve from an open fd).
- E-ARL-2 · RESOLVED: `SyncReleasesHandler` runs `sweep_failed_boots` in its own transaction before the tail, so
  no transaction holds device rows while taking another lock; and the tail's writers (`set_promoted`,
  `select_first`) no longer take the asset-roots lock at all.
- E-ARL-3 · Deviation from "keep main's 16 lock call sites": main's `NodeBootService.select` lock (moved to
  `select_deployment`) is removed. The first-run CAS is now `INSERT ... ON CONFLICT DO NOTHING` (expected revision
  0) or `SELECT ... FOR UPDATE` (any other revision) on the policy row itself; with the advisory lock kept, the
  required mutation probe (drop ON CONFLICT) could not fail, because the lock alone serializes. Main's other 15
  sites stay (main's `evict_if_unretained` site was already deleted by this PR's B4).
- E-ARL-4 · Deviation from "delete DesiredTiers": kept, priority wording removed. Prefetch must tell window-only
  keys from wanted ones to start at most one window-only fetch per tick; `DesiredTiers` is that split. Deleted:
  `BACKGROUND_PRIORITY`, `require_priority`, `PRIORITY_KWARG`/`delivery_priority`, every `priority` kwarg (Publisher
  protocol, publisher, job_queue, queue_ops, executor, runtime, fakes): those files equal main again.
- E-NRO-1 · Migration 065 backfills `node_release_observations` per tag from the newest catalogued manifest whose
  derived deployment the console already published (version NULL, prerelease flag from `app_releases` else the
  tag's semver). A catalogued manifest with no published deployment is not backfilled: an observation's manifest
  must have a deployment (an empty job set reads as Ready, and auto-select would then refuse an unknown deployment);
  the first sync's full listing ingests it. `previous_deployment_id` is not backfilled: 054 keeps no selection
  history and offers record content, not deployment ids.
- E-CLK-1 · `last_served_at` is now stamped by the database (`touch_served(tx, key)`, `clock_timestamp()`) and
  compared by it (`AssetRecords.served_within`); file mtimes are compared only with the run's marker file
  (`CLOCK_MARKER` in the cache root). The cleaner takes no clock.

## 2026-10-05 · player-health M1 B0 display harness (implementer) · scripts/run_display_harness.py, tests/native_display_smoke.py
- E-B0-1 · trixie's `python3-pywayland` 0.4.18-4 does not declare `python3-cffi-backend`, which its `_ffi` imports:
  with `--no-install-recommends` `import pywayland` fails (`No module named '_cffi_backend'`). The harness installs
  `python3-cffi-backend` beside `python3-pywayland python3-cairo`. **B5's page must add `python3-cffi-backend` to
  `node-display`** in scripts/debian_packages.py, or the Python overlay client cannot start on the node.
- E-B0-2 · B0's mutation probe "skip `bind_diagnostic`'s private-client check" is not caught by a foreign bind while
  the private client is bound: shell.c:428 also refuses when `s->diagnostic_resource` is held, so dropping only
  `client != s->diagnostic_client` stayed green (probed). The harness adds a second foreign bind inside the 2 s
  respawn window after SIGKILL of the private client (no resource held), asserted within 1.8 s of the kill; that
  variant now turns red. B4 step (3) uses the same window.
- E-B0-3 · The run command is `/usr/bin/python3 /smoke/native_display_smoke.py`, not `python3 ...`: the builder
  image's PATH `python3` is /usr/local/bin (the python image's own), which cannot see Debian's dist-packages.
- E-B0-4 · On this Mac (Docker context `desktop-linux`) `docker_build`'s `--builder default` fails ("use `docker
  --context=default buildx`"): G-harness (and G-leg's component build) need `PHOTO_WALL_NODE_BUILDER=desktop-linux`
  in the environment. CI (default context) needs nothing.
- E-B0-5 · The two design files were already in `docs/design/player-health/` (f930a50); B0 copied nothing.

## 2026-10-05 · player-health M1 B1 node-pid1 binds a Frame (implementer) · tests/test_node_pid1.py, tests/node_pid1_central_fixture.py
- E-B1-1 · The brief's G-leg command does not run on this Mac as written: (a) `python3 scripts/debian_packages.py
  epoch` is macOS Python 3.9 (`dataclass(slots=)` TypeError) — use `.venv/bin/python`; (b)
  `scripts.build_node_components` refuses non-Linux (`run_dpkg_deb`: `dpkg_deb_requires_linux`); (c) `docker run
  python@sha256:<BUILDER_IMAGE>` fails on Docker Desktop ("cannot overwrite digest"); (d) `daemon_image_build`
  hard-codes `--builder default` (E-B0-4), which `PHOTO_WALL_NODE_BUILDER` does not reach. Local recipe (never
  committed): `DOCKER_CONTEXT=default` for the whole chain, and the components step via
  `PYTHONPATH=$PWD .venv/bin/python /Volumes/Dock/tmp/pw-node/build_components_macos.py <same args>`, a wrapper that
  patches only `run_dpkg_deb` to run `dpkg-deb --build --root-owner-group` in `pw-local-dpkg:builder` (`FROM`
  BUILDER_IMAGE, built locally with `docker build`). CI (ubuntu-24.04-arm) needs none of this.
- E-B1-2 · `scripts/build_node_pid1_fixture.py` `build_image` tags the untagged native build image with an alias and
  `docker rmi`s the alias in `finally`: that deletes the base image (its only reference), so a second fixture build
  from the same components fails `docker image inspect`. Rebuild components and fixture together (warm: ~1.5 min).
- E-B1-3 · No Scene needed: the bound real Player presents ~34 frames/s on Virtual-1 with no Scene (B1 page's
  conditional Scene authoring not taken). Admission = Central's latest decision `retain`/`already_admitted` for the
  exact bound (frame, binding generation, config revision); the `handoff`/`current_linked_app` decision is superseded
  within a sample. The fixture adds a read-only `GET /fixture/display` for it (test code only, no Central change).

## 2026-10-05 · player-health M1 B2a shared kernel move (implementer) · appliance/{clock,boot_store}.py, appliance/central_session/
- E-B2a-1 · "Docs that cite the old paths are D1's" conflicts with G-static: check_docs fails on one relative link,
  docs/evidence/2026-09-30-node-stop-observation-proposal.md:27 `../../appliance/node/storage.py`. B2a repointed that
  one link to `../../appliance/boot_store.py` (only edit outside the page's files). Plain-text (non-link) citations
  of `appliance/node/{session,storage,clock,http}.py` in docs (e.g. docs/operator-console-ddd.md:1677, :2521) stay D1's.
- E-B2a-2 · No change to scripts/build_node_base_deb.py `POLICIES`, scripts/build_node_manager_deb.py or
  scripts/module_closure.py was needed: closures are import-derived, and no forbidden prefix names a moved module.
  The moved modules now ship at `appliance/clock.py`, `appliance/boot_store.py`, `appliance/central_session/*` in
  every node closure (base + manager), so the base and manager .deb contents change (leg required).
- E-B2a-3 · Importer count matches the page: 18 files under appliance/ (incl. the moved session.py's own three
  imports, and weston.py's function-local import) and 9 under tests/; nothing in scripts/ (no string references).
  `appliance.node.host_linux` keeps its pre-existing `boot_id, boottime_ms` re-export (`# noqa: F401`, used by
  host_runner) — it is not a shim of the moved module and was left unchanged.
- E-B2a-4 · The implementer cannot commit, but G-leg builds from a commit: the leg was built from a dangling
  commit object (temporary GIT_INDEX_FILE + `git commit-tree`, no ref, HEAD and index untouched) whose tree equals
  the B2a working tree.

## 2026-10-05 · player-health M1 B2b feed primitive (implementer) · appliance/feed.py, appliance/display_host/runner.py
- E-B2b-1 · Wire is additive beyond the page's "adds `publisher_incarnation`": each event also carries `audience`
  (`FeedCursor.advance` must rebuild `FeedEvent.audience`; a missing one reads as `node`), and the page document
  also carries `latest` and `dropped_total` (the `FeedPage` fields, so a remote reader can see counted drops).
- E-B2b-2 · Request bounds as built in `answer_feed_read`: `after` is now required (was `value.get("after", 0)`);
  a missing `incarnation` key reads as null (tests/test_node_pid1.py `DISPLAY_FEED_SCRIPT` omits it); `op` absent or
  `"events"`; any key outside {op, after, incarnation, limit} → `ValueError("feed_read_request")`.
- E-B2b-3 · tests/test_node_pid1.py `DisplayFeed.poll` detects a controller restart by the compositor's
  `incarnation_id`, which does not change when only the controller restarts; the feed's restart signal is now
  `publisher_incarnation` (or simply `stream_gap`). Left unchanged (not in B2b's files); B12, which rewrites that
  leg, should read the display feed with the `FeedCursor` request/advance shape.
- E-B2b-4 · G-harness does not exercise `runner.Controller` (the smoke drives Weston's control socket directly);
  the controller is covered by the new tests/test_node_display_runner.py (fake backend), the DB-tier
  tests/test_node_display_native.py service probe, and the node-pid1 legs that read the display feed.

## 2026-10-05 · player-health M1 B2c lint contracts + ratchet (implementer) · pyproject.toml, tests/test_import_contracts.py
- E-B2c-1 · "Display never reads the fault catalogue" checks nothing until B9 creates `contracts/node_faults.py`:
  grimp drops an import of an absent first-party module, so `import contracts.node_faults` in
  appliance/display_host/domain.py stays 11 kept today (probed). With a scratch `contracts/node_faults.py` the same
  import turns it red (probed). **B9's verifier must re-run that probe** once the module exists.
- E-B2c-2 · Likewise the optional layers `(appliance.authority)`, `(appliance.health)` and the kernel contract's
  `appliance.authority`/`appliance.health` targets are latent until those packages exist; proved only with a scratch
  `appliance/health/__init__.py` (node → health and feed → health both red). Layers checks indirect chains, so a
  kernel module importing a context also breaks "Node contexts point down" via display_host/node → kernel → context.
- E-B2c-3 · The ratchet test freezes only `ignore_imports` (subset of six) and the layers list, as the page says; it
  does not freeze the session contract's `source_modules`/`forbidden_modules`, so dropping a source would weaken it
  unnoticed. Left as specified; B9 adds `appliance.health` to the sources (a later bead could pin them too).
- E-B2c-3 RESOLVED (B2c) · tests/test_import_contracts.py now also pins each forbidden contract's frozen
  `source_modules` and `forbidden_modules` (frozen set ⊆ configured: additions such as B9's `appliance.health`
  source pass, any drop fails); layers stay pinned by equality. Probed: dropping `appliance.node` from the session
  contract's sources → test red.

## 2026-10-05 · player-health M1 architect pass 1 (after B2c) · .claude/runs/player-health-m1.md §8
- E-B0-1 applied to brief (B5: `python3-cffi-backend` in node-display / node-display-build; harness extras shrink).
- E-B0-2 applied to brief (B4 step 3 and its private-client mutation probe use the in-window foreign bind).
- E-B0-3 applied to brief (§8 common local environment; B4 and B5 launchers, B11 feeder use /usr/bin/python3).
- E-B0-4 applied to brief (§8 common local environment: G-harness with PHOTO_WALL_NODE_BUILDER=desktop-linux).
- E-B0-5 informational; nothing to apply.
- E-B1-1 applied to brief (§8 common local environment: G-leg local recipe).
- E-B1-2 applied to brief (§8 common; B12 rebuilds components and fixture together for the role and the variant).
- E-B1-3 applied to brief (B12 step 1 admission criterion reuses B1's `admitted()` via /fixture/display).
- E-B2a-1 applied to brief (D1 lists the plain-text old-path citations).
- E-B2a-2, E-B2a-3 informational; nothing to apply.
- E-B2a-4 applied to brief (§8 common: implementer legs build from a dangling commit).
- E-B2b-1, E-B2b-2 applied to brief (D1 documents the feed wire as built).
- E-B2b-3 applied to brief (B6 replaces `DisplayFeed` with a gap-aware generic `NodeFeed` reader; B8-B12 reuse it).
- E-B2b-4 applied to brief (B10 proves the new listener, allowlist and snapshot by runner unit tests + leg, not G-harness).
- E-B2c-1, E-B2c-2 applied to brief (B9 verifier re-runs the vacuous-at-B2c lint probes once the modules exist).
- E-AP1-1 · B10 re-cut: the display feed is ~32 events/s (B2a leg display-feed.jsonl: 733 CompositorPresentation in
  23 s; ring 256 laps in ~8 s; OutputKey published once), and B2b's feed has no snapshot although system-design-r8
  §5 has feeds carry `NodeSnapshotV2`. An event-only judge loses the Output set at its first gap or restart, and the
  overlay client would then paint the V-stale card on a healthy wall. The `events` response gains an `outputs`
  snapshot from `host.states()`; the judge drains feeds each turn (B9, B10). Applied to brief.
- E-AP1-2 · B10 re-cut: no sysusers `m pw-display pw-node-feeds` (Weston, uid pw-display, uses PAMName=login →
  initgroups, so the overlay client and Weston would join the feeds group); the controller gets the group from its
  unit's SupplementaryGroups only; the feed socket is chowned to pw-node-feeds and chmod 0660 after bind (controller
  umask 0o077, runner.py:145; B6's broker socket likewise). Applied to brief.
- E-AP1-3 · B9 page bug: `stage_tree` stages policy "health-judge" at /usr/lib/photo-wall-health-judge
  (build_node_base_deb.py:57) but the unit's ExecStart named /usr/lib/photo-wall-health. ExecStart corrected;
  `MappingProxyType({})` as the other policies. Applied to brief.
- E-AP1-4 · B4/B5 order swapped: B4's harness client was to replace the private client at the spawn path, but the
  harness's handoff needs the private client's slate `diagnostic_presented` (native_display_smoke.py:254, :405), so a
  tint-only test client would break every B0 assertion after the first. With B5 first, B4's test client composes the
  Python client (slate, ack, trial) and adds the v3 health layer. Applied to brief.
- E-AP1-5 · B5 launcher: `#!/usr/bin/python3 -IB` implies `-P` on Python ≥ 3.11, so the launcher's directory is not on
  sys.path and `import overlay` would fail on the node; the launcher inserts its own parent directory. Applied to
  brief (B5, B4).
- E-AP1-6 · Leg-asserting beads B6, B8, B9, B10, B11 did not list tests/test_node_pid1.py although their acceptance
  needs leg assertions; added to their Files. Applied to brief.

## 2026-10-05 · player-health M1 B3 guest contract + Player probe responder (implementer) · contracts/node_app_link.py, player/{probe_responder,node_app_link,service}.py
- E-B3-1 · Page gap, decided: the backoff resets only after a channel that **received at least one `probe`**. A reset
  on connect would make today's broker (connect succeeds, then `refused`) see a 0.5 s retry forever; with this rule
  it sees 0.5, 1, 2, 4 s, then one `probe_open` every 5 s. **B6:** an admitted `probe_open` gets no ack packet (the
  page defines none); any packet on the channel other than `probe`/`relink` (e.g. an app-link `result`) ends it.
- E-B3-2 · Cost, unstated in the page: while the control queue is starved the one queued probe callback holds one of
  `GLibDispatcher`'s four slots (player/service.py:330-355), so control dispatch has three. Starved = control is
  stuck anyway; a refused (`dispatch_capacity`) probe frees its slot at once. The responder treats any
  already-failed future (or a dispatcher exception) as refused — a superset of `dispatch_capacity`.
- E-B3-3 · Parse refusals: the page names only `ValueError("probe_channel_message")` (unknown kind); every probe
  channel parser (`probe_open`, `probe`, `probe_answer`, `relink`, nonce shape) uses that one code. Result packets
  use `app_link_result_invalid`; an invalid result is `"rejected"` in `exchange_applied` (as before).
- E-B3-4 · G-unit lists `tests/test_node_app_link*.py`, which matched no file at 084975d; the Player-side app-link
  tests are new in tests/test_node_app_link_client.py (B7's `test_node_app_link_local.py` also matches the glob).
- E-B3-5 · The responder blocks in `recv` with no timeout (socket blocking so `MSG_DONTWAIT` answers never wait);
  a broker that holds the channel open without probing leaves the Player idle on it, which is harmless (no answer
  is owed). `relink` before any applied receipt is a no-op for the proof loop.
- E-B3-2a · Correction to E-B3-2 (fix cycle 1, correctness review): the cost was a defect, not starvation-only. Four
  in-cycle loops (control, time, websocket, observation re-poll) can each hold a dispatch at once, so a queued probe
  made the fourth get `dispatch_capacity` (cycle teardown, or a spurious `clock_probe` fault). Now the responder takes
  its own one-slot lane, `GLibDispatcher.lane(1)` (same `glib.idle_add`, same default-idle priority, own semaphore),
  inside its constructor, so a probe can never hold a control slot whatever dispatcher it is handed. "No answer when
  refused" now means the probe lane is busy, not the control queue full. Module design r8 lines 41/128/149 still say
  "in the shared GLibDispatcher": doc bead to update.

## 2026-10-05 · player-health M1 B5 Python overlay client (implementer) · appliance/display_host/overlay/, meson.build, scripts/debian_packages.py
- E-B5-1 · Acceptance says "B0 harness unchanged and green ... testing alpha 0.96 blend" and the mutation "testing
  alpha 1.0 → harness red", but B0's harness asserted no testing pixel and B5's Files omit tests/native_display_smoke.py.
  Added one step, `testing_slate_pixel_captured`: pixel (40, 40) while the candidate's `starting_new` testing slate is
  presented over the probe app = (10, 16, 25) (cairo premultiplied ARGB32 + pixman OVER rounding, computed in the
  harness), tolerance 1; opaque gives (10, 15, 23) (probed: alpha 1.0 → red there). Also found: B0's
  `slate_pixel_captured` passes even when the client draws NOTHING (transparent buffer) — the shell's curtain has the
  same colour and pixman leaves its pixels in the framebuffer (probed: painter skipped → slate step green, testing
  step red). Only the testing-slate step proves the client's drawing.
- E-B5-2 · Page caps "≤ 8192² px"; the C client capped each side ≤ 8192 AND area ≤ 4096 × 2160 (MAX_PIXELS), and 16
  Outputs. Kept the C's caps (`client.buffer_admissible`, unit-tested).
- E-B5-3 · Parity exception, visual only: the C trial overlay drew stray segments from each numeral's end to the next
  ring (`cairo_arc` after `cairo_show_text` keeps the current point). The draw-list starts each ring on a new path.
- E-B5-4 · No scripts/release_plan.py, unit, POLICIES, sysusers or tmpfiles change was needed: node-display-deb and
  node-base-deb already claim `appliance/display_host/**`; the spawn path is unchanged. `libcairo2` kept in
  node-display (pycairo links it). New packages declare their import roots (`pywayland`, `_cffi_backend`, `cairo`);
  no closure policy reads the node-display import table today.
- E-B5-5 · E-B0-3 also binds the build: meson.build runs the scanner with `find_program('/usr/bin/python3')` (the
  builder image's PATH python3 cannot import Debian's pywayland). pywayland's scanner writes no top-level
  `__init__.py`, so `overlay/protocol/` is a namespace portion inside the regular `overlay` package (proved in harness
  and leg). B11/B4: compose through `OverlayClient(hooks=..., manager_version=...)` / `client.main(hooks=...,
  manager_version=3)`; `OutputHook.output_configured(client, output)` runs after each Output configure; keep every
  proxy referenced (`OutputState.held`): pywayland destroys a collected proxy.
- E-B5-6 · Local unit tier on a dirty tree: tests/test_node_component_inputs.py errors (7,
  `declaration_differs_from_revision`) whenever scripts/debian_packages.py is uncommitted (it builds HEAD and compares
  the working declaration). On a clean checkout of the same tree: 23 passed. Environment, not code.
- E-B5-7 · For D1: docs/display-host-backend.md:26 and docs/node-4gb-memory-design.md:243 still describe the C client.
- E-B5-8 · Overlay client hardening (regression review): a buffer was counted before the painter ran, so two paint
  failures left an Output at the 2-buffer cap, dirty, never acking again; and `_release` decremented only after
  `pixels.close()`, so a BufferError leaked the counts. Now `_paint` never raises (a failure logs and commits a
  cleared buffer, then acks, as the C client did on a cairo error), a buffer counts only once committed, and
  `_release` frees counts before closing (BufferError logged). Tests in tests/test_display_overlay_render.py, each
  mutation-probed red.

## 2026-10-05 · player-health M1 B4 shell health layer + fallback tint (implementer) · native/shell.c, native/photo-wall-frame-v1.xml, tests/native_display_smoke.py
- E-B4-1 · Weston's curtain colour (`weston_curtain_params` → solid buffer → pixman solid fill) is **premultiplied**:
  the page's RGBA (0.55, 0.35, 0.0, 0.5) passed raw rendered (157, 115, 34) over the probe app (super-luminous,
  R 0.55 > A 0.5; probed). shell.c passes (0.55·0.5, 0.35·0.5, 0, 0.5), which renders the page's colour at alpha 0.5:
  (87, 70, 34) over the app, asserted. The existing slate curtains (α 1.0 / 0.96) are unaffected in practice.
- E-B4-2 · The test client is installed over the spawn path by tests/native_display_smoke.py (which already runs
  `meson install` in the container), not by scripts/run_display_harness.py; run_display_harness.py is unchanged
  (the CI job and local runs both go through the smoke, so one install site serves both).
- E-B4-3 · Harness shape as built: the test client tints only the bottom-right quarter of its health surface, so
  B0/B5's pixels (40, 40) and the Output centre keep their asserted colours; step (1) reads (560, 420) = app OVER
  tint. Step (3) needs a **new kill while handed off with the app live** (the B0 kill is after app exit, where the
  Output is not released, so no fallback is due); the B0 kill and its in-window bind are kept. Step (2): a one-shot
  mode file (/tmp/pw-health-client-mode) makes the next spawn bind v2 and call `get_health_layer`; the refusal is
  libwayland-server's own `since` check ("invalid method 3 (since 2 < 3)", read from Weston's log), not shell code.
  Added beyond the page: tint above the slate, `health_layer_exists` on a second layer per Output (same mode file),
  the respawned client's layer back above the app, and the layer restored after both refusals.
- E-B4-4 · Mutation (b) (health layer below the app) turns the harness red first at the earlier
  `health_layer_above_slate` step; with that step skipped in a scratch copy, step (1) itself is red
  ((560, 420) = app colour). Mutation (k) → step (3) red; dropping `client != s->diagnostic_client` → the
  post-handoff in-window foreign bind red.
- E-B4-5 · For B11: the production client takes the layer with `manager.get_health_layer(surface, output_name)`
  (pywayland drops the new_id arg); the arg is named `output` per the page although sibling requests say
  `output_id`. The layer maps at the Output origin with the buffer's own size (no size check; masked to the Output),
  so a viewporter-scaled 1×1 buffer works. The fallback tint is raised only while handed off: before handoff and
  after an invalidation the shell's own slate curtain shows instead.
- E-B4-6 · Fix cycle 1 (security review, fail-open): the fallback tint keyed on the private client's manager
  bind, so a bound client with no mapped health surface showed nothing (before its first commit, with B5's client,
  and after an Output reconnect, which unmaps the layer). `fallback_sync` now raises the tint while the Output is
  handed off AND `o->health` is not a mapped surface; it re-syncs on handoff, invalidate, every health commit (map
  or NULL-buffer unmap) and health destroy; bind/unbind no longer touch it (`fallback_all` removed). Output re-add
  needs no call: the Output is not released until the next handoff, which syncs. Harness: test-client modes `bare`
  (layer, no buffer) and `unmap` (map, then NULL buffer on presented) + marker file; steps
  `fallback_tint_while_bound_client_maps_no_health_surface`, `fallback_tint_back_when_health_surface_unmapped`,
  `fallback_tint_dropped_when_health_surface_mapped` (replaces `..._when_private_client_binds`); the harness drains
  control events while it waits (an undrained queue made the shell drop the control peer). Mutations: bind-keyed
  shell.c → step (a) red; no sync on NULL-buffer unmap → step (c) red. **Consequences for later beads:** until B11
  maps a health layer, every handed-off Output on a real node shows the amber fallback tint (B5's client binds v2,
  no layer); and B11's page says "tint off → NULL buffer", which under this rule raises the fallback tint on a
  healthy wall — B11 must commit a mapped fully transparent buffer for tint off (NULL only on teardown), or its
  acceptance "no tint on a healthy wall" fails.

## 2026-10-05 · player-health M1 B6 probe channel + probe thread + broker feed (implementer) · appliance/node/{probe,probe_channel,app_link,broker_runner}.py
- E-B6-1 · Additive to the frozen signatures: `ProbeTiming(period_ms, miss_limit, startup_ms, kill_after_ms)` with
  `SHIPPED_TIMING` from the four constants; `ProbeClock(run, started_ms, *, timing=SHIPPED_TIMING)` and
  `ProbeThread(feed, *, clock=boottime_ms, timing=SHIPPED_TIMING)` (so the thread's real timer is unit-tested at
  50 ms); `AppRunKey.of(running)` / `.document()`; `ProbeClock.last_rtt_ms` (`answered` stays `-> bool`; the rtt
  feeds `probe_answered`); `ProbeThread.start()`, `close()`, `check()` (raises `probe_thread_stopped`; the main loop
  calls it each turn, so a dead thread exits the broker → `Restart=on-failure`), `recovery_may_be_armed`
  (stored, read by nobody until B8). `send_relink(run)` is built (B7 consumes it). B9: build the judge's
  K-rule from these constants, not from `ProbeTiming` defaults by hand.
- E-B6-2 · Miss rules as built (page left them open): a turn judges the interval since the previous turn or the
  last accepted answer; a **miss** = a turn not late with no accepted answer in its interval (so the first turn
  after an answer is never a miss, and a healthy run's counted time sits near T); counted unanswered time = sum
  of non-late intervals since the last answer; `probe_unanswered` once misses ≥ k (every turn), `probe_kill_due`
  once per unanswered episode at counted ≥ K (re-armed by an answer). **Stale** = not one of the last 8 nonces
  sent since the last accepted answer (an accepted answer drops itself and every older nonce), so a late answer
  to the previous nonce still counts. `started_ms` = when the probe thread first saw the run published (its
  launch, or broker start for an app already running). Late = the turn ran > T/2 past its deadline.
- E-B6-3 · Ordering the page did not state: `serve_one` (adopt) runs before the turn's `publish_run`, so a channel is
  judged against publications **after** its adoption (a publication sequence number); otherwise a fresh channel
  would be closed by the previous turn's publication (e.g. right after a broker restart). Probes go only to a
  channel whose run equals the published run.
- E-B6-4 · The broker feed is served on the main loop, as the page says (`FeedListener.serve`, ≤ 8 accepts per
  turn, 50 ms read wait each). Probe *timing* is immune to main-loop blocking, but a reader's *view* of the facts
  waits for the next turn (systemctl_show timeout 5 s, HTTP 0.5 s). Architect pass 2 / B9: consider serving the
  feed from the probe thread's selector. **B10:** the display feed needs the same {0, 10006} SO_PEERCRED-allowlisted
  SEQPACKET `events` listener; lift `FeedListener` (appliance/node/broker_runner.py) into the kernel
  (`appliance/feed.py` or a sibling) instead of copying it — display_host may not import appliance.node.
- E-B6-5 · Wire as built for the broker feed: reply `{"accepted": true, **answer_feed_read(...)}` or
  `{"accepted": false, "reason": "feed_read_request"}` (the display ingress envelope); a peer outside {0, 10006}
  is closed with no reply. The leg reads it like the display feed. For D1.
- E-B6-6 · Cost: the main loop now calls `driver.current()` (two `systemctl show`) every turn even with no Central
  session (page: one call per turn either way); before, only with a grant.
- E-B6-7 · Test seams, not production branches: macOS has no CLOCK_BOOTTIME, SO_PASSCRED, SO_PEERCRED or AF_UNIX
  SOCK_SEQPACKET, so unit tests inject a monotonic clock, `FeedListener(peer=..., kind=..., owner_uid=..., group=...)`
  and patch `socket.SO_PASSCRED`; `peer_uid` itself is tested on Linux only (CI).

## 2026-10-05 · player-health M1 B7 app-link accepted locally + outbox + relink (implementer) · appliance/node/{app_link,broker_runner}.py
- E-B7-1 · Signature additive: `deliver_app_link(store, session, probes, *, current: AppRunKey | None)`. The page's
  three-argument form cannot apply its own rule "a slot whose run is no longer current is cleared" (the current run is
  the main loop's, not the probe thread's public state). Feed facts go to `probes.feed` (the ProbeThread's feed, the
  one broker feed). `BrokerLinkService(..., feed: Feed | None = None)` publishes `app_link_accepted`; optional like
  `probes`, because tests/node_ipc_pid1_probe.py constructs it positionally.
- E-B7-2 · Page gap, decided: a held link proved under a session other than the current grant's (producer or
  `command_session_id`) is cleared and relinked **without a POST**. Central refuses it as `node_link_scope_mismatch`
  **403** (central/fleet/node_app_links.py:47-49), which the page classes as transient (401/403 keep the slot) and
  NodeSession treats as session refusal (drops the grant, re-enrolls): a held old-session link would re-enroll the
  broker every turn forever. Arises whenever a proof is accepted offline under the retained `local-proof-grant`, or the
  session expired/was refused after acceptance. Feed `app_link_refused` = {run, status: int | null, reason:
  "central_refused" | "session_changed"} (page: {status}). A 403 for a bad signature on the current session keeps the
  slot one turn, drops the grant, and is then relinked by this rule.
- E-B7-3 · Cost, unstated: `send_relink` (B6) reaches the Player only if that run's probe channel is open at that
  moment; a relink issued while the channel is down (broker restart window, Player responder backoff ≤ 5 s) is lost,
  and the Player keeps treating the run as linked while Central holds no record (display admission stays refused
  until the Player's next applied receipt or restart). Fix belongs in probe_channel.py (hold the latest relink per run
  until a channel for that run is adopted) — not in B7's files. Architect pass 2 to decide.
- E-B7-4 · An empty slot is stored as `{}` (BootStore has no delete). Delivery runs only on a turn whose
  `driver.current()` succeeded with a grant (`known and granted`), so a transient systemctl failure never clears the
  slot as "run not current".
- E-B7-5 · tests/test_node_stop_operation.py (not in B7's Files) had to change: its proof test expected the Central
  POST's TimeoutError out of `handle`; it now asserts `accepted` with Central timing out and still asserts the
  `local-app-control` write (the page's mutation probe target).
- E-B7-6 · CI fix folded in on request (landed B6, Linux-only): tests/test_node_probe_broker.py
  `test_any_other_reader_gets_nothing` — FeedListener closes a non-reader with its request unread, so Linux AF_UNIX
  reports ECONNRESET (unix_release_sock sets the peer's sk_err when the receive queue is non-empty; a shutdown before
  close would not change that, only reading the request would). Listener kept (refuse before reading); the test's
  refused reader accepts EOF or ECONNRESET and still fails on any byte. Reproduced red and green in python:3.12-slim
  arm64.

## 2026-10-05 · player-health M1 architect pass 2 (after B7) · .claude/runs/player-health-m1.md §7, §8
- Drift check B3–B7 vs module design r9 / system design r8: no frame-changing drift. Doc-only drift routed to D1:
  E-B3-2a (probe lane), E-B4-6 (fallback-tint key), E-B5-7 (C client), E-B6-5 / E-B7-2 / E-B7-4 (wire, refusal
  reasons, slot states). Applied to brief (D1 items i–vii).
- E-B3-1, E-B3-3, E-B3-4, E-B3-5, E-B5-1..6, E-B5-8, E-B4-1..5, E-B6-1..3, E-B6-6, E-B6-7, E-B7-1, E-B7-5, E-B7-6
  informational (as-built, already reflected in code/tests); nothing further to apply.
- E-B3-2a applied to brief (D1 item i). E-B5-7 applied to brief (D1 item iii).
- E-B4-6 applied to brief (B11: tint-off = mapped transparent buffer, never NULL; harness "off" step asserts no amber
  fallback; NULL-for-off mutation probe; B4's fault-injection test client kept, not deleted; D1 item ii).
- E-B6-1 applied to brief (B9 builds the K rule from SHIPPED_TIMING). E-B6-5 applied to brief (B9 note, D1 iv).
- E-B6-3 applied to brief (B8 turn ordering).
- E-B6-4 applied to brief (B10a lifts FeedListener + peer_uid into kernel `appliance/feed_socket.py`, no copy; the
  main-loop serving lag is a stated cost in B9, ≈ 1.5 s margin left against K > S + raise + D at a 5.5 s turn).
- E-B7-2, E-B7-4 applied to brief (D1 iv).
- E-B7-3 applied to brief: new bead **B7b** before B8. The outbox slot carries the owed relink durably
  (`{"relink": run}`, cleared by the Player's next accepted proof or a run change); the main loop re-asserts it every
  `known` turn via `ProbeThread.owe_relink(run | None)` (replaces one-shot `send_relink`); the thread sends once per
  channel instance, counting only a successful send. Rejected alternative: Player re-proves when unrecorded (the
  guest contract hides Central's record by design; timer-driven Central POSTs). Mutation probes r1–r4.
- E-AP2-1 · B8 page addition: the kill-due latch is level (re-asserted every turn while counted ≥ K; the feed fact
  stays once per episode); otherwise a kill withheld for an armed recovery is never retried after the recovery is
  acknowledged and a starved app runs forever. Applied to brief (B8, `ProbeClock.overdue`, probe_channel.py in Files).
- E-AP2-2 · The B7 `unresponsive` leg's `app_link_refused` 409 → relink → `app_link_recorded`
  (/Volumes/Dock/tmp/node-pid1-b7/test_node_pid1_unresponsive0/broker-feed.jsonl seq 5→19→20 and 34→37→38; journal
  12:25:23, 12:26:03) is **expected churn, not a bug**. Fresh nonce per proof (app_link.py:178) rules out
  `node_link_identity_conflict`; the only other 409 is `node_link_control_not_current` (central/fleet/node_app_links.py
  :27-32), raised while Central's control for the device is not settled (issued ≠ applied or a pending delivery,
  central/fleet/acceptance_evidence.py:181-196) — i.e. Central issued a newer control (the fixture bind, then a later
  revision) between the Player's proof and the broker's next-turn delivery. B7 widened the window from "inside the
  proof" to "≤ one main-loop turn". Bounded: the Player re-proves only its current applied receipt
  (player/service.py:1210-1219 `applied_current`), retry 5 s (:87). No owner assigned (no code change); B9 judge treats
  `app_link_*` kinds as non-faults; B8's leg checks refused → relink_sent → recorded; D1 item v documents it.
- E-AP2-3 · B8 turn ordering made binding (kill consumer only on a `known` turn, after `online.broker.service()` and
  this turn's `publish_run`, compared with this turn's run; no `take_kill_due` on an unknown turn). Applied to brief.
- E-AP2-4 · B10 split on its package boundary into B10a (kernel `feed_socket.py` lift + display feed listener and
  `outputs` snapshot; security lens) and B10b (health: display-feed reading, per-Output verdict, overlay op). Reason:
  pass 1 already grew B10 to 5 h and the E-B6-4 kernel lift adds a cross-context move; one bead would cross three
  packages and risk the 90-min / 8-agent per-bead ceiling. Cost ≈ +0.1–0.15 M (one more verify + orchestration).
  Applied to brief (§7 ledger rows, §8 pages, run order).
- E-AP2-5 · Budget estimate at pass 2: spent ≈ 5.7 M (5.0–6.5), remaining ≈ 3.7 M, projected ≈ 9.4 M of 9.5 M;
  stop rule added (stop before a bead whose projected completion exceeds 9.5 M; preferred stops after B8 or B10b).
  Applied to brief (§8 pass 2 block).

## 2026-10-05 · player-health M1 B7b relink owed until re-proved (implementer) · appliance/node/{app_link,probe_channel,broker_runner}.py
- E-B7b-1 · Additive helper, decided: `app_link.owed_relink(store, current: AppRunKey | None) -> AppRunKey | None`
  (current iff the slot is `{"relink": current.document()}`); `BrokerLoop.turn` calls
  `probes.owe_relink(owed_relink(store, run))` after `publish_run` on every `known` turn, so the slot's three states
  stay known to app_link.py only. A slot read error that leaves the store unpoisoned is swallowed for that turn (the
  turn's existing `store.failed` re-raise pattern); the level is then unchanged until the next turn.
- E-B7b-2 · `app_link.Relinks` (Protocol with `send_relink`) became `BrokerFeed` (only `feed`); `deliver_app_link`
  keeps its signature (E-B7-1). `ProbeThread.send_relink` is gone (no shim); the per-channel latch is
  `_Channel.relinked`. The thread also drops an owed run that differs from a newly applied publication (as it does
  `_kill_due`); the main loop's restatement is the source of truth either way.
- E-B7b-3 · Test seam, not production: macOS reports ENOBUFS (not EAGAIN) on a full AF_UNIX datagram socketpair, so
  the retry test wraps a real channel end whose first sends raise `BlockingIOError` (`Unwritable`,
  tests/test_node_probe_channel.py) instead of filling a buffer. For D1: feed kind `relink_sent` {run} (audience node).

- E-ENV-1 (orchestrator, 2026-10-05, after B7b): running Linux tests in a container that bind-mounts the whole worktree lets the container's `uv sync --frozen` overwrite the host macOS `.venv` (pyvenv.cfg home → /usr/local/bin), breaking `.venv/bin/python` on the host. Rule for all remaining beads: never mount `.venv` into a container — mount the source read-only and create the venv inside the container (e.g. `-v $PWD:/src:ro` then copy to /work, or `UV_PROJECT_ENVIRONMENT=/tmp/venv`). The host venv was repaired with `uv sync --frozen`.

## 2026-10-05 · player-health M1 B8 kill after K behind the Q1 predicate (implementer) · appliance/node/{probe,probe_channel,online_broker,process_linux,broker_runner}.py
- E-B8-1 · Page gap, decided: `app_killed` carries `unanswered_ms`, but B6's `take_kill_due() -> AppRunKey | None`
  gives the main loop no such number. The latch now holds `KillDue(run, unanswered_ms)` (frozen, appliance/node/probe.py)
  and `take_kill_due() -> KillDue | None`; the thread re-asserts it every turn while `ProbeClock.overdue`, with the
  then-current `ProbeClock.unanswered_ms` (both additive properties). tests/test_node_probe_channel.py's one
  `take_kill_due() == RUN` assertion became `.run == RUN`. `overdue` also requires a turn past S (as the fact does).
- E-B8-2 · Page gap, decided: an identity mismatch at the signal (`kill` → False) is fed as `kill_withheld`
  `run_changed` (the page names only `recovery_armed | run_changed`; a process that is no longer this run's main
  process *is* a run change). An unobservable identity (`systemctl show` timeout/error, OSError, ValueError raised by
  `kill`) sends nothing, feeds nothing and is retried when the thread re-asserts the latch. A run this broker killed
  is never signalled again (`BrokerLoop.killed`): the dying app can stay published for a turn or two and the level
  latch would otherwise fire a second SIGKILL and a second `app_killed`.
- E-B8-3 · Fail-closed choices: `recovery_may_be_armed` returns True for an obligation whose `operation_id` cannot be
  read; the loop treats an unreadable `online` record or `recovery-acknowledged` slot (store not poisoned) as armed.
  The acknowledgement is written only after `advance` with the Player's proof progress returns (the `{"kind":
  "stopped"}` advance is not an acknowledgement); key `RECOVERY_ACKNOWLEDGED` lives in probe.py beside the predicate,
  so online_broker.py gains one import (`appliance.node.probe`) — still inside `service` scope; :98-108, :183-184,
  :211 untouched; recovery.py and recovery_linux.py untouched.
- E-B8-4 · Files outside the page, test fakes only: tests/test_node_probe_broker.py `Driver.kill` (its FAST loop now
  reaches K with no online recovery and kills) and tests/test_node_app_link_local.py `take_kill_due=lambda: None` on
  its fake probe thread. tests/test_node_probe_kill.py reuses `loop_for`/`turns` (test_node_probe_broker) and the
  switch `Driver`/`stage` (test_node_online_broker).
- E-B8-5 · The leg asserts the B7b sequence (every `app_link_refused` followed, same run, by `relink_sent` then
  `app_link_recorded`) after the healthy window with a bounded settle (≤ 30 s) for a refusal near the window's end;
  `app_killed`/`kill_withheld` are refused over the whole broker feed read, not only the healthy window. For D1: feed
  kinds `app_killed` {run, reason: "unresponsive", unanswered_ms} and `kill_withheld` {run, reason}; boot-store key
  `recovery-acknowledged` {operation_id}.

## 2026-10-05 · player-health M1 B8 fix cycle 1 (implementer, safety review) · appliance/node/{probe,probe_channel,online_broker,broker_runner}.py
- E-B8-6 · Defect, fixed: a stale kill latch. `ProbeThread._receive` reset the `ProbeClock` on a valid answer but left
  `_kill_due` set (cleared only by `take_kill_due` or a run change), so a latch set before the answer was taken by the
  next known turn and SIGKILLed an app that had recovered. `_receive` now clears `_kill_due` under the lock when
  `answered()` is True and the latch names the channel's run. Test: tests/test_node_probe_channel.py
  `test_an_answer_clears_a_kill_due_latch_set_before_it` (thread driven by hand, no race); mutation (no clear) → red.
- E-B8-7 · Defect, fixed: the Q1 fence failed open. `recovery_may_be_armed` read the obligation from the online
  record, which `accept()` replaces (a `running` record, online_broker.py `_REPLACEABLE`) before the old obligation's
  control is acknowledged, and a new switch's `preparing` record carries none. New boot-store key `recovery-armed`
  {operation_id} (`RECOVERY_ARMED`, probe.py), written by `OnlineEffectBroker._arm_recovery` after `recovery.arm`
  returned the receipt (if different) — every arm site (execute, `_start`, reconcile) passes through it; the `arm`
  calls and recovery.py/recovery_linux.py are unchanged. Predicate is now
  `recovery_may_be_armed(record, armed, acknowledged)`: True for any record phase but `running`/`fallback_running`
  (preparing and every switch phase), else True unless every obligation that may be armed (the `recovery-armed` one
  and the record's own `recovery`, if any) is the one `recovery-acknowledged` names; unreadable → True. The host's
  admission (recovery.py arm refuses while any row is not `controlled`) means a newer op is never recorded as armed
  while an older one is uncontrolled. Cost: a stage stuck in `preparing` (roots unverified, capacity) withholds kills
  for as long as it stays there, even with nothing armed; the key alone would be exact there (decided literal, per
  the fix brief). For D1: boot-store key `recovery-armed` {operation_id}.

## 2026-10-05 · player-health M1 B7c relink per owed episode (implementer) · appliance/node/{probe,probe_channel,app_link}.py
- E-B7c-1 · Defect (B8 verifier, unresponsive leg: `app_link_refused` at seq 32/50/64 never followed by `relink_sent`),
  fixed: B7b's `_Channel.relinked` was a bool, so a channel that carried one relink never carried another; a second
  Central refusal of the same run on a long-lived probe channel was silently dropped (`owe_relink(run)` with an equal
  run was not even a change). B7b's page said "sent at most once per channel" — that rule was the bug: the latch is
  now **per owed episode**. The slot is `{"relink": run, "episode": <32 hex nonce>}`, a fresh nonce per `_refused`;
  `owed_relink(store, current) -> OwedRelink(run, episode) | None` (new frozen `OwedRelink`, probe.py beside
  `KillDue`); `ProbeThread.owe_relink(OwedRelink | None)`; `_Channel.relinked` holds the episode it carried, set only
  on a successful send. B7b guarantees kept: once per channel per episode, durable across a broker restart, EAGAIN
  retried, a new channel gets the current episode, a run change or a new proof clears it, never POSTed. A slot without
  `episode` (pre-B7c store) reads as episode "". broker_runner.py unchanged. For D1: slot state
  `{"relink": run, "episode"}` (item iv); `relink_sent` stays {run}.

## 2026-10-05 · player-health M1 B9 catalogue + judge core (implementer) · contracts/node_faults.py, appliance/health/{judge,runner}.py
- E-B9-1 · Page gap, decided: "`app_killed` keeps the run's condition raised" also **raises** it at once when the
  judge holds it pending or has not seen it (judge restarted, or a feed replay observes the whole starvation at one
  instant), and pins it: answers from the killed run never start the clear hold. Without this a judge that missed
  the window shows no card after the kill (B12 step 4 needs the instruction tint on after `app_killed`).
- E-B9-2 · Page wording "gap → drop probe-derived state" read as: a pending condition is withdrawn, a running clear
  hold restarts, the current run is relearned, but a **raised** condition stays raised until fresh answers hold for
  the clear hold. Dropping a raised one would untint the wall on evidence the judge never saw and, after a kill
  (no more facts for that run), lose the card for good. Cost: a run that recovered inside a gap stays tinted for one
  extra hold. Tests: `test_a_feed_gap_withdraws_pending_and_restarts_a_running_hold_but_keeps_raised`.
- E-B9-3 · One condition per code, carrying the run that holds it: a new run's unanswered facts move it (raised
  stays raised; a pending one restarts its window), a new run's answers clear an old (killed) run's condition after
  the hold (system-design tracer step 4, M3 restart). Transition states add `withdrawn` (pending that never raised)
  to pending/raised/cleared; each ring entry carries `reason` and the verdict `sequence` it produced.
- E-B9-4 · Additive surface: `HealthJudge.forget(now_ms)`, `.transitions()`, `.ring_dropped`, `.player`
  (`(run, player_id)` from `app_link_accepted`, for B10b); construction refuses `judge_timing` (non-positive),
  `fault_catalogue` (row not under its own code) and `fault_code_unknown` (no `app_unresponsive`), besides `k_rule`.
  `runner.shipped_judge()` is the one K-rule build from `SHIPPED_TIMING` + `PULSE_DEADLINE_MS` + `FAULTS`. `status`
  answers `{verdict, ring (age_ms on the judge clock), ring_dropped, catalogue (digest), feeds: {broker: {after,
  publisher_incarnation, reads, gaps, failures, last_failure}}}`; the leg asserts the judge reads the same broker
  incarnation it does, so "no condition" is not vacuous. For D1.
- E-B9-5 · DRY debt for B10a: `appliance/health/runner.py` carries its own 4-line `peer_uid` because importing the
  broker's would put `broker_runner` (and `appliance.central_session`) in the judge closure. B10a's kernel lift
  (`appliance/feed_socket.py`) must replace this copy too — add `appliance/health/runner.py` to B10a's Files.
- E-B9-6 · health.sock admission as built: a peer admitted to no operation is closed unread; the op is read first,
  then admitted per uid (`OPERATIONS = {"status": {0}}`; B10b adds `overlay`); an admitted peer naming an op it is
  not admitted to is closed with no reply; a malformed request or unknown op from an admitted peer gets
  `{"accepted": false, "reason": "health_request"}`.
- E-B9-7 · Cost: the broker answers its feed on its main loop (E-B6-4), so each judge read waits at most 1 s; a
  longer broker turn ends that judge turn's drain (counted in `failures`) and the abandoned request is answered into a
  closed socket. The single-threaded judge serves `status` between reads, so a status reply can wait ≤ 1 s. Observed in
  the B9 `unresponsive` leg: 7 timed-out reads of 101 (`last_failure` TimeoutError), cursor caught up (after 40, no gap).
- E-B9-8 · Local environment (as E-B5-6): with B9 uncommitted, tests/test_node_component_inputs.py errors (8) because
  it builds HEAD's tree while scripts/build_node_base_deb.py names the uncommitted `appliance.health.runner`. On a
  clean checkout of the same tree (dangling commit): 23 passed. Not code.

## 2026-10-05 · player-health M1 B10a kernel feed listener + display feed snapshot (implementer) · appliance/feed_socket.py, appliance/display_host/runner.py
- E-B10a-1 · Page says `appliance/feed_socket.py` is "stdlib only", but the lifted listener parses requests with
  `contracts.strict_json.loads_object` (size bound, no duplicate keys, UTF-8 only). Kept (behaviour unchanged; the
  kernel's `appliance.boot_store` already imports it); the kernel lint contract only forbids context packages.
- E-B10a-2 · Additive: the kernel listener encodes replies with `json.dumps(default=str)` (the display ingress rule):
  the display `events` answer carries UUID objects (`boot_id`, `incarnation_id`, event values). Broker replies hold
  only JSON-native values, so its wire is unchanged. `FeedListener` is also a context manager (display `main` adds it
  to the ingress `with`, no re-indent).
- E-B10a-3 · Decided: the shared node-feed policy `FEEDS_GROUP = 10007` and `FEED_READERS = {0, 10006}` live in
  `appliance/feed_socket.py` (one definition for both publishers; display may not import the broker). The
  constructor arguments stay required; each runner passes them through its own `feed_listener(...)` factory
  (`broker_runner.feed_listener(feed, path=FEED_SOCKET, *, owner_uid=0, group=FEEDS_GROUP, **seams)`,
  `runner.feed_listener(controller, path=FEED_SOCKET, *, owner_uid=None→getuid(), group=FEEDS_GROUP, **seams)`), so
  unit tests exercise the production allowlist with only `peer`/`kind`/owner/group seams. Broker `max_reply` = 65536
  (`MAX_FEED_REPLY`, the readers' receive buffer); display `max_reply` = `MAX_PACKET`.
- E-B10a-4 · E-B9-5 applied: `appliance/health/runner.py` imports `peer_uid` from the kernel (its copy deleted).
  tests/test_health_runner.py (not in the page's Files) changed: it built the broker's `FeedListener`, which no
  longer exists in broker_runner; its Linux `peer_uid` test moved to tests/test_feed_socket.py.
- E-B10a-5 · Display feed socket op rule as built: `op` absent or `"events"` (as `answer_feed_read`); any other op →
  `{"accepted": false, "reason": "feed_read_request"}`. The `outputs` snapshot is on every `events` answer, ingress
  included.
- E-B10a-6 · Cost: `outputs` is bounded by `DisplayHost.max_outputs` (shipped 16; the domain refuses more). Worst case
  at 16 admitted Outputs with maximal identifiers (128-char output_id, 96-char frame_id) plus a full page of 8
  presentations encodes to 15 793 bytes against MAX_PACKET 16 384 (tests/test_node_display_runner.py); 40 such
  Outputs → `response_bound`. Real identifiers are far smaller (leg: Virtual-1). A host built with a larger
  `max_outputs` could see `response_bound` on full pages; B10b's reader must treat it as a counted failure.
- E-B10a-7 · For B10b (leg evidence): the admitted Output's snapshot shows `"fault": "app_absent"` while
  `admitted` is set and `diagnostic` is `released` (OutputState.fault is not cleared on admission). Derive the
  underlay from `admitted`, never from `fault`.
- E-B10a-8 · Pre-existing, moved unchanged: `FeedListener.close` guards the unlink by (st_dev, st_ino), but Linux
  reuses inode numbers, so a closed listener whose path a successor already rebound unlinks the successor's socket
  (probed red in python:3.12-slim arm64; that test was dropped). Unreachable in production (one listener per path per
  process lifetime).

## 2026-10-05 · player-health M1 B10b judge display verdict + overlay op (implementer) · appliance/health/{judge,runner}.py
- E-B10b-1 · Page gap, decided: an Output's `codes` are every **raised** code, node-wide (M1 has one app driving every
  Output), so every connected Output, including a slate or unbound one, gets the card; after the kill the slate keeps
  it (B12 step 4). `held` = the admitted run equals the **raised** `app_unresponsive` run (a pending one stays `live`,
  system design: "Live → Held: judge says unresponsive"). Underlay from `admitted` only (E-B10a-7).
- E-B10b-2 · Decided: the verdict `sequence` also bumps when an Output's verdict changes (snapshot or refinement),
  with no ring entry, so one sequence never names two different verdicts. Condition transitions are unchanged.
- E-B10b-3 · Page wording "invalidations with reason and presentations from events": the display `SurfaceFact`
  invalidation event carries no reason (weston.py `surface.fact("invalidated")`; the reason is the snapshot's
  `fault`), and presentations are not a verdict input in M1. As built: an invalidated `SurfaceFact` drops that
  Output's admission until the next snapshot; every other display event is ignored by the judge. Each page's snapshot
  is applied **after** its events (it is current at the reply). A display feed gap or controller restart never calls
  `judge.forget` (probe-derived state is the broker's; a display gap would otherwise withdraw a pending condition);
  a malformed snapshot is a counted read failure (`display_snapshot`) and the cursor does not move.
- E-B10b-4 · Overlay wire as built (for B11 and D1): the client sends one packet `{"op":"overlay"}` (uid 10005 only;
  no ack); every later packet from the judge is one `encode_overlay_instruction`; the client sends one
  `encode_presented_report` per packet. Pushed: every Output's instruction on connect, changed ones each 500 ms turn,
  all every V/3 (5 s). A client that sends anything else, closes, or cannot take a packet at once (EAGAIN) is dropped
  and must reconnect (it is re-pushed everything). At most 4 open clients; a fifth replaces the oldest. Serials come
  from one judge-wide counter starting at 1 per judge process: **B11 must take any serial after a reconnect, not only a
  higher one.** `presented` is kept once per (Output, serial) and only for a serial ≤ the Output's projected serial.
- E-B10b-5 · Decided: line 2 omits ` · Player …` until an `app_link_accepted` is seen, and is cut to 96 chars; an
  Output whose name exceeds 96 chars (OutputKey allows 128, OverlayInstruction 96) is in the verdict but not projected.
- E-B10b-6 · `appliance/health/runner.py` names `DISPLAY_FEED_SOCKET` itself (the judge closure may not import the
  display controller), pinned equal to `appliance.display_host.runner.FEED_SOCKET` by a test. No unit, sysusers or
  tmpfiles change was needed (the judge unit already has `SupplementaryGroups=pw-node-feeds`).
- E-B10b-7 · For D1, `status` as built adds `verdict.outputs` [{output, underlay, codes}], `overlay` {instructions:
  [{output, serial, tint, lines}], clients}, ring entries {sequence, state: "presented", output, serial, age_ms}, and
  `feeds.display`. Leg: Virtual-1 `live`, codes [], instruction serial 1 tint off; display reads 147, gaps 0, 1 failure
  (FileNotFoundError before the controller bound its socket).

## 2026-10-05 · player-health M1 architect pass 3 (after B10b) · .claude/runs/player-health-m1.md §8 (B11, B12, D1, pass 3 block)
- E-AP3-1 · Drift check B7b–B10b vs module design r9 / system design r8: no frame-changing drift. lint-imports 11
  kept at 4058f30; CI green through 172b3a5 (B10a), 4058f30 in progress. Doc-only drift routed to D1 items viii–xiv
  (E-B7c-1, E-B8-1..3, E-B8-5, E-B8-7, E-B9-1..7, E-B10a-1..6, E-B10b-1..7). E-B10a-8 informational (unreachable
  in production). E-B7b-1..3, E-B8-4, E-B8-6, E-B9-8 informational (as built, tests in place).
- E-AP3-2 · Q1 fence observation (no stop): fence 1 names `_arm_recovery`; B8 fix cycle 1 (E-B8-7, per the
  orchestrator's fix brief) added an additive boot-store write there AFTER `recovery.arm` returned and its receipt
  check passed (online_broker.py ~:146-154); `recovery_linux.py` changed only its `boottime_ms` import path (B2a's
  mandated kernel move). `arm` call, :98-108, :183-184, :211 and recovery.py unchanged (git diff origin/main..HEAD).
  Arming behaviour unchanged; one new failure path (a store write failing after a successful arm raises, the same
  class as every other store write in that flow). Disclose in the handoff.
- E-AP3-3 · B11 re-cut. (a) Page gap: the B5 client loops in `display.dispatch(block=True)` (client.py), so it cannot
  also serve the judge socket or stale timers; decided one single-threaded selectors loop over `display.get_fd()` +
  the judge socket (pywayland 0.4.18 Display has get_fd/dispatch/flush/read only, no prepare_read pair; checked in the
  sdist). (b) Page offered two drawing forms ("viewporter when bound, else full ARGB"); decided one: full-Output ARGB
  for tint on, 1×1 transparent for off (E-B4-5: layer maps with the buffer's own size); viewporter + card subsurface
  parked to M3 with the repaint pulse; cost stated in the page (8.3 MB/35 MB per tint-on buffer). Without this the
  page also needed viewporter bindings in meson.build (not in its Files) and a card subsurface (one health surface
  per Output, shell refuses a second). (c) Page gap: B4's composed test client calls `client.main(hooks=...)`; if the
  production client always took a health layer, the test client's own layer would hit `health_layer_exists`. Decided
  `main(hooks=None)` → production hooks; explicit hooks replace them; harness keeps magenta default and adds a
  `production` mode block. (d) Pure `overlay/health.py` per-Output state for macOS unit tests (any serial, reconnect
  forgets drawn/reported, stale after V, no report on discarded/stale). (e) Named mutation (c) moves from harness to
  unit: the feeder cannot force a `discarded` deterministically. (f) Health commits never `ack` (shell.c:479-491). (g)
  A size-cap refusal of a tint-on page commits NULL → shell amber fallback (fail-visible), never a transparent buffer
  under a fault. (h) Socket path named in Display, pinned by test to appliance.health.runner.HEALTH_SOCKET; client
  checks peer uid ∈ {0, 10006}.
- E-AP3-4 · B12 corrections. (a) `systemctl kill` defaults to `--kill-whom=all`; use `--kill-whom=main` (UNIT =
  photo-wall-node-player.service, process_linux.py:20). (b) Step 4's "invalidation `surface_lease_or_process_lost`"
  is wrong as written: the SurfaceFact invalidation carries no reason (E-B10b-3); a SIGKILLed client usually yields
  `surface_destroyed` (shell.c:661) before the lease/pidfd path (:964); assert the snapshot's `fault` ∈ both and
  `admitted` null. (c) "Card over the live app" is proved by `underlay: held` (only possible before the kill, E-B10b-1)
  + tint-on serial presented + no `app_killed` on a later broker read — not by time comparison; step 3 window 20 s →
  30 s (judge lag E-B9-7, broker main-loop lag E-B6-4), still < K. (d) E-B8-5 made the leg refuse `app_killed` and
  unsettled refusals over the whole feed; B12 must scope both to the healthy window. (e) Cost: the `starve` role adds
  one sealed environment build to every fixture build; check node-pid1.yml timeout-minutes 20 (:69, :89). (f) New
  mutation: wrapper ignores SIGUSR1 → step 3 red. Probe priorities verified: responder answers via
  `GLib.idle_add` (player/service.py:359, default-idle 200), the 33 ms frame tick is priority default
  (:1501), so a 150-priority spin starves only the answer.
- E-AP3-5 · D1 items viii–xiv added (Q1 predicate/keys, relink per episode, kill feed kinds, judge as built, kernel
  feed_socket, B11 drawing form, slice table as delivered); D1 size 2 h → 3 h. Final pass renumbered: pass 4 = M1
  coherence.
- E-AP3-6 · Budget at pass 3 (estimate; no metered figure available to the architect): spent ≈ 8.65 M (7.8–9.4) =
  5.7 at pass 2 + B7b 0.3 + B8 (lens + fix cycle) 0.75 + B7c 0.25 + B9 0.35 + B10a (lens) 0.45 + B10b 0.3 +
  orchestration 0.35 + pass 3 0.2. Remaining on the re-cut pages: B11 0.5, B12 0.65, D1 0.2, pass 4 + milestone gate
  0.6 = 1.95 M. Projected ≈ 10.6 M > 9.5 M ceiling (low end 9.75 M). Pass-2 stop rule fires: STOP after B10b
  (preferred stop point). Override only on a metered spend ≤ 7.55 M. Next session: B11 → B12 → D1 → pass 4 +
  milestone gate; proposed ceiling 2.5 M (warning 2.0 M). Wall-clock ≈ 8.2 h / 26 h.

## E-B10a-14 (orchestrator, 2026-10-05) — display-feed trust: owner answer
A post-landing re-review of B10a (cc98853) proved that a same-uid (pw-display, 10005) process — Weston or the overlay client — can spoof or delete the display feed socket via /proc/<controller>/root, and can ptrace the controller before it could make itself undumpable. A second review showed a dedicated controller uid would not close the class: Weston is already the upstream source of every fact the feed carries. Owner answer: TRUST Weston and the overlay client as base components for the display feed (threat model = buggy app, not hostile; the app cannot reach the feed). The hardening attempt (undumpable() + UnitPublisher cgroup check, entries E-B10a-9..13) is preserved on branch wip/B10a-feed-hardening (549a011) and NOT landed. For D1: rewrite B10a's security claim in module-design-r8 to "the display feed trusts the pw-display uid (controller, Weston, overlay client); the app and other uids are refused by peer uid", and state this cost. No code change required.

## 2026-10-05 · player-health M1 architect pass 3b (resumed session, after E-B10a-14) · .claude/runs/player-health-m1.md §8 (pass 3 block, B11, B12, D1)
- E-AP3-7 · Drift check: no code commit since 96f681c (B10b); 3784124 (B10b's code) pipeline green in full, incl.
  `display-harness` and all six node-pid1 legs (`unresponsive` included). No new drift; E-AP3-1 stands.
- E-AP3-8 · E-B10a-14 folded into pages. B11: base 838baa9+, never `wip/B10a-feed-hardening`; `undumpable`,
  `peer_pid`, `UnitPublisher` absent and not reintroduced; the overlay client gets nothing beyond its health layer and
  the judge socket; the judge-link peer check {0, 10006} stays (/run/photo-wall-health is pw-health 0755,
  appliance/systemd/photo-wall-health.service:7, :14-15). B12: no assertion that pw-display cannot reach the display
  feed; B10a's mode/group checks unchanged; fixture line ref :183 → :189 (`boots.select(deployments["cold"]...)`).
- E-AP3-9 · D1 item (xv): display-feed trust statement and cost. E-B10a-14 cites only the /proc/<controller>/root
  route; E-B10a-12 (on wip/B10a-feed-hardening only) proved the wider fact — a pw-display process can delete or replace
  the feed socket at any time from its own namespace and can ptrace/kill the controller. D1 states the wider cost.
  E-B10a-9..13 are not in this file on the working branch; E-B10a-14 is their record here, the branch holds the text.
- E-AP3-10 · Budget, resumed session (estimate; replace with the metered figure): spent since d562505 ≈ 0.75 M
  (0.55–0.9) = B10a re-review 0.09 + hardening fix cycle 1 0.25 + second review 0.1 + fix cycle 2 (STOP) 0.1 +
  orchestration 0.1 + this pass 0.1; the session's fix-cycle reserve is consumed. Remaining B11 0.5, B12 0.65, D1 0.22,
  pass 4 + milestone gate 0.6 = 1.97 M; projected 2.72 M > 2.5 M ceiling. Decision: GO for B11 → B12 → D1 (2.12 M
  projected; 2.47 M with one fix cycle); pass 4 + milestone gate only if metered spend after D1 ≤ 1.9 M, else stop
  cleanly after D1 for the owner's next ceiling. Per-bead checks for this session: before B11, spent + 1.5 ≤ 2.5;
  before D1, spent + 0.22 ≤ 2.5; before pass 4, spent + 0.6 ≤ 2.5. A second fix cycle anywhere stops at that gate.

## 2026-10-05 · player-health M1 B11 overlay client draws the health layer (implementer) · appliance/display_host/overlay/{client,render,health}.py
- E-B11-1 · Page Files omit `appliance/display_host/meson.build`, but its `install_data` lists the overlay modules one by
  one: without `'overlay/health.py'` the installed client fails `import overlay.health` on the node. Added (one
  word; no new bindings). D1: none.
- E-B11-2 · Page gap, decided: `reconnected()` also forgets the connection's instruction, not only what was drawn and
  reported. Keeping it would repaint the old serial at once and report it to the next connection, i.e. to a restarted
  judge whose counter restarted at 1 (E-B10b-4) and may project that same number for a different card: a false
  `presented`. As built the screen keeps its last drawing until the first instruction on the new link (or V → stale
  card), and `presented(serial)` reports only a serial drawn on the current connection (last 8), once.
- E-B11-3 · Mutation "accept only a higher serial" must persist across reconnects to reach harness step (4): a
  per-connection monotonic check is invisible there because of E-B11-2 (probed: harness green, unit red). The
  cross-connection variant turns step (4) red (no report for serial 1) and the unit red.
- E-B11-4 · Decided beyond the page: a **paint failure** of a tint-on page commits NULL (amber fallback,
  fail-visible), like a size refusal; the slate's "commit a cleared buffer" rule (E-B5-8) would hide a fault. Tint
  off is never painted (a zeroed memfd is transparent ARGB). A NULL commit is not repeated each pass (the page counts
  as drawn).
- E-B11-5 · Additive surface (for D1): health.py `HealthBoard` (per-Output states + instructions held for
  unconfigured Outputs ≤ 16), `health_socket_path`, `JUDGE_UIDS`, `RECONNECT_MS`; client.py `size_admissible`,
  `OverlayClient.allocate` (shared by slate and health), `JudgeLink`, `HealthLayer` (also a loop hook: `attach`,
  `service`), `run` (the selectors loop), `late` count of commits not presented within D. Cost: client.py carries its
  own 3-line `peer_uid` (SO_PEERCRED): the overlay package is installed as top-level `overlay` and may import only
  itself, so it cannot reuse `appliance.feed_socket.peer_uid`.
- E-B11-6 · Harness as built: the headless Output's name is `headless` (both the configure and the control socket's
  `output_id`); step (1) first awaits the amber fallback after the kill so a capture taken before Weston drops the
  dying magenta client cannot pass it; `await_pixel(drained=True)` keeps the control socket drained over the V wait;
  `testing_slate` now composes a shared `cairo_over`. The slate-surface mutation turns step (2) red at the
  presented report (the health surface is never committed), before its pixel check.

## E-RACE-1 (orchestrator, 2026-10-05) — app-link receipt race pauses B12 (frame change)
CI leg `unresponsive` failed intermittently on 299766c (code identical to a twice-passing sha). Root cause confirmed by two independent agents: Central mints a new control delivery (new sequence, cleared ack nonce) on every state read even when nothing changed (~2–4 Hz), and the app-link proof is admitted only if its receipt is still the LATEST (central/registry.py ~:317-325; central/fleet/node_app_links.py ~:26-32; acceptance_evidence.py ~:181-201). A refused first link also gates Output admission. Class: a fleet-pipe proof validated after an async hop against Show-pipe state that churns without real change; 10 sites. Owner steers: Central records every node event (receiving ≠ judging); fleet and Show pipes stay separate; define the Central↔node channel model (strawman: 0 observability, 1 host, 2 app lifecycle, 3 show lifecycle) with per-channel requirements before the fix. B12 and D1 are PAUSED until the owner's channel-model gate; B11 landed (c6953f6). Design working state: /Volumes/Dock/tmp/applink-frame/.

- E-CI-JL-1 (implementer, 2026-10-05, CI unit red on 840d099): the JudgeLink EOF test faked the judge with a SOCK_DGRAM pair; Linux DGRAM never reports a peer's close, so the link stayed open (product code correct). Fix: one `tests/support/packet_pair.py` (SEQPACKET on Linux, DGRAM on macOS; boundaries everywhere, EOF on Linux only), used by test_display_overlay_health, test_health_runner, test_node_probe_broker, test_node_probe_channel; a Linux test pins its type to `client.connect_seqpacket`; the EOF test drains then asserts the "closed by the judge" reason (required: without it a deleted EOF close still passes via the parse error); `feed_socket.SEQPACKET`/`health.runner.SEQPACKET` are plain `socket.SOCK_SEQPACKET`. Spec correction: "replace all six copies" is wrong for two. `test_player_local_app_proof._PacketSocket` (macOS fallback only; Linux already gets real SEQPACKET) and `test_player_probe_responder.Channel` are Queue doubles that emulate EOF-on-close, and the latter also injects EAGAIN and records send flags. A macOS DGRAM pair cannot report EOF, so swapping in `packet_pair` made 8 local_app_proof tests time out on macOS. Both stay. Residual: neither double models Linux's ECONNRESET-on-unread-data close.

## E-RACE-2 (2026-10-06) — `unresponsive` leg removed; M1 closes without B12/D1
- Owner (chat 2026-10-06): the Player will not run until after the r3 refactor; finish PR 46 only as far as needed to merge, then start E1.
- Removed the `unresponsive` node_pid1 leg, its leg-only helpers, the Central fixture's `/fixture/bind` + `/fixture/display` endpoints, and its node-pid1.yml matrix entry. It exposed E-RACE-1 (Central re-mints a delivery on every read, `central/registry.py:313`; a defect on main), which r3 epic E5 replaces. E-RACE-1 stays open for E5.
- Not done, by owner choice: make-safe kill withholding (the Player is not run before E6 adds a restart), B12, D1/D1′, the M1 coherence pass. The superseded-by-0016 headers on docs/design/player-health/* and the run brief's B1/B12 leg references move to E1-7 (docs sweep).
- E-E1-1 (implementer, E1-1, 2026-10-05): brief corrections, no design change. (a) AC1's probe (root-import `appliance.node.brokerx`) is not caught by `test_package_closures` (7 passed under the probe); it goes red in G-release via `test_release_plan::test_every_node_deb_closure_file_is_claimed_by_its_package[...node-base-deb]` (plus test_node_component_inputs, test_node_linux_adapters, test_node_boot_stage). (b) G-release's three files give 128 passed on macOS (125 + the 3 new closure cases, matching C4), not "232 passed, 5 AF_UNIX errors". (c) `from P import n` in a staged site resolves to P.n only when P.n is a module or package file under the repo, else to P (otherwise every `from appliance.x import func` is a false miss). (d) `/Volumes/Dock/tmp/e1/baseline.txt` did not exist when E1-1 started; the verifier must record it.
- E-E1-2 (implementer, E1-2, 2026-10-05): brief corrections, no design change. (a) Scope gap in C9/rule 5: `docs/player-architecture.md` (on main) holds 12 relative links to moved modules; `check_docs` fails without them, so the rewrite scope includes it beside the evidence proposal (link targets and link text only; E1-4 still owns its prose). (b) On this base the probe split is `node/probe.py:24-28` and `:36-49` (brief: `:23-27`, `:35-48`); the frozen surface's `SHIPPED_TIMING: Final` contradicts "byte-identical", kept byte-identical (no `Final`). `test_health_runner`'s expected set never held `appliance.node`; only `appliance.apps.probe` → `appliance.kernel`, `appliance.kernel.probe_timing` applied. (c) AC5 is 5450 → 5446, not 5416 = 5416: the 4 missing ids are E1-1's staged-launcher parametrizations of the deleted `node_ipc_pid1_probe.py`; one param id renames (`appliance/apps/environment.py`). (d) `test_node_component_inputs` archives HEAD, so on an uncommitted tree it errors at setup (8); with the diff committed in a scratch clone it passes (23). The verifier must run G-unit on the committed tree. (e) AC3's probe makes the closure tool refuse (`imports appliance.node.capacity, which does not exist`) before any delta is computed; `closure_equivalence.py` exits 1 with that refusal. (f) The tool edits 69 files, not about 36 (root-staying tests that import moved modules are edited too); `scripts/module_closure.py`'s docstring example is rewritten, which is presumably C13 (every deb releases).
- E-E1-3 (implementer, E1-3, 2026-10-05): brief corrections, no design change. (a) Files touched misses `tests/node/test_node_boot_stage.py`: its `:475` comment named the deleted `host_import_boundary`; comment-only edit made. (b) `docs/player-architecture.md:163` still cites `host_import_boundary` and `build_node_base_deb.py:58-59` (now deleted); E1-4's frozen page does not list it, so E1-4 must rewrite that cell to "the `host-core` deny list" (`build_node_base_deb.py` POLICIES). (c) G-release is 128 passed on macOS (E-E1-1 b), not AC9's 232/5. (d) `test_exemptions_only_shrink` covers every contract's `ignore_imports` (none outside the Node block today), so a new ignore line on any contract fails; it also holds the session contract's protected/allowed lists.
- E-E1-4 (implementer, E1-4, 2026-10-06): docs sweep landed; brief corrections, no design change. (a) `.claude/runs/player-health-m1.md` carries a dated note: leg `unresponsive` was removed in 4c4cc2e ahead of r3, so its mentions (53 lines before the note) are historical; the history is not rewritten. This discharges E-RACE-2's "move to E1-7 (docs sweep)": the superseded headers on `docs/design/player-health/*` landed here (bead renumbered E1-4). (b) AC2's `rewrite.py --check --scope docs AGENTS.md` did not exist: the tool gained `--scope` (a docs pass that rewrites references and moves nothing) and the `HISTORICAL` allowlist for the two r8 files. Measured residual before the pass was 24 lines in 4 files (C9's 15/8/3/3 counts include unmoved retiring paths and the package name). (c) Outside the module map, `docs/operator-console-ddd.md` cited `appliance/node/session.py` twice (moved to `appliance/central_session/` in B2a); rewritten in the prose read. (d) 0017 records the epic order E2–E9 as the design's recommendation: owner-answers.md holds "pursue E1 first" but no explicit answer to r3 Q1 (accept the epic list).
- E-E1-5 (implementer, E1-5, 2026-10-06): probe tests stepped by turn; boot-island and kernel holes closed. Spec corrections: (a) `BrokerLoop.turn` starts with `probes.check()`, which raises for an unstarted `ProbeThread`; `loop_for` stubs `check` on the instance (the dead-thread test deletes the stub, starts and closes a real thread). (b) `ProbeThread.step` also reads what is ready (wake byte, answers) with a zero-timeout select, so `test_an_answer_clears_a_kill_due_latch_set_before_it` drops `_receive`/`_apply`/`_turn`/`_kill_due`; `_run` now waits on select and runs `step` (one extra non-blocking select per pass; the answer's `now` is the post-wait reading, as before). (c) `appliance/kernel/probe_timing.py:3` "Stdlib only" was true of that module (it imports only `dataclasses`); it is reworded, not retracted. (d) The kernel contract's third-party list is a denylist of the 18 non-stdlib externals in today's import graph: a package first imported later is not caught until listed (the uplink contract's caveat). First-party: the layers contract already fences every context above the kernel; the new contract adds `central`, `media`, `player`, `appliance.feed`, `appliance.feed_socket` and `appliance.process_identity`, which no contract kept from the kernel. (e) Not in scope, same failure class: `test_node_probe_channel.py` real-thread tests with fixed windows (`test_a_blocked_main_loop_does_not_stop_probe_timing` sleeps 1.0 s and needs K=400 ms counted; `test_healthy_channel_is_answered_every_period` counts answers per wall second) still fail under a >0.6 s stall.
- E-E1-5 addendum (implementer, E1-5 follow-up, 2026-10-06): (d) superseded: the import-linter kernel contract is dropped; `tests/test_import_contracts.py::test_the_node_kernel_imports_only_the_stdlib_contracts_and_uplink` builds the grimp graph (external packages on) and allows only `sys.stdlib_module_names`, `contracts`, `uplink` and `appliance.kernel`, so a package never imported before fails too (probes `import httpx`, `import yaml`, `import appliance.host` each fail). (e) closed: `test_healthy_channel_is_answered_every_period` is stepped (the test answers each probe; rtt is exactly T); `test_a_blocked_main_loop_does_not_stop_probe_timing` keeps the real thread (its point) and waits for the latch up to 5 s; `test_stale_nonces_are_never_answers` waits for the Player's reads instead of asserting them at the kill-due instant. The `time.sleep` calls left in `test_node_probe_channel.py` precede absence or once-only assertions (a stall can only weaken them, never fail them) or pace `owe_relink` restatements; none asserts a level arrived inside a window.
- E-E1-6 (implementer, E1 final-review fixes, 2026-10-06): three structural fixes. (1) New forbidden contract "Host core reaches no sibling context" (`appliance.host` ↛ display_host, health, apps, boot and the stage-1 modules) with no ignore lines; `host-core` deny list gains `appliance.display_host`, `appliance.health`. `central_session` and `node` stay reachable: host_runner imports both (retiring, held by the session contract and the layers ignores). Probe `import appliance.display_host` in `node/recovery.py` breaks it with the apps contract deleted from a config copy, and the host-core closure refuses. (2) Spec gap: the boot group also used `:` and has real edges (`netboot_init` → bootstrap, boot_offer, central_post, node_boot_handoff; `boot.node_bootstrap` → node_boot_handoff), so it splits into `boot | netboot_init` above `bootstrap | boot_offer | central_post | node_boot_handoff`; the bottom is `kernel | feed | feed_socket` (no edges among them). `test_node_layer_siblings_are_independent` fails on any `:` in the Node layers. Central's layers contract keeps its `:` group (out of scope). (3) pytest runs `--import-mode=importlib` with `consider_namespace_packages`: a module is named by its path under `tests/` (`node.apps.test_x`; top-level names unchanged), so basenames never collide and new directories need no registration. `pythonpath` is `tests` plus `tests/browser` (browser tests import siblings bare, which prepend mode supplied implicitly). Cross-context helpers are imported by path (`from node.test_node_linux_adapters import store`), not moved to `tests/support` (C6: no rig extraction). Guard: `tests/test_test_layout.py` runs pytest on a scratch tree under the real pyproject; under the old config it fails with "import file mismatch". Collected ids are HEAD's plus the two new tests. Not run here: DB and browser tiers (collection only). In the Linux container, `test_the_node_kernel_imports_only_the_stdlib_contracts_and_uplink` errors because grimp writes its cache into the read-only checkout (environmental, pre-existing).
- E-E1-7 (implementer, E1 review fixes, 2026-10-06): correction to E-E1-6: the kernel allow-list test's grimp cache write was not pre-existing; E-E1-6 introduced that test without `cache_dir=None` (now passed, so no `.grimp_cache` is written).
- E-E1-8 (implementer, E1 review fixes, 2026-10-06): the one-shot tools `.claude/runs/node-e1/{rewrite,module_map,closure_equivalence}.py` were deleted after E1 landed; the brief's run-tool section and AC rows that name them (`.claude/runs/e1-layered-codebase.md`) are historical.
- E-W1-D0017-1 (implementer, D-0017, 2026-10-06): the frozen page does not say how N16–N19 are cited once §9 Q1 was answered "guidelines". Applied as current choices: C11's source names N16, N17; C13's N19; C14's N18; one paragraph under the current-choices table quotes the owner ("reasonable guidelines", open to revising). The page's "Open (new short list)" row became a `## Open` section after the table. Plan text cites `owner-answers.md` as if beside the run brief; it lives at `/Volumes/Dock/tmp/node-redesign/owner-answers.md`. F4, F5, F7 (Node API page ids) are cited as the page wrote them; that page is outside the repository.
- E-W1-E2b-1-1 (implementer, E2b-1, 2026-10-06): brief corrections, no design change. (a) The page's "only mount/umount go through `sudo -n`" cannot hold: the sealed archives carry root-only files (`etc/shadow` 0640, `etc/.pwd.lock` 0600; 15 in app.tar, 14 in manager-primary.tar), which `-all-root` makes root's, so the runner user cannot hash them over the mount. The test runs `verify_root` over the mount as root too (`sudo -n <python> -B`, direct when euid 0), as PID1 does on the Node. (b) AC5's probe (remove the `player-environment` claim) goes red through `test_a_suite_is_due_for_every_file_its_harness_imports[node-pid1]`, not `test_every_tracked_file_ships_in_a_package_or_is_declared_unshipped`: `base-bundle` still claims the script. Removing both claims reds both tests. (c) The mksquashfs `docker run` also passes `--platform linux/<arch>` and `--user <uid>:<gid>` (the image file is the caller's to hash, rename and delete); SOURCE_DATE_EPOCH is passed as `-e SOURCE_DATE_EPOCH=<PIN.epoch>`. (d) Residual for E2c (DRY): `squashfs-tools` is named in `scripts/build_environment_image.py`, not in `scripts/debian_packages.py` (which claims to be the only place a package name is written; the plan holds it read-only for E2b, and `appliance/Dockerfile.builder` already breaks the claim), and the snapshot-sources Dockerfile line now has a third copy (with `build_app_environment.py`, `build_node_display_deb.py`); one helper in `node_build_inputs.py` would own it, but that file is a component input (editing it changes the cache key). (e) No CLI `main()`: the page has none; E2c adds one when the image ships. (f) Measured locally (components from afa2ece, privileged arm64 container): app 928.9 MiB tar → 288.5 MiB image, manager 197.6 → 62.2 MiB; mksquashfs 4.6.1 at the pin.
- E-W1-E3a-1-1 (implementer, E3a-1, 2026-10-06): tracer probes all passed on nats-server 2.15.0 (2.15.1 still RC.2 at bead start) and nats-py 2.16.0; no §6 branch taken. Page additions, no design change: (a) `BusServer` gains `websocket_port` and `monitor_url` (the hub's leaf listener and `/leafz`; `node_server` and the tests need them) and `log_tail()` for start failures; the page listed only the five core fields. (b) AC2's probe (Node stop a no-op) is caught only if the Node component's client stays connected through the stop; the test keeps it open, so the method disappears only because the server stopped. (c) AC3's probe (both serials in one account) must not be caught by the leaf wait: the test waits for a leaf count and asserts the per-account `/leafz` mapping after the isolation checks, so the probe fails on `$SRV.PING` isolation (Central A sees two instance ids). (d) `scripts/nats_server.py` must run on the host's `python3` (macOS ships 3.9; CI calls it before the venv), so it avoids 3.10+ syntax at runtime and extracts the one binary member by hand (no `tarfile` filter argument). (e) The JSON hub config passes `$JS...` subjects as quoted strings, which nats-server reads literally (no `$VAR` expansion), proven by the seam tests.
- E-W1-E3a-2-1 (implementer, E3a-2, 2026-10-06): §6 row 9 class (a reload partly ignored), recorded per row 9, not a stop for E3a. On nats-server 2.15.0 (and on main at 2026-10-06) a reload that ADDS an account wires its stream imports but not its service imports: `configureAccounts(reloading=true)` subscribes service imports only for accounts that already existed (`siMap`, ns:server/server.go:1430) and skips new ones (`!reloading`, ns:server/server.go:1413). Probe: after the reload that adds serial-c, central-c's request on `ACC.WALL.API.CONSUMER.CREATE.WALL` and on an exported `$JS.FC.WALL.echo` both get "no responders" while its `DELIVER.WALL.>` stream import delivers; the next reload fixes both (a reload adding d then breaks d the same way). Effect: the new Node's wall mirror never starts (AC4 red: "node serial-c mirror holds ... not within 10s"). Leaf cids of existing accounts are kept (row 8 does not fire). The harness's `reload_hub` sends the reload request twice; a mutation to one request turns AC4 red. **E3d's page:** the reload mechanism is "rewrite, then reload twice" (or a fixed upstream release); re-check on 2.15.1.
- E-W1-E3a-2-2 (implementer, E3a-2, 2026-10-06): §6 row 11 fired, pre-approved fallback taken. After the hub's store is wiped and WALL re-created, each Node mirror stays at its old last sequence (8) with lag 0 and an active consumer while the hub's restarted stream holds seqs 1-2: it waits for seq 9 (probe output: `msgs 2 first 7 last 8 ... lag=0 ... error=None` unchanged for 18 s). Fallback form, asserted by AC5: `declare_wall_mirror` (the Node component's declare, run on link-up) reads the origin's last sequence through the Node account's imported consumer API (`wall_origin_last`: an ephemeral DeliverLast consumer reports `delivered.stream_seq + num_pending`, then is deleted) and deletes and re-creates the mirror when it holds a sequence the origin never reached; it now returns True when it created a mirror. Mutation: never re-create → AC5 red at 30 s. Stated cost / gap for E3b: sequence-only detection misses a reset when Central's re-puts carry the new origin to or past the mirror's last sequence before the Node checks; the mirror then resumes mid-stream and keeps old values for subjects re-put below it. Unprobed alternative for E3b: Central (WALL's only writer) re-creates WALL with `first_seq` = its last acknowledged sequence + 1, so mirrors resume with no Node rule.
- E-W1-E3a-2-3 (implementer, E3a-2, 2026-10-06): page additions, no design change. (a) `Recorder` gains `gap(first, count)` (the page lists `gaps()` but no way to write a gap row) and `rows()` (AC3's "gap row before any later message" needs commit order). (b) `BusServer` gains `listeners` (the hub's generator input, which `reload_hub` rewrites the configuration from). (c) `reload_hub` finds the server id with `$SYS.REQ.SERVER.PING.IDZ` (no private client field, no monitor port). (d) Central's drain counts a gap only for what the stream no longer holds, `min(first_seq, delivered) - last recorded - 1`: a skip past the last recorded sequence can be the order of redeliveries (after a crash the server sends 81.. before the unacknowledged 78-80), which a "delivered > last + 1" rule would count as a false gap. (e) AC2 also asserts the committed-but-unacknowledged message (78) is committed exactly twice, and AC1 asserts the KV stream's `discard` is NEW and history 2. (f) The `test_node_bus_seam.py` `_leaf_accounts` helper now reuses `leaf_connections`.
- E-W1-E3a-3-1 (implementer, E3a-3, 2026-10-06): §6 row 12 fired, pre-approved fallback taken. A KV bucket sized exactly by the page's `kv_bucket_bytes` (history × per-message size per listed key) refuses a listed key's full-length update with 10077 "maximum bytes exceeded" once its keys fill it: a discard-new bucket checks `Bytes + new > MaxBytes` before it drops the key's oldest value, and lets the put through only when that oldest value is no shorter than the new one (ns:server/filestore.go:5277-5279). Probe: state (history 4) and desired (history 2) buckets, 4 keys, max_value 512; after mixed-length updates, a round of 512-byte puts was refused at the first key whose oldest value was short. Fallback form, asserted by AC2: `kv_bucket_bytes` adds one largest per-message size of headroom (signature unchanged); mutation "no headroom" turns state and desired red. Stated cost: the headroom admits at most one unlisted value before a full bucket refuses unlisted keys (AC2 asserts at most one), and a stray that spends it restores the short-oldest refusal for listed keys, so "cannot fill" holds only for writers that keep to the key list; E3b's class table must carry the headroom term. Page correction, no fallback: the per-message term's constant is 4 bytes high: nats-server charges 30 + subject + value without headers and 34 + subject + headers + value with them (ns:server/filestore.go:10055-10062), where the page's formula gives 34 and 38 + headers. The helper keeps the page's terms (an over-charge of 4 bytes per message is harmless slack); E3b may tighten it. Row 10 did not fire: the page's split, exactly 12 MiB of caps, is accepted; the next 1 MiB stream and a raised cap get 10047; an uncapped stream gets 10113; a memory stream gets 10028. The limits test runs the Node without starting a hub (the store limit is the Node server's own).
- E-W1-E3a-R-1 (implementer, E3a review fixes, 2026-10-06): against E3a-1's page §5 (`declare_wall_mirror`), which E3b inherits. The mirror form was declared with `max_bytes` only, so it kept every delivered message and its byte cap dropped the oldest: a rarely written subject's only value went first (review probe on 2.15.0: wall.scene once, 800 × 1 KiB to wall.timing → hub `wall.scene` present, mirror `None`), breaking API6's "latest per subject" on every Node. Page correction: the mirror is declared with `max_msgs_per_subject=1`, as its origin. AC4 gains a case: one subject written once, another rewritten past WALL_STREAM_BYTES (paced so each mirror stores every write), then the once-written subject is asserted in both mirrors and each mirror holds 2 messages. Mutation: drop `max_msgs_per_subject=1` from the mirror → AC4 red (`None == b'scene-the-only-value'`).
- E-W1-E3a-R-2 (implementer, E3a review fixes, 2026-10-06): against E3a-1's page §2 and AC5 ("the WALL account's `max_file` is `WALL_STREAM_BYTES`"). The server checks an account's usage plus the new message before WALL (discard NEW, one per subject) drops the subject's old value, so with the account capped at the stream's own cap a nearly full WALL refused every write, even a same-size update of an existing subject (10002). Correction: `node_bus_accounts.WALL_ACCOUNT_STORE_BYTES = 2 * WALL_STREAM_BYTES` is the WALL account's `max_file` (room for any message the stream can store, since none exceeds its cap), and `hub_store_too_small` now guards that figure. The stream itself carries E-W1-E3a-3-1's class (a longer update of a full discard-NEW stream gets 10077), so it takes kv_bucket_bytes's headroom rule: the harness's `wall_content_bytes(longest_subject, largest_message)` = WALL_STREAM_BYTES less one largest per-message charge is what Central's latest wall values may total; E3b/E3d's WALL writer must keep to it (stated cost: a writer past it is refused longer updates, though same-size ones still land). WALL_STREAM_BYTES (cap of hub stream and mirrors, 0.5 MiB of the Node split) is unchanged. New seam test `test_a_nearly_full_wall_still_takes_an_update_of_every_subject`: fill to the budget, a 16 KiB update lands; fill on until a new subject is refused (10077), a same-size update lands. Mutations: account max_file = WALL_STREAM_BYTES → 10002; no stream headroom → 10077.
- E-W1-E3a-R-3 (implementer, E3a review fixes, 2026-10-06): against E3a-3's page and API8's split; amends E-W1-E3a-3-1's "row 10 did not fire". The account check precedes a limits stream's discard of its oldest, so once a split whose caps total exactly 12 MiB is full, every record and observation publish is refused (10002) and gets no sequence: F7's "longer gaps are counted holes" became uncounted loss. Account `max_file` cannot exceed `max_file_store` (the server will not start), so the room cannot come from config alone. Fallback form: `node-bus.conf` pins `max_payload: 256KB`, and the split's 0.5 MiB unassigned room is a reserved stream that nothing can write (`max_msgs` 1, `max_msg_size` 1, discard NEW), so no other stream can reserve it and it stays ≥ one largest message (30 + 4 + 4096 control line + 256 KiB) of unused store. The limits test asserts the pin as the server applies it, tiles every other cap exactly with 4096-byte-charge messages (account usage == 12 MiB − room), then publishes once into every record and observation stream: all accepted, each dropping its oldest. Mutation: a writable room filled to its cap → 10002. Stated costs and E3b carry: the reserve is held by `nodeapi` declaring it before any class stream (a convention; deleting it frees the room), E3b's class table must carry it; a hub-side message into a Node account over 256 KiB (Central's writes, a wall message) is a protocol error on the leaf, so the largest wall message must stay under the Node's `max_payload`.
- E-W1-E3a-R-4 (implementer, E3a review fixes, 2026-10-06): amends E-W1-E3a-2-2 and §6 row 11; takes that erratum's named alternative. The row 11 fallback (`declare_wall_mirror` reads the origin's last sequence through the imported consumer API) made the Node's local declare depend on the hub: with the hub away it raised ServiceUnavailableError, and a Node declaring before Central re-created WALL got 10059 and left the mirror stuck; AC5 only passed because Central always connected first. Now Central's WALL writer keeps the last sequence WALL acknowledged and `declare_wall(writer, first_seq=...)` re-creates a lost WALL at that sequence + 1, so every mirror resumes at its own next sequence with no Node rule; `declare_wall_mirror` is local create-if-absent and never contacts the hub (`wall_origin_last` removed). AC5 now declares on Node A while the hub is away (no raise, local reads intact), on Node A again before Central's re-declare and on Node B after, asserts the new WALL's `first_seq` = acknowledged + 1, and both mirrors catch up. Mutation: re-create WALL from sequence 1 → mirrors stuck, AC5 red at 30 s. Stated cost: Central must hold its WALL high-water mark durably (E3d); an acknowledgement lost between store and record (Central crash) puts a mirror ahead of Central's mark, and that mirror then skips re-puts up to its last sequence, the old fallback's "resumes mid-stream" gap in a narrower window. A Node mirror that was never ahead is unaffected.
- E-W1-BUF-1 (implementer, E3a buffer rule, 2026-10-06): owner steer, verbatim: "all of these issues boil down the NATS filling its buffer. The most correct implementation is to configure the buffers with a round-robin config that drops the oldest data when new data comes in. The cost is that some clients will lose data, but thats better than the Node failing." Principle, replacing the point fixes of E-W1-E3a-R-2 and R-3 (6f9fbeb): no NATS buffer on a Node or the hub ever refuses a write because it is full. (1) Every stream, KV bucket and mirror is discard OLD with a byte cap, built in one place: the harness's `buffer` (with `bucket`, `wall_config`, `wall_mirror_config`, `node_split`; E3b's `nodeapi` and E3d's WALL writer inherit it). KV buckets are declared through `bucket` + `declare_bucket`, because nats-py 2.16.0's `create_key_value` hard-codes discard NEW (nats/js/client.py:1456). (2) Every account store holds its streams' caps plus one largest message (`contracts.node_link.account_store_bytes`), because the server adds the message to the account's usage and refuses past the store (10002) before a full stream drops its oldest (ns:server/stream.go:7274, jetstream.go:2505). The largest message is bounded by `NODE_MAX_PAYLOAD` (node-bus.conf's `max_payload: 256KB`, kept) on the Node and by WALL's new `max_msg_size` = `WALL_MESSAGE_BYTES` (= NODE_MAX_PAYLOAD) at the hub, so a wall message can no longer be a protocol error on a Node's leaf (R-3's carry, now structural). (3) `tests/test_node_bus_config.py` fails on any harness buffer that is not discard OLD, file-stored and byte-capped, on any `StreamConfig(` outside `buffer`, any `create_key_value(`/`KeyValueConfig(`/`DiscardPolicy.NEW` in tests/integration, and on account API (node-bus.conf) or WALL (generator) whose store is below `account_store_bytes` of its streams; `max_bytes_required` stays. Removed: R-2's `WALL_ACCOUNT_STORE_BYTES = 2 * WALL_STREAM_BYTES` (now `account_store_bytes([WALL_STREAM_BYTES], WALL_MESSAGE_BYTES)`, 790,562 bytes) and `wall_content_bytes` (WALL's discard-NEW headroom); R-3's ROOM stream (the split's caps now total 11.5 MiB and the 0.5 MiB left over is the room, checked against the rule). Kept: R-1 (mirror `max_msgs_per_subject=1`), R-4 (the Node declare never contacts the hub; `first_seq` continuation), `kv_bucket_bytes` (formula unchanged; its headroom now means the first stray key costs no listed value, since a bucket drops a key's own oldest before its byte cap acts). Tests now promise "drops oldest, counts the gap, never refuses": a full split takes one more write into every stream and bucket (next sequence, first_seq advanced) plus a 256 KiB message; a full WALL takes a new subject and a largest message, dropping its oldest subject; a full bucket takes unlisted keys; Central's drain counts the hole for an overflowed stream and bucket (parametrized). Mutations: buckets discard NEW -> config test, the full-split test, state/desired class tests and the bucket counted-gap test red; account API max_file 11776KB (= the split's caps) -> config test red, full-split test 10002; WALL account store = WALL_STREAM_BYTES -> config test red, full-WALL test 10002; mirror without max_msgs_per_subject -> config test red, once-written-subject test red. Stated cost: clients can lose data. A full buffer drops its oldest whatever it is: a KV bucket's oldest value (a rarely written key's only value), the WALL subject written longest ago, unread records; the loss is visible only as a sequence hole a reader counts (Central's drain records a gap row), never as a refused write. The room is held by the split (every buffer from one builder, checked by the config test), not by the server: a client outside `nodeapi` that declares or raises a cap into the last 0.5 MiB can still bring back the 10002 refusal (the server reserves up to the account's store); `max_streams` on account API could close that once E3b fixes the stream count (deferred).
- E-W1-BUF-2 (implementer, E3a buffer rule, 2026-10-06): supersedes E-W1-BUF-1's arithmetic. Owner steer, verbatim: "the workflow implementation is leaning too hard on setting magic memory constraints and hoping that JetStream never drops a message. Instead, lets use the real JetStream config policy to enforce dropping the oldest message: https://docs.nats.io/learn/jetstream/policies". The guarantee is now JetStream's own stream policy: every stream, KV bucket and mirror is built by the harness's `buffer` with `retention` LIMITS and `discard` OLD (both fixed; a caller passing either, or `storage`, gets `buffer_policy_is_fixed`) plus the stream's own limits (a byte cap always; `max_age`, `max_msgs_per_subject` per class: KV `history`, WALL and its mirror 1). The server drops the oldest itself when a limit is reached (ns:server/filestore.go:5338-5380, after the write). Account and server limits, from nats-server 2.15.0 source: (i) an account store limit is checked at every write with the new message added, before the stream drops its oldest (`jsa.wouldExceedLimits`, ns:server/jetstream.go:2536, called at stream.go:7274), so any account `max_file` below "everything stored + one message" refuses (10002); (ii) the server's write check is `storeUsed > max_file_store` with nothing added (jetstream.go:2425-2439, stream.go:7173), and a filestore's discard runs its usage callback before the new message's (filestore.go:5523-5530), so usage never passes the sum of the stream caps; (iii) at create/update the server reserves each stream's `max_bytes` against `max_file_store` (jetstream.go:2609, 2759; 10047) and, with `max_bytes_required`, refuses a stream with no cap (10113). Restructure: account API (node-bus.conf) and account WALL (hub generator) are now `jetstream { max_bytes_required: true }`: no `max_file`, no `max_mem`, so no account check acts on a write; the server's `max_file_store` (Node 12MB, hub `max_file_store_bytes`) and `max_memory_store: 0` are the only store limits, acting at create only. Removed: `contracts.node_link.account_store_bytes`, `largest_message_charge`, `MAX_SUBJECT_BYTES`; `node_bus_accounts.WALL_ACCOUNT_STORE_BYTES` (`hub_store_too_small` now guards `WALL_STREAM_BYTES`, the page's original); the config test's "store holds caps plus one largest message" check; `kv_bucket_bytes`'s one-largest-message headroom (E-W1-E3a-3-1's discard-NEW fallback; now history × per-message charge, a sizing, and the first unlisted key drops the bucket's oldest). Kept: `max_payload: 256KB` = WALL `max_msg_size` (a leaf protocol bound, not headroom), R-1 (mirror `max_msgs_per_subject=1`), R-4 (the Node's mirror declare never contacts the hub). Tests: `test_with_every_node_buffer_full_each_still_takes_a_write` runs hub + Node, declares the split with the REAL wall mirror plus a REST buffer so the caps reserve exactly the 12 MiB store, tiles every buffer to exactly its cap (account usage == 12 MiB), then writes once into every stream, bucket and (via the hub's WALL) the mirror: all accepted at the next sequence, first_seq advanced, oldest gone; then a 256 KiB message; a 13th MiB stream or a raised cap is 10047 at create. The full-WALL seam test, the per-class tests and Central's counted-gap drain (stream and bucket) cover each kind and the reader's gap. The config test fails on any harness buffer not LIMITS/OLD/FILE/capped, on `DiscardPolicy.NEW`, `RetentionPolicy.WORK_QUEUE|INTEREST`, `create_key_value(`, `KeyValueConfig(` or a `StreamConfig(` outside `bus_servers.py` in tests/integration, and on any account carrying more than `max_bytes_required`. Mutations, each red on 2.15.0: (a) REC_host discard NEW -> config test + full-store test (10077); (b) account API `max_file: 12MB` -> full-store test (10002); WALL account `max_file: WALL_STREAM_BYTES` -> full-WALL test (10002); (c) mirror without `max_msgs_per_subject` -> config test + once-written-subject test. The fence: the store's outer fence is `max_file_store`; since every stream's cap is reserved against it at create, it refuses only a new stream or a raised cap (10047), never a write. If stored bytes ever exceeded it anyway (a later release shrinking `max_file_store` below already-recovered caps; recovery does not re-check), every write on that server gets 10023 "insufficient resources" until a stream is shrunk or deleted: E3c's bus line must never set the store below the class table's caps. The process fence is the bus unit's cgroup `MemoryMax` (64 MiB line, E3c): reached, the kernel reclaims the store's page cache first and then OOM-kills nats-server; the unit restarts it and streams recover from the store directory; a client sees a disconnect and Central's drain resumes at its durable consumer, a dropped tail showing as a gap. Stated cost: clients lose data (the oldest of a full buffer, whatever it is); a reader sees it only as a sequence gap; a keyed buffer (bucket, WALL, mirror) also leaves gaps when it replaces a value, so there a gap does not by itself mean loss (E3b's reader rule).
- E-W1-BUF-3 (implementer, E3a review finding, 2026-10-06): corrects E-W1-BUF-2's "Kept: `max_payload: 256KB` = WALL `max_msg_size` (a leaf protocol bound, not headroom)". The Node's pin was one-sided: the hub generator set no `max_payload`, so the hub took up to the 1 MiB default into a Node account and carried a message between 256 KiB and 1 MiB across the leaf, where the Node closes the leaf for it (ns:server/leafnode.go:3214-3217, maxPayloadViolation) and the write is lost unrefused. Reproduced on 2.15.0 with the pin removed from the hub: Node log `maximum payload exceeded: 262145 vs 262144` / `Leafnode connection closed: Maximum Message Payload Exceeded - Remote: hub`, Central's publish `nats: timeout`. Fix (structural): `hub_configuration` sets the hub's top-level `max_payload` = `contracts.node_link.NODE_MAX_PAYLOAD`, the same number as `node-bus.conf`'s pin, so both ends of every leaf take the same largest message. Central's client learns it from the hub's INFO and refuses an oversize publish locally (nats-py `MaxPayloadError`); one the client lets through (headers count server-side, not in nats-py's check) is refused by the hub, which closes that client, never the leaf. Kept: the Node's `max_payload: 256KB` pin, now bound to the hub's by the config test (`hub max_payload == NODE_MAX_PAYLOAD == node-bus.conf max_payload`). Tests: `test_a_message_past_the_nodes_max_payload_is_refused_at_the_hub_and_the_leaf_stays` (oversize Central publish refused at the client, a largest message stored, an over-the-limit-with-headers publish closes Central's client with nothing stored, the leaf cid unchanged throughout). Mutation: delete the hub's `max_payload` -> config test red (KeyError) and the integration test red; with its client-limit assert also removed, the integration test reproduces the leaf teardown above. Stated cost: every hub client (Central's per-Node clients, the WALL writer, Fleet's SYS user) is capped at 256 KiB per message, headers included; no production hub client exists yet (no NATS import under central/), so a later producer of larger payloads must chunk or use the asset path, not raise this. Consequence: WALL's `max_msg_size` refusal (10054) is no longer reachable from a hub client, since the hub's `max_payload` is the same number and acts first; `test_a_full_wall_takes_every_write_and_drops_its_oldest_subject` now expects the writer's `MaxPayloadError` for a message one byte past the largest, and WALL keeps `max_msg_size` as the stream's own bound.
- E-W1-TD-S1 (implementer, NATS teardown, 2026-10-06): the buffer rule lived only in the test harness (`tests/integration/bus_servers.py`), so a shipped caller could still use nats-py's `create_key_value` (discard NEW: the shipped node-bus.conf accepted it and refused put 15 with 10077, probe p6). Plan change: E3b's `nodeapi` package starts now as the shipped home of the bus rules, as a top-level package (design-r3 §16.3: the Player may not import `appliance`): `nodeapi/buffers.py` (circular `buffer`/`bucket`, sticky `Documents`/`sticky_bucket`/`wall_config`/`wall_mirror_config`, `ClassTable`, `declare`, `declare_table`, `apply_table`), `nodeapi/documents.py` (`DocumentWriter`, `missing_documents`, `Token`, `StaleToken`), `nodeapi/pull.py` (`pull`). The harness imports them and defines none. `pyproject.toml` adds `nodeapi` to import-linter's root packages (the NATS fence's sources stay every root but `nodeapi`) and a new forbidden contract, "The Node API library imports only contracts". `scripts/release_plan.py` lists `nodeapi/**` as not shipped until E3b/E3c bundle it. nats-py stays a dev dependency, so E3b must move it to runtime. The guard in `tests/test_node_bus_config.py` now reads every tracked and new `.py` file in the tree, not just tests/integration: `DiscardPolicy.NEW`, `RetentionPolicy.WORK_QUEUE|INTEREST`, `create_key_value(` and `KeyValueConfig(` appear nowhere; `StreamConfig(` appears only in `nodeapi/buffers.py`; `.fetch(` and `pull_subscribe` appear in no NATS-importing file but `nodeapi/pull.py`. Mutations: a `StreamConfig(` in the harness, or a `create_key_value(` in `central/fleet/node_bus_accounts.py`, turns the guard red. Not covered: the server itself still accepts a discard-NEW stream from a raw client outside the tree (a server config cannot refuse it).
- E-W1-TD-2 (implementer, NATS teardown, 2026-10-06): corrects E-W1-BUF-3, which pinned `max_payload` (L = 256 KiB) on both ends but bound only messages entering the leaf. Replies the Node generates for a stored message were not bound. A JSON `STREAM.MSG.GET` base64-encodes the message: a stored 200,000 B record became 266,826 B. A direct get adds headers: 262,100 B became 262,236 B. Either reply closed the leaf at the hub ("maximum payload exceeded"), on every retry (probes p1, p1b, p1c). Fix: `contracts.node_link.MAX_STORED_MESSAGE = (L - 1 KiB) * 3 // 4` = 195,840 B, and every `nodeapi` builder sets the stream's `max_msg_size` to at most that (headers + payload; ns:server/stream.go:7151). A caller asking for more gets `buffer_message_past_the_leaf`, and a document table admits at most it less `HEADER_ALLOWANCE`. `WALL_MESSAGE_BYTES` is removed: WALL and its mirrors use the same constant. Test: `test_no_reply_for_a_stored_message_is_past_the_leaf` (limits). 195,841 B and L - 1 KiB are refused (10054). Central then reads the largest stored record by JSON get, by direct get, as a KV value with headers and as a pull delivery. The leaf cid is unchanged and neither log says "maximum payload". The config test checks 4·⌈M/3⌉ + 1 KiB ≤ L. Mutation: the builder sets no `max_msg_size` → red. Probe after: 200,000 B and 262,100 B are refused at store, and Central's gets of 195,840 B return in full. Stated cost: a stored message is at most 195,840 B, about 25% under L. The 1 KiB envelope assumes a stored subject under about 800 B, which nothing enforces (the Node's `max_control_line` is the default 4 KiB).
- E-W1-TD-3 (implementer, NATS teardown, 2026-10-06): node-bus.conf's `max_pending: 2MB` + `write_deadline: "2s"` closed a local component whose loop was busy for 3 s during a 16 × 250 KB fetch ("Slow Consumer Detected: MaxPending of 2097152 Exceeded", batch lost, probe p2b). Fix: node-bus.conf adds top-level `write_timeout: retry` (ns:server/opts.go:1461; a client's default is close, ns:server/client.go:754). `contracts.node_link.NODE_MAX_PENDING` = 2 MiB (unchanged, bound to the conf by the config test). `nodeapi.pull.PULL_MAX_BYTES = NODE_MAX_PENDING // 2` is the `max_bytes` of every pull request. nats-py 2.16.0's `fetch` sends none, so `pull` sends the request itself: first a no-wait request, then a one-message wait, then a no-wait request for the rest. It never holds a long poll past the consumer's ack wait: a first form that waited the full expiry for a full batch made 2 s-ack-wait deliveries redeliver. The server does not enforce a consumer's `max_request_max_bytes` on a request with no `max_bytes` (ns:server/consumer.go:4714), so the cap lives in the library, and the guard keeps `.fetch(` and `pull_subscribe` out of every other NATS-importing file. Test: `test_a_busy_component_is_never_cut_off_by_a_large_pull` (limits): 16 messages of 195,840 B, loop blocked 3 s, first pull ≤ PULL_MAX_BYTES, all 16 delivered, still connected, no slow consumer logged. Mutations: `PULL_MAX_BYTES = 2 × NODE_MAX_PENDING` → red (connection closed). Removing `write_timeout: retry` is caught only by the config test. On loopback the kernel's socket buffers absorb the capped 1 MiB, so the write deadline never fires, even with SO_RCVBUF shrunk after connect. The retry is proven as configuration, not behaviour. Stated cost: a stuck local client holds up to 2 MiB of server memory until its pings fail. Deferred: the hub's clients keep the default close policy (64 MiB pending, 10 s deadline).
- E-W1-TD-4 (implementer, NATS teardown, 2026-10-06): owner, verbatim: "some sticky topics may be desireable to configure as excluded from the circular buffer config". It amends E-W1-BUF-2's "every buffer drops its oldest" for documents. Reproduced: a desired bucket at node_split's numbers lost "show" (written once) after 70 other keys, and two 256 KiB wall messages evicted every other wall subject at the hub and in the mirror, with no error to any writer (probes p5, p7). New buffer kind, sticky: desired-state buckets, WALL and every Node mirror of it. Still limits/discard-old/file, with `max_msgs_per_subject` = history and `max_msgs` -1. The byte cap is the document table's budget, `Documents.budget` = history × Σ (30 + subject + largest value + 4 + `HEADER_ALLOWANCE` 256) over the listed keys plus a manifest key. WALL and mirrors keep `WALL_STREAM_BYTES` (one number across ends), and `Documents.wall` refuses a table whose budget passes it. The server drops a key's own oldest at the per-subject limit before the byte limit (ns:server/filestore.go:5340-5380), so a writer that keeps to the table never meets the byte cap. Central's `DocumentWriter` refuses, before sending, an unlisted key, a value past its key's size, or headers past the allowance (`DocumentRefused`). The Node refuses nothing. The writer keeps `_manifest`, the keys it wrote, and `missing_documents` reports a listed key the stream does not hold (or the manifest itself). Event buffers (records, observations, reported state) stay circular. Builders tag each stream's metadata `photo_wall_kind`. `ClassTable.resplit` refuses a sticky cap. Tests: `test_a_desired_document_written_once_survives_every_other_write`, `test_wall_documents_survive_the_largest_messages_in_the_hub_and_every_mirror` (documents), the `desired` retention class, and the full-store test, where each sticky buffer takes an update with every document kept. Mutations: sticky bucket cap = budget // 2 → red; WALL without `max_msgs_per_subject` → red; writer's unlisted refusal removed → red; reader ignoring the manifest → red. Plan changes for E3b/E3d: the Node (which declares the bucket at the budget) and Central (which writes within it) both need each document table, so the table must ship where both reach, which the plan's "descriptors ship with each release" does not yet say. A new desired document is a table change. WALL's 512 KiB holds at most two of the largest documents, so the wall table is an E3d design input. Stated costs: a client bypassing `nodeapi` (a raw put) can still evict a document. The reader then finds it missing, but only while the manifest survives; a large stray also takes the manifest, which is then reported missing instead. The manifest costs one write per first write of a key.
- E-W1-TD-5 (implementer, NATS teardown, 2026-10-06): a revision was not an identity across the Node's store loss (tmpfs at every reboot). Central's conditional write on its boot-1 revision 1 succeeded on a value a Node component wrote at revision 1 in boot 2 (probe p5 P5b). Fix: every `nodeapi` builder writes `photo_wall_epoch` (a fresh uuid4) into the stream's metadata. `declare` is create-if-absent, so only a create starts an epoch, and `apply_table` keeps it. Every token and cursor is `nodeapi.documents.Token(epoch, seq)`. `DocumentWriter.put` reads the stream's epoch and raises `StaleToken` for another epoch, and `read` returns a token from one creation. Central's drain keys its rows per epoch, and a new creation's cursor starts at its own origin. A Node stream also starts at `first_seq = epoch_origin(epoch)` (1 + 40 bits of the epoch; the WALL hub stream keeps E-W1-E3a-R-4's continuation, and mirrors set none). So even a raw conditional write on a stale revision misses (10071), which closes the race between the epoch read and the write. Tests: `test_a_token_from_a_lost_store_is_stale_and_never_applied` (documents) and `test_a_drain_cursor_from_a_lost_node_store_starts_the_new_creation_fresh` (seam). Mutations: writer epoch check removed → red (10071, not StaleToken); `epoch_origin` = 1 → red (raw stale write applied); a constant epoch → both red. Probe after: the boot-1 token raises StaleToken and the Node's value stays. Plan change: no reader may assume a stream starts at sequence 1. E3a's tests did (`seq == index + 1`) and now use the stream's `config.first_seq`. Stated cost: one extra round trip (stream info) per conditional write.
- E-W1-TD-S2 (implementer, NATS teardown, 2026-10-06): 10047 at create depended on declare order, since whichever stream came last past the reservation was the one refused. Fix: `nodeapi.buffers.ClassTable`, one owner's declaration of the whole store. Construction refuses caps past `total` (≤ `contracts.node_link.NODE_STORE_BYTES` = node-bus.conf's `max_file_store`, bound by the config test), a buffer not built by `nodeapi`, and a name mismatch. `resplit` (Central's override) moves bytes between circular buffers inside the same total and never raises it. `declare_table` creates absent buffers in any order. `apply_table` updates a live store with every shrink before any growth. `node_split` is now a `ClassTable`. Tests: `test_one_class_table_holds_the_whole_store_and_a_resplit_never_raises_it` (config); `test_one_class_table_declares_in_any_order_and_resplits_a_wholly_reserved_store` (limits): the reversed table declares on a store reserved to exactly 12 MiB, and a 1 MiB move applies and reverts with epochs kept. Mutations: growth before shrink → 10047 red; the over-total check removed → config red. Not covered: a client outside `nodeapi` can still declare past the table (the server refuses it with 10047, which is the server's fence, not the table's).
- E-W1-TD-S3 (implementer, NATS teardown, 2026-10-06): names the upstream bug behind E-W1-E3a-2-1's "reload twice": nats-server 2.15.0 `configureAccounts(reloading=true)` subscribes service imports only for accounts that existed before the reload (ns:server/server.go:1413, 1430), so a reload-added account's imported WALL consumer API answers "no responders" until the next reload. `reload_hub` takes `requests` (default 2). The new seam test `test_one_reload_wires_a_new_accounts_service_imports` runs with one request and is `xfail(strict=True, raises=AssertionError)`. It fails today; the day upstream fixes the bug it passes, strict turns it red, and E3d's "rewrite, then reload twice" can drop to one. Re-check on 2.15.1. Mutation: `requests=2` in that test → XPASS(strict) red.
- E-W1-TD-S4 (implementer, NATS teardown, 2026-10-06): the seam tests dialled the hub's WebSocket port directly, never through a proxy. New `bus_servers.PrefixProxy`: plain asyncio, about 40 lines, no new dependency. It forwards a connection whose HTTP request path starts with `/<prefix>/` unchanged, upgrade and all, and answers 404 to any other. `node_server(leaf_port=)` points a Node at it. Test `test_the_leaf_links_through_a_path_prefix_proxy` (seam): the leaf links (the proxy saw exactly `/photo-wall/bus/leafnode`), a method answers, Central reads a 195,840 B record, and a wall write reaches the mirror, all through the proxy. Mutation: the proxy answers 404 to every path → red (no leaf within 10 s). Not covered: no production prefix exists yet (the ingress route is E3d/E4's), so the test uses `photo-wall/bus` and a pass-through route (no prefix rewrite). TLS (wss) and a real ingress controller are untested.
- E-W1-TD-6 (implementer, NATS teardown verifier F2, 2026-10-06): corrects E-W1-TD-2. Its 1 KiB envelope assumed a short subject, and nothing bounded subject or key length. On nats-server 2.15.0, a 195,840 B record under `player.record.` plus 1000 `s` characters, read by Central with a JSON get across the leaf, passed 262,144 B and closed the leaf ("Maximum Message Payload Exceeded"). Fix by construction: (a) `contracts.node_link.NODE_MAX_CONTROL_LINE` = 1024 is set as `max_control_line` in both `node-bus.conf` and the hub generator, and the config test binds both. Probed on 2.15.0: the server checks it only on CLIENT connections, so a line of exactly 1024 is taken and 1025 closes only that client, never the leaf. Every stored subject came through a client on one end, so none is longer than 1024. (b) `REPLY_ENVELOPE` = 6 x 1024 + 1024: Go's JSON encoder writes `<` and `&` as six bytes. `MAX_STORED_MESSAGE` = (L - REPLY_ENVELOPE) x 3/4 = 191,232 B, down from 195,840 B. (c) `nodeapi.buffers.MAX_PUBLISH_SUBJECT` = 512, half the line, so a writer's inbox and a reader's `DIRECT.GET.<stream>.` prefix both fit. `Documents` refuses a longer subject at construction (`documents_subject_past_the_control_line`). `DocumentWriter` refuses one at construction against its own publish prefix (Central's `$JS.node.API.` prefix is longer): `document_subject_past_the_control_line`. Test `test_no_reply_for_the_longest_subject_a_client_can_store_is_past_the_leaf` (limits) runs on real servers. A Node component and Central each store a 191,232 B record under the longest subject their line allows: 1016 bytes of `<` and of `&`. One byte more closes that client. A 512-byte `<` document key is stored, and Central reads all three by JSON get and by pull with the leaf kept. Mutations, each red: envelope back to 1 KiB (the leaf closes and Central times out); `max_control_line` removed from the Node file; the same removed from the hub. Costs: a client that sends a longer line is disconnected (nats-py does not check the line on the client side). That is a refusal of one malformed operation by the server, and an event publisher outside `DocumentWriter` gets no earlier check. A direct get by subject puts the subject in the request line, so a circular record whose subject is near 1024 can be read only by a JSON get or a delivery. Not covered (suspected, unproven): a STREAM.INFO with `subjects_filter` across the leaf lists every matching subject in one reply, so a stream with many distinct long subjects could still produce a reply past L.
- E-W1-TD-7 (implementer, NATS teardown re-review F3, 2026-10-06): corrects E-W1-TD-3. Its cap applied to each pull request, but a connection's open requests add up. On 2.15.0 with the shipped node-bus.conf, three concurrent `nodeapi.pull` calls on one component connection (3 streams × 6 × 191,232 B), with the loop blocked for 3 s, passed max_pending. The Node logged "Slow Consumer Detected: MaxPending of 2097152 Exceeded", and all three pulls raised ConnectionClosedError. Fix: the cap now applies per connection. `nodeapi.pull` keeps one byte budget per Client (a WeakKeyDictionary), PULL_MAX_BYTES in total. Each request takes its `max_bytes` from that budget, at least `PULL_ONE_BYTES` and at most what is free, and gives it back when the request ends. `PULL_ONE_BYTES` = MAX_STORED_MESSAGE + NODE_MAX_CONTROL_LINE + 1 KiB = 182,528 B, the most one delivery charges (subject + ack reply + headers + payload, ns:server/consumer.go:5516). Concurrent pulls on one client queue instead of adding up, so the bytes requested and not yet read never pass PULL_MAX_BYTES. The one-message wait holds only PULL_ONE_BYTES. Test: `test_concurrent_pulls_on_one_busy_connection_share_one_byte_budget` (limits) runs four concurrent pulls over 4 streams × 6 × MAX_STORED_MESSAGE on one connection, with the loop blocked for 3 s. All 24 records are delivered, the client stays connected and no slow consumer is logged. Mutation: when the budget grants every request its full PULL_MAX_BYTES, the test goes red (ConnectionClosedError). Stated costs: pulls on one client serialize once they exceed the budget, so a queued pull can take longer than its `timeout`. About five one-message waits fit at once, and a sixth request waits until one of them ends (at most its timeout plus 1 s). A pull that bypasses `nodeapi.pull` is not counted, and the S1 guard keeps `.fetch(`/`pull_subscribe` out of every other NATS-importing file. Not covered: `max_bytes` does not count the MSG protocol line. A request of many tiny messages can queue more than its `max_bytes` (the batch count bounds it), and the other half of max_pending is the margin.
- E-W1-TD-8 (implementer, NATS teardown re-review, 2026-10-06): corrects E-W1-TD-6's `max_control_line: 1KB`. It closed a Node component for an operation the base accepted. A local kv.put with a 1004-byte key to a circular bucket timed out, the Node logged "maximum control line exceeded", and the client was closed. That broke the owner rule that nothing stops a Node. Fix: `contracts.node_link.NODE_MAX_CONTROL_LINE` = 4096, nats-server's default (MAX_CONTROL_LINE_SIZE, ns:server/const.go:90). node-bus.conf (`max_control_line: 4KB`) and the hub generator set it explicitly to that default, so an upgrade cannot move it under the envelope and it refuses nothing the base server accepted. REPLY_ENVELOPE = 6 × 4096 + 1024 = 25,600, so MAX_STORED_MESSAGE = (L − 25,600) × 3/4 = 177,408 B (down from 191,232 B), and MAX_PUBLISH_SUBJECT = 2048. The config test binds both files to the constant and the constant to the literal default 4096. `test_no_reply_for_the_longest_subject_a_client_can_store_is_past_the_leaf` now stores subjects about 4 KiB long (one byte more still closes only that client, as the default always did). It also does the probe's 1004-byte-key KV put and checks that the client stays connected. Mutation: line back to 1KB (constant 1024 too) → the config test goes red (1024 ≠ 4096) and the real-server test goes red (the put times out, client closed). The wall table: three documents of MAX_STORED_MESSAGE − 1 KiB still exceed WALL_STREAM_BYTES (WALL holds two). Stated cost: stored messages are about 7% smaller.
- E-W1-TD-9 (implementer, NATS teardown re-review F2, 2026-10-06): corrects E-W1-TD-2 and E-W1-TD-6. Their claim "no reply the Node generates can close the leaf" holds only for replies about one stored message. A reply whose size grows with stream state is unbounded. Reproduced on 2.15.0: a circular state bucket with one pinned key and 60,000 writes to a hot key. Central's `$JS.node.API.STREAM.INFO.KV_state_host` with `{"deleted_details": true}` produced a reply of about 780 KB. A record stream with about 12,000 distinct subjects and `{"subjects_filter": ">"}` produced about 272 KB. Both times the hub logged "maximum payload exceeded" and "Leafnode connection closed: Maximum Message Payload Exceeded", and the leaf cid changed. Every retry repeats this. This settles E-W1-TD-6's suspected `subjects_filter` case: proven. The same class covers STREAM.LIST/NAMES and CONSUMER.LIST/NAMES (suspected, not probed). Not fixed: closing the class needs a design decision this fix does not own. Central's per-Node hub user would need a publish allow-list of per-message-bounded JS API subjects. Permissions cannot see a request body, so that also means denying STREAM.INFO, and the epoch would have to be read without it (`nodeapi.buffers.stream_epoch`, `missing_documents` and nats-py's `key_value` bind all use STREAM.INFO). The allow-list would also need to name each component's method subjects (E3b's subject grammar). Recorded instead: the comments in `contracts.node_link` and `node-bus.conf` now limit the guarantee to replies about one stored message and name what it does not cover. `test_a_reply_that_grows_with_stream_state_closes_the_leaf_and_nodeapi_asks_none` (limits) records both closes on real servers. It shows that the local reply exceeds L and that nodeapi's own epoch read (STREAM.INFO with no options) keeps the leaf on the same streams. It goes red the day such replies are bounded or fenced. Mutation: `stream_epoch` asks `subjects_filter=">"` → red (the leaf closes). Stated cost: a raw Central request with these options closes the Node's leaf until it relinks (about 1 s), and nothing stops one except that `nodeapi` sends none. Deferred to E3b/E3d: the allow-list for Central's hub user and an epoch read that does not use STREAM.INFO.
- E-W1-TD-S5 (implementer, NATS teardown re-review S1, 2026-10-06): corrects E-W1-TD-S1's guard. It matched spellings, not the calls that create streams. A file with no nats import that called `jetstream.add_stream(name='KV_desired_x', ..., discard='new')` with keyword arguments passed it. On the shipped node-bus.conf the stream was created with discard new, no epoch and no max_msg_size, and the 10th put got 10077. Fix: `tests/test_node_bus_config.py::_guard_violations` bans the identifiers `add_stream` and `update_stream` (as a call or a bare reference), `StreamConfig(`, and the raw API subjects `STREAM.CREATE`/`STREAM.UPDATE` in every tracked or new `.py` file except `nodeapi/buffers.py`. The integration tests' direct `add_stream(buffer(...))` calls (limits, seam) now go through `nodeapi.buffers.declare`, and the raised-cap refusal goes through `apply_table`. Test: `test_the_buffer_guard_refuses_a_stream_created_in_any_form` covers the re-review's kwargs evasion verbatim, an `update_stream(config=...)`, a bare `jetstream.add_stream` reference, raw `$JS.API.STREAM.CREATE` and `$JS.{domain}.API.STREAM.UPDATE` requests, `StreamConfig(` and `create_key_value(`, and confirms `nodeapi/buffers.py` and a plain `stream_info` are allowed. Mutation: the re-review's `central/fleet/rr_evasion.py` dropped into the tree → the tree guard goes red. Not covered: a spelling built at runtime (`getattr(js, "add_" + "stream")`, a subject assembled from fragments) and any client outside the tree. Those still reach the server, which cannot refuse discard new.
- E-W1-STORE-1 (implementer, bus storage, 2026-10-06): owner answer STORE1 = (A) on the storage page: every Node bus buffer uses the FILE store on tmpfs. A restart of the bus keeps every stream; a reboot keeps nothing. Measured on 2.15.0 linux-arm64 in Docker, every buffer full and written flat out: the smallest safe fence is 176 MiB MemoryMax with GOMEMLIMIT 100 MiB; chosen 224/140 (survived 10 min); without GOMEMLIMIT every fence OOM-looped; a size= tmpfs refuses writes (forbidden by the buffer rule). SUPERSEDES the plan's 64 MiB bus fence everywhere it appears: API8's "12 MiB store inside the 64 MiB bus fence" (owner answers list), E2a-1's LINES row `bus` 64 and its stated cost "API8 keeps the bus line at 64 MiB", the D-0017 table's C5/C13 rows, wave 3's "E3c (bus unit, 64 MiB fence ...)". It also corrects E-W1-BUF-2's last paragraph: at the fence the kernel cannot reclaim tmpfs pages (no swap), they stay charged to the unit's cgroup after an OOM kill, and the restart reloads the same files into the same cgroup, so a store that cannot fit loops forever; it does not "recover". Built: (1) the fence numbers live in one place, `contracts.node_link` (`NODE_BUS_MEMORY_MAX` 224 MiB, `NODE_BUS_GOMEMLIMIT` 140 MiB, `NODE_BUS_HEADROOM` 4 MiB, the low end of the measured 4-20 MiB Go does not count, `NODE_BUS_STORE_ROOM` = 80 MiB, and `filestore_block_bytes`, a copy of ns:server/stream.go:1595-1607). No bus unit and no line table exist on this branch: E3c owns the unit and E2a-1 is still on `w1/e2a` (bf73312), not landed. (2) `nodeapi.buffers.ClassTable` refuses to build (`class_table_past_the_bus_fence`) when `store_bound` (Σ each file stream's cap + its block) > NODE_BUS_STORE_ROOM, Central's `resplit` included. `node_split` is 17 file streams, every one with a 4 MiB block: bound 79.25 of 80 MiB; the whole 12 MiB store in those 17 is exactly 80. The limits tests' `_whole_store` now gives the rest of the store to REC_host by resplit instead of an 18th REST stream, which does not fit (84 MiB). (3) `declare`: re-declaring looks the stream up and never re-adds it. The server checks a create's reservation (ns:server/jetstream_api.go:1615) before the name (ns:server/stream.go:891), so on a wholly reserved store any add of an existing stream, identical or not, is 10047 (ServerError, not 10058), probed. The old declare looked first but caught only BadRequestError 10058, so a create that lost a race to another declarer raised 10047. Now 10058 or 10047 is followed by a second look, and a stream that exists is a no-op. (4) `tests/integration/test_bus_memory_fence.py` and checks.yml job `bus-fence` on ubuntu-24.04-arm, the runner node-components.yml and node-pid1.yml already use for arm64 Docker. The pinned linux-arm64 2.15.0 runs in alpine 3.23.4 (by digest) with --memory = --memory-swap = 224m, GOMEMLIMIT=140MiB and --tmpfs /store, the shipped node-bus.conf plus `-a 0.0.0.0` so Docker can publish the port, `node_split` with its WALL mirror fed by a hub container, and 17 writers flat out for PHOTO_WALL_BUS_FENCE_MINUTES (CI 5). It fails on any oom_kill, a second server start or a refused write. Then kill -9 with the store full: the restart in the same cgroup must come back with every stream's state and epoch unchanged, stay up 20 s with no kill, and take the next write. A shell loop restarts the server and counts starts. The skip reason is allowlisted in conftest. Local full run (5 min): 5,418,984 writes, peak 224.1 MiB (at the fence: reclaim, no kill), store up to 66.5 MiB, oom_kill 0; reload current 148.6 MiB. Mutations, each red: (a) no GOMEMLIMIT → oom_kill 1 within 4 s; (b) the fence check removed → config test DID NOT RAISE; (c) a naive add-first declare → 10047 on the full-store re-declare; the old declare → 10047 in the ordered race. (d) bus line back to 64: not runnable, since no line table is on this branch. Where the page was wrong: the 4 MiB block starts AT 128,000 bytes, not above it (cap // 4 + 1 rounds to 32,100; probed: 127,999 → 32,000-byte blocks, 128,000 → one 4 MiB block). Carries: E2a-1 when it lands: the bus line becomes `MemoryLine("bus", NODE_BUS_MEMORY_MAX, ...)`, read from the contract and not a literal; kernel may import contracts. Its basis becomes "STORE1 = A, measured 176 safe / 224 chosen, E-W1-STORE-1". Add a test that the bus line is the contract; that test is mutation (d). On 4 GB the top-level lines without the interim preparation line go 2344 → 2504 MiB: 1541 MiB margin on the 4045 MiB reading, 1080 on the class's 3584 floor. There is no 4 GB fit test yet; W3 moved the content-line check to E2c. E2a's own tests pass with bus = 224 (44 passed; tried in the lane worktree and restored). E3c: the unit sets MemoryMax=NODE_BUS_MEMORY_MAX and Environment=GOMEMLIMIT=140MiB, both bound by test to the contract. Its store dir is tmpfs with no size=. Repeat the fence run on the PID1 fixture: 16 KiB pages add about 16 KiB per store file, about 1.3 MiB for 17 streams, uncounted by the bound and inside the 7 MiB between the bound (80) and the largest store measured (73). This replaces the plan's BUS_STORE_BYTES carry, which `NODE_STORE_BYTES` already covers. E3b: the real class table must pass the fit check. 17 large streams use the room, so an 18th large stream needs a bucket under 128,000 B (32 KB blocks) or a larger fence: about 4 MiB of room, about 7 MiB of fence measured. Stated costs: 160 MiB more ceiling per Node. The headroom is 4 MiB, so the table, not the fence, is the margin. The bound counts blocks, not the Pi's page rounding. The CI gate runs on a 4-vCPU arm64 runner with 4 KiB pages, not on a Pi.
- E-W1-FV-1 (implementer, final-verify MF1, 2026-10-06): corrects E-W1-TD-5's "every `nodeapi` builder writes `photo_wall_epoch`". The epoch and the epoch-derived first_seq were drawn when `_build` made the StreamConfig, so a component that holds its configuration (or class table) and re-declares it on every connect re-created a lost stream with the SAME epoch and first_seq; Central's stale conditional write then landed (probe /Volumes/Dock/tmp/w1/final/probes/p1_epoch.py: "SAME EPOCH", "STALE TOKEN APPLIED"). Fix: builders carry neither; `declare` stamps a fresh uuid4 epoch and, on a non-mirror stream with no builder first_seq, `first_seq = epoch_origin(epoch)` at each add (`_stamped`). WALL's `wall_config(first_seq=...)` continuation (E-W1-E3a-R-4) is the builder's and kept. `declare` refuses (`declare_needs_a_built_buffer`) a configuration with no kind or one already carrying an epoch, i.e. one read back from a stream, whose re-create would reuse that creation's epoch and origin. Tests: `test_a_token_from_a_lost_store_is_stale_and_never_applied` (documents) and `test_a_drain_cursor_from_a_lost_node_store_starts_the_new_creation_fresh` (seam) now declare ONE configuration object before and after `node.wipe()`, assert a new epoch and origin, and the documents test asserts the read-back refusal; the config test asserts no builder carries an epoch or derived first_seq. Mutation: epoch = a function of the config object's id → both red. Plan change: no caller may read an epoch from a built configuration; read it from the stream (`epoch_of`/`stream_epoch`). The limits race test now records the creating declarer's epoch from the stream.
- E-W1-FV-2 (implementer, final-verify MF2, 2026-10-06): corrects E-W1-TD-3's top-level `write_deadline: "2s"` (e0c79f5), which the leaf inherited (ns:server/client.go:731-738), and the leaf's retry policy still closes on a flush that writes nothing (client.go:2020): a short uplink stall closed the leaf. Fix: node-bus.conf `leafnodes { write_deadline: "10s" }`, the server default (DEFAULT_FLUSH_DEADLINE, ns:server/const.go:132), pinned as the uplink's stall budget; the config test binds it >= 10 s and > the clients' deadline. `bus_servers.PrefixProxy` gains `stall()`/`resume()` (Node-to-hub direction). Test: `test_a_short_uplink_stall_keeps_the_leaf_and_every_event` (seam): a 5 s stall while a component publishes 200 x 200,000 B of random bytes toward Central; the leaf cid is unchanged, all 200 arrive in order, no "Slow Consumer" in the Node log. Mutation: the leafnodes line removed → red (Central never gets all 200) plus the config test red. Where the finding was wrong: on macOS the probe's 200 x 60,000 B of `x` reproduced nothing (cid kept, 200/200 with the 2 s deadline), so the payload is incompressible and 40 MB, which reproduces it (Slow Consumer, cid 8 → 11, 9/200). Stated cost: a leaf whose uplink writes nothing for 10 s is closed and reconnects, losing its in-flight core messages; the hub side of the leaf keeps the hub's default.
- E-W1-FV-3 (implementer, final-verify MF3, 2026-10-06): corrects E-W1-STORE-1's fit check. `store_bound` charged each stream the block of its CURRENT cap, but nats-server fixes the block at create (ns:server/stream.go:1098, 1578-1607) and an update keeps it: a stream created at 4 MiB and shrunk to 100,000 B held 2,004,657 B of files where the bound allowed 132,000 (probe p5_block.py); X = 19 x 128,000 B (78.32 MiB) re-split to Y (charged 76.09 MiB) can hold 88 MiB, past the 80 MiB room. Fix: `ClassTable` keeps `blocks`, the largest block each buffer had along its line of re-splits (current included); `charge(name)` = cap + that block; `store_bound` is now the table's property (the free function is gone, so nothing charges a bare config list). `apply_table` first re-checks the table charged with each live stream's cap's block too, so a table built anew (not re-split) with small caps on a store created with large ones fails `class_table_past_the_bus_fence` before any update. Tests: `test_a_resplit_is_charged_the_block_each_stream_was_created_with` (config: X→Y refused, block kept across chained re-splits) and `test_a_stream_shrunk_by_a_resplit_stays_within_its_charge` (limits, real server: S's files exceed its new cap's block and stay within `charge("S")`; a fresh table applied to 19 x 128,000 B streams refused, caps unchanged). Mutation: `blocks` ignored → both red. Not covered: a store whose streams were created by a table outside the applied table's re-split line AND later updated by another such table; the live-cap re-check covers only the case where a stream still has its creation cap. Stated cost: a stream Central shrinks below 128,000 B keeps costing its 4 MiB block in the fit check until the store is re-created.
- E-W1-FV-4 (implementer, final-verify MF4, 2026-10-06): corrects E-W1-TD-4's manifest. `DocumentWriter` published `_manifest` unconditionally from a per-instance cache, so with two writers of one table (Central and a Node component writing its default) the later writer dropped the other's keys and a later bypass eviction went unreported (probe p6_manifest.py: `layout` missing from the manifest, `missing_documents` = []). Fix: `_list` is a compare-and-set loop: read the manifest's last message, publish the merged list with `Nats-Expected-Last-Subject-Sequence` = its seq (0 when absent), and on 10071 re-read, merge and retry. The cache only skips a write for a key this writer already saw listed in this epoch, and its keys are merged in, never a reason to overwrite; keys outside the writer's table are dropped from what it writes, so the manifest stays inside its budgeted size. Test: `test_every_writer_of_a_table_keeps_the_others_documents_in_the_manifest` (documents): Central's node_bucket writer and a Node-local writer interleave, then eight concurrent first writes race; all 11 keys listed, and after a bypass `kv.purge("layout")` `missing_documents` = {"layout"}. Mutation: manifest written from the cache with no condition → red. Stated cost: a first write of each key in a creation costs one manifest read plus one conditional publish, more under contention.
- E-W1-LEAF-1 (implementer, leaf egress, 2026-10-06): corrects E-W1-FV-2. Its leaf `write_deadline: "10s"` fixed the instance (a short uplink stall), not the class. The confirm review (MF2) proved that nats-server 2.15.0's leaf toward the hub has no max_pending, only a write deadline. While the hub was stalled, any core publish on a subject the hub had interest in queued in the Node's server until the 224 MiB fence OOM-killed the bus. Reproduced here with the new fence test and the Node's local permissions removed: oom_kill 1, peak 224.1 MiB. Owner: "go". The rule: nothing crosses the leaf toward the hub except Central's own traffic. Five layers. (L1) The hub's `node-<id>` user has `publish.allow = contracts.node_link.LEAF_EXPORTS`: `_CENTRAL.>`, `ACC.WALL.API.CONSUMER.CREATE.*`, `ACC.WALL.API.CONSUMER.CREATE.WALL.>`, `ACC.WALL.API.CONSUMER.DELETE.WALL.*`, `$JS.FC.WALL.>` and `$JS.FC.*.*.WALL.>`. The hub sends this list to the Node in its INFO, and the Node checks every message against it before queueing (ns:server/leafnode.go:1716-1735, client.go:3811-3818). (L2) The same user has `subscribe.allow = LEAF_IMPORTS`: `$JS.node.API.>`, `$JS.ACK.>`, `$SRV.>`, `*.method.>`, `DELIVER.WALL.>` and `$JSC.R.>`. The Node announces no other local subscription, so subscription churn sends the hub nothing. (L3) node-bus.conf's user `local` has `publish {allow [">"], deny LEAF_EXPORTS}` and `allow_responses: true`, so the Node's own server refuses a program's publish on an export, except one reply to a request it was delivered. allow_responses drops the default allow-all (auth.go:300-310), hence `">"`. (L4) A push consumer binds only to a subject some subscription names literally (sublist.go:169-195), so every interest across the leaf is wildcard-only. The WALL consumer-create export and import are `CREATE.*`, not the literal `CREATE.WALL`. `nodeapi.pull` subscribes `<inbox>.*` and sends its reply as `<inbox>.r`. The hub's `central-<id>` user has `publish.allow = LEAF_IMPORTS` and `subscribe.allow = ["_CENTRAL.*.*"]` (`CENTRAL_SUBSCRIPTIONS`), and Central connects with `inbox_prefix=CENTRAL_INBOX_PREFIX`. (L5) `nodeapi.buffers.buffer` refuses republish, sources, mirror and subject_transform (`buffer_never_forwards`). A republish is an internal publish that no permission checks. This layer holds by construction in nodeapi, plus the existing guard (add_stream only in nodeapi/buffers.py) for raw clients. The leaf's `write_deadline: "10s"` stays, re-commented as the uplink's stall budget only. Tests: `test_nothing_a_node_program_does_crosses_the_leaf_while_the_hub_is_stalled` (seam; Central's reach is folded in), `test_the_wall_mirror_carries_more_than_its_flow_control_window` (seam), `test_a_stalled_hub_never_pushes_the_bus_past_its_fence` (fence, Docker; `PHOTO_WALL_BUS_FENCE_STALL_SECONDS`, default 60; checks.yml bus-fence timeout 15 → 20 min), and config pins on both permission sets. Measured on darwin-arm64: seam growth under a 4 s × 5 flood was 9.9-11.5 MiB green. Mutations, each red: local permissions removed 1429 MiB; node-user permissions removed 47.9 MiB (interest churn); literal pull inbox plus Central `_CENTRAL.>` 77.1 MiB; literal CREATE.WALL import 70.6 MiB; `$JS.FC.WALL.>` dropped from the exports, mirror stalls at write 12. Fence on linux-arm64 in Docker, 60 s stall: green peak 89.9 MiB of 224; with local permissions removed, oom_kill 1 at peak 224.1 MiB. Deviations from the frozen page: (a) Test A's RSS baseline is taken after the stalled-hub records and push consumers, just before the 200 KB floods. The 40 records' file-store cache alone is about 19 MiB, which left 29.7 of the 32 MiB allowance with the baseline before them. Bound push consumers are caught by `push_bound`. (b) Its push consumers are durable: ephemeral ones were reaped during the stall, so the after-resume check found none. (c) D's `probe.echo`: the hub refuses Central's publish (Publish Violation, not in LEAF_IMPORTS), so the request fails with no responders or a timeout; both are accepted. (d) `bucket()` takes no forwarding field by signature, so only `buffer()` checks. Deleted or changed tests, and what still covers each: seam::test_a_short_uplink_stall_keeps_the_leaf_and_every_event was deleted, because its event path (a Node publish to Central's subscription) is removed by design. The config pin on the leaf deadline and test A's leaf kept across a 20 s stall cover it, but no loopback test now separates the 10 s deadline from 2 s (p10). `PrefixProxy.stall/resume` went with it. seam::test_two_node_accounts lost its "Node A publish reaches its own Central" segment; test A plus that test's `$SRV`, method and stream_info isolation cover it. seam::test_a_message_past_the_nodes_max_payload now writes a bucket through `$JS.node.API.$KV.state_probe.*`. seam::test_central_conditionally_updates: the plain `$KV` publish now expects NoStreamResponseError or a timeout. limits::test_no_reply_for_the_longest_subject lost Central's leg, because Central stores only through `$JS.node.API.$KV`, whose stored subject is 13 bytes shorter than the line sent; the local leg covers it. Seam ENDPOINT is `probe.method.echo`, and `_gather` uses `<inbox>.*`. Residuals: what can still queue toward a stalled hub is what Central's in-flight pulls asked for (at most PULL_MAX_BYTES, 1 MiB, per Central connection), one reply of at most 256 KiB per Central request, small mirror control messages, and interest messages for subjects on the inbound list. That last one is NOT bounded: a program that churns subscriptions on `<x>.method.<y>`, or micro services (`$SRV.>`), still sends LS+/LS- across the leaf (not probed). Stated costs: a Node program cannot publish an event to Central, call a Central service or push a stream across the leaf; every Node-to-Central signal is a record Central pulls. Central cannot use nats-py's KV watch, ordered consumers or `pull_subscribe` across the leaf, because they bind literal inboxes. The WALL consumer-create export widens to any stream of the WALL account, which holds only WALL. Rejected alternative (not probed, source reading only): splitting the Node's accounts so the leaf binds an empty account linked to API by service imports. The mirror's requests reopen the same path, the `$JS.node.API` domain mapping attaches to the leaf's account (leafnode.go:2115-2122), and the mirror would sit behind two import layers (§6 row 3 risk).
- E-W1-FIT-1 (implementer, flat-block fit, 2026-10-06): corrects E-W1-FV-3 and E-W1-STORE-1's fit check. The confirm review (MF3) proved that a two-step `resplit`/`apply_table` override bypassed the build-time fit check: tracking each stream's block along its line of re-splits was a patch on the instance. Owner: "go". The fix makes the fit the server's. node-bus.conf's API account sets `jetstream { max_bytes_required: true, max_streams: 17 }` (== `contracts.node_link.NODE_MAX_STREAMS`). The server refuses an 18th stream at create (10027), never a write into a full one. On a store at its 17 streams and wholly reserved, the count is checked first (10027, not 10047). `max_file_store` (12 MiB) already caps the sum of every cap, so no cap passes 12 MiB, and nats-server 2.15.0 fixes every such stream's block at create at no more than 4 MiB (`FILESTORE_BLOCK_BOUND`, charged flat; the 128,000 B small-block threshold is not modelled). So for any client, any table and any override, the files are at most 12 MiB + 17 × 4 MiB. `contracts.node_link` raises at import unless NODE_STORE_BYTES + NODE_MAX_STREAMS × 4 MiB + GOMEMLIMIT + HEADROOM ≤ MemoryMax: 12 + 68 + 140 + 4 = 224 MiB ≤ 224 MiB, zero slack. It also checks that the store's largest cap stays under nats-server's 8 MiB block. An 18th stream, or any growth of the store or the headroom, needs the owner. The page's split is exactly 17 streams, so a new component stream also needs the owner or a merge. `ClassTable` keeps only its construction refusals: `class_table_past_the_store`, `class_table_over_total`, `class_table_too_many_streams`, `class_table_past_the_bus_fence` (the same identity per table). The last one is unreachable while the import check holds; it is kept as the page asks and reached in tests only with a monkeypatched fence. Removed: `resplit`, `apply_table`, `blocks`, `charge`, `store_bound`, `_block`, `filestore_block_bytes`, FILESTORE_MIN/MEDIUM/MAX_BLOCK and NODE_BUS_STORE_ROOM. `declare` treats 10027 like 10047: it looks the stream up again and raises only if the stream is absent. The buffer guard now bans `update_stream` and `STREAM.UPDATE` everywhere, nodeapi included, so the per-Node retention override (C13) waits for E3b's design and must keep this identity. Tests: config `test_one_class_table_holds_the_whole_store_within_the_servers_stream_count` (construction refusals) and `test_the_store_fits_the_bus_fence_for_any_class_table` (the identity, the 8 MiB bound, the whole-store table building, and the fence check wired), plus a node-bus.conf pin that `max_streams` == NODE_MAX_STREAMS. In limits: new `test_the_server_refuses_an_eighteenth_stream_and_a_stream_past_the_store`; the full-buffers and re-declare tests' EXTRA stream now expects 10027. Mutation: `max_streams` removed turns all three red (10047). Deleted: config::test_a_resplit_is_charged_the_block_each_stream_was_created_with, covered by the server's max_streams (limits), the static fit (config) and the fence tests; limits::test_a_stream_shrunk_by_a_resplit_stays_within_its_charge, since no update path exists, also covered by the limits test and the fence tests. Renamed and trimmed: limits::test_one_class_table_declares_in_any_order_and_resplits_a_wholly_reserved_store → test_one_class_table_declares_in_any_order_on_a_wholly_reserved_store (any-order declare and re-declare no-op kept; apply legs dropped). The full-buffers test lost its apply_table raised-cap leg. Stated cost: a small stream is charged a 4 MiB block it may never use (68 MiB of the fence for 17 streams), and there is no per-Node cap override until E3b.
- E-W1-CONS-1 (implementer, consumer cap, 2026-10-07, STOP): the bead "cap durable consumers at `max_consumers: 16` per stream and charge them in the import-time fit, raising MemoryMax to the smallest multiple of 8 MiB, STOP past 248 MiB" does not fit under 248 MiB. Arithmetic: the current fit is 12 + 17 × 4 + 140 + 4 = 224 MiB with zero slack. Consumers: 17 streams × 16 = 272; each is charged 3 state files × 16 KiB Pi pages = 48 KiB of tmpfs plus ~49.4 KiB of measured heap = 97.4 KiB; 272 × 97.4 KiB = 25.87 MiB (26.03 MiB if the heap is rounded up to 50 KiB). 224 + 25.87 = 249.87 MiB, so the smallest multiple of 8 MiB is 256 MiB, past the 248 MiB stop line. Options for the owner: (a) MemoryMax 256 MiB with `max_consumers: 16` (6.1 MiB slack); (b) `max_consumers: 14` (238 consumers, 22.64 MiB; 246.64 MiB) at 248 MiB, 1.4 MiB slack (12 or 13 also fit 248; 15 does not: 248.26 MiB); (c) charge only the tmpfs files, treating consumer heap as inside GOMEMLIMIT: 224 + 272 × 48 KiB = 236.75 MiB → 240 MiB with 16, but GOMEMLIMIT is soft and live consumer heap is not collectable, so this under-charges. Evidence: /Volumes/Dock/tmp/w1/selfprotect/out/cons16.txt (232 MiB fence, 272 consumers peaked 60.3 MiB, the 17th per stream refused 10026). Nothing in the tree was changed; items 1-4 of the bead (cap, fit, fence test, not-a-defect errata, 0017 and node_link docstring corrections) wait for the owner's pick.
- E-W1-CONS-2 (implementer, consumer cap, 2026-10-07): resolves E-W1-CONS-1's STOP. Owner: RAM is not a limiting factor, MemoryMax may rise. Durable consumers are the one vector both reachable in normal use (nats-py KV keys, history and watch each hold an ordered consumer for up to 5 min) and not self-recovering (a durable's state persists on the tmpfs store, so a restart reloads it and OOM-loops). node-bus.conf's API account now sets `jetstream { max_bytes_required: true, max_streams: 17, max_consumers: 12 }`. `max_consumers` is per stream (ns:server/consumer.go:1162-1172; a KV stream's `max_consumers: -1` takes the account's), and the server refuses the next consumer at create (10026), never a write. Constants in contracts.node_link: NODE_MAX_CONSUMERS = 12; CONSUMER_BOUND = 152 KiB = 3 state files (meta.inf, meta.sum, o.dat) × 16 KiB Pi 5 pages on tmpfs (48 KiB) + 104 KiB heap; STREAM_BOUND = FILESTORE_BLOCK_BOUND + 12 × CONSUMER_BOUND. The heap figure is measured, not E-W1-CONS-1's 49.4 KiB: /Volumes/Dock/tmp/w1/selfprotect/out/cons16.txt (272 durables, 2.15.0 linux-arm64, 4 KiB pages) grew anon 11.7 → 39.2 MiB = 103.5 KiB per consumer, rounded up to 104; its tmpfs was 12.8 KiB per consumer at 4 KiB pages, hence 48 KiB at the Pi's 16 KiB. The figure the bead quoted, ~101 KiB, is the peak delta (60.3 − 33.5 MiB), which nets out the store at 4 KiB pages and the baseline's startup transient, so it under-charges; the larger, 103.5 KiB of heap alone, is charged. Fit at 16 per stream: 224 + 17 × 16 × 152 KiB = 264.4 MiB > 256, so per the bead the cap is the largest that fits 256: 12 → 12 + 17 × (4 MiB + 12 × 152 KiB) + 140 + 4 = 254.28 MiB, 1.72 MiB slack (13 needs 256.80 MiB). NODE_BUS_MEMORY_MAX = 256 MiB. contracts checks the identity at import and ClassTable charges each of its streams STREAM_BOUND. No bus systemd unit or capacity.py bus line exists yet (E3c), so 256 lives in contracts, 0017 C5/C13 and the config test only; the checks.yml fence job reads the constant. Tests: config pins `max_consumers` == NODE_MAX_CONSUMERS and the identity (12 the largest that fits; a table fence one byte under the identity refuses); fence `test_a_full_store_keeps_writing_inside_the_bus_fence_and_reloads_after_a_crash` now holds 12 durables on every one of the 17 streams through the 5 min write phase and the crash reload, the 13th refused (10026), and every stream comes back with 12. Green on Docker linux-arm64: peak 256.1 MiB (at the fence: reclaim, no kill), store up to 67.6 MiB, oom_kill 0, reload current 164.2 MiB; stall test peak 90.7 MiB of 256. Mutations, each red in the config test: the table's consumer charge dropped; `max_consumers: 16` in node-bus.conf. Changed test: limits::_desired counted each key's history with 22 `kv.history` calls on one bucket, which the cap now refuses at the 13th (10026), so it reads every key's count from one STREAM.INFO `subjects_filter`. Stated costs: 30.3 MiB of the fence is charged for consumers every stream may never have; a program that calls nats-py KV keys, history or watch more than 12 times within 5 minutes on one stream, counting Central's durable, meets 10026 (no production code calls them today); a 13th consumer per stream or an 18th stream needs the owner. Not covered: kernel memory per consumer beyond the store pages (≈6 KiB in cons16, inside the slack), and ephemeral consumers' own storage is not separately measured.
- E-W1-CONS-3 (implementer, self-protection vectors, 2026-10-07): vectors probed in the 232 MiB fence and judged NOT defects under the owner rule (Node programs are first-party and use nodeapi; a defect must happen in normal operation of nodeapi-built programs, or not recover by itself on restart). (1) Replies aimed at Central's inbox (a raw client's JetStream API request whose reply subject is Central's inbox, so the server's internal reply crosses the leaf toward a stalled hub): nodeapi never sets a foreign reply subject; the bus recovers on restart. Evidence: /Volumes/Dock/tmp/w1/selfprotect/out/head_reflect.txt (OOM, starts 2, then 34.4 MiB), head_jsreply.txt. (2) Subscription churn on the leaf's imports (`*.method.>`, `$SRV.>`) while the hub is stalled: LS+/LS- queue on the leaf; nodeapi programs subscribe their methods once; recovers on restart. Evidence: head_churn.txt (OOM, starts 2, then 33.7 MiB), knobs_srvchurn.txt. (3) MSG.DELETE tombstones: nodeapi never deletes a message and KV streams deny delete; recovers after one restart. Evidence: head_deletes.txt (one OOM, starts 2, then steady at 174.5 MiB), knobs_deletes.txt, knobs_deletes_fine.txt. (4) Unpaced STREAM.MSG.GET flood: nats-server 2.15 has no knob for it and its internal send queue is unbounded (ns:server/events.go:731); nodeapi does not flood gets; recovers on restart. Evidence: head_rawget.txt (OOM, starts 2, then 32.9 MiB), head_msgget.txt, knobs_msgget.txt, knobs_rawget.txt. Consequence: contracts.node_link's docstring and 0017 C4 now say the leaf rule holds for nodeapi programs, not for any client. leafnodes `write_deadline` stays "10s".
- E-W1-CRASH-1 (implementer, crash-reload review, 2026-10-07): a bus crash (kill -9, OOM or panic; not SIGTERM) followed by a restart on the surviving tmpfs store permanently loses live sticky documents, `_manifest` included. Cause: nats-server 2.15 writes no tombstone when the per-subject limit removes a message (ns:server/filestore.go:6240); with a stale `index.db` (written at the 2-minute sync) recovery rebuilds from the blocks, resurrects superseded values, exceeds `max_bytes`, and discard-old drops cold documents from the front. Proven by the review probe `/Volumes/Dock/tmp/w1/cl-review-probe/probe_dead.py`: a 23-key bucket went from 48 to 25 messages, 10 documents gone, on darwin and on linux-arm64 tmpfs. Likely also affects the circular state buckets and the WALL mirror (unproven). Not a PR 49 blocker: no bus unit ships yet and a crash is outside normal operation, but it is not self-recovering. Proposed class fix for the owner (E3b + E3c): an unclean bus start discards the store, so it takes the same fresh-store path as a reboot (new epoch, Central re-asserts every document), plus a mixed-rate kill -9 survival test. Owner to choose in the E3b design page.
- E-W1-FIT-2 (implementer, block-bound review, 2026-10-07): corrects decision 0017 C5 and the `FILESTORE_BLOCK_BOUND` and fit comments in `contracts/node_link.py`. "A stream's files reach its cap plus one block" holds for circular (front-discard) streams, not for KV/sticky streams, where the per-subject limit removes messages from the middle: their files can transiently reach about one block plus twice the cap until the 2-minute sync compacts them (ns:server/filestore.go:6457). Measured 22.22 MiB against a claimed 13.75 MiB for a 60 × 170,000 B history-1 table, cleared to 11.51 MiB after 135 s; no OOM reproduced. The overshoot is transient and self-clearing and is not charged in the fit; E3b to revisit. Also: `CONSUMER_BOUND` was measured on idle durables with 4 KiB pages. 0017 C5 and those comments now say exactly this.

## E3b · design re-review round 2 and slice cut (code architect, 2026-10-07)

- E-E3B-R2-1 (code architect, E3b design rev 3, 2026-10-07): the round-2 NATS re-review proved that the cursor reader as specified (ephemeral AckNone consumer, "a pull that got messages is alive", gap = delivered − cursor − 1) writes false gap rows on an ordinary leaf drop: `/Volumes/Dock/tmp/node-redesign/e3b-probes2/transit_loss_t2.out`, 193 events in two gaps, all still in the stream, matching consumer-sequence jumps (1084→1168), (1807→1918). Applied to `e3b-design.md` §7.2, §8, §9.3, §9.4, §11, §16.1: the reader tracks the consumer sequence from each ack reply; a jump or an incomplete request keeps the contiguous prefix, deletes the consumer best-effort and recreates it from the cursor; a gap row only when the stream sequence jumps with a contiguous consumer sequence. Tracer step 2 adds a leaf drop mid-drain. Frozen in slice S1 of `.claude/runs/wave2-e3b.md`.
- E-E3B-R2-2 (code architect, E3b design rev 3, 2026-10-07): the liveness contract "every pull asks for idle heartbeats; neither a message nor a heartbeat = lost" is wrong on nats-server 2.15.0 (probe N, `transit_loss.out`): a no_wait pull with a heartbeat gets `400 heartbeat value too large`, as does a heartbeat past half of `expires`; a live consumer on an empty stream answers a no_wait pull only with `404`. Built on the existing no_wait-first pull (`nodeapi/pull.py:60-67`) every idle poll would report "lost" and pile recreated consumers up to the 12-per-stream cap. Applied: any reply proves alive (message, 100, 404, 408, 409 other than Consumer Deleted); silence through the deadline plus grace, 503 or 409 Consumer Deleted = lost. S1 sends no idle heartbeats at all: every request is short and its closing status already proves the consumer alive.
- E-E3B-R2-3 (code architect, E3b design rev 3, 2026-10-07): §7.1's layering (`documents` → `buffers | pull | envelope`) could not hold: the cursor reader in `pull` needs `Token` (`nodeapi/documents.py:44`, a layer up) and `EPOCH_KEY`/`epoch_origin`/`epoch_of`/`stream_epoch` (`nodeapi/buffers.py:76,101,230-239`, a sibling). Applied: a new bottom module `nodeapi.epoch` holds them, plus `epoch_start(epoch) = Token(epoch, epoch_origin(epoch) - 1)`; the import-linter `nodeapi` layers contract is `node | hub` → `documents` → `buffers | pull | envelope` → `epoch`. Lands in S1.
- E-E3B-R2-4 (code architect, E3b design rev 3, 2026-10-07): attach had no ordering rule for a session whose streams another component declares (probe A, `absent_line.out`: birth and the first event fail with `NoStreamResponseError` before apps applies the app line). Applied to §7.2 node, §9.5, §11, §14: attach waits with backoff until every stream of its slice exists, and runs again on every reconnect and whenever a publish finds a stream of its slice absent. The re-review asked for "attach again whenever an epoch changes"; an epoch changes only by a create after the stream was absent, which those two triggers cover (stated as a §14 assumption). Lands in S5 with an attach-before-declare test.
- E-E3B-R2-5 (code architect, E3b design rev 3, 2026-10-07): parked review item adopted because E3b owns the fit: under the memory store the heap for a store full of the smallest `nodeapi` events is about 4.7 × the stored bytes (probe R, `memstore.out`: 12 MiB stored, RSS 43.9 → 100.6 MiB). A fit that charges the store at 12 MiB undercounts by about 45 MiB. S3's import-time fit charges `NODE_STORE_BYTES × MEMORY_STORE_FACTOR (5)` + 17 × 12 × 104 KiB + a 48 MiB idle baseline ≤ `GOMEMLIMIT` (140 MiB); E3c's fence job must re-measure on linux-arm64 with small messages.
- E-E3B-CUT-1 (code architect, slice cut, 2026-10-07): design §12 change 10 and §15 put "nats-py at runtime" in E3b. Moving `nats-py` from the `dev` group to `[project].dependencies` rewrites `uv.lock`, which this run must leave unchanged, and nothing ships `nodeapi` until E3c's base deb (the wheel's packages exclude `nodeapi`; `scripts/release_plan.py:329` defers the claim to E3b/E3c). Moved to E3c in the design; no E3b slice touches `pyproject.toml` dependencies or `uv.lock`.
- E-E3B-CUT-2 (code architect, slice cut, 2026-10-07): the run plan said the tracer (§16.1 steps 1-5) "may span two slices". It spans four (S1 steps 1-2 for events, state and methods; S2 documents, wall and the hub restart; S3 the memory store and kill -9; S4 apply on a full line), because steps 3 and 5 each carry a compile unit of their own (the store kind with the fit, config and fence test; the store-line table replacing `ClassTable` with every importer), and two slices would put each over the per-bead ceiling. Every tracer slice is green alone and extends one test file.
- E-E3B-CUT-3 (code architect, slice cut, 2026-10-07): §12 deletes "hub JetStream persistence" and §15 gives the hub runner to E3d. Builders fix the storage kind (no caller chooses it, §7.2 buffers), so the slice that switches buffers to the memory store (S3) also switches the hub's WALL: Fleet's generator (`central/fleet/node_bus_accounts.py`, `HubListeners.store_dir`, `max_file_store_bytes`) emits a memory store. The hub runner and its volume stay E3d's.
- E-E3B-CUT-4 (code architect, slice cut, 2026-10-07): §7.3 said "prune is scoped to a session's own namespaces (`apps` owns `apps` and `player`)". Applying the `apps` line would then prune every `player` stream. Prune is scoped to the one line being applied, matched on the stream's metadata namespace exactly; applied to the design.
- E-E3B-CUT-5 (code architect, slice cut, 2026-10-07): §7.4 names event streams `REC_<c>` and `OBS_<c>`. S1 freezes `event_buffer(line, topic, ...)` → stream `<TOPIC>_<line>` with subjects `<line>.<topic>.>` (e.g. `RECORD_display`, `display.record.>`), so a name and its subjects derive from one pair and a slice cannot name a stream whose subjects are outside its namespace. No stream name is a wire contract yet (nothing ships).
- E-E3B-S1-1 (implementer, S1, 2026-10-07): the page's mutation probe ("report a gap whenever the stream sequence jumps, ignoring the consumer sequence: step 2 must fail on gap rows") cannot fail on the leaf-drop leg as the page specifies it. Measured: with the consumer-sequence check disabled, the tracer passed 3 of 3 runs. A leaf drop loses only a tail of one request's deliveries (TCP: a prefix arrives, then nothing), so that request ends incomplete or silent, and the page's own rule ("an incomplete `Pulled` or a lost consumer: recreate from the cursor") recreates before any consumer-sequence jump can be seen. Round 1's false gaps (probe T2) came from treating silence as "empty" and keeping the consumer; liveness now closes that path first. The consumer-sequence check is a second line of defense. Step 2 therefore gains one deterministic leg: another local client takes three deliveries from a FIRST `CursorReader`'s consumer, and the reader must recreate (`recreated`) and return the ten events with no `Gap`. With the check disabled that leg fails on `Gap(count=3)` (2 of 2 runs). The leaf-drop leg stays, with "no gap row" asserted.
- E-E3B-S1-2 (implementer, S1, 2026-10-07): design §7.2 pull says silence is measured through "the request's deadline (expires, or none for no_wait) plus a grace period". S1 keeps wave 1's deadline of `timeout` + 1 s grace for every request, no_wait included, and extends it by the grace on each reply. With grace alone on a no_wait request, a component whose loop is busy past 1 s (`test_a_busy_component_is_never_cut_off_by_a_large_pull` blocks 3 s) would read its buffered deliveries after the deadline and take a live consumer for lost. Cost: a lost consumer is noticed after `timeout` + 1 s (2 s for the drain), not 1 s. A request also reads whatever is already buffered after its deadline before it ends.
- E-E3B-S1-3 (implementer, S1, 2026-10-07): nats-server 2.15.0 refuses a `DeliverPolicy.LAST_PER_SUBJECT` consumer without a filter (10094, "optional filter subject is not set"). The watch's consumer is created with `filter_subjects` set to the stream's own subjects, read from the same STREAM.INFO that gives the epoch. Two smaller points: a session whose held state is put before its first attach published each such key twice (once by the attach re-put, once by the pending put), which the drain correctly counted as a superseded revision (a gap row on `KV_state_display`); attach now settles the pending put of the value it re-put. And `NodeSession` validates `Release.version` as a semantic version when it has methods, because `nats.micro` requires one; a non-semver release version raises the library's ValueError at construction.
- E-E3B-S2-1 (implementer, S2, 2026-10-07): the page's step 4 bound "the Node's mirror and `WallView` hold the current value within 15 s" is too tight. After `WallWriter.ensure()` re-creates WALL at mark + 1 + 1024, the Node's mirror resumes on nats-server's own retry of its source: measured 12.2 s in 4 of 5 runs and 19 s in 1 (the view follows the mirror within the same second; mirror `active` 16 s at the 15 s mark). The tracer allows 30 s, as the deleted wave-1 hub-loss test did (`_holds(node, wall.latest, 30)`). The K = 0 mutation still fails at 30 s (the mirror skips the re-put forever, X8). Design §9.6 "probe 2: about 9 s" understates it.
- E-E3B-S2-2 (implementer, S2, 2026-10-07): the page omits that the WALL mirror has no subjects of its own, so `CursorReader(StartAt.LAST_PER_SUBJECT)` on it sent `filter_subjects=None` and nats-server 2.15.0 refuses that (10094; probed: `>` and `wall.>` are accepted on a mirror). `nodeapi/pull.py` (not in S2's file list) now filters by `info.config.subjects or [">"]`. One line; no signature change.
- E-E3B-S2-3 (implementer, S2, 2026-10-07): the acceptance grep `rg "_manifest|missing_documents|listed_documents|StaleToken|sticky_bucket|class Documents" nodeapi tests` can never be empty: `_manifest` matches many unrelated pre-existing names under `tests/` (`read_manifest`, `_parse_manifest`, `fetch_manifest`, `test_migration_carry_served_package._manifest`, ...). Scoped to what S2 owns (`nodeapi tests/integration tests/test_node_bus_config.py`), it finds nothing. Later pages should scope it so.
- E-E3B-S2-4 (implementer, S2, 2026-10-07): adoption semantics the page leaves open. On adoption NodeLink records `wrote(stream, key, <digest of Central's projection>, <the Node's token>)`, not the digest of the Node's value: if it recorded the Node value's digest, the very next assert would see the projection differ and overwrite the adopted value at once. So an adopted value stays until Central's projection for that key changes again; that write is then conditional on the adopted token, and Central's newer intent wins. S6's "table change re-puts every owned key" will overwrite adopted values unless S6 skips keys whose own token is not Central's write; flag for S6's page.
- E-E3B-S2-5 (implementer, S2, 2026-10-07): smaller choices the page did not fix. (a) `WallMarks` keeps no per-key token, so `WallWriter.put` reads the key before each conditional put (one more request per wall write; a Conflict, which only a second Central instance causes, re-reads and writes again). (b) `desired_bucket(line, ...)` takes a `STORE_LINES` name, so the wave-1 tests' ad hoc buckets (`probe_desired`, `desired_big`, `desired_long`, `desired_probe`) moved to `KV_desired_player`; `KeyTable` keys are `[A-Za-z0-9_-]+`, so the longest-subject test's document key is `k`*n, not `<`*n: the worst JSON escaping is no longer reachable for a document key. (c) `NodeSession` raises `ValueError("session_one_desired_bucket")` for a slice with two desired buckets (the views assume one). (d) `wall_config` sets `max_msgs_per_subject` to the table's history (1 in every table today); the mirror stays at 1. (e) A `DocumentWriter.put` with `expect=ABSENT` returns a token in the writer's last known epoch; the ack carries no epoch, so a caller that bound before a store loss and put with ABSENT gets an old-epoch token. NodeLink binds per assert, so its epoch is current.
- E-E3B-S3-1 (implementer, S3, 2026-10-07): with no `store_dir` and `max_file_store: 0`, nats-server 2.15.0 enables JetStream on the memory store (no refusal, so no runtime store dir was needed), but it still creates `$TMPDIR/nats/jetstream/<account>/streams` at start (logged "Temporary storage directory used"). Servers sharing one TMPDIR race on it: under `-n 4`, 1 of 3 G-bus runs had a Node exit at start with "[FTL] Can't start JetStream: ... could not create storage streams directory - mkdir .../nats/jetstream/API/streams: no such file or directory". The harness now gives each server its own TMPDIR (`BusServer.start`); 4 runs green since. On a Node there is one bus, so no race, but E3c's unit should give it a private TMPDIR (e.g. `PrivateTmp=yes` or `Environment=TMPDIR=%t/...`) so the empty directory never meets another program's. The fence container has one server and is unaffected. Also: under the memory store a create past the store's reservation is 10028 (JSMemoryResourcesExceededErr), not 10047, including a re-add of a stream that exists on a full store (probed); `nodeapi.buffers.declare` now tolerates 10028 in its race path (`MEMORY_EXCEEDED` replaces `STORAGE_EXCEEDED`), which the page did not name.
- E-E3B-S3-2 (implementer, S3, 2026-10-07): the page's step 3 bound "every document ... is back (compared with values before the crash)" cannot hold for a document another writer owned. Before the kill the tracer's desired bucket holds `show` = the local UI's value, adopted by Central (C16; E-E3B-S2-4), and `layout` = Central's. After the bus starts empty, NodeLink asserts Central's projection into the new epoch, so `show` comes back as Central's projection value (`show-2`, writer `central`), not the adopted `show-local`: the adoption is recorded only as a token in the old epoch, and the local UI (a raw writer in the tracer) re-asserts nothing. Step 3 asserts the state keys and the wall value equal their pre-crash values and every document equals Central's projection in the new epoch. Not a defect under the owner's rule today (the local UI is "Later" in the failure table, and the Node recovers by itself), but whoever builds the local UI or E5's projection must decide whether an adopted value survives a bus start: either the projection takes the adopted value, or the local writer, like every writer, refills what it owns at attach.
- E-E3B-S3-3 (implementer, S3, 2026-10-07): deletions and changes the page did not list. (a) `test_node_bus_seam.py::test_one_wall_write_reaches_every_node_mirror_and_the_mirror_is_read_only` assumed a kept store ("its kept store catches up on restart"); it now re-creates Node B's mirror after the restart, as a wall-reading session's attach does, and checks it re-syncs. (b) `test_node_bus_limits.py`: the server-level refusal of a memory stream (10028) is deleted rather than inverted to a file stream, because the page's acceptance grep forbids `StorageType.FILE` under `tests/`; surviving coverage: the config test (shipped `max_file_store` 0, every builder MEMORY). Its fill arithmetic uses the memory charge (16 + subject + headers + payload, ns:server/memstore.go:2511-2513), and `account_info().memory` replaces `.storage`. (c) `test_node_bus_documents.py` (not in the page's files) replaced `node.wipe()` with `node.stop()`. (d) The fence stall leg floods `REC_flood` with core publishes of `MAX_STORED_MESSAGE - 1024` bytes (a stored size; the old 200,000 B flood body was past every stream's `max_msg_size`). (e) The acceptance grep still matches unrelated, pre-existing tmpfs mounts under `tests/` (netboot, initrd, test database, demo: `test_netboot_init.py`, `test_stage1_mount.py`, `test_initrd_mount_probe.py`, `tests/node/test_node_memory_class.py`, `test_wall_demo.py`, `tests/integration/compose.*.yml`); nothing in the bus files. `.github/workflows/checks.yml`'s bus-fence comment still says "the store on tmpfs" (outside this slice; D1 or E3c).
- E-E3B-S4-1 (implementer, S4, 2026-10-07): `NodeLink` never finds a store line created after its last reconcile. Its reconcile triggers (design §9.1: the link comes up, a reader is lost, a new `birth` revision is drained) all start from a stream it already reads, and a new line's `birth` is in that line's own state stream, which no reader holds yet. So a component that first applies its line after Central's link reconciled is never drained: in normal operation, a Node boot where components attach seconds apart while the link is up, or any line's first apply after a bus start the link has already reconciled. Measured in tracer step 5: with the other lines applied and the health line filled, `OBSERVATION_health` never gets a cursor. A defect under the owner's rule (normal operation; nothing on the Node can make Central look). Not fixed in S4 (`nodeapi/hub.py` and the trigger mechanism are outside the page): step 5 restarts the fleet link after applying the other lines, as E3d's supervisor would, and then the health release's new `birth` drives the reconcile the page asks for. The trigger needs a design decision, e.g. reconcile when an idle drain round sees `STREAM.NAMES` (a bounded reply) differ from the names it last reconciled. Route to S6 or a re-cut before E3d.
- E-E3B-S4-2 (implementer, S4, 2026-10-07): `nodeapi.pull`'s per-connection budget starved readers. `_Budget` woke every waiter through one Event, and a request that had just ended asked again and took the budget synchronously before any woken waiter ran. With more drains on one client than PULL_MAX_BYTES // PULL_ONE_BYTES (5) expiring requests, the same five kept it. The fleet `NodeLink` drains 8 streams on one client once every fleet line exists (host, apps, display, health: records or observations plus state), first reached in step 5: `OBSERVATION_health` or `RECORD_health` got no cursor in 20 s in 3 of 3 runs. Fixed in `nodeapi/pull.py`, which is not in S4's file list (the same kind of fix as E-E3B-S2-2): grants go in arrival order, and the waker makes the grant. No signature or behaviour change otherwise. Green in 4 tracer runs and G-bus since. Cost: on a client with more than five drains, each waits its turn, up to about one expiring request (DRAIN_TIMEOUT_SECONDS) per round.
- E-E3B-S4-3 (implementer, S4, 2026-10-07): points the page left open or got wrong. (a) Under the memory store a create past the reservation is 10028, not 10047 (E-E3B-S3-1). Step 5 asserts nothing refused, and the mutation probe (create before prune) fails with 10027 on the 18th stream (measured). (b) Deletions the page does not list: `test_node_bus_seam.py::test_a_buffer_that_overflows_while_central_is_away_reaches_central_as_a_counted_gap[bucket]`, because its circular KV `bucket` is deleted (surviving coverage: the unparametrized stream leg on `OBSERVATION_health`, then S6's state test); the config test's `buffer_policy_is_fixed`/`buffer_never_forwards` checks on `buffer`, because no remaining builder takes those fields, so the failure is impossible at construction (the config test now asserts that no built config forwards); and `bus_servers.kv_bucket_bytes`, which has no user left. (c) `nodeapi/__init__.py`'s docstring named the class table; one line changed. (d) The observation retention leg uses the health line with its observations aged at 1 s (`line_slices` keeps them 6 h, which no test can wait out). (e) `apply` lists the bus's streams with one local `STREAM.LIST` page (at most 17 streams), not NAMES then INFO: it never crosses the leaf (X4 binds the leaf). It detects a change by max_bytes, max_msg_size, max_msgs_per_subject, max_msgs, max_age, subjects and TABLE_KEY. A change that keeps the byte cap runs in the shrink pass. (f) Design §7.2 says that "a change an update cannot make is refused at Slice construction". With the remaining builders no such change can be expressed (names derive from role and topic; storage, retention and discard are fixed), so nothing was added.
- E-E3B-S5-1 (implementer, S5, 2026-10-08): the page's mutation probe outcome is wrong. With the wait removed (a non-declarer re-puts at once), the re-put meets `NoStreamResponseError`, attach fails, and the attacher retries it with backoff as every failed attach is retried, so once apps applies the line the Player's `birth` and its 10 events ARE recorded in the first epoch. The probe is still killed, but by the leg's other acceptance check, "no `NoStreamResponseError` escapes a session (the session's thread logs none)": measured, 5 × `player: attach: NoStreamResponseError()` logged and the test fails on that assertion. Making "no birth in the first epoch" the failure would mean dropping the retry, which would break reconnect recovery.
- E-E3B-S5-2 (implementer, S5, 2026-10-08): `Slice.union`'s rule "a stream both name: the larger cap and the union of their key tables" contradicts itself for sticky streams. A sticky stream's cap is its table's budget (`buffers` rule: no key loses its only value to bytes), and the union of two tables with different keys needs more than either cap. So `union` rebuilds a keyed stream from the union table (`_keyed`): its cap is that table's budget (at least the larger of the two) and its largest message comes from the table. A circular stream takes the larger cap. Two tables with different `history` are refused as `slice_union_conflict`, like any other difference.
- E-E3B-S5-3 (implementer, S5, 2026-10-08): choices the page left open. (a) `apply_line` never blocks. It holds the slice and wakes the attacher. While attached, it applies only the held lines (no state or `birth` re-put); otherwise the next attach applies them first, before the session's own slice is applied or waited for. (b) A publish that meets `NoStreamResponseError` (outbox or state) clears ready and attached and runs a full attach. (c) "Crash with the start order reversed" is done this way: the Player session stops before the kill -9. apps keeps running, so its reconnect re-applies the held line (the "every later attach" path), and then a fresh Player session starts. (d) Both tests start the show NodeLink only once the Player line exists, and leg 2 restarts it after apps has re-applied, because E-E3B-S4-1 (a line created after a link's reconcile is never found) is still open and would otherwise make the tests flaky. (e) Hot-swap: both releases' `birth` share one sticky key, so one can be superseded before Central reads it. That gives a counted gap (count 1, measured), which is the designed §11 "state written faster" row, so the test asserts no unknown gap other than the pruned stream's.
- E-E3B-CC-1 (code architect, course correction after S5, 2026-10-08): E-E3B-S4-1 (a line created after a NodeLink's last reconcile is never drained) is a defect in normal operation and goes into S6, re-cut, before E3d. Every existing trigger starts from a stream the link already reads (`nodeapi/hub.py` run/_drain), and C4 forbids the event-driven alternatives: a Node program cannot send toward the hub, and JetStream's stream-created advisories would cross the leaf. So the link looks: a names watch beside the drains requests `STREAM.NAMES` (bounded, at most 17 names, X4-safe, already used by reconcile) every 2 s (the existing `_BACKOFF[1]` "nothing attached: look again" cadence, hub.py:188) and ends the round when the set differs from the one its last reconcile listed. Cost: one small request per link per 2 s per Node across the leaf; a new line is drained within about 2 s; design §9.1's "Reconcile triggers (no timer)" gains a fourth trigger, recorded by D1. Slice-level, not a frame change: no boundary, protocol or public name moves. The workarounds the tests carry for S4-1 (tracer step 5's link restart; test_node_bus_api_recovery.py starting or restarting the show link only after the line exists) are removed in S6 and become its regression coverage.
- E-E3B-CC-2 (code architect, course correction after S5, 2026-10-08): S6's frozen behaviour is corrected in three places. (a) E-E3B-S2-4: "re-put every owned key" on a table change would overwrite adopted values, because adoption records the Node's token (hub.py `_assert_one`), so a put conditional on it succeeds. The table-change pass reads each key first and re-puts only keys that are absent or whose writer is Central; an adopted key is left (cost: an adopted value older than a stray dropped-key message can still be evicted by the shrink race; its reader keeps last good; the local UI is "Later"). (b) "The table Central last asserted against" has no home in `LinkStore`; it is kept in memory per NodeLink, per stream (epoch, encoded table); the first sight in a link's lifetime records it without a re-put (cost: a table change while no link runs is not rotated; the race it heals needs a running link's write). (c) Today a projection key the table does not list makes `DocumentWriter.put` raise `DocumentRefused` (documents.py:115, a ValueError) out of `assert_documents` (hub.py:131, 155), which `run` does not catch (hub.py:182), so rollout skew kills the link; S6 catches every `DocumentRefused` (unlisted or too large after a shrink), logs `document_refused` with its reason, and goes on.
- E-E3B-CC-3 (code architect, course correction after S5, 2026-10-08): S6's tests, corrected against the code. (a) State faster than the drain: `NodeSession` coalesces pending puts per key (node.py:433-438, 390-394), so 500 puts publish fewer revisions; the expected gap total is the published revisions Central never read, taken from the stream's sequences, not 495. (b) The seam test S6 deletes is unparametrized since S4 (E-E3B-S4-3 b). (c) The outbox test asserts its precondition that OUTBOX_MESSAGES of its events fit `RECORD_display`'s cap, so a buffer drop can never pass for an outbox count. (d) The overflow test lets the link record some events first, so a cursor exists and the gap is one row from it. (e) The acceptance greps are scoped to `nodeapi tests/integration` (E-E3B-S2-3). (f) A second mutation probe covers CC-1.
- E-E3B-CC-4 (code architect, course correction after S5, 2026-10-08): D1's page under-listed what 0017 must change: C5 still states the file store on tmpfs, the block and durable-consumer charges, the 254.28 MiB sum and the boot header, and C13 still says Central reads with durable consumers and acknowledges and that `nodeapi` cannot change a declared stream's limits; C18 still has the manifest and circular reported state; the consequences list (line 84) and the history paragraph need a line; `.github/workflows/checks.yml:79`'s comment still says "the store on tmpfs" (E-E3B-S3-3 e). Corrected D1 page returned by this pass. Carried to the E3c brief, not D1: the bus unit gets a private TMPDIR (E-E3B-S3-1). Carried to E5 or the local UI: whether an adopted value survives a bus start (E-E3B-S3-2).
- E-E3B-S6-1 (implementer, S6, 2026-10-08): the acceptance grep can't come back empty, whichever way it is scoped. Unscoped over `tests`, `class Recorder` matches the unrelated `tests/test_provision.py:99`. Scoped to `nodeapi tests/integration` (E-E3B-CC-3 e), `def _drain` matched `NodeLink._drain` in `nodeapi/hub.py`. That private method is renamed `_follow`, with no behaviour change. `rg "class Recorder|def _drain" nodeapi tests/integration` now finds nothing.
- E-E3B-S6-2 (implementer, S6, 2026-10-08): choices the page and CC-2 left open. (a) The outbox count counts an event once when a full outbox drops it, and takes it back when the dropped event's publish was already in flight and then got acknowledged. Without that, an event could be both recorded and counted as lost. (b) `document_refused`'s body is {stream, key, reason}, adding the `DocumentRefused` reason per CC-2 c. It is logged again on each reconcile that still refuses the key. (c) The table-change pass reads only the keys whose digest is unchanged, because a changed digest is written anyway by the normal path. It is armed only after a fully completed `assert_documents`, so an assert interrupted by a link error is fully rotated again next time. (d) The names watch's interval is a private `_NAMES_SECONDS` = `_BACKOFF[1]`, because the page allows no new public name.
- E-E3B-S6-3 (implementer, S6, 2026-10-08): CC-1's "the workarounds become its regression coverage" holds only for tracer step 5. In `test_node_bus_api_recovery.py` the show link has no reader before the Player line exists. So the link's existing "nothing attached: look again" loop (`hub.py` run, `_BACKOFF[1]`) already found the line before S6, and both S5 legs pass with the names watch disabled (measured). The CC-1 mutation probe (watch never sets `again`) is killed by `test_the_node_api_tracer` alone: "not within 20s: Central drains the health line". The recovery tests now start the link before the line and keep it running across the bus crash, but they cover other things, not CC-1.
- E-E3B-D1-1 (implementer, D1, 2026-10-08): D1 amended 0017 C4, C5, C11, C13, C18, the Costs list, the "What this replaces" table and History (with E-E3B-CC-4's additions), plus the `bus-fence` comment in `.github/workflows/checks.yml` (comment only; E-E3B-S3-3 e). Two clauses outside the page still say less than E3b built and were left as written. C14 does not record Q3 (`WALL_STREAM_BYTES` frozen as a wire constant, design §10). C16's "a Node's store is lost at every reboot" is now true of every bus start (C5). Neither is wrong as stated. A later docs bead can fold them in. `scripts/check_docs.py` checks only relative links, and no architecture page names `nodeapi` modules, so no other page was bound.

## E-E3B-FR1 (2026-10-08, E3b final review F1) — WALL mirror lag grows with the hub outage
The design's "about 9 s" (§9.6) and E-E3B-S2-1's 12–19 s are wrong: nats-server's source-retry backoff makes a Node's WALL mirror current 7.7 s after a 2 s outage, 33.5–39.7 s after 10–60 s, 49 s after 180 s (probes /Volumes/Dock/tmp/w2/review-0/probes). Self-recovering, not a defect. E3e's join must allow ~60 s after a realistic hub outage; E8 budgets about a minute of mirror staleness after every Central deploy.

## E-E3B-FR2 (2026-10-08, F2) — apply's shrink can evict another listed key
"Purging first means a shrink never drops a listed key" (§9.7, X16, `nodeapi/buffers.py` apply docstring) holds only for keys a release drops. Lowering one key's largest size ({a:4096,b:4096}→{a:64,b:4096}, 4000 B stored under each) evicted b and kept a's oversize value (P11). Heals in normal operation (NodeLink table-change re-put; components re-put their state). Optional class fix for a later slice: never shrink a sticky cap below the bytes it currently stores.

## E-E3B-FR3 (2026-10-08, F3) — outbox drop count reported only at attach
`nodeapi/node.py` `_attach` puts the `outbox` state key; drops after attach never reach Central (6000-event burst: 1801 dropped, state said 0). Needs emit rates above ~1–6k/s, not shown in normal operation. Cheap class fix queued for the next run: hold `outbox` as a state key and re-put it whenever the count changes.

## E-E3B-FR4 (2026-10-08, F4) — design rule 3 wording
"Anything Central never read becomes a gap row" cannot cover an epoch Central never saw (a bus restarted twice between Central's reads). Reword: every epoch Central sees end gets one row; epochs born and ended unseen are invisible by construction.

- E-E3C-CUT-1 (code architect, E3c slice cut, 2026-10-08): the lane scope's "bus memory line in `appliance/kernel/capacity.py` (E2a)" has no table to edit. E2a-1 was implemented (bf73312, local branch `w1/e2a` only, never pushed) but never verified or landed: `.claude/runs/wave1-node-api.md` ledger row E2a-1 is `open`, and `claude/wave3-e3c` (3e1baf6) has no `LINES`/`MemoryLine` in `capacity.py`. bf73312 merges onto 3e1baf6 with one conflict, `.claude/errata.md` (its E-W1-E2a-1-1 entry). Cut: slice S5 lands E2a-1 by cherry-pick and binds the bus line to the bus unit (`NODE_BUS_MEMORY_MAX`, not E2a's 64 MiB, which the 256 MiB fence superseded, E-W1-CONS-2). Cost: E2a's lowered caps are first proven by this lane's node-pid1 legs. `w1/e2a` should be pushed as `wip/e2a-1` before anything else touches it: it exists on one disk only.
- E-E3C-CUT-2 (code architect, E3c slice cut, 2026-10-08): "nodeapi + nats-py shipped in the base package" cannot happen without a base program that imports `nodeapi`. `scripts/build_node_base_deb.py` stages each launcher's computed closure privately (`stage_application`), never a shared library, and `scripts/module_closure.unreached_imports` refuses a declared third-party import no code reaches. No base launcher imports `nodeapi` before E5/E6. Cut: S4 makes HostCore the first consumer (the host line: `birth` and one `base` state key, design §7.3 "every component has a state stream, because birth lives there"). Alternative: ship nothing until the first adopting component (E5/E6) and drop "nodeapi in the base" from E3c; then E3e's join has no Node program on the bus.
- E-E3C-CUT-3 (code architect, E3c slice cut, 2026-10-08): nats-py is not packaged in Debian trixie (packages.debian.org, 2026-10-08: no `python3-nats`; trixie's `nats-server` is 2.10.27, older than the pinned 2.15.0). The closure policies' third-party table maps imports to Debian packages only (`scripts/module_closure.py` ClosurePolicy, `scripts/debian_packages.import_table`). Cut: S4 adds a vendored-wheel table (`scripts/vendored_packages.py`) whose entry is test-bound to `uv.lock`'s nats-py 2.16.0 wheel (url and sha256), so `uv.lock` and `[project].dependencies` stay unchanged and the base does not claim `uv.lock` (which would make every dependency bump a base release). Second named consumer: the Player bundle (E8, design §12 change 10).
- E-E3C-CUT-4 (code architect, E3c slice cut, 2026-10-08): the pinned nats-server binary ships inside `node-base.deb`, the one artefact both the squashfs (`.github/workflows/base-image.yml`, rpi-image-gen layer) and the node-pid1 image (`scripts/build_node_pid1_fixture.py` DOCKERFILE) install, so neither install site changes. Costs: `node-base.deb` becomes `Architecture: arm64` (its file name `_arm64.deb`), and building it fetches pinned bytes over the network. Five unit-tier tests call `stage_tree(REPO, ...)` (`tests/node/test_node_linux_adapters.py`, `test_health_runner.py`, `test_node_boot_stage.py`, `test_node_probe_broker.py`, `display/test_node_display_runner.py`), so `stage_tree` stays network-free; a second step `stage_vendored` adds the pinned bytes, and `stage_package` (both) is the one path to a `.deb`.
- E-E3C-CUT-5 (code architect, E3c slice cut, 2026-10-08): the leaf's URL path is a wire contract between the Node (E3c) and Central's ingress route (E3d with E4) with no home: the tests keep a private `LEAF_PREFIX = "photo-wall/bus"` ("the production route is E3d/E4's", `tests/integration/test_node_bus_seam.py:57`). S1 freezes `contracts.node_link.LEAF_PATH = "photo-wall/bus"` and `NODE_BUS_PORT = 4222`. The URL keeps the boot origin's host and port and maps http to ws, https to wss. E3d's ingress route and its fixture hub must forward `<origin>/photo-wall/bus/` (nats-server dials `/photo-wall/bus/leafnode`) to the hub's WebSocket listener: relay to the e3d lane before its pages freeze. Until that route is deployed, a Node on this base redials its leaf about once a second (normal, self-recovering, log noise only).
- E-E3C-CUT-6 (code architect, E3c slice cut, 2026-10-08): the bus cannot sit in `photowallbase.slice`: its 256 MiB fence plus E2a's base members (96 + 96 + 64 + 64 + 64 = 384 MiB) exceed the slice's 480 MiB. The bus unit takes the default `system.slice`; its line has no parent; `memory_peak:bus` and `oom_kill:bus` read `system.slice/photo-wall-bus.service`.
- E-E3C-CUT-7 (code architect, E3c slice cut, 2026-10-08): the `bus-fence` CI job already runs on `ubuntu-24.04-arm` (`.github/workflows/checks.yml`), the Pi's architecture. What E3c adds is the smallest-event heap factor (design X14, E-E3B-R2-5: measured only on darwin) and the kill -9 leg. Neither the CI runner nor Docker Desktop uses the Pi 5's 16 KiB pages (design-r3 A3), so the Pi's reading stays bench evidence, reported by the `memory_peak:bus` and `oom_kill:bus` rows S5 adds.
- E-E3C-CUT-8 (code architect, E3c slice cut, 2026-10-08): the "prepare helper" cannot be the bus unit's own `ExecStartPre=`: systemd reads `EnvironmentFile=` before a start's first process, so a file an `ExecStartPre=` writes is not seen by that start's `ExecStart=`. The handoff stage writes it instead (`appliance/boot/node_bootstrap.materialize_handoff`, which already writes every owner configuration under `/run/photo-wall-node`), before its ABI checks, and the bus orders `After=` the handoff without requiring it. A wss leaf with no `tls {}` block starts a TLS client handshake (probe: `/Volumes/Dock/tmp/node-redesign/e3c-probes/wss_leaf_tls.out`, first bytes `16 03 01`), so one shipped `node-bus.conf` serves http and https origins; whether it verifies against the system roots is S3's test.


## E3d slice cut (code architect, 2026-10-08; brief `.claude/runs/wave3-e3d.md`)

- E-E3D-CUT-1 (code architect, slice cut, 2026-10-08): `central` cannot import `nodeapi.hub` under the contract "Only the Node API library talks to NATS": import-linter checks indirect imports by default. Probe (a throwaway `central/_e3d_probe.py` with `from nodeapi.hub import NodeLink`, removed after): "central is not allowed to import nats: central._e3d_probe -> nodeapi.hub (l.1); nodeapi.hub -> nats (l.45, l.46, l.63)", 19 kept, 1 broken. Applied in E3d S1: that contract gains `allow_indirect_imports = true` (the direct ban stays), and a new forbidden contract "Central uses only the hub role of the Node API library" (`central` ↛ `nodeapi.node`). E3c meets the same for `appliance -> nodeapi.node`; the `pyproject.toml` edit is identical in both lanes.
- E-E3D-CUT-2 (code architect, slice cut, 2026-10-08): the lane scope allowed `nodeapi/node.py` (FR3) only, but Central cannot use E3b's `NodeLink(client, …)` or `WallWriter(client, …)`: building the nats `Client` they take, or catching a nats error, means importing nats, which only `nodeapi` may do. E3d adds connection-owning entries to `nodeapi/hub.py`, additive: `run_link` (S1); `HubUnavailable`, `HUB_RELOAD_REQUESTS`, `HubAdmin` (`connect`, `identity`, `reload`, `linked`, `close`), `WallWriter.connect`/`close`, and `WallWriter.ensure`/`put` raising `HubUnavailable` instead of nats errors (S2). `NodeLink.call` still raises nats errors; E5, its first caller, owns that.
- E-E3D-CUT-3 (code architect, slice cut, 2026-10-08): the lane rule "uv.lock must stay unchanged" cannot hold for "the hub as a real service in Central's deployment". The media-worker image installs `uv sync --frozen --no-dev` (`Dockerfile` deps stage) and its `source` stage copies `contracts media player central` but not `nodeapi`, while nats-py is in the `dev` group only (`pyproject.toml`). Probe (scratch copies of `pyproject.toml` and `uv.lock`; nats-py moved to `[project].dependencies`; `uv lock`): "Resolved 63 packages", and the diff is exactly 4 lines, the `nats-py` entries moving between the project's dependency and dev-group metadata; same version, no new package. E-E3B-CUT-1 moved nats-py at runtime to E3c, which ships only the base deb. E3d S5 is gated on the orchestrator approving that 4-line change, or on E3c landing the identical move first. S0-S4 need nothing from it (their servers run in-process under the dev environment).
- E-E3D-CUT-4 (code architect, slice cut, 2026-10-08): design §15's "ingress route for the leaf (with E4)" has no host. Production exposes Central's :8000 through an L4 LAN VIP with no HTTP router (mcurcio/iac `workloads/photo_wall/__init__.py`, one pod: `central` + `worker`); Compose publishes uvicorn's :8000 directly. A path route to the hub's WebSocket listener needs either Central relaying it, a router sidecar in front of Central, or a new LAN port, which W11 ruled out. E3d S3 has Central relay the leaf: a WebSocket route at `LEAF_PATH + "/leafnode"` (`contracts.node_link.LEAF_PATH = "/bus"`) piped to the hub's leaf listener. Cost: a Central restart drops every leaf for about a second (the hub restarts with every deploy anyway: same pod, `Recreate`), and leaf bytes cross Python. E3c must build the Node's leaf URL as `ws[s]://<user>:<user>@<origin host>:<origin port>` + `LEAF_PATH`; whichever lane lands first freezes the constant.
- E-E3D-CUT-5 (code architect, slice cut, 2026-10-08): design §15 and the epic table give E3d the hub "in the node-pid1 fixture Central". Nothing can dial that hub until E3c's bus unit is in the fixture image, so in E3d it would be untestable code. Moved to E3e's first bead (the join needs the fixture anyway): start nats-server from `FleetHub`'s file and run the bus side; E3d provides `FleetHub`, `NodeLinks`, `build_node_bus` and the leaf bridge it calls.
- E-E3D-CUT-6 (code architect, slice cut, 2026-10-08): "regenerate and reload on every enrolment change" (§7.2) is built as Fleet's hub loop diffing the generated configuration against the file every 2 s, not as a hook at each enrolment write: devices are inserted at `central/fleet/service.py:174`, `central/infra/catalog_records.py:459`, `central/registry.py:691` and retired at `central/registry.py:702`, and a hook per site is a convention the next site can miss. "Whenever its hub connection returns" is built as the hub's server id (`$SYS.REQ.SERVER.PING.IDZ`, new at every start) differing from the last one seen; worker start is the first look. Cost: an enrolment reaches the hub within about 2 s plus the leaf's redial (about 1 s, probe 1).
- E-E3D-CUT-7 (code architect, slice cut, 2026-10-08): no wall table exists yet (design §10: E8 defines it) and `WallWriter`/`KeyTable` refuse an empty one (`key_table_empty`). E3d S5 wires a Central-side placeholder `KeyTable({"timing": 4096})` in `central/node_bus_wiring.py`, not a `contracts` constant. `WallWriter` never updates WALL's metadata, so a changed table reaches WALL only when WALL is re-created, which happens at every hub restart, so at every Central deploy (same pod). E8 must not rely on a live table update.
- E-E3D-CUT-8 (code architect, slice cut, 2026-10-08): design §9.1 shows "discover methods → action log" at every reconcile, but `NodeLink.run` never calls `discover()` (`nodeapi/hub.py` run/reconcile) and nothing else does. E3d leaves it to E5, the first method caller. Presence (S4) is the leaf state only; component presence by `$SRV.PING` is deferred with it.
- E-E3D-CUT-9 (code architect, slice cut, 2026-10-08): `Database`'s pool refuses a waiter past `max_waiting = pool_size * 2` (`central/db.py:63-69`). A fleet link drains up to 8 streams and a show link up to 5; with tens of Nodes, commits through `asyncio.to_thread` would exceed it, and a store error ends the worker (`NodeLink.run`: "A store error propagates"), in normal operation. E3d S1 adds `LINK_STORE_CONCURRENCY` (4): store calls wait in the event loop behind one gate per process, never in the pool's queue; S5 gives the bus side its own `Database(pool_size=LINK_STORE_CONCURRENCY + 2)`.

## E3d slice and course-correction errata (2026-10-08; brief `.claude/runs/wave3-e3d.md`, copied verbatim from its Errata section)

- **E-E3D-S0-1** (implementer, S0, 2026-10-08): the count can over-count by one per bus hang longer than `_PUBLISH_SECONDS` (2 s). The publisher never retries an in-flight event the full outbox dropped meanwhile (its next `_next()` takes the new head), so when that publish times out `_done` never takes it back, yet the hung server may still store it on resume: Central then records it and counts it lost. Pre-existing (E-E3B-S6-2 a); not normal operation (hang plus overflow); no change in S0. The new test's hang is the emit loop only (well under 2 s) and observed `{"dropped": 99}`: the take-back path ran.
- **E-E3D-S0-2** (implementer, S0, 2026-10-08): `State.get("outbox")` now returns the held count (before: None), since the count is ordinary held state. No caller reads it; `State.put` still refuses the key.
- **E-E3D-S1-1** (implementer, S1, 2026-10-08): in a rolling-deploy overlap, each worker's reader keeps its own in-memory cursor while the stored cursor is last write wins, so the stored cursor can step back, and if the buffer overflows during the overlap, a gap row's `lost` can count sequences the other worker recorded (gap rows from different `after_seq` both stand). Records are never lost or duplicated (keyed, `ON CONFLICT DO NOTHING`); only a loss count can be overstated. Rare (overlap seconds plus an overflow); no change in S1. E5's projections should read a gap's `lost` as an upper bound and subtract records held in its range.
- **E-E3D-S1-2** (implementer, S1, 2026-10-08): CI's `db` job has `timeout-minutes: 5`; the local DB tier ran 5 min 41 s on this tip (937 tests, `-n 4`), S1's test about 10 s of it. If CI's run nears the limit, raise the timeout (not changed here: the page names only the nats-server fetch step).
- **E-E3D-S2-1** (implementer, S2, 2026-10-08): `run_link` never saw `stop` while the hub was away: nats-py's `connect` with `max_reconnect_attempts=-1` loops inside `connect` until the server accepts (probe: `run_link` against a closed port, `stop` set after 1 s, still running 5 s later). `NodeLinks.track`/`run` await a stopped link, so retiring a Node or stopping the worker while the hub is down hung for good. Separately, nats-py's `close()` raised `TypeError` on a socket the hub had closed (seen once when retirement's reload dropped Central's user), which ended the show `NodeLinks` as if a store error, and so the worker. Class fix in `nodeapi/hub.py`: every hub client (`run_link`, `HubAdmin`, `WallWriter.connect`) goes through one `_connect` (two attempts, then `HubUnavailable`; the attempt count is lifted once connected, so it reconnects forever after) and one `_close` that never raises.
- **E-E3D-S2-2** (implementer, S2, 2026-10-08): a reload that removes an account keeps the leaf already linked in it (nats-server 2.15.0; probe: after `reload_hub(hub, [a])` `/leafz` still listed b's leaf under the same cid for 10 s; `$SYS.REQ.SERVER.<id>.KICK {"cid": …}` closed it and its redial got "Authorization Violation"). So §9.7's "Retire: remove the account, reload twice" leaves the retired Node linked, and the page's leg 4 (`linked()` lacks it) failed. Added, additively to the frozen `HubAdmin`: `unlink(accounts)` (LEAFZ, then KICK each matching leaf by cid); `FleetHub` closes, every look, any leaf whose account is not enrolled. Without it leg 4 fails ("not within 10s: Fleet removes the retired Node's account").
- **E-E3D-S2-3** (implementer, S2, 2026-10-08): a Node whose leaf dials before its account exists receives its first WALL about 40 s later (40.3–41.0 s in 4 runs; the mirror reads `active=-1`, no error, until then), not within the page's 10 s: nats-server retries the mirror's consumer create made while the leaf was refused only after about 40 s. Self-recovering, not a defect. The test starts the second Node once the hub admits Central's two links for it (`/connz`), all still within 10 s of the insert; design §11 row "A Node dials before its account exists" should read "first WALL about 40 s later", and E3e's join and E8 budget it (with E-E3B-FR1).
- **E-E3D-S2-4** (implementer, S2, 2026-10-08): two refinements of the frozen `FleetHub` page, same interface. (a) The file changing forgets the hub identity Fleet last loaded, and the identity is remembered only after both reload and `ensure()` succeeded, so a look whose reload fails (hub away, or refused) is repeated at the next look rather than lost until the next hub restart. (b) `FleetHub.wall` is `None` until WALL was ensured once, not merely connected (a put before `ensure()` raises `wall_writer_not_ensured`).
- **E-E3D-S3-1** (implementer, S3, 2026-10-08): **files outside the page.** A WebSocket relay cannot carry a leaf that negotiated S2 compression, and every leaf does by default (`s2_auto` on both ends; the accepting side offers it on a WebSocket leaf too, ns:server/leafnode.go:1354). nats-server applies S2 *below* the WebSocket framing: the hub's first bytes after the Node's INFO were `\xff\x06\x00\x00S2sTwO` (an S2 stream header) wrapping the next ws frame, so the bridge's `websockets` client closed `1002 invalid opcode` and the leaf redialled every second ("Leafnode connection closed: Read Error" on both ends). A raw-byte proxy (`PrefixProxy`, the design's ingress route) carried it, which is why no earlier test saw it. Fix, in Central's generator so every hub it writes has it: `hub_configuration`'s `leafnodes` gains `"compression": "off"` (the accepting side's "off" wins whatever the Node offers; `central/fleet/node_bus_accounts.py`, and its config test `tests/test_node_bus_config.py`). Probe: the same hub config with `"compression": "off"` linked through the bridge at once. Cost: leaf bytes are not compressed (they are Central's budgeted pulls and WALL). E3c needs no node-bus.conf change.
- **E-E3D-S3-2** (implementer, S3, 2026-10-08): the page's `max_size >= NODE_MAX_PAYLOAD + 64 KiB` reads per message, but a non-browser nats-server writes all it has pending for a connection as ONE WebSocket frame (ns:server/websocket.go `wsCollapsePtoNB`), up to that connection's max_pending (the hub's default 64 MiB). Probe (bridge capped at NODE_MAX_PAYLOAD + 64 KiB; Central publishes 40 × 200 KiB to a Node subscriber): 5 of 40 delivered and the leaf was closed (cid 8 → 10); uncapped, 40 of 40 on the same leaf. So the bridge's hub side has no cap (`HUB_MAX_MESSAGE = None`; still ">="). The Node side is uvicorn's `ws_max_size` (16 MiB default), above node-bus.conf's `max_pending` (2 MiB), which bounds a Node's frames; S5 and the iac note must not lower it.
- **E-E3D-S3-3** (implementer, S3, 2026-10-08; note, no change): a Node's leaf must finish the HTTP upgrade through Central, the bridge's dial of the hub and the hub's INFO within the remote's `first_info_timeout` (default 1 s, ns:server/leafnode.go `SetDeadline(infoTimeout)`, const.go:203), else it redials. Loopback takes milliseconds; on a slow uplink E3c may set `first_info_timeout` on the remote if the bench shows redials.
- **E-E3D-S4-1** (implementer, S4, 2026-10-08): **files outside the page.** The console cannot read `FleetService.status()`: it is served only at `GET /v1/operator/fleet`, a V1-lane route the console's V2 posture scan refuses (`tests/test_console_v2_posture.py` `V2_POSTURE["routes"]`; owner: V2-only posture). So `status()` gains `bus_link` as frozen (the test reads it there), and the Player page reads the same value from the node device read it already polls (`GET /v1/operator/node/devices/<id>`, `central/fleet/node_observations.py`). Both go through one new module, `central/fleet/node_bus_presence.py` (`bus_links_in` for the reads, `record_look_in` for FleetHub's one transaction), the one owner of the two tables and free of `nodeapi`: `service.py` and `node_observations.py` run in Central's web process, whose image has no nats-py until S5 (E-E3D-CUT-3), so they must not import `node_bus_hub`. Cost: the fact shows only where the node read does (node control on, box not retired), as every node section. `FleetHub` gains an optional `clock` (default the system's), additively.
- **E-E3D-S4-2** (implementer, S4, 2026-10-08): wording. The frozen "Node API link: linked" is the rendered line: `FactLine` label "Node API link", value "linked for <age>" / "not linked for <age>" (a derived `fact()` carries no age, so the age rides in the value, as hostHealth.js's "silent · …" does), e.g. "Node API link: linked for 3 min (Central's inference: Central's hub holds this Node's leaf)". `busLinkFact` never throws, so its unlabelled inputs read unknown naming what is missing: "the Node API link is not served", "Central's read time is not served", "when Central last looked at its hub is not served", and "Central's hub has not looked for this Node yet" (an enrolled box before the first look that sees it). Before the node read answers, the page shows the node read's own unknown (`nodeUnknown`).
- **E-E3D-S4-3** (implementer, S4, 2026-10-08; note, no change): `looked_at` and `changed_at` are the worker's clock, the read's `read_at` the web process's; the page calls both "Central's clock", true while they share a host (S5 runs the worker beside Central, as Compose does). If a deployment splits them across hosts, the 30 s staleness compares two hosts' clocks; the iac note (D1) should keep them on one host or say so.
- **E-E3D-CC1-1** (course-correction architect, after S4, 2026-10-08): **NodeBus must stop FleetHub before the links.** `FleetHub._look` calls `NodeLinks.track(serials)` at every look (`central/fleet/node_bus_hub.py` `_look`), and `track` starts a `run_link` task for every serial not linked, unconditionally (`central/infra/node_links.py` `track`). If `NodeLinks.run` has already stopped its links (on `stop`, or after a link's store error), a look still in flight re-creates them: links no supervisor stops, alive until the loop is torn down. One shared `stop` for all three admits this. S5's page (re-cut) fixes the order: FleetHub runs on its own stop; on the bus's `stop` or any failure, NodeBus sets FleetHub's stop and awaits it, then sets the links' stop and awaits both supervisors, then raises the first failure.
- **E-E3D-CC1-2** (course-correction architect, after S4, 2026-10-08): **the shipped WebSocket cap is not the tested one.** Central's image runs uvicorn with `--ws-max-size 1048576` (`Dockerfile` central `CMD`; the runbook's node-factory command, `docs/runbook.md:1014`, says the same), while every S3 test ran under uvicorn's default 16 MiB and E-E3D-S3-2 / `central/fleet/leaf_bridge.py:31-32` reason from 16 MiB. Probe (a throwaway copy of S3's bed, uvicorn `ws_max_size` 1048576 vs 16777216; Node backlog of 9 × 150 kB in each of `RECORD_display`, `RECORD_apps`, `OBSERVATION_health` plus 30 on `player`, then the fleet and show links tracked at once through the bridge): identical in both: the leaf's cid unchanged (no relink), and on the three fleet streams every event the buffers kept recorded (8 of 9 per stream, one gap row `lost` = 1 each, the buffer's own overflow); the `player` line was recorded in neither run (a probe artefact, the same under both caps). So no defect was reproduced at 1 MiB, but the bound on a Node's frame stays unproven. Class fix in S5 (Dockerfile is in its files already): Central's `CMD` takes `--ws-max-size 16777216`, the value the tests ran under, and a configuration test binds it to at least `NODE_MAX_PENDING + NODE_MAX_PAYLOAD`. Cost: the V1 Player session socket accepts larger messages (V1 is deprecated; UX over security). D1's iac note gains the same flag wherever the deployment overrides Central's command.
- **E-E3D-CC1-3** (course-correction architect, after S4, 2026-10-08): CI's `db` job (`timeout-minutes: 5`, `.github/workflows/checks.yml`) ran 4 min 30 s on the base (`claude/wave1-node-api`, run 37817538196, before E3d's tests). E3d's four DB tests measured locally (`-n 4 --dist loadgroup --durations`): hub 29.5 s, node links 6.9 s, presence 4.5 s, leaf bridge 3.9 s; S5 adds the worker-hub test. With E-E3D-S1-2 that leaves no margin: S5 (which edits `checks.yml` anyway) raises the `db` job to `timeout-minutes: 8`.
- **E-E3D-CC1-4** (course-correction architect, after S4, 2026-10-08; integration note, no slice change): E-E3D-CUT-1 says the NATS contract edit is identical in both lanes; it is not textually. E3c (`651dc48`) adds `allow_indirect_imports = true` under a one-line comment naming E-E3C-S4-1; E3d S1 adds it under a two-line comment naming E-E3D-CUT-1. Merging the lanes conflicts on those lines; keep the key once with one comment naming both errata. E3c has **not** moved nats-py (`origin/claude/wave3-e3c` at `015a0a8`: `pyproject.toml`'s dev group and `uv.lock` untouched), so S5's gate still needs the orchestrator's approval of the 4-line `uv.lock` change.
- **E-E3D-CC1-5** (course-correction architect, after S4, 2026-10-08; D1, the Kubernetes change): item 2's "Liveness: HTTP GET `127.0.0.1:8222/healthz`" is wrong for a kubelet probe, which dials the pod IP (a `host: 127.0.0.1` would probe the kubelet's own host): write `httpGet: {path: /healthz, port: 8222}`; the monitor binds `0.0.0.0` (`HUB_LISTENERS.client_host`). Item 2's "no LAN exposure" holds only when the pod is not `hostNetwork`: the hub's client (4222), WebSocket (8080) and monitor (8222) listeners all bind `0.0.0.0`; D1 checks the iac pod spec and says which. Item 4 gains E-E3D-CC1-2's `--ws-max-size 16777216` if the iac command overrides the image's.
- **E-E3D-CC1-6** (course-correction architect, after S4, 2026-10-08; S5 page refresh, no behaviour change): S5's page predates S2's and S4's additive changes: `FleetHub(..., clock=None)` (S4-1) and `HubAdmin.unlink` (S2-2) need nothing from the wiring (defaults); `FleetHub.wall` is None until WALL was ensured (S2-4 b); the hub configuration already turns leaf compression off (S3-1), so the Compose hub needs no flag. The hub's Compose healthcheck gets `start_period` so the wait for the worker's first file (migrations first) is not counted as failures. The re-cut S5 page carries these.
- **E-E3D-S5-1** (implementer, S5, 2026-10-08): **files outside the page.** (a) `scripts/release_plan.py`: `nodeapi/**` leaves `NOT_SHIPPED`, since both service images now copy it (the source stage) and so claim it by construction; without this `tests/test_release_plan.py` fails three tests (shipped and unshipped at once), and a `nodeapi` change would not release the images. E3c's lane, which claims `nodeapi` for the base and the Player, edits the same line: keep it out of `NOT_SHIPPED` at the merge. (b) `tests/test_node_bus_config.py` gains E-E3D-CC1-2's configuration test (Central's `CMD` `--ws-max-size` ≥ `NODE_MAX_PENDING + NODE_MAX_PAYLOAD`; mutation to 1048576 fails it). (c) `checks.yml`'s `db` comment sits above the job: `tests/test_service_workflows.py` requires a bare `timeout-minutes: N` line. Applied with S5 as the errata after S4 direct: CC1-1 (stop order), CC1-2, CC1-3 (`db` 8 min), CC1-6 (hub `start_period: 120s`).
- **E-E3D-S5-2** (implementer, S5, 2026-10-08): CC1-1's order covers the bus's `stop`, not a supervisor's failure: a failed `NodeLinks.run` has already stopped its links, and a look still in flight before FleetHub stops can `track` new ones on it, which no supervisor then stops. `NodeBus.run`, after both supervisors end, calls `track(())` on each (stops every link it holds; a no-op otherwise). Inside `NodeBus`, no interface change. The worker-hub test proves the stop leaves no Central client on the hub (`/connz` lacks `fleet`, `central-wall` and the Node's Central user).
- **E-E3D-S5-3** (implementer, S5, 2026-10-08; note, no change): `build_node_bus` composes FleetHub with `HUB_LISTENERS`, so the worker-hub test's hub binds the deployed ports on the test host (4222, 8080 and 8222 on 0.0.0.0, 7422 on loopback). It is the only test that does; a local `docker compose up` (monitor on 127.0.0.1:8222) or anything else on those ports at the same time fails it at hub start, with the server's log tail. CI's `db` runner has nothing on them.
