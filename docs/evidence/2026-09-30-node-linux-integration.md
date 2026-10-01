# Node Linux integration checkpoint — 2026-09-30

This is a working-tree checkpoint, **not completed qualification**. It accompanies
the [node domain contract](../player-node-domain-model.md) and supersedes no dated
physical or released-artifact evidence. The starting tree already contained the
[M1 portable slice](2026-09-30-node-control-m1.md) and uncommitted design changes.
No deployment, real reboot, Pi boot, HDMI measurement, or fleet command activation
has been performed by this lane.

## Implemented source boundaries

- `appliance/node/storage.py` owns root-owned boot/policy binding, a lifetime writer
  lock, bounded JSON, atomic fsync writes and poisoned-write refusal. Separate
  `host_storage.py` and `lifecycle_storage.py` adapt the portable journals.
- HostCore has its own `host_runner.py`, standard-library HTTP/session/clock
  closure and Linux sampler. Reboot request submission is separate from observed
  PID1 `stopping` state. A journaled invocation remains unknown after an ambiguous
  effect and cannot be blindly repeated.
- `process_linux.py` checks the systemd invocation, kernel process birth tick,
  cgroup and root before reporting a running app. Its transient app unit uses a
  read-only private root, explicit IPC/configuration binds, no raw DRM devices,
  separate UID and bounded CPU/memory/tasks/tmpfs.
- `app_link.py` checks Linux credentials on both seqpacket messages around the
  exact observed process, then forwards the signed control-receipt challenge to
  Central. `broker_runner.py` journals positive process evidence in a bounded
  outbox. These facts do not authenticate physical hardware or establish pixels.
- `manager_launcher.py` supervises exact primary/fallback roots independently of
  the manager. An explicitly absent fallback exhausts one primary attempt and
  reports recovery required. `manager_runner.py` and `preparer.py` provide a
  separately packaged, unprivileged staging loop with no app-effect/reboot port.
- `scripts/build_app_environment.py` installs a real `.deb` and its recursive
  dependencies only inside a digest-pinned Docker build root. Authenticated
  snapshot sources and priority 1001 normalize preinstalled packages before
  application installation. Exported roots are sealed with an exact package lock,
  source/build-image provenance, file/link inventory and expanded-byte counts.
- `scripts/build_node_base_deb.py`, `build_node_manager_deb.py`, and
  `build_node_display_deb.py` package separate closures. The image layer accepts
  explicit optional node-base/display `.deb` inputs. `photowall.node=v2` selects
  the node units and excludes the legacy provision/app/watchdog-reboot path.
- The new initramfs branch requests a canonical V2 frozen boot offer, verifies its
  exact base bytes and writes a protected handoff. `node/bootstrap.py` validates
  installed base/display ABI markers, writes owner-specific configurations and
  stages exact roots. Host configuration is written before graphics validation,
  so missing graphics cannot hide the independent host path.

These are implementation claims, not a claim that all paths have completed their
Linux, PostgreSQL, mixed-version or physical acceptance gates.

## Executed checks and failures

- Focused adapter/M1/Debian-declaration run: **67 passed, 2 failed**. The failures
  identified absolute loader-link handling and the expected list of package
  builders. Both were corrected.
- Adapter/M1/Debian/release-plan run: **152 passed, 1 failed**. The remaining failure
  was an expected release-summary string which did not yet name the new sealed
  environment artifact; corrected.
- Corrected focused set: **153 passed**, 34.40 seconds.
- Subsequent adapter/M1/Debian run including the manager executable and explicit
  no-fallback policy: **71 passed**, 1.82 seconds.
- Symlink-policy focused adapter run before the final format/capacity additions:
  **11 passed**, 0.74 seconds.
- Focused Ruff passed after automatic import ordering fixes. Later source edits
  still require the final full lint/test pass.

A current-tree, architecture-independent Player `.deb` was built using Linux
`dpkg-deb` in a disposable container. The first invocation requested arm64 for a
locally resolved amd64 digest and failed with Docker's digest/platform conflict;
retrying with the inspected amd64 platform succeeded. This is package evidence,
not an arm64 runtime claim.

The actual app closure build used
`python@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f`,
platform `linux/amd64`, and Debian snapshot `20260904T000000Z`. It authenticated
repository metadata, downgraded 12 preinstalled packages to the snapshot, installed
422 recursive application dependencies and configured the Player package. It
produced build image
`sha256:eaa2dace892a546abefbfa21b2780f1e572f48a5d64f0c0593eee237d13b5333`.
The build-root maintainer script's `systemctl daemon-reload` reported no systemd
PID1, as expected inside an image build; it did not run against the host root.

Sealing initially refused a Debian directory alias under an overstrict
materialize-all-links policy. The replacement retains validated root-confined
symlinks, forbids writes through archive link parents and creates links last.
A subsequent seal refused `etc/alternatives/awk.1.gz` and an optional locale
configuration link. The final policy separates confinement from existence: safe
in-root dangling links remain exact Debian state, while required entrypoint and
interpreter targets must exist. Alias links such as `X11 -> .` are valid; actual
resolution cycles, escapes and link-parent writes fail. The format is explicit
uncompressed `pw-debian-root-v2`, with regular-file bytes and file/link counts.

The cached final seal completed. The archive is
`2128d7a38a99ac61a06b72253cecca111bcfb661f332afc9c63c8ea53ec4c520.tar`,
984,207,360 bytes; its manifest records **960,908,679 expanded regular bytes,
21,169 regular files and 1,223 symlinks**. The lock contains 509 packages and hashes
`32529190901af6b46ca65ae915b85abef266998fdbd3a23fb75fe1d941e2608d`.
Its input Player package hashes
`16c5a40f57da28d4a18978dfcd362f2a29cd5d09ea3264b7168a43fc8f970d47`.
This fixture predates the latest Player display/linkage changes and lacks the new
native client bridge; it cannot certify those current source bytes.

The first Linux extraction hit unsupported xattr enumeration on Docker Desktop's
host file-sharing filesystem. The validator now treats only ENOTSUP/EOPNOTSUPP as
an unsupported facility and still rejects attributes such as capabilities. Actual
private-root imports reached `/usr/bin/python3`, root-local GI and Player service,
GStreamer 1.26.2 and 259 root-local plugins. Repeat verification then caught font
and GStreamer cache writes in the writable probe, even with the requested numeric
UID on host-shared files. A corrected probe uses an actual Linux volume followed
by a read-only volume mount, matching the launcher's `ProtectSystem=strict`.

## Current Linux integration checkpoint

The corrected earlier streaming/private-root probe completed, with immutable Linux
volume roots rather than host file-sharing semantics. Private `/usr/bin/python3`,
GI, Player imports, GStreamer 1.26.2 and 259 root-local plugins loaded as UID 10004.
Repeat root verification passed after the read-only child exited. The older manager
root's actual executable ran as UID 10003 with a private runtime tmpfs; independent
host-namespace verification then passed (session 48140). That manager package
`2.0+ece687216aa3` predates the current desired-policy loop and is only an entrypoint
and isolation milestone. It does not qualify current manager bytes.

Focused stable adapter/online/boot/package checks completed **104 passed in 2.25s**
(session 43664). Earlier cold/legacy checks completed **178 passed, 1 skipped**;
the skip is Linux-only dpkg on macOS. The extraction suite includes interrupted
stream, source mutation, link escape/cycle/parent and exact reuse/refusal coverage.
No complete repository or physical qualification result is asserted here.

The four slices are now root-level `photowallhostcore.slice`, `photowallbase.slice`,
`photowallapp.slice`, and `photowallpreparation.slice`. Linux's real systemd analyzer
confirmed generated `Slice: -.slice` and `InSlice: -.slice (origin-implicit)` for
all four. This corrects hyphenated names that would have created implicit parent
slices, defeating the intended HostCore isolation. HostCore has MemoryMin 64MiB
and MemoryMax 96MiB independently of the 768MiB base, 2GiB app and 4GiB preparation
caps. This is configuration evidence, not measured hardware pressure behavior.

The online implementation is in `online_broker.py`, `online_runner.py`,
`import_worker.py`, `root_import.py`, and `manager_desired.py`. Separate manager
credentials only download/prepare. The broker owns process effects; its protected
same-boot flock/journal serializes effect intents and irreversible no-effect seals.
A durable stop-intent flag survives outbox delivery. A terminal seal atomically
retains its event, watermark, revalidation identity and per-operation tombstone.
Recovery-only permit receipts never authorize execution. Crash tests cover worker
intent-before-spawn, worker result reuse, stopped-before-start recovery, terminal
seal restart, prior-intent refusal and non-executable receipt recovery. Real composed
PID1 worker/session/credential behavior remains a separate pending fixture.

## Arm64 native-v2 closure milestone

Build session **24563 completed successfully**; no build remains live. Frozen inputs
are in `/private/tmp/photo-wall-node-trial-fixture/source-inputs.json`, SHA-256
`62a7c0ebf26b9dba13958953238199b4acfa26fa63cfbd9a0483ef56b658735c`.
These are explicitly uncommitted working-tree bytes. All **48** Player closure
source files in the sealed root were individually compared to that frozen source
inventory and matched; this is distinct from the sealer's input provenance.

- Player package: `0.13.0+nodeworkingtree.cd79a82cc6df.client897cbd04d206`, arm64,
  SHA-256 `b33eba897343028c69d5c94a24af542b8feda8597b1327f145295f592c4a3fe6`.
- Immutable dependency image:
  `sha256:81102e1a5ad933ac7ff00bc4b297a6a44183cd41446a42970b53cb9ab0deefd8`.
- Sealed archive SHA-256:
  `7057f26cd3ef6dc31c25f58ed9f19cd3dddf4024b6fd6c56956d1dfefffd92f4`;
  **974,028,800 archive bytes; 950,654,412 regular bytes; 21,151 files; 1,259 links**.
- Exact dependency lock:
  `12df8d573f976079c0825799802d0771f016dca0f41eaab4ab5cc0c6f629780b`.
- Sealer/build-source provenance:
  `c023c8be12653026ba76e84fc71f8a053274e7df0a95d4fb66ca86c580205375`.
- Base ABI:
  `node-v2-5134002392bc152123215784b69b6a42a293972038c4b7e2340279e1e0d6f7d9`.
- Graphics ABI:
  `weston14-52e870ad55b28bfd7efb85f83113a98a778abde0c755d35b23326029323482a3`;
  protocol `frame-v2`.
- Private client library:
  `897cbd04d206d5222c4ab7812494538f62d6a505b45d2f32a0f237428425626d`,
  85,848 bytes, exactly matching the sealed manifest.

The display lane subsequently found a late GTK app-ID configure issue and is
changing the compositor and receipt-history behavior. This archive is therefore
a **milestone**, not the final display ABI qualification. The immutable image and
frozen source inventory were handed to that lane for actual GTK verification;
a successor package/root must name the corrected ABI and any changed Player bytes.

## Capacity and publication progress

The store is a bounded tmpfs for the 8GiB class (at least 7GiB reported RAM), with
`cap = min(4GiB, total - 4GiB)`. The 4GiB reservation accounts for app/base/HostCore
and kernel/overlay once. Current MemAvailable is sampled immediately before
allocation; admission additionally preserves 512MiB available headroom. It does
not subtract the complete 4GiB again from MemAvailable, which already excludes
resident old-root and running-process pages. Aggregate filesystem usage is checked
against the store cap. The mount also imposes a 600,000-inode hard bound.

For an old root matching the current 950,654,412-byte app and one 174,519,414-byte
manager root, a same-sized target requires conservative incremental
`2 * 974,028,800 + 268,435,456 = 2,216,493,056` bytes (archive plus root upper bound
plus metadata/inode margin). With those retained roots the total bound is
3,341,666,882 bytes, below 4GiB; an exact already-retained fallback is deduplicated.
Current available memory must independently exceed 2,753,363,968 bytes. Two retained
manager roots still fit this example at 3,516,186,296 bytes. This is a tested policy
equation, not a Pi memory-pressure measurement. Lower real availability refuses
preparation without stop authority. The extractor never retains a second archive
copy: exact-stream hash verification precedes publication of its private root.

Repository-owned `build_node_components.py` builds native/base/manager/Player and
sealed roots from one committed revision using the pinned Debian builder and
snapshot. `node_release_artifacts.py` produces a separate `photowall.node=v2` boot
tree and integrates canonical `manifest.node-v2.json` into the existing packager
and sole release seal. The base-image workflow checks the installed squashfs ABI
against the exact component refs before publishing either cohort tree. The
legacy tree has no V2 flag. CI execution and live TFTP routing are not claimed.
The current shared TFTP map serves one tree; selecting the separately deployed
V2 cohort tree remains an explicit infrastructure deliverable, not an implemented
per-device selector.

First producer/release regression run: **243 passed, 2 failed** (release dependency
ownership expectations). The ownership map and expectation were corrected; rerun 90481 found one further dependency-map expectation; after correcting it,
the complete producer/packager/seal/planner/observation run finished **246 passed
in 27.31s** (session 78460). Both workflow YAML files parsed. Full repository gates remain
outstanding. The next bounded probe exercises real PID1 RootDirectory,
LoadCredential, UID/cgroup identity and worker recovery in a disposable container,
with host-acting boot units masked. It does not exercise reboot or deployment.

Remaining buildable work includes current manager telemetry and artifact refresh,
composed cold/online service fixtures, bounded unreferenced-root collection and
expired pre-stop preparation recovery. Final native/Player artifacts await stable
display ABI. Hardware-only work includes Pi DRM/seat behavior, physical rendering,
continuity/visible timing, pressure qualification and deployed boot routing. D16
bound-app withdrawal and D17 deployment certification remain distinct gates.


## Actual PID1 manager and preparation-worker evidence

The current manager milestone `2.0+a2a2a9ab3837` was sealed in completed session
81047. Its package SHA-256 is
`15baa6883e24bc045bcd968743704298efb0450354fbfb0922951c52aebd83eb`;
archive SHA-256 is
`8f0e62ff229fa2f482534d4ac2fc438cde09786525bd99260ecd07c51d65e906`,
207,114,240 bytes. It contains 200,936,361 regular bytes, 5,777 files and 518 links.
Its dependency lock is
`c162d100e34f72d3e8998d4787e36fa2a342e4334f317834bf010d82b1da511a`;
source provenance is
`b94082c431a6de17d6a947070f7b074dc76e8929991eafd037f9f0fc88f06f71`.
All **23** installed manager source files matched the frozen source inventory,
SHA-256 `c19fb8963b9040f34c498157273e9df63524b6d70df534a6ba4041eade43076d`.
This package includes the real desired-policy loop and durable preparation telemetry.
It retains the milestone graphics ABI; coherent final release refs await frame-v3.

A disposable arm64 container `photo-wall-node-pid1-review` booted actual systemd
as PID1 with a private cgroup namespace and no external network. Before any test
service, host-acting device/module units, legacy application units and reboot/halt
units were confirmed masked and inactive. Its own `/run` tmpfs initially had
`noexec`; extraction there produced 203/EXEC. The corrected fixture invoked the
actual base-owned `mount_storage`, yielding an executable, nosuid,nodev tmpfs of
3,830,824KiB with 600,000 inodes. This is the actual budget derived from that
VM's 8,025,128KiB MemTotal, not a silently unlimited host temporary directory.

A second fixture failure made LoadCredential unavailable even to a minimal service
without RootDirectory. Its cause was Docker's private `/run` mount propagation:
systemd's credential helper moved a mount that other helper namespaces could not
see. Making only this container-owned tmpfs shared resolved the minimal case.
Both speculative launcher mount changes were reverted. The original private `/run`
and credential settings then worked. The standard credential interface is described
by [Debian's systemd.exec documentation](https://manpages.debian.org/trixie/systemd/systemd.exec.5.en.html#CREDENTIALS).
No host mount was shared and no product isolation was weakened.

The actual root check exposed a product bug: after pivot_root, `/proc/PID/root`
can read as `/` despite identifying the sealed root. `process_linux.py` now pins
an O_NOFOLLOW directory descriptor and compares device/inode against the kernel
root capability, rejecting deleted/replaced roots, then resamples PID1 and process
birth ticks. Manager observation uses the same check. The source-corrected base
closure requires a refreshed package before final artifact qualification.

Manager roundtrip session **45346 passed**: real manager executable, UID/GID10003,
RootDirectory, exact prep cgroup and 4GiB cap; canonical session claim/grant,
authenticated desired-policy request and idle observation POST to a typed loopback
fixture server. This peer was **not real Central persistence/admission**. Credential
read was allowed for UID10003 and denied for UID10004; host `/run/photo-wall-node`
and `/run/systemd/private` were absent inside the root. Immutable root verification
passed after PID1 stopped the child.

Worker session 80855 found an InvocationID spelling bug: PID1 reports UUID hex
without hyphens, while the journal retained canonical UUID text. `systemctl_show`
now normalizes at the adapter boundary and rejects malformed identifiers. Portable
checks also reject a same-unit replacement with a different invocation. The first
worker completed despite the observer refusal and its root verified. Corrected
session **90465 passed**: broker store closed/reopened while the real worker was
active, the same process was recovered without duplicate spawn, exact manager-owned
archive bytes became an immutable root/result, and completed recovery did not
respawn. PID1 subsequently confirmed quiescence. The worker's observed limits were
25% CPU, 4GiB memory and 16 tasks in `/photowallpreparation.slice`; the measured
workload used **9.625 CPU seconds and a 1GiB peak**. This does not qualify combined
Pi memory pressure. A synthetic StageCommand drove preparation only; no app stop,
app launch, physical display or reboot authority was exercised.

The container was removed after copying diagnostic logs; no PID1 fixture remains
live. Reusable source is `scripts/node_service_probe.py` with
`tests/node_manager_pid1_probe.py` and `tests/node_worker_pid1_probe.py`. The two
inner scenarios were exercised above; the packaged CLI wrapper still needs an
entire matching final artifact set. Boundary/import/observation tests completed
**60 passed in 1.91s**. Logs remain at `/private/tmp/node-pid1-*-probe.log`,
`/private/tmp/node-pid1-manager-roundtrip.log` and
`/private/tmp/node-pid1-full-journal.log` on this development machine.

## Expired preparation and explicit approval dependencies

The broker now handles expired preparation through canonical
`cancelled_before_stop`, requiring exact old process/epoch/environment, positive
worker/process quiescence and an irreversible durable executor seal. Only Central's
transactional `stage_closed=true` receipt closes the stage; a missing receipt or
GET404 never proves closure. If a permit won the race, the seal remains, a recovery-only
permit is retained, and post-expiry quiescence/revalidation follows the existing
no-effect path. No subsequent mutating effect can run. Focused cancellation,
contract and import checks completed **44 passed in 1.70s** (session 43169).

Automatic approval review rejected two separate changes: adding the privileged
standalone probe invocation to shared CI, and adding automatic unused-root cache
reclamation. Neither was applied. The existing CI artifact build/seal wiring remains;
the new privileged probe is standalone source only. The node continues to refuse
preparation when bounded storage is full. The parent has requested explicit user
authorization for each; no answer is assumed. Candidate reclamation must additionally
prove dedicated tmpfs identity, exact immutable manifest, protected current/fallback/
active references and process/worker quiescence before any removal.

The native lane has now identified mandatory Frame identity across rebinds and
qualified the frame-v3 protocol. All frame-v2 artifacts above remain milestones.
The builder marker is updated to frame-v3; final native/Player/base/manager coherent
artifact assembly waits for that source-frozen handoff.

## Coherent Frame-v3 artifact snapshot

The working-tree snapshot at `/private/tmp/photo-wall-node-frame3-review/source`
produced matching base, native, Player and manager artifacts. It is explicitly dirty
source qualification, not a committed release claim. `source-inputs.json` hashes to
`576f370dac7342270c8b1bd17506f544adb291e2d5ed0e15fa81194f3e194627`;
the captured Git HEAD is context only. The staged private closures matched all 49
Player files, 23 manager files and 107 base source copies. The 112 distinct runtime
input paths still matched the working tree when checked. The native source digest
was independently reconstructed as
`e6f4310155ecc26b3a9805c8a97133b9576c420487e601caef90915cf8ab681b`,
matching the native builder's provenance.

The initial Docker package assembler encountered a local multiarchitecture digest
cache conflict. Assembly continued from the same frozen trees using exact native
build image `sha256:90c772d80b3d8a6c0b733e3fdd4464e14e83bc331dc757d828c0b4b8e57cef8c`.
Session 51335 assembled the three `.deb` files, then sandbox access to Docker's
BuildKit activity metadata failed before dependency resolution. Scoped escalation
allowed session **58040 to complete successfully**, reusing those exact `.deb` bytes.
`build-provenance.json` records the assembler separately from dependency builder
`python@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f`.
Both dependency closures were normalized to authenticated snapshot 20260904T000000Z.

The common base ABI is
`node-v2-ada15cf2de7e1d23a1d4cdc85b2ac5f6ad12bcda7bdb3699a69ed20e0a1bb1f3`;
graphics ABI is
`weston14-3734f476404779775c755b829a6583bcfeb9142a52f3c30a6b15b97709e3f31f`,
plugin ABI `frame-v3`. Native package SHA-256 is
`00731c94838f064a147605fac011739f995d3771f00d7ea783373131fc613ebe`;
app-private client SHA-256 is
`a76f6ae10ffecc7c61242161a22106539056c2337fbf8c5c12ed551ab29120e6`.

Player package SHA-256 is
`52f26603befce9159ae2e73333b0f784903f94f5031552baf9fa93eeae8b887e`;
sealed archive is
`d4b000f43029905b37b1a1948430348ada0374c6714669139123848893bf787a`,
974,039,040 bytes. Its manifest inventories 950,657,521 regular bytes, 21,152 files
and 1,259 links. Its dependency image is
`sha256:461acad6ed3291a485ed7289d3136e5126ec70bd7f85de6c2a372e09a378bf4e`,
lock `9644f9b05e8682b185169252e187a841a62e43bd49c6ee14b080dcedf996f809`,
source provenance `ceb6fa1dce75b22fbb36b0a775ca93fd4084fc02d029ac4553385007f53c523f`.

Manager version `2.0+0410485ed122` package SHA-256 is
`726b8aa2455f00c613685f2a78c8173dadd5feba78f5cc5c6436bfe0e5f98e34`;
sealed archive is
`66c09bdbe4e4101ceae8da2fd95176d0de86591e1e0e6dba10f409fce3602dd5`,
207,114,240 bytes, with 200,936,692 regular bytes, 5,777 files and 518 links.
Its dependency image is
`sha256:b02adfcf90927c7481cc1077d8f2b70243638e4639fc6b5ee358445e6fe7b81a`,
lock `ec708482a321e2f657e9b6b7573abea37f52e0c6ab6db7612d216bcbfbb8bfa4`,
source provenance `086067b260259bbf929edb3d8bc2ca52ce838492098711e3a43b66fa2a20c038`.

The frozen Player tree and dependency image were handed to the display lane for
actual GTK verification. Matching-artifact PID1 fixture assembly is underway;
artifact construction alone does not prove a complete PXE cold boot, real online
stop/start/fallback, physical output, or combined Pi pressure qualification.

## Stable pause checkpoint after Linux unit repairs

At the user's requested pause, the six audited repairs are saved in source:
broker `CAP_CHOWN`; protected root-owned public proof directory with a read-only
app directory bind; app-owned `0700` bounded XDG runtime tmpfs; Weston runtime0700
and the existing logind/PAM/tty launcher pattern; fresh boot-scoped base supervisor
summary plus bounded PID1 HostCore observations; and command-first HostCore polling
with a durable fair delivery cursor and at most two evidence sends per tick.
Acknowledgements are stored only after HTTP200; canonical evidence bytes and
original samples are retained, and the anti-replay journal is not deleted.

The runtime source changes are in `appliance/node/{app_link,broker_runner,process_linux,
base_status,host_linux,host_runner,host_storage,manager_launcher}.py`, the broker and
Display systemd units, and the base/sealed-environment builders. `app_unit_properties`
is the single sandbox definition used by production and the actual unit fixture.
The socket cleanup accepts only finite root-owned initialization states inside the
validated dedicated directory: gid0/mode0700, gid10004/mode0700, or gid10004/mode0660.
PAM may replace unit environment values, so the fixed Weston command explicitly
sets the intended XDG runtime after PAM through `/usr/bin/env`.

Actual probes distinguished three fixture errors from product defects: an attempted
storage-module import outside the private broker closure, sampling the pre-exec
PID1 child, and an incorrect capability bit assertion. The original audit's numeric
mask `0x200004` was wrong: Linux assigns SYS_PTRACE bit19, not SYS_ADMIN bit21.
The original named set was `0x80004`; the repaired process was measured as
**`0x80005`**, exactly CHOWN, DAC_READ_SEARCH and SYS_PTRACE, bracketed by stable
PID1 invocation and kernel process birth. See the primary
[Linux capability definitions](https://github.com/torvalds/linux/blob/master/include/uapi/linux/capability.h).

The real unit probe then exposed one product issue: systemd created the missing
`etc/photo-wall/public.json` bind target in the sealed root. The manifest diff showed
exactly that one added file. The sealer now inventories an explicitly empty0444
mountpoint, accepts an existing exact empty placeholder, and rejects links or
non-placeholder configuration. No deployment data is baked into it. This preserves
the existing app config path and prevents launch from modifying the sealed root.
The format remains `pw-debian-root-v2`; normal manifest/source/ref digests bind the
additional filesystem entry.

Compact manager reseal session **74245 passed** using the existing exact manager
`.deb`; this is a unit-fixture artifact, not a new complete release set. Archive SHA:
`fc692511d4435a62ebbf25514d3d13434df2364b15f049b008e77de28eecca3d`,
207,114,240 bytes. Its source provenance is
`0800032a28168b487ea3d4769fa12b44d7d9cf5a57e0a7abd6fbc169875c50bf`.
The repaired base fixture package identifies
`node-v2-a59e2d5431c02664730320210f5cb5fd1b20ee3810787167c223c6db4661802e`;
fixture image is
`sha256:4ed7bc287bc3c7ae9a13356e4bd2ae0209b87e688c4e5ac9da4efec14e7187b2`.

Final actual restricted-unit session **6335 passed**: exact narrow capabilities;
one retained client with unchanged PID and InvocationID reconnecting after each
of the three socket-initialization crash states; manager UID denied the proof
socket; app XDG owner/mode0700 and writable private runtime; read-only proof directory;
private broker journal and host systemd socket absent from the app root; and exact
post-exit root inventory with **no added, removed, or changed files**. The actual
PAM service retained the intended XDG runtime and mode0700 using a private
pseudo-terminal. This does **not** qualify physical tty1/DRM seat acquisition or
pixels. The test used the real compact sealed Python root and production app
sandbox properties with a fixture client, not the full Player executable.

Focused final session **83877 passed: 71 tests in2.06s**. Cases include1023 retained
reboots plus a new command, full1024 capacity refusal without effect, bounded delivery,
exact lost-response retry/ACK restart/later event, fair cursor, stale supervisor
unknown, unauthorized proof peer, protected directory rejection and explicit config
mountpoint validation. Scoped Ruff and `git diff --check` passed after import-order
cleanup. The full repository suites were intentionally deferred at the requested
pause. No real reboot, physical display effect, deployment or publication occurred.

All owned handles completed and the disposable `photo-wall-six-repair-pid1` container
was confirmed absent. Logs: `/private/tmp/node-six-repair-pid1-v6.log`,
`/private/tmp/photo-wall-six-repair-pid1-probe/journal.log`, and
`/private/tmp/node-six-repair-final-tests.log`. Reusable inner fixture source is
`tests/node_ipc_pid1_probe.py`; outer invocation remains
`/private/tmp/run-node-ipc-probe.py` (standalone, not added to CI).

Next session should run the deferred broad checks, reconcile any canonical renewed
carrier response change with HostCore delivery filtering, then build a newly frozen
coherent base/Player/manager set. The older Frame-v3 artifacts remain pre-repair
milestones; no complete new Player archive, production squashfs/initrd/PXE bundle,
or complete cold/online lifecycle qualification was built at this pause. Explicit
base dependency coverage for PAM/logind should be checked against the production
squashfs; this fixture inherits those dependencies from the Player closure.
Persistent privileged CI wiring and automatic root-cache reclamation remain
unapproved and unapplied.


## Resumed coherent component checkpoint

Resumption revalidated all35 saved Linux source hashes, all49 staged Player and
23 manager source copies, the native builder/source closure and exact native
package/client hashes. The reusable dependency/fixture images were present with
the recorded arm64 identities. CodeGraph's directory existed but the CLI reported
no usable index; ordinary source inspection was used without reindexing.

The production base declaration omitted the PAM/login dependencies required by
its actual Weston unit. Read-only `dpkg-query` against the exact saved arm64
fixture identified `login` as the owner of `/etc/pam.d/login`, `systemd` as the
owner of `/usr/lib/systemd/systemd-logind`, and `libpam-systemd` as the owner of
`pam_systemd.so`. The installed `libpam-systemd` version257.13-1~deb13u1 depends
on exact systemd, libpam-runtime, a system D-Bus provider and systemd-sysv; the
installed login1:4.16.0-2+really2.41.5-0+deb13u1 depends on libpam-modules and
libpam-runtime. The fixture common-session includes pam_systemd. The canonical
node-base declaration now explicitly requires login and libpam-systemd, including
when the production image installer uses `--no-install-recommends`.

Dependency-only changes previously left the base ABI unchanged. A temporary
regression demonstrated this failure against the saved source. The base builder
now hashes the canonical dependency tuple before deriving its ABI/version and
uses that same tuple for Depends. This invalidates app/manager references naming
the older base. After repairing an initially missing test import, the focused
Debian/Linux/HostCore/boot/release suite passed **80 tests in2.46s**; scoped Ruff
with `--no-cache` and whitespace validation passed. The release-artifact test
import ordering was also corrected. Whole-repository results are recorded in the
resumption evidence independently of this leaf.

Scoped filesystem approvals permitted writes in the exact readiness-design
checkout and a task-specific build directory on the Dock volume. Host temporary
storage had only5.2GiB free; Dock had721GiB free and also held Docker's backing
store, whose guest filesystem had859GiB free. Existing artifacts/caches were not
removed. The unchanged production sealer used Dock scratch and full dependency
roots; no compact manager-root substitute was used for the Player.

The frozen dirty-source components are at
`/Volumes/Dock/Temp/photo-wall-node-resume-20260930/components`.
`source-inputs.json` covers908 Git-visible files including documentation, with
SHA256 `d4ffd96d26701a50a7557e8e8a6b698c248aaebba0373514e264d902270ce992`.
This full build-source file hash differs in scope/encoding from the handoff's
canonical non-documentation inventory and is not a commit identifier. Before and
after non-documentation path/hash/mode/link inventories matched during build.

Build session90214 completed, followed by independent reopened-archive/package
verification session59589. The verifier compared every regular/link archive member
with its manifest, full archive size/hash, immutable reference/ABI, lock/source
hashes, installed first-party source copies and the exact empty0444 configuration
mountpoint. It also reopened the actual base `.deb` and compared141 regular
members plus links with its staged payload. `artifact-verification.json`,
`build-provenance.json`, reference files, source manifests and logs remain beside
the binaries in the task scratch directory.

| Component | Verified identity |
|---|---|
| Base ABI | `node-v2-4ddc9a7bc6b10fdd37d9c9141b2f4bda8ea944745467985ef5618dab963f27e3` |
| Base `.deb` | `edb7679d8320e5bb6d11f25366da6f9b9dfc71ad0ab61b9d8dca25da19eff652` |
| Player `.deb` | `a0ddeb816fd84bb041bd243cbd2ca4c4ce0f92e1cdc7ba3a3a73b216f043cc57` |
| Player archive | `f5c9c77ed96ff799c845cc11fe1258cfa8b782d03a87e99e928cfb99597103c8`,974039040 bytes |
| Player lock | `d1f440c5928eb8d5490e68fbcb497a0b5df2a04b7faf3eecd97fe875154be2de` |
| Player source provenance | `afaeb5eca1a1ab68ad6ac656884e2d31fdb268ecd6694f3f07a12dbf0e4e503c` |
| Player dependency image | `sha256:78a46bee8889c8f2f60734f0887cf42f829b7cda28da3ffc36963f79c01c7c9b` |
| Manager `.deb` | `395de493e91d8e6379826a40876a19a5fa026a6e86bfc3abc0d3f0cc65856dbe` |
| Manager archive | `989a15830be43901c60254d5eb030486fe453b8b20e3cf32d985d75f2980b25d`,207114240 bytes |
| Manager lock | `ec708482a321e2f657e9b6b7573abea37f52e0c6ab6db7612d216bcbfbb8bfa4` |
| Manager source provenance | `771cca29c2cc12fff1e266ebe94568c162d775a8ac123471628c4a7861fd313e` |
| Manager dependency image | `sha256:4c9ba3ef99e0aca453320a9fd77d45b3bf8cc85f83fdc42f669779cd06de4f8f` |

Player membership is21153 regular files and1259 links,950657525 regular bytes;
manager membership is5778 regular files and518 links,200936692 regular bytes.
All49 Player and23 manager source copies match the frozen source. Native reuse
was permitted only after reconstructing the exact source digest and checking the
builder/input and package/client hashes; graphics ABI remains
`weston14-3734f476404779775c755b829a6583bcfeb9142a52f3c30a6b15b97709e3f31f`,
frame-v3. Native package/client hashes remain the exact values in the preceding
Frame-v3 checkpoint. Assembly used immutable arm64 image
`sha256:90c772d80b3d8a6c0b733e3fdd4464e14e83bc331dc757d828c0b4b8e57cef8c`;
dependency resolution used the pinned python builder and authenticated Debian
snapshot20260904T000000Z.

This is a coherent full **component** set from dirty source, not a complete new
production release. No new production squashfs, kernel/initrd/PXE bundle,
serving/rollback certification, privileged CI wiring or physical qualification
has been produced by this checkpoint. PAM/logind package declaration is repaired;
the actual production squashfs package contents and physical tty1/DRM session
still need qualification. Actual repaired-unit cold/online/fallback/no-effect
proof against Central remains the next standalone fixture task. Absolute U1
requirements involving graphics/kernel/panel/pre-display failures remain open;
limited app/manager recovery does not satisfy them.


## Actual unit discovery and directory-policy checkpoint

The first composed real PID1/Central cold run exposed `EXDEV` in the production
prepare unit: separately bind-mounted writable downloads and roots directories
made its atomic rename cross a mount boundary. A temporary common-parent mount
proved the diagnosis but is not a production qualification. The implemented fix
preserves separate writable roots and stages each role under its destination's
private `.cold-staging` directory; it removes the unused downloads write grant.
Existing exact-root retry and capacity accounting remain unchanged; no cache
reclamation was added. Focused cold/boot tests passed69 with1 explicit DB skip.

The preserved intermediate full component set is `components-coldfix` under the
task scratch directory. Its base ABI is
`node-v2-254ca39a92b0ff75546edb7c9ef6f8deaa01e1ebcb791980aabcec65ba791049`.
Its actual run65297 completed cold preparation, opened the real PAM session and
started actual Weston Virtual-1, but both restricted runtime units failed200/CHDIR.
The prepare unit's unchanged UMask0077 had made extracted rootfs directories0700.
The broker truthfully retained an unknown-effect reconciliation state; this was
not a successful Player run. Logs remain in `pid1-success-fd745b34`, and its owned
container `photo-wall-resume-success-a0354993` was removed after terminal exit1.

The reviewed production fix normalizes only real directories in unpublished
rootfs trees to the format's0755 mode, using lstat and no-follow traversal/chmod.
The sealer and extractor share this policy; private staging/envelope directories
remain0700, regular file modes and exact link bytes are preserved. Verification
rejects noncanonical existing directory modes and wrong rootfs ownership without
repairing published roots. Newly created empty public configuration placeholders
receive explicit0444 even under a restrictive umask. Tests cover022/077, implicit
and link-only parents, host targets untouched by absolute links, private0600
files, private wrappers,700/777 reuse refusal and wrong rootfs owner. The focused
extractor/release suite passed29 tests in1.87s and scoped Ruff passed. One earlier
command named a nonexistent security test file and exited4 without running tests;
the corrected focused command produced the reported result.

Fresh complete roots are preserved at
`/Volumes/Dock/Temp/photo-wall-node-resume-20260930/components-dirfix`.
The source-inputs manifest SHA256 is
`b3ed51e70fb994bb6c53de1dc3201be23ea6451cbd88a2778e53ec39b2d3a5a4`;
non-documentation inventories before/after the build match. Build41988 and
independent reopened archive/package/source verifier14931 completed successfully.
Manager source closure changed, so its Debian package was freshly rebuilt. The
Player package was reused only after complete staged payload equality; its
version retains the original d4ffd96d2670 source snapshot provenance, while all
new environment references and sealer provenance use this new frozen build.

| Component | Verified new identity |
|---|---|
| Base ABI | `node-v2-dc5cc30128ce4e36526ef77b4ed1042554dece94be488789992c7b379c20ec7b` |
| Base Debian package | `27ca337411438eaee5da4d38d7beee17dbfbab702a32d0e2dcb8a98748c9ad61` |
| Player deb_sha256 | `a0ddeb816fd84bb041bd243cbd2ca4c4ce0f92e1cdc7ba3a3a73b216f043cc57` |
| Player environment_sha256 | `3a818375845d6c9390501404a3ba03cde180fcb40fa77b38e8b47d588b37017e` |
| Player dependency_lock_sha256 | `d1f440c5928eb8d5490e68fbcb497a0b5df2a04b7faf3eecd97fe875154be2de` |
| Player source_snapshot_sha256 | `7a3dd1db5de0ac9887c3d6cd82101894c0f356fa7e313fbb69d945092dc77c0a` |
| Manager deb_sha256 | `3c24c83b119ac959f5b1e50d70809dbc054b6e36cfa442e80858e188d6953a8c` |
| Manager environment_sha256 | `67c4dca5c84cad1934b9a9014fb382eddc4c30f37c5685e84a42e26828b589f3` |
| Manager dependency_lock_sha256 | `352d1d1b9572406577c3fde335f834c2933e539d1feb7678d0fe4abc2bc27f67` |
| Manager source_snapshot_sha256 | `75eeb8284fbed59a783f04cd2acedf9b41b21ec6acb68af5434bd25f136e570d` |

Every full archive regular/link member, immutable reference, package digest,
source provenance, first-party source copy, native bridge and placeholder was
independently compared. Detailed counts and dependency image identities are in
`artifact-verification.json`; all prior component sets remain intact. These are
full component roots, not complete squashfs/PXE artifacts or release certification.
Actual corrected-unit lifecycle proof is pending at this checkpoint.

The standalone runtime fixture uses actual production units, full sealed Player
and manager roots, real Central HTTP/database transactions and actual process and
Display observations. Its headless Weston output name and read-only synthetic
sysfs/CPU serial are explicit hardware fixtures; fallback/deployment qualification
is explicit configured test data. No process witness is invented. The fixture
module is separate from the production native package. It preserves PrivateTmp
and PAM; a prior module path under /var/tmp was hidden by PrivateTmp and was fixed
only in fixture construction by installing that module under /usr/lib. An earlier
Docker FROM digest parser failure was likewise fixture construction, not a product
failure. No physical DRM/tty1/HDMI, actual PXE boot, serving-image certification or
full U1 graphics/kernel/panel/pre-display guarantee follows from these fixtures.


## Actual cold success and stop-observer checkpoint

The directory-policy replay53282 demonstrated real cold preparation, PAM/Weston,
unprivileged manager PID264 and Player PID393, and an actual Central app link
for the exact components-dirfix environment. Its online operation imported the
full target and received a real stop permit. PID1 stopped the original Player,
but the broker emitted `intent_stop` sequence1 then `effect_unknown` sequence2
and retained Central's drain. The active-only process observer had no permitted
way to sample a matching deactivating unit inside the stop wait. This is failed
online qualification, not a successful transition or timeout-based release.
The323.32s attempt exited1; container `photo-wall-resume-success-c2624747` was
removed. Complete observations, journal, failed Central status and credential-free
owner records remain in task scratch `pid1-success-b5b95b66`.

A separate observed `health_storage` fault came from the Player's legacy health
writer: its default `/run/photo-wall/player` parent was absent in the V2 private
run mount. This did not substitute for or invalidate the independently observed
app link. The production process-property authority now supplies a1MiB private
nosuid/nodev/noexec tmpfs at that exact child, UID/GID10004 and0700, retaining the
existing atomic0600 diagnostic writer without exposing a host writable bind.

The reviewed stop-only sampler preserves public active-only process readiness.
It treats matching teardown and unavailable transient samples as pending; retained
PID/birth/invocation/root/cgroup mismatches refuse progress. Terminal proof requires
old process absence, recursive `cgroup.events` populated0 or removed group,
no PID1 job and repeated terminal observations. One stop request is issued under
the original permit; the bounded15s wait neither retries the effect nor extends
authorization. A bounded read-only5s preflight waits for old transient-unit
collection before reuse of its fixed name, including durable failed-exec intent
without an invented RunningApp identity. No force-reset, prior journal repair or
release of the failed attempt's drain was performed.

Focused driver, online-broker and artifact checks passed60 tests in2.10s; scoped
Ruff passed. These cover teardown races, replacement identities including retained
terminal values, recursive population, missing evidence, queued jobs, one stop
syscall, timeout, collection delay and failed-exec fallback intent. An initial
command included a nonexistent IPC test filename and exited4 without tests; the
corrected command produced these reported results. Actual replay remains required.

The next full component set is
`/Volumes/Dock/Temp/photo-wall-node-resume-20260930/components-stopfix`.
Build69900 and independent reopened-archive verifier79180 passed. Frozen source
manifest SHA256 is793834f315daafaee704a5e59fc2a29748417615a02a500cd1bf5182531b9801;
non-documentation before/after inventories match. The unchanged Player package
again passed complete staged payload equality before reuse. Manager was rebuilt
and reproduced its prior3c24c83b package bytes because this leaf does not change
its closure. All environment references were resealed with the new base ABI.

| Component | Verified identity |
|---|---|
| Base ABI | `node-v2-90790aa555a9879a348efab6098d6c65dd31b1b3b833a561bed96f242e51e938` |
| Base Debian package | `a7ea65726afec5550255fccd7a609f969756ed7bf2d6557a00061d27a39c560d` |
| Player environment | `34eeb17b00ba7b2d330beafcecfd2868c6d94a9b45be54fbd130aa64e974d1d7` |
| Manager environment | `4206a47cb9d8fd659cc7f409fd2b44b97e4ee3f2cb4ab5550cc9ab3581ec3e48` |

The independent artifact verification, full refs, frozen source and build
provenance remain beside these binaries. Prior component sets remain preserved.
This checkpoint is still a full-component/local-unit checkpoint, with the same
explicit synthetic hardware, configured qualification and physical/U1 limits.


## Natural real Central discharge and durable standalone harness

The stopfix replay68218 proved real cold operation, private health and ordered
stop/start into the exact target: old PID384 became new PID3853, app and authority
epochs advanced to2, and durable effects were intent_stop, stopped, starting_new,
running. Passive PID1 samples captured the real deactivating stop-sigterm state,
then not-found/no job, then the new active invocation. Its drain nevertheless
remained held until the323.23s test timeout. Scheduler health was good. This is a
failed full reconciliation attempt despite demonstrated local process transition;
all owner state and journal remain in `pid1-success-7a34ecd4`.

The diagnostic replay98360 then **passed naturally in240.95s**, with no manual
reconciliation, ACK or record modification. In `pid1-success-68338f3b`, original
PID376/app_epoch1/authority1 became PID2169/app_epoch2/authority2 using exact target
`e1a8d26ec32ae6b0d6d966354bb4cd60f0d5e809446869e5341e8675dbc5f15f`.
Central recorded `operational_target`, explicitly qualified
`unbound_control_only_not_rollback_acceptance`, and zero active drains. During
reconciliation, every predicate except current receipt freshness was initially
true: one sample showed current applied/issued sequence73 versus link receipt58,
with matching state digest but different nonce/delivery/sequence. The eventual
successful cut had link/current receipt sequence228 equal and every canonical
predicate true. This establishes a timing-sensitive gap, not an unreachable
operation or a reason to weaken receipt freshness. The delegated Central fix is
recorded separately from immutable node component provenance.

The real private health file had UID10004, mode0600, parent0700, a1MiB private
mount, the actual kernel boot and current authority epoch, and no host health file.
There were no health_storage faults. After the successful phase, owners stopped
and all three full runtime roots independently reverified before container
removal. Both attempts' owned containers were confirmed absent. The natural pass
reported1 passed,2 deselected and4 deprecation warnings. These are actual local
PID1/Central results with the previously described synthetic hardware and configured
qualification limits; they do not establish NodeAcceptance or physical U1 claims.

Independent reopened tar-directory verification also checked all canonical0755
directories:2964 in each Player/fixture target and790 in the manager. The report
is task scratch `runtime-directory-verification.json`, separate from complete
regular/link/archive/provenance verification.

The proven harness source is now durable and explicitly opt-in; see the
[standalone lifecycle qualification guide](player-node-handoff-support/node-lifecycle-qualification.md).
Its checked-in probe, Central fixture, inner setup and separate C module preserve
the same real owner paths. Three explicit scenarios collect successfully; scoped
Ruff and documentation links pass. It refuses mutable image tags, checks arm64
identity and the exact embedded base/native packages and fixture source, and has
no default-suite or CI wiring. Build and independent verifier recipes are retained
with that guide. Later Central/test/support source changes are intentionally
reported as a new repository inventory, not relabeled as the source snapshot that
produced the unchanged node packages.


## Final resumption checkpoint: stop ambiguity remains open

The subsequent durable three-phase attempt (handle `99225`) did **not** establish
aggregate qualification. Its success case, scratch `pid1-success-5cbf8bb9`, observed
real old Player PID 414 stop at 23:54:34–35 UTC, but the adapter returned the generic
`stop_outcome_unknown`; the durable ledger contains `intent_stop` then
`effect_unknown`, and no replacement process. The exact underlying exception was
not captured. The next failure case was deliberately interrupted after preserving
evidence; no-effect was not run. The terminal result was exit 2 after 434.48 seconds
(one failed case, four warnings). All owned `photo-wall-resume-*` containers were
independently confirmed absent. This does not invalidate the earlier natural
success run, but exposes an intermittent stop-observation defect still requiring
resolution. No drain was forcibly discharged and no journal was repaired.

The standalone harness now requires exact installed package version/payload binding,
combined lifecycle event order, actual final active/running PID/InvocationID/birth
identity, and immutable first permit/deadlines plus real boot-clock expiry margin.
Cleanup is unconditional even when diagnostic capture fails. An opt-in diagnostic
wrapper preserves the original driver behavior and records the original stop
exception plus bounded existing observation samples. These strengthened assertions
and the wrapper have **not yet been executed** against PID1. Ruff passed and explicit
collection found all three scenarios; collection is not integration qualification.

The full production squashfs was separately converted into an exact imported Docker
image with five fixture files and two fixture directories, without package installs.
The source squashfs SHA-256 is
`a3952fe8c2cb0be37d3617dc0e0a16ef06f5c74490b2fe9945955c70f8c13bca`.
The resulting image ID is
`sha256:424882b698cd09144f557a7c78e21b5ce0a5d1b3b89a8de306b44b7d5a9167cf`.
All **30,554 baseline members** preserve exact type/content/link/mode/ownership/xattr
inventory. The independently reopened saved image's single layer is byte-for-byte
the input tar, SHA-256
`7b58fb81baefb07302ca2ac87dc9feaea76eac379a2bb7515159582016d1cbba`.
Docker's never-started-container export changes `/dev/console`, `/etc/mtab`,
`/etc/hostname` and `/etc/hosts`, and adds `/etc/resolv.conf` and `/.dockerenv`.
These are explicitly reported runtime differences, not changes to the imported
production layer. The initial export comparison correctly failed before this
independent layer verification; no runtime equivalence was silently assumed.
The final report is
`/Volumes/Dock/Temp/photo-wall-node-resume-20260930/pid1-fullroot-image/image-verification.json`.
The [construction attempt](player-node-handoff-support/build-fullroot-fixture-image.txt)
and [final independent verifier](player-node-handoff-support/verify-fullroot-fixture-image.txt)
are preserved. No actual lifecycle scenario has run on this full-root-derived image.

Remaining work preserves the complete scope: diagnose the exact stop failure;
implement the reviewed owned stop Future/operation contract, with transient
observation errors internal to that owner; design and validate bounded base-owned
recovery/reboot when operational control cannot be restored; rebuild all affected
closure/ABI references; replay cold/success/fallback/no-effect with the strengthened
harness, including the full-root-derived image; retain physical PXE/DRM/HDMI and
absolute U1 qualification gaps. The user authorized code/local-test design for
recovery, not actual equipment reboot or deployment. The separate stop-observation
proposal records the architecture direction; no speculative adapter redesign or
reboot implementation was applied at this checkpoint. Privileged CI, D17/guard and
other recorded approval boundaries remain unchanged. No new long run was launched
after the user's credit-limit wrap instruction.
