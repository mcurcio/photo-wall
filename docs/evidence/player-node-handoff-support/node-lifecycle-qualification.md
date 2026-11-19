# Real PID1 and Central node lifecycle scenarios

[`tests/test_node_pid1.py`](../../../tests/test_node_pid1.py) (marker `node_pid1`) runs the
actual sealed Player and manager, production base units, real HTTP Central owners with Central's
hub and the worker's bus side, and a disposable test database. Each scenario boots a fresh
privileged arm64 container with systemd as PID 1, masks host actions (and reboot, except on a
boot the scenario reboots over the bus), and removes only its own containers, database and
archive copies. The [node-pid1 workflow](../../../.github/workflows/node-pid1.yml)
runs the seven scenarios as a parallel matrix whenever the release plan finds a node package,
a file the harness imports (Central's Python included) or the scenarios' own paths changed; the
pipeline gate requires it. Every leg boots the one component set and fixture the run's
[node-components workflow](../../../.github/workflows/node-components.yml) built.

The hardware seam is explicit: read-only synthetic CPU serial and DRM inventory,
a separately compiled headless Weston Virtual-1 module, and fixture cohort and
TTY drop-ins. Release/base provenance is a configured fixture. The harness does
not inject process, control or Display witnesses. It cannot certify physical DRM/tty1/HDMI, a serving deployment, rollback
acceptance, PXE boot or the absolute U1 graphics/kernel/panel/pre-display cases.

## Inputs and exact image checks

`tests/node_pid1_fixture/build.sh` takes the run's local repo
([`debian-packaging/build-repo.sh`](../../../debian-packaging/build-repo.sh)'s output) and the
component set the release writer ([`scripts/node_release_writer.py`](../../../scripts/node_release_writer.py))
wrote from it, and writes the nonrelease `success` target (the component Player at a higher
version), the `failure` target (an `equivs` stub Player whose entry exits), both built by
[`debian-packaging/build-root.sh`](../../../debian-packaging/build-root.sh) from the repo and so
sealed for the same ABI, and an arm64 image built FROM the pinned Debian build container with
`photo-wall-node` and the packages it pins installed by name from the repo
([`tests/node_pid1_fixture/Dockerfile`](../../../tests/node_pid1_fixture/Dockerfile)) and the compiled
[headless module](../../../tests/node_pid1_fixture_head.c).
`PHOTO_WALL_NODE_PID1_FIXTURE` names that output; the tests skip without it, and fail instead
under `PHOTO_WALL_TEST_REQUIRE_NODE_PID1=1` (the CI job).

The image must be its exact `sha256:` local image ID; mutable tags are refused. Before starting
the node units, the harness checks embedded base/native Debian package hashes against the
supplied components, checks the C fixture source hash against the checked-in file, and verifies
the installed packages with dpkg. It also compares each installed version and every
package-owned regular-file byte/mode/owner and symlink target/owner to the embedded archive;
shared parent-directory metadata is not attributed to one package.

```sh
docker compose -f tests/integration/compose.test-database.yml up -d --wait
debian-packaging/build-repo.sh --output "$WORK/debs"
mkdir "$WORK/roots"
for pair in photo-wall-player:app photo-wall-app-manager:manager-primary; do
  debian-packaging/build-root.sh --repo "$WORK/debs" --package "${pair%%:*}" \
    --output "$WORK/roots/${pair#*:}.squashfs"
done
.venv/bin/python -m scripts.node_release_writer write --repo "$WORK/debs" --images "$WORK/roots" \
  --output "$WORK/components" --revision "$REVISION" --inputs-sha256 "$INPUTS_SHA256"
tests/node_pid1_fixture/build.sh --repo "$WORK/debs" --components "$WORK/components" --output "$WORK/fixture"
PHOTO_WALL_NATS_SERVER=$(python3 scripts/nats_server.py fetch --dest "$WORK/nats") \
PHOTO_WALL_NODE_PID1_FIXTURE="$WORK/fixture" PHOTO_WALL_TEST_REQUIRE_NODE_PID1=1 \
  .venv/bin/python scripts/test_local.py -q -m node_pid1 -k "$SCENARIO" \
  --basetemp "$WORK/run" tests/test_node_pid1.py
```

`REVISION` is the commit the packages were built from (`build-repo.sh` and `build-root.sh` read the committed tree at `HEAD`) and `INPUTS_SHA256` any 64-hex digest naming the inputs (CI passes the roots' cache key from `node_release_writer key`).

`SCENARIO` is `success`, `failure`, `outage`, `reboot`, `refused`, `join` or `dark`. Put `$WORK` on a volume
with about 10 GB free. Collection alone does not execute privileged Docker work.

## Scenarios, evidence and limits

Every scenario runs with Central's hub and the worker's bus side, its job runtime running
`RebootPi` ([`tests/node_pid1_central_fixture.py`](../../../tests/node_pid1_central_fixture.py)),
and reads only what Central reads of the bus path (decision 0034, S4c): presence on the hub,
the host line, the device's version, its reboot records and its Outputs (`GET /fixture/pi`,
`GET /fixture/host`). The fixture's Central half runs without Docker in
[`tests/test_node_pid1_fixture.py`](../../../tests/test_node_pid1_fixture.py).

- **success**: cold (the roots are image mounts, the bus and its memory reach Central on the host
  line, and three signal range restarts of Weston under the running Player each see the broker
  start it again); the seeded Frame is bound to Virtual-1 and the Output document on the Pi's own
  bus names it, which is all the Display Host hands the Output off by. (The fixture's headless
  pixman Weston gives the Player no frame protocol, so no Player surface exists to hand off; the
  handoff rule is `tests/node/display/test_display_handoff.py`'s.) Install of the success release answers
  "restarting" and reboots the Pi over the bus (one reboot record, delivered). Boot B of the same
  device runs the target root from its next boot offer and its Output document binds the Frame
  again with no operator action; the memory lines hold, an induced bus OOM is reported on the host line, and a
  crashed Player then a crashed Weston leave the Player down (issue #125).
- **failure**: cold, then Install of a target whose entry exits reboots the Pi over the bus; boot
  B's Player exits at start and stays down, and the debug overlay's card reads `app_absent`.
- **outage**: cold and the Frame bound; then Central is unreachable for 35 s (every request
  but the fixture's answers 503 and the hub is stopped): the Player's MainPID, InvocationID and
  launch epoch never change, no unit fails and the Pi's Output document still binds the Frame;
  once Central is back it reads the Pi linked again.
- **reboot**: cold, then the operator's reboot (`POST /v1/operator/node/devices/{id}/reboots`)
  asks the Pi's host over the bus; boot B, a new bind-mounted kernel `boot_id` for the same
  device and database, boots without operator action, its Player runs, and Central lists the
  reboot asked and delivered.
- **dark** (issues #123, #125, #126): the cold Player's open-file limit is lowered just above what
  it holds open, then it is frozen (SIGSTOP), then killed (kill -9). Each time the debug overlay's
  card on Virtual-1 shows within the leg's limit, naming the cause (`app_descriptor_limit`,
  `app_unresponsive`, `app_absent`) and its Pi-clock time, read on `health.sock`; afterwards
  nothing starts the Player again, and a crashed Weston starts nothing either. Central's database
  then holds the judge's verdict from the Pi's `health` line, Virtual-1 dark with `app_absent`.
- **refused**: a drop-in binds a 2 GiB `/proc/meminfo` (read-only) into the storage unit
  alone, below the smallest memory class. Handoff and the Node bus run, then storage, prepare,
  Host Management, the broker and the manager supervisor start as one PID1 transaction. Storage
  must fail; prepare, the broker and the manager supervisor must never start (their `Requires=`);
  Central must read on the host line the facts `boot` report with storage `refused`, fault
  `node_memory_class`, required 3584 MiB and room the fake total, and metrics with
  `memcg_present` 1 and a `memory_peak:` row. The fake is valid for this leg only: the bind gives
  the unit its own mount namespace, so a store it mounted would not reach the host.
- **join**: the Node API whole (E3b design §16.2): the new serial's leaf links through Central's
  origin, a kill -9 of the bus and a reboot each end an epoch Central records with its gap rows,
  and after a hub restart WALL is current on the Node and its leaf relinked.

Before intentional teardown, a live PID1 active/running observation and `/proc` birth ticks must
match the broker's launch record of the running Player. Owners are then stopped and the boot's
roots are reverified before container deletion. A container that rebooted is captured as far as
it still answers. Removal is attempted even if diagnostic capture fails; absence is independently
checked.

The separate fixture configuration token is random and its local file is 0600;
do not publish that file. Predicate diagnostics contain counters and booleans,
not receipt nonce/digest or bearer credentials. Owner evidence contains actual
signed protocol observations and should remain with the local qualification run.

## Opt-in stop diagnostic wrapper

Set `PHOTO_WALL_NODE_STOP_DIAGNOSTICS=1` only for a diagnostic run. The separate
[wrapper](../../../tests/node_pid1_stop_diagnostic.py) replaces the fixture broker
entry point while calling the installed production broker and driver unchanged.
It records a bounded ring of the driver's existing systemd samples and, only on
failed stop, the original exception/result plus read-only process/cgroup state.
It does not add retries, alter outcomes, issue effects or change authorization.
These runs are labeled diagnostic fixture composition, not exact-entry-point
qualification. The wrapper's journal marker is `NODE_STOP_DIAGNOSTIC`.
