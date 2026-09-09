# Native Player health to trial acceptance — 2026-09-06

Status: **actual native-adapter integration passed**. The test used two real
GTK GLArea surfaces, initialized the production NativeRenderer, and published
health through PlayerService's production `_health`/`_write_health` methods.
The signed SlotStore trial was accepted after **30.437s** of the production
health gate; total fixture duration was **32.322s**. The
[public report](2026-09-06-native-health.json) records the actual Linux boot
identity, synthetic release and exact qualification boundaries.

The fixture establishes a healthy synthetic authority clock sample before
native initialization, proves native capacity and Player health are false,
then drives GLib until both become true. It refreshes synthetic authority
samples while emitting real Player health; it never hand-writes a healthy
report or shortens the production 30-second interval. Identity is actually
generated and persisted with `load_identity`; root-owned temporary state,
boot report, real Ed25519 signatures, OpenSSL verification and actual Linux
boot identity bind promotion. The test does not run networking or the complete
Player process loop. Central configuration/registration and tiny rootfs bytes
are synthetic; no physical display, image boot, systemd transition or reboot
is qualified by this test.

Execution used the retained Ubuntu ARM64 native fixture image
`sha256:8bf573f5fd7e4d09e4f8c0e0ad51e70d2ebb6058d3f15d7f7651284d20249e6d`,
with current Player/contracts/appliance source from `bbd57b6` copied into
`/app` and the verified `d9f656b` Player wheel closure installed offline. Those
production paths are unchanged between these revisions. The container had
no network or host mounts, 1 GiB memory and two CPUs. No Pi image was built.
Native package versions are recorded in the [renderer contract](../module-native-renderer.md).
The optional missing-NumPy OpenGL warning was nonfatal.

Executed source SHA-256 values:

- Test: `804d40e6713aef6c442388f67161f2a7baae63b7432cfc00fe54e290bd1b70cf`
- Player service: `61e269ca78389d2479b375dc1a03c813e7075ae95556f5a0ae487fec4ab581a9`
- Native renderer: `48db5845318230d22d257702f6de7515852371e8a79911ed15fc342951bc5afa`
- Updater: `57c2d5a76416bd747c87bbc276fcdc80fba61ead4c53dde4b38754c703d9725b`

The command inside that prepared container was:

```sh
xvfb-run -a -s '-screen 0 640x480x24' /opt/native-venv/bin/python \
  tests/native/health_smoke.py --report /tmp/native-health-report.json
```

All temporary fixture directories were absent after the run. An independent
source/report audit found no material overclaim or cleanup defect. Actual
full-image native acceptance and physical rollback remain separate gates.

The owned native-health container was identity-checked and removed after its
public report was copied out. No temporary fixture path or native test
process remains active.
