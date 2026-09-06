# Four-boot rollback gate preparation — 2026-09-06

This is local harness and adapter preparation, not a hosted rollback pass.
The [automated boot contract](../module-appliance-e2e.md) owns the lifecycle.
Physical Pi/PXE, committed native presentation and preservation of a populated
Player media cache remain separate acceptance gates.

The CI builder reuses the already prepared root to produce one signed candidate
with a failing Player service override. It retains the same source revision,
configuration and boot ABI as the primary image. Only an additional SquashFS
compression is required; extraction, package installation and Player packaging
are reused. The candidate remains in the private job deployment directory and
is excluded from image uploads. Both releases use the disposable signing key,
which is removed before boot testing.

The generic test initramfs adds the read-only `photo-wall-ci` 9p share and a
controller service in the root's volatile overlay. It preserves production
initramfs/bootstrap bytes. The controller accepts only a bounded command bound
to the accepted A2 Linux boot ID, delegates staging to the production CLI,
verifies active A and unconsumed pending B, and requests the initial reboot.
It does not select, reject or promote slots. Trial B and the fallback boot
cannot reuse the old command. No guest credentials or Player health are injected.

The host gate requires A1 trial acceptance, accepted A2 after a power cycle,
the signed B trial, a successful boot-bound production recovery predicate,
and actual accepted A3 with the same Player identity and higher authority epoch.
The production recovery service owns the fallback reboot. Kernel crashes,
unexpected boot sequences, stale events, a promoted failure candidate and
unchanged/changed-authority mismatches cannot satisfy that evidence sequence.

## Executed validation

- The integrated PostgreSQL-backed suite passed **924 tests**, with **15 explicit
  platform/opt-in skips** and **four dependency warnings**, in **106.35 seconds**:
  `env PATH=/opt/homebrew/opt/openssl@3/bin:$PATH .venv/bin/python scripts/test_local.py -q --tb=short`.
  Local log: `/private/tmp/photo-wall-rollback-regression.log`.
- After a shared serial-parser cleanup, the final updater, host VM, generic
  initramfs, guest controller and candidate/build checks passed **160 tests**,
  with **one Linux-root-only skip**, in **4.57 seconds**. Local log:
  `/private/tmp/photo-wall-rollback-final-focused.log`. These counts overlap.
- Ruff, documentation links, workflow lint and diff whitespace checks passed.
- Read-only inspection of the retained Linux builder found kernel
  `6.8.0-139-generic`, all required modules, the `9p`/`9pnet`/`netfs` dependency
  closure, and `virtio_gpu`/`virtio_dma_buf`. Actual initramfs scripts place
  `init-bottom` before moving `/run` into the root, preserving the mounted share.
- Actual `qemu-system-aarch64 -device virtio-9p-pci,help` in that builder confirmed
  `fsdev` and `mount_tag` support. This option probe did not start a VM.
- Independent bounded reviews covered candidate generation/signing and the host
  lifecycle/recovery event. Review identified a double-counted fallback deadline;
  fallback boot and enrollment now share 600 seconds. A1's process is explicitly
  restarted for A2; the second process's waits total at most 3,180 seconds within
  its 3,600-second cap, plus bounded command overhead. No remaining material
  finding was reported in those scopes. Final strong review remains open.

The first focused signing test failed because production intentionally uses
`/usr/bin/openssl`, which is LibreSSL on this macOS host. The synthetic test now
uses the installed OpenSSL 3 path, as existing updater fixtures do; production
defaults are unchanged. The subsequent focused runs above passed.

No local full Pi image build, VM launch or physical reboot was performed for
this checkpoint. GitHub Actions must build and execute this new four-boot gate
before `automatic_rollback` can be qualified.

## Reviewed source identity at `3f88b33`

| File | SHA-256 |
| --- | --- |
| `appliance/updates.py` | `45f534cce9c5940ec147d2f9d8da91429b92f7ee58b410e5c60244ac7b5f9e0a` |
| `scripts/build_ci_image.py` | `ede7258d0eb93a634b284c9220d81479ff524d9787a996491bd4cf66cbe7e9e6` |
| `scripts/build_rollback_candidate.py` | `ba3dd99c4130f5d5eaab9d62f76bfa0e558a65e3f56422afb8247cdb9c6e320f` |
| `scripts/build_vm_initrd.py` | `2d461d99dd775b9d56c97aeb53404c0b27789801a5069a2ebd82d4aa51c2fe2e` |
| `scripts/test_appliance_e2e.py` | `761d9e8b79a2fae74eabb0e16613e62a946d6aefb9af5bd7a4236410576c0273` |
| `scripts/vm_rollback_control.py` | `a51c7c23b5cb21e6994f924b68a604699f30faec65e9397e27892fd926b79416` |

## Actual candidate assembly and superseded hosted run

A focused Linux probe used actual `mksquashfs`/`unsquashfs` on a tiny synthetic
root. The 4,096-byte candidate contained the original inventory plus exactly
the 46-byte failure override. Reopening returned the exact override bytes,
and the original root and base bundle were unchanged. Candidate SHA-256 was
`c750ef3e8e8dc56acd5da24f8c8775614d8d20ea09e13faec6b9907f0edb81ea`.
The base payload was synthetic (42 bytes); this was packaging-adapter evidence,
not a full Pi image or boot. Only the probe's temporary directory was removed.

[Hosted run 34027456271](https://github.com/mcurcio/photo-wall/actions/runs/34027456271)
then completed the actual primary image and signed rollback candidate at
source `062c5904503726b64812769a3d9d5a891526af9c`, the PR merge for `3f88b33`.
Assembly took **11m57s**. The pristine cache was a verified hit restored in
**7.68 seconds**, and preparing the second candidate took **155.535 seconds**,
without repeating package installation. Both A/B compressed roots are
672,530,432 bytes, with different hashes:

- A release: `14c9df851a2853f5d7a778cfd4bee068057a4b4a6ddb679a51be44ac072161a8`;
  rootfs: `b98e29eda5bc96d03f2517d4ee954e772ec483e9f43dd59f4d98d0b71bd64081`.
- B release: `a860c6874ed99ce3323374c40ce59c8bca7fb511f22ce15f60c5a1e1354c18e2`;
  rootfs: `acb0e0d0424a90e48fc0c9d3cde3b9faeb14b83ac19b61facb532d6521776350`.

The original disk is 5,906,628,608 bytes, SHA-256
`48a6ca3df43e6d4e4cc265a586876e0ff2e48d56bad4444c2d3701fe0d2ffdc0`.
The run was intentionally canceled after successful assembly because its
acceptance code retained the reproduced stale-health defect below. Cancellation
was confirmed by GitHub; it was not triggered by a polling timeout. No hosted
rollback qualification or downloadable passing image is claimed from this run.
Log: `/private/tmp/photo-wall-ci-34027456271-full.log`.

## Verify before observing final trial health

The preceding [native hosted run](2026-09-05-github-image.md#native-trial-image-failure-at-afff7b1)
enrolled its Player but exhausted the acceptance service's 200-second cap.
That report does not identify the delayed phase. Independent inspection found
that full-slot verification happened after the final fresh health observation,
allowing a long verification to age the sample before promotion.

The new regression test was run against an isolated copy of the old `3f88b33`
updater. It supplied fresh health through the initial 30 seconds, then removed
health during a simulated 240-second verification delay. The old code promoted
the trial: the test failed with `DID NOT RAISE`. The corrected implementation
verifies first, then requires a fresh interval under the same update lock, and
rejects that scenario without changing active state. The old-source probe
directory was cleaned; log: `/private/tmp/photo-wall-old-acceptance-regression.log`.

Full corrected regression: **930 passed / 15 skips / four warnings in 101.06s**
(`scripts/test_local.py -q --tb=short`), log
`/private/tmp/photo-wall-trial-verification-regression.log`. Focused updater,
host evidence and service-fixture checks: **131 passed / six platform or opt-in
skips in 4.94s**, log `/private/tmp/photo-wall-trial-verification-focused.log`.
The owning [boot contract](../module-appliance-e2e.md) records the separate
verification/health bounds and phase diagnostics. The 30-second interval,
180-second health deadline and two-second sample freshness are unchanged.
The [actual Linux systemd rerun](2026-09-06-systemd-updates.md#rerun-after-verificationhealth-ordering-correction)
also passed five scenarios in 216.70 seconds, with 30.389-second healthy
promotion and 180.2-second failed-health recovery. Recovery used the verified
marker override; it was not an actual reboot. Bounded independent updater
review found no material blocker. The hosted gate remains unqualified.

Corrected source identities:

| File | SHA-256 |
| --- | --- |
| `appliance/updates.py` | `2ea2fe260177ca53c8fa7b443ee64a29c5d5eee5bb3365787fc573b2a01d5ae5` |
| `appliance/systemd/accept-trial.service` | `1974ad28ca2aa48e554fdc37c2dfcd041ca1f23e3a80df3a052e8ec9e56600e8` |
| `appliance/systemd/trial-recovery.service` | `7aae2b0e5d504b93e06249233280f08112fdd20e096241fbb484cb30d2d701ec` |
| `scripts/test_appliance_e2e.py` | `4277c1b42265f96108552b0829690fc7320d8febb8b5dd22f4f5369653ef762d` |

## Corrected verification reached health; enrollment deadline failed

[Run 34028634101](https://github.com/mcurcio/photo-wall/actions/runs/34028634101)
at feature `d449103` (actual merge/source
`12f2be9279bc68a60e536bccff820f8bda419ca2`) assembled A and B in **12m09s**,
10:54:23–11:06:32 UTC on 2026-09-06. Its exact-artifact e2e failed
`guest_enrollment_timeout`; cleanup and the original disk checksum passed.
The [sanitized report](https://github.com/mcurcio/photo-wall/actions/runs/34028634101/artifacts/9988218403)
records a durable A trial boot, but zero enrolled Players. All qualification
flags remain false and no downloadable image was published.

For boot `a35464d0-20b0-4bf0-942a-fd643db93672`, the host observed verification
at 11:15:41.278 and the health phase at 11:16:03.777 UTC, approximately 22.5s
apart. The test finished at 11:17:40.914 after the 600-second enrollment
deadline. This establishes that verification reached the health gate; it
does not establish healthy Player startup, trial acceptance or the precise
reason enrollment was delayed. There were no classified failed services,
Python errors, kernel panic or OOM in the retained diagnostic projection.

| Artifact | Identity |
| --- | --- |
| Original disk | 5,906,628,608 bytes; `c9a50b6fc97d88675c0c1d8e7c50521d603c41cd95926cbfe769fbe9490a3e80` |
| A release | `82c26294265175dcee1c26224f4e717576c41229aed616b5c24bd9b3681f308c` |
| A rootfs | `89b397904b33c131c5740c7583b491f33d6fcc41f71680323a35165e3a223f9d` |
| B release | `c31e04f12061b1068a04e102230cddba12c3ced76e029dc5beb96122c8abd190` |
| B rootfs | `85de36829a6830e913e7cf250659a1aa048d7fb2b2e3f013810ef75394a24ae8` |
| Generic initramfs | `479fe86cf86aad51e0f020e1ae850f1ab0f6696c45a39b3bc3be92251a9e1c9d` |

The already queued [26ee139 image run](https://github.com/mcurcio/photo-wall/actions/runs/34029064970)
then started automatically. It has unchanged application/appliance source;
its outcome must be read independently rather than inferred from this failure.
