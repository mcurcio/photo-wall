# 2026-10-01 Player node fix: real-PID1 scenarios automated, first local result

> **Historical record.** The builders and tools named below (`build_node_components.py`, `build_node_pid1_fixture.py`) were deleted by [0019](../decisions/0019-debian-packaging-with-debhelper.md); the current build is the [Debian packaging module](../module-debian-packaging.md).

Scope: the node fix at `c30025b` (a rebooted Player re-enrolls and supersedes its previous boot;
an app switch converges locally on the latest stage and reports its effects, with no Central
round trip). Its four real-systemd scenarios are now automated tests, not a by-hand procedure:
[`tests/test_node_pid1.py`](../../tests/test_node_pid1.py), marker `node_pid1`, run by the
[node-pid1 workflow](../../.github/workflows/node-pid1.yml) as one matrix leg per scenario and
required by the pipeline gate when a node package, Central's fleet owners or the scenarios'
paths change. What each scenario asserts, how to run it and its limits are in the
[scenario guide](player-node-handoff-support/node-lifecycle-qualification.md).

**No CI run of the node-pid1 job exists yet.** The result below is local: one development Mac
(Apple silicon, Docker Desktop, arm64 containers), the disposable test database.

## Inputs

- Source: `c30025b0696c7504ea18b3a01854c972cd19affe`, clean tree; source manifest SHA-256
  `9b33188fb0300d5a296342467020c0f77e091c9d293a0e1a124160d9cc85afc0`.
- Components (`components.json` `6a0993f8…ab42b`): node-base.deb `74c03309…8d20`,
  node-display.deb `6b28e6f9…ebe87a` (rebuilt from this revision: `appliance/display_host`
  changed), app environment `0a92f94d…343b1`, manager environment `fe0c0d1c…6e5b7b`. Built
  with the dated macOS-compatible component recipe, because `scripts/build_node_components.py`
  needs Linux `dpkg-deb`; the independent archive verifier passed (app 22412 members,
  manager 6295; the manager closure no longer contains `contracts/node_app_link.py`).
- Fixture from `scripts/build_node_pid1_fixture.py`: success target `e9de580c…109a9`, failure
  target `d6eb93cc…c713`, PID1 image `sha256:66c240de14c564f89d6689118dfbca587ce14b0ae2ae8bce7702b1e2174342c7`
  FROM the node-display build image `sha256:3510d9e8…42a4`.

## Result: the CI leg's exact test command, once per scenario

`PHOTO_WALL_TEST_REQUIRE_DATABASE=1 PHOTO_WALL_TEST_REQUIRE_NODE_PID1=1
scripts/test_local.py -q --tb=short -p no:cacheprovider -m node_pid1 -k "$SCENARIO"
--basetemp … tests/test_node_pid1.py`, run sequentially; each passed on its first run.

| Scenario | Result | Wall | Observed |
|---|---|---|---|
| success | passed | 222 s | `intent_stop`, `stopped`, `starting_new`, `running`; `target_running` |
| failure | passed | 180 s | `intent_stop`, `stopped`, `target_failed`, `fallback_starting`, `fallback_running` |
| outage | passed | 169 s | node exchanges dropped for 71 s (227 refused requests); the target ran locally while Central was unreachable and no effect had been reported; all four effects reported after reconnect, none refused |
| reboot | passed | 222 s | boot A superseded with 0 live sessions, its broker credential refused `403 node_session_superseded`; boot B (4 sessions) staged to `target_running` with no operator action |

Every leg removed its own containers (absence confirmed), database clone and archive copies;
1.4 MB of evidence remained. An earlier run of the same scenarios with the pre-automation
harness (outage dropped exchanges at the stage fetch, so the switch finished after reconnect)
also passed all four.

## Limits

Synthetic hardware only: sysfs Virtual-1 and headless Weston in a container. No physical Pi,
PXE boot, DRM/tty1, HDMI output or timing is exercised. The `outage` scenario's window starts
once Central has served the target artifact, because the download itself needs Central.
`scripts/build_node_components.py` and the workflow have not run on a GitHub-hosted
`ubuntu-24.04-arm` runner for this job: the components step is the one base-image runs (5 min
48 s there in run 36922459509) and privileged systemd containers already run there, but the
Linux `host-gateway` route and the `boot_id` bind mount are proven only on Docker Desktop.
