# Actual systemd acceptance and recovery — 2026-09-06 Pacific

Status: **five Linux service-integration scenarios passed in 213.18 seconds**.
This closes the direct service-wiring test gap; actual image reboot/rollback,
native health and physical Pi qualification remain separate gates.

The test used the production appliance modules and units from
`1eb16ef9d9815713cab87d70de00a54e62414247`, in a disposable Ubuntu ARM64
container running systemd 255.4-1ubuntu8.17. The retained tooling image was
`sha256:fde06a1b8663d43f349c3cdf828cc9202fd2fe388a5c7fff273926e296537f12`.
It had its own systemd/cgroup namespace, no network and no host mounts.
No Pi image was built locally.

The `opt-in test` installs the production
acceptance and recovery units and uses their normal isolated Python command,
sandbox, `OnFailure` transition, and recovery `ExecCondition`. Before any unit
starts, it replaces only the recovery action with a marker write and verifies
the effective command. It never executes the reboot command. Fresh fixtures
use actual Linux boot identity and real signatures; a previously accepted
fallback is seeded through SlotStore APIs under a historical fixture boot ID.

Executed results:

- Fresh healthy samples promoted the selected trial after **30.338 seconds**
  under the installed acceptance unit.
- Missing health reached the production deadline after **180.137 seconds**.
  The acceptance unit failed and its `OnFailure` transition invoked the recovery
  action only after the real predicate authenticated an active fallback.
- No-active-fallback, common and accepted boot reports skipped the recovery
  action through the actual `ExecCondition` exit handling.
- Failure and skip cases preserved every stored slot, state, identity and cache
  file byte. The healthy case preserved identity and cache while promoting the
  exact candidate. All test-owned state, configuration, units, drop-ins and
  installed module paths were confirmed absent after the suite.

The executed test SHA-256 was
`fd167c2ba4d2fd44be372ba415a4f41ab354758c47c19c9b14cf5cff27c27e32`.
The updater SHA-256 was
`57c2d5a76416bd747c87bbc276fcdc80fba61ead4c53dde4b38754c703d9725b`;
acceptance unit `2c7d193a3668c4ea5a67bfa7dbb8b6ff8192a082184877feceba4604fbb32392`;
recovery unit `6acbe3ca47d50adbe7b62c625f2cd49dfcd79f4c22464f58bebfa6371eb73be7`.

The command inside the prepared disposable container was:

```sh
PHOTO_WALL_TEST_SYSTEMD_UPDATES=1 PYTHONPATH=/work/deps:/work/source \
  python3 -m pytest --noconftest -q -s --tb=short tests/test_systemd_updates.py
```

The explicit environment flag, root/Linux/PID-1/systemd-version checks and
preexisting-resource refusal gate execution. Ordinary development and CI
suites skip these five cases. The root orchestrator and resumed Spark agent
reviewed the test before execution and fixed command argument, reboot-guard,
cleanup-ownership and assertion gaps. Synthetic health and rootfs payloads
qualify the adapters and state transitions; they do not prove native rendering,
SquashFS boot, actual power loss or physical rollback.

## Rerun after verification/health ordering correction

The corrected updater and service limits passed the same five actual Linux
scenarios in a fresh container: **5 passed in 216.70 seconds**. Healthy
promotion took **30.389 seconds**; missing health reached recovery after
**180.2 seconds**. The recovery action remained a verified marker override,
so this is service integration evidence rather than an actual reboot.

The container used the same tooling image and systemd version, private
cgroups, disabled networking and no host mounts. It and its temporary dependency
staging were removed. Retained run record:
`/private/tmp/photo-wall-systemd-updates-rerun-20260906.log`.

The test file hash remained
`fd167c2ba4d2fd44be372ba415a4f41ab354758c47c19c9b14cf5cff27c27e32`.
Corrected updater SHA-256:
`2ea2fe260177ca53c8fa7b443ee64a29c5d5eee5bb3365787fc573b2a01d5ae5`;
acceptance unit:
`1974ad28ca2aa48e554fdc37c2dfcd041ca1f23e3a80df3a052e8ec9e56600e8`;
recovery unit:
`7aae2b0e5d504b93e06249233280f08112fdd20e096241fbb484cb30d2d701ec`.
The [regression record](2026-09-06-vm-rollback.md#verify-before-observing-final-trial-health)
documents the reproduced old-code failure and corrected lock/health ordering.
Bounded independent review found no material blocker in that updater scope.
Hosted image qualification and final strong review remain open.
