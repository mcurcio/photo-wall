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

## Reviewed source identity

| File | SHA-256 |
| --- | --- |
| `appliance/updates.py` | `45f534cce9c5940ec147d2f9d8da91429b92f7ee58b410e5c60244ac7b5f9e0a` |
| `scripts/build_ci_image.py` | `ede7258d0eb93a634b284c9220d81479ff524d9787a996491bd4cf66cbe7e9e6` |
| `scripts/build_rollback_candidate.py` | `ba3dd99c4130f5d5eaab9d62f76bfa0e558a65e3f56422afb8247cdb9c6e320f` |
| `scripts/build_vm_initrd.py` | `2d461d99dd775b9d56c97aeb53404c0b27789801a5069a2ebd82d4aa51c2fe2e` |
| `scripts/test_appliance_e2e.py` | `761d9e8b79a2fae74eabb0e16613e62a946d6aefb9af5bd7a4236410576c0273` |
| `scripts/vm_rollback_control.py` | `a51c7c23b5cb21e6994f924b68a604699f30faec65e9397e27892fd926b79416` |
