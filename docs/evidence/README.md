# Acceptance evidence

Scenario proof lives in automated tests, not in manual run logs. A behaviour is
qualified when a test that exercises it runs in CI; [validation](../validation.md)
maps each tier (unit, database, browser, images, wall e2e, node PID1) to its
command and authority.

- **Player node lifecycle:** [`tests/test_node_pid1.py`](../../tests/test_node_pid1.py)
  (marker `node_pid1`) boots the sealed node components under real systemd against a
  real Central for the success, failure, Central-outage and reboot/re-enrollment
  scenarios. The [node-pid1 workflow](../../.github/workflows/node-pid1.yml) runs one
  matrix leg per scenario and the pipeline gate requires it when node, fleet or
  contract code changes. How to run it and what each scenario asserts:
  [scenario guide](player-node-handoff-support/node-lifecycle-qualification.md).
- **What CI cannot reach:** physical Pi PXE boot, DRM/HDMI output, visible timing and
  continuity on named equipment. Those still need a dated record here, naming the
  revision, equipment, commands, result and limits. Never record an unexecuted result.

Evidence classes stay separate: **simulated** (deterministic domain, recording
Actuator, protocol and cache faults), **integration** (real database, HTTP service,
packaged launch, declared-version Immich), **appliance** (checksum-identified image
and PXE tree, hosted generic-VM boot) and **physical** (fresh Pi, HDMI, visible
coordination). Keep binary artifacts, credentials and private deployment data out of
Git; the [delivery checklist](../implementation-checklist.md) tracks open acceptance.

## Current records

- [2026-10-01 Player node fix](2026-10-01-node-fix-qualification.md): the four
  real-PID1 scenarios automated, with their first local result and limits.
- [Owned stop operation contract](2026-09-30-node-stop-observation-proposal.md):
  the adapter-owned stop and bounded recovery design the node domain model cites.

The other dated files in this directory record earlier revisions and earlier
architectures ([decision 0006](../decisions/0006-central-authority-and-stateless-players.md)
superseded the durable-Player designs). None of them qualifies the current revision.
Manual run logs, inventories and hashes from the 2026-09-29/30 node work were pruned;
they remain in Git history.
