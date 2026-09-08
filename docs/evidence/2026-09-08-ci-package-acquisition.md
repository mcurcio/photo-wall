# CI package acquisition failures

The branch was clean at `770b55fef1963ddc314d50acc6aa4324c46c1a77` when this
continuation began. It already contained four commits after the supplied
`1f0075ae3fe521ca8db8aa8a2ae98ff2dc42d403` handoff. PR #2 remained draft.

The [software E2E](https://github.com/mcurcio/photo-wall/actions/runs/34197752739)
and [ordinary checks](https://github.com/mcurcio/photo-wall/actions/runs/34197752769)
passed at that branch head. These pull-request jobs used GitHub's synthetic
merge checkout, so they do not replace exact branch-head qualification.

Both attempts of the
[appliance smoke run](https://github.com/mcurcio/photo-wall/actions/runs/34197752740)
failed during `runtime_packages`, before image finalization or VM execution.
Its checkout was `0a1af353e3c02bce6e1661c1b3f0f3f6f48c010c`.

| Measurement | Attempt 1 | Attempt 2 |
|---|---:|---:|
| Verified extraction-cache restore | 7.833 s | 7.843 s |
| APT index acquisition, 41.2 MB | 628 s | 153 s |
| Complete runtime-package phase before failure | 1,543.927 s | 1,068.112 s |
| Failed operation | Package archive download | Package archive download |
| Operation deadline | 900 s | 900 s |

The first archive-download log shows repeated deferred requests to the pinned
`20260905T000000Z` Ubuntu snapshot. The second reaches request 216 without
completing the acquisition phase and contains no deferred-request lines.
The evidence establishes archive acquisition timeouts; it does not identify a
particular DNS, IP-family, proxy, or server fault. An unchanged retry reached
the same download boundary.

The exact public diagnostic ZIPs were downloaded by artifact ID and their bytes
were checked against GitHub's reported SHA-256. Each contains only
`apt-update.log`, `apt-purge.log`, and `apt-download.log`:

| Attempt | Artifact ID | ZIP SHA-256 |
|---|---|---|
| 1 | `10045467769` | `0e96dee1f363a044915ae905f95929756ccc3ddab9848049f326f1e5ac116c52` |
| 2 | `10070024064` | `9d0654a5dabc141f60bebe20a29a3b3d6efdab52e458c5a8a9ba94c9191be71d` |

Both artifacts use the name
`photo-wall-arm64-build-log-0a1af353e3c02bce6e1661c1b3f0f3f6f48c010c`;
the distinct IDs and hashes distinguish the attempts. No VM or Docker-helper
failure report exists for this phase because those services were never started.

For comparison, the earlier successful
[smoke run](https://github.com/mcurcio/photo-wall/actions/runs/34188879506)
at branch head `83f1e6d1b9de4490514cf6e6f94fa4369324ad10` restored extraction
in 7.786 s and installed runtime packages in 143.941 s. It also spent 132.201 s
preparing a rollback candidate that smoke did not execute. These timings support
separate acquisition reliability and scope-aware build work; they do not qualify
the current revision or the full native/cache/reboot/rollback scenarios.

APT is a [CI image-construction dependency](../module-appliance-ci.md), not a
Player updater. Physical Pi PXE enrollment, replacement, dual HDMI, watchdog,
continuity, and visible coordination remain outstanding.

The correction gives CI an optional, bounded archive cache whose contents are
checked against a fresh APT SHA-256 acquisition plan from signed indexes. APT's
normal download and installation still run. Package-only index acquisition
avoids translation, desktop, and command-not-found metadata. The 900-second
download deadline and the image's signature, provenance, clock-health, and dwell
requirements remain in force.

Three actual-APT regressions passed in the existing immutable ARM64 builder
`sha256:fde06a1b8663d43f349c3cdf828cc9202fd2fe388a5c7fff273926e296537f12`
with Ubuntu APT 2.8.3. Its public localhost repository was signed using host GPG;
APT itself verified the signatures inside the network-isolated container. These
checks establish download admission, not Debian package installation:

- The unsafe control demonstrates APT reusing same-size corrupted bytes in its
  final archive directory. The helper removes them and refuses a stale cache
  digest before the ordinary APT download. Package-only acquisition omits the
  advertised auxiliary indexes.
- One archive completes while another remains unavailable. A fresh APT state
  authenticates its own plan, reuses the completed archive without another
  request for it, and downloads the missing archive after service recovery.
- Tampering with a signed package index makes APT update fail, including with
  `APT::Update::Error-Mode=any` enabled.

The workflow repeats these regressions in the exact builder image produced by
that job, generating its signed fixture there. These local results do not yet
establish hosted build-time savings or qualify the final appliance revision.

The retained local `pinned-apt-regression.log` reports three passes in 4.57 s and
has SHA-256
`ee21f3720189db23350886e1e1e1afaf103509033d5f6e44acd495fce4dfb945`.
During integration the portable suite passed 1,198 tests with 201 expected
integration/platform skips, and the PostgreSQL suite passed 1,376 tests with 23
expected platform/opt-in skips. The final focused cache, builder, and workflow
checks separately cover the completion receipt: failed publication preserves
prior cache progress and cannot authorize the immutable completed-cache key.
