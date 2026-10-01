# Standalone real PID1 and Central lifecycle qualification

This explicitly invoked local harness runs the actual full sealed Player and
manager, production base units and effect driver, real HTTP Central owners and a
random disposable PostgreSQL schema. It is excluded from the default pytest file
glob and has no CI wiring. Each phase receives a fresh container and schema; the
harness masks host/reboot actions and removes only its own container/schema.
Normal tool permission review is required for its privileged Docker invocation.

The hardware seam is explicit: read-only synthetic CPU serial and DRM inventory,
a separately compiled headless Weston Virtual-1 module, and fixture cohort and
TTY drop-ins. Release/base provenance and fallback qualification are configured
fixtures. The harness does not inject process, app-link, control or Display
witnesses. It cannot certify physical DRM/tty1/HDMI, a serving deployment, rollback
acceptance, PXE boot or the absolute U1 graphics/kernel/panel/pre-display cases.

## Inputs and exact image checks

Use independently verified full components with `components.json`, `app.tar`,
`manager-primary.tar`, their refs and Debian packages; distinct success/failure
fixture archives and refs; and an existing arm64 PID1 fixture image. The image must
be its exact `sha256:` local image ID. Mutable tags and implicit image builds are
refused. Before starting the node units, the harness checks embedded base/native
Debian package hashes against the supplied components, checks the separate C
fixture source hash against the checked-in fixture, and verifies the installed
packages with dpkg. It also compares each installed version and every package-owned
regular-file byte/mode/owner and symlink target/owner to the embedded archive;
shared parent-directory metadata is not attributed to one package. The image must contain the pinned native Weston stack and the
compiled fixture head at the paths used by the supplied build recipe.

The checked-in files are [probe](../../../tests/node_pid1_central_probe.py),
[Central fixture](../../../tests/node_pid1_central_fixture.py),
[inner unit setup](../../../tests/node_pid1_central_inner.py), and
[separate headless module](../../../tests/node_pid1_fixture_head.c).

The following exact local paths identify the 2026-09-30 qualified candidate. They
are evidence artifacts, not a release or a mutable default. Create a new scoped
output directory for a different run; do not delete existing artifacts to make
space.

```sh
export PHOTO_WALL_NODE_COMPONENTS=/Volumes/Dock/Temp/photo-wall-node-resume-20260930/components-stopfix
export PHOTO_WALL_NODE_FIXTURE_TARGETS=/Volumes/Dock/Temp/photo-wall-node-resume-20260930/lifecycle-fixtures-stopfix
export PHOTO_WALL_NODE_PID1_IMAGE="$(cat /Volumes/Dock/Temp/photo-wall-node-resume-20260930/pid1-image-stopfix/image-id)"
export PHOTO_WALL_NODE_QUALIFICATION_WORK=/Volumes/Dock/Temp/photo-wall-node-resume-20260930
export PYTHONPATH="$PWD:$PWD/tests"
.venv/bin/python -B docs/evidence/player-node-handoff-support/run-isolated-db-tests.txt tests/node_pid1_central_probe.py -s
```

Run this from the intended checkout. The database wrapper reads the already
running local Compose database credentials into memory and invokes normal pytest;
it does not create an `.env` file or print credentials. Tests use a random schema
and clean only that schema. Select `-k success`, `-k failure` or `-k noeffect` for a
single phase. Collection alone does not execute privileged Docker work.

## Evidence and limits

Every phase captures real cold process linkage and private health publication,
immutable commands, repeated read-only reconciliation predicates, real owner
records, final scheduler health, PID1 properties and journal. Success assertions
require the actual durable event order, exact selected process/environment and
higher app/authority epochs, and Central's real unbound operational discharge.
No-effect additionally requires response loss after actual permit commit, original
process preservation, immutable permit expiry, sealed journal/revalidation and
fresh local proof. On completed phases, owners are stopped and all three full
runtime roots are reverified before container deletion. Before intentional teardown,
a live PID1 active/running observation and `/proc` birth ticks must match the exact
admitted current process; no-effect also matches the original process. The first
committed permit identity, payload hash and both clock deadlines remain identical
through every later poll, and the final real boot clock exceeds expiry by the
owner's two-second margin. Removal is attempted even if diagnostic capture fails;
absence is independently checked. Failure leaves truthful
unknown effects/drains; the harness never repairs journals or forces ACK/discharge.

The separate fixture configuration token is random and its local file is 0600;
do not publish that file. Predicate diagnostics contain counters and booleans,
not receipt nonce/digest or bearer credentials. Owner evidence contains actual
signed protocol observations and should remain with the local qualification run.

## Preserved reconstruction and verification sources

These dated Python reconstruction recipes are preserved as text so they are not default
pytest/CI entry points. Review their fixed source/output paths before deliberate
reuse; existing output directories fail closed rather than being reclaimed.
The image recipe uses the checked-in C fixture path and verifies the pre-existing
unique local FROM alias against its exact assembler image ID before building.

- [Initial full component reconstruction](build-full-components-initial.txt)
- [Stop/health fixed full components](build-full-components-stopfix.txt)
- [Independent full component verifier](verify-full-components-stopfix.txt)
- [Initial distinct nonrelease target packages](build-lifecycle-targets-initial.txt)
- [Reseal targets for the repaired base ABI](reseal-lifecycle-targets-stopfix.txt)
- [Independent target verifier](verify-lifecycle-targets-stopfix.txt)
- [Pinned PID1 fixture image construction](build-lifecycle-pid1-image-stopfix.txt)
- [Independent canonical directory-mode verifier](verify-runtime-directory-members.txt)

The full source manifest used to build node packages predates the later Central
and harness changes. Node package/closure identities remain those frozen in the
component directory; final repository inventory and later tests are reported
separately in the [Linux evidence](../2026-09-30-node-linux-integration.md) and
[resumption evidence](../2026-09-30-player-node-resumption.md).

## Opt-in stop diagnostic wrapper

Set `PHOTO_WALL_NODE_STOP_DIAGNOSTICS=1` only for a diagnostic run. The separate
[wrapper](../../../tests/node_pid1_stop_diagnostic.py) replaces the fixture broker
entry point while calling the installed production broker and driver unchanged.
It records a bounded ring of the driver's existing systemd samples and, only on
failed stop, the original exception/result plus read-only process/cgroup state.
It does not add retries, alter outcomes, issue effects or change authorization.
These runs are labeled diagnostic fixture composition, not exact-entry-point
qualification. The wrapper's journal marker is `NODE_STOP_DIAGNOSTIC`.

The separately verified full-squashfs-derived fixture construction is preserved as
[construction attempt](build-fullroot-fixture-image.txt) and
[final saved-layer verification](verify-fullroot-fixture-image.txt). The first recipe
retains its conservative failed runtime-export comparison; the second explains and
checks the exact imported layer and declared Docker runtime differences. This image
has not yet run the lifecycle scenarios.
