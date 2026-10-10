# Real PID1 and Central node lifecycle scenarios

[`tests/test_node_pid1.py`](../../../tests/test_node_pid1.py) (marker `node_pid1`) runs the
actual sealed Player and manager, production base units and effect driver, real HTTP Central
owners and a disposable test database. Each scenario boots a fresh privileged arm64 container
with systemd as PID 1, masks host and reboot actions, and removes only its own containers,
database and archive copies. The [node-pid1 workflow](../../../.github/workflows/node-pid1.yml)
runs the six scenarios as a parallel matrix whenever the release plan finds a node package,
a file the harness imports (Central's Python included) or the scenarios' own paths changed; the
pipeline gate requires it. Every leg boots the one component set and fixture the run's
[node-components workflow](../../../.github/workflows/node-components.yml) built.

The hardware seam is explicit: read-only synthetic CPU serial and DRM inventory,
a separately compiled headless Weston Virtual-1 module, and fixture cohort and
TTY drop-ins. Release/base provenance and fallback qualification are configured
fixtures. The harness does not inject process, app-link, control or Display
witnesses. It cannot certify physical DRM/tty1/HDMI, a serving deployment, rollback
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
PHOTO_WALL_NODE_PID1_FIXTURE="$WORK/fixture" PHOTO_WALL_TEST_REQUIRE_NODE_PID1=1 \
  .venv/bin/python scripts/test_local.py -q -m node_pid1 -k "$SCENARIO" \
  --basetemp "$WORK/run" tests/test_node_pid1.py
```

`REVISION` is the commit the packages were built from (`build-repo.sh` and `build-root.sh` read the committed tree at `HEAD`) and `INPUTS_SHA256` any 64-hex digest naming the inputs (CI passes the roots' cache key from `node_release_writer key`).

`SCENARIO` is `success`, `failure`, `outage`, `reboot`, `refused` or `join`. Put `$WORK` on a volume
with about 10 GB free. Collection alone does not execute privileged Docker work.

## Scenarios, evidence and limits

- **success**: the durable effect order `intent_stop`, `stopped`, `starting_new`, `running`,
  Central's projected `target_running`, the exact selected process/environment and higher
  app/authority epochs.
- **failure**: `intent_stop`, `stopped`, `target_failed`, `fallback_starting`,
  `fallback_running`, back on the previous environment.
- **outage**: once Central has served the target artifact, every node exchange is dropped for
  at least 35 s and until the test has watched the node's own Player unit run the target
  root with no effect yet reported. Afterwards every effect must be reported in order.
- **reboot**: boot A links, then powers off (container removed); boot B, a new bind-mounted
  kernel `boot_id` for the same device and database, enrolls without operator action. A's
  admission is superseded with no live session, A's actual broker credential is refused
  `node_session_superseded` on a real node route, and a stage then completes on B.
- **refused**: a drop-in binds a 2 GiB `/proc/meminfo` (read-only) into the storage unit
  alone, below the smallest memory class. Handoff runs, then storage, prepare, Host Management,
  the broker and the manager supervisor start as one PID1 transaction. Storage must fail;
  prepare, the broker and the manager supervisor must never start (their `Requires=`); Central
  must receive the host facts `boot` report with storage `refused`, fault `node_memory_class`,
  required 3584 MiB and room the fake total, and an observation with `memcg_present` 1 and a
  `memory_peak:` row. The fake is valid for this leg only: the bind gives the unit its own
  mount namespace, so a store it mounted would not reach the host. No app is linked or staged.

Every completed scenario also requires the reported terminal effect to name the exact current
process, epochs and environment, no refused effect report, and no pending control delivery
(nothing held). Every phase captures real cold process linkage and private health publication,
immutable commands, repeated read-only reconciliation predicates, real owner records, final
scheduler health, PID1 properties and journal. Owners are then stopped and all three full
runtime roots are reverified before container deletion. Before intentional teardown, a live
PID1 active/running observation and `/proc` birth ticks must match the exact admitted current
process. Removal is attempted even if diagnostic capture fails; absence is independently
checked. A failure leaves truthful unknown effects; the harness never repairs journals or
forces ACK.

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
