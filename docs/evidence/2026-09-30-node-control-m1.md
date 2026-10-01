# Node control M1 software evidence — 2026-09-30

## Revision and scope

These results apply to the uncommitted working tree based on
`0a9b530caae00757ebae66b74b47adb38f1a5fb0`, not a released artifact. The
[dispatch map](../player-fleet-implementation-map.md#node-implementation-dispatch-2026-09-30)
assigns subsequent production integration. The requirements, execution contract,
decision register and node domain model already contained uncommitted review
changes when this implementation began; this record does not attribute those
prior edits to the implementation wave.

The new software slice provides standard-library immutable V2 evidence/environment
contracts, a bounded strict codec, pure per-fact reconciliation, portable HostCore,
cold-start broker, background preparation, persisted manager recovery policy, and
per-Output display admission. The executable
[`scripts/node_control_demo.py`](../../scripts/node_control_demo.py) composes these
with recording adapters. The dispatch map and a non-shipped simulator classification
in the release manifest complete the integration metadata.

The implementation uses ports for protected boot/session context, journal writes,
process observation and compositor observation. No production session verifier,
command ingress, database evidence transaction, Linux effect driver, compositor
adapter or systemd wiring was added. An environment reference is not a built or
verified Debian closure. Local domain admission is not transport authentication.

## Scenario and adversarial checks

The actual demo CLI completed 13 steps: boot with a requested diagnostic; explicitly
injected diagnostic presentation; exact fixture environment cold start; surface
candidate and injected compositor observation; explicit handoff; failed and
superseded background preparation; lost control transport; observed process exit
with snapshot-before-event reconciliation; delayed/duplicate evidence; and separate
reboot admission and injected initiation. Its output declares `evidence_class:
simulated` and `production_effects: false`. A requested backend action alone never
establishes presentation. Neither injected callbacks nor software tests prove
physical pixels.

Forty-six focused tests cover malformed/bounded wire input, exact reference and
identity validation, distinct command/effect roles, owner checks, independent fact
watermarks, snapshot/event reordering, conflicting/deduplicated evidence, explicit
producer admission, reboot expiry/retries/journal failure, ambiguous effects,
manager retry-budget reconstruction, failed persistence, surface renewal/expiry,
Output identity reuse and the composed simulator.

The deferred adversarial review corrected mutation before buffer validation,
stale candidate presentation authorizing handoff, repeated exit reconciliation
regressing to unknown, manager recovery lacking persisted pre-effect fencing,
invalid cross-owner projection construction, and late diagnostic-release
acknowledgments. Each handoff now has a new UUID: even reuse of the same surface and
buffer cannot make an old release acknowledgment complete a later handoff.

## Executed checks

- `uv sync --frozen`: installed the locked development environment successfully;
  no lockfile change.
- `.venv/bin/python -m pytest -q tests/test_node_protocol.py tests/test_node_evidence.py tests/test_node_control_m1.py`:
  **46 passed**, 0.14 seconds on the final behavioral sources.
- `.venv/bin/python scripts/node_control_demo.py`: successful JSON trace, 13
  simulator steps. No external effects.
- `.venv/bin/python -m pytest -q`: **1 failed, 2891 passed, 1194 skipped**, three
  dependency deprecation warnings, 186.48 seconds. The sole failure was the new
  simulator missing an explicit release-manifest classification. It was corrected
  by adding the exact script to `NOT_SHIPPED`; the full suite was not rerun for this
  metadata-only correction. The corrected `.venv/bin/python -m pytest -q tests/test_release_plan.py`
  rerun passed **84 tests** in 23.92 seconds.
- Initial final-source Ruff, documentation links (122 Markdown documents before
  this evidence addition), and `git diff --check` passed. The first sandboxed lint
  attempt could not write its cache; the permitted rerun worked. Initial focused
  tests similarly reported cache-write warnings; the permitted final 46-test run
  had none.

After the release classification and evidence updates, Ruff passed, documentation
links passed across **123 Markdown documents**, and `git diff --check` passed.

The full-suite warnings concern Starlette's AnyIO alias and deprecated WebSockets
interfaces; no warning was reported by the final focused suite.

## Skipped and unqualified checks

The 1194 skips comprise 849 PostgreSQL tests without a test DSN, 317 opt-in browser
tests, 10 opt-in published Player package tests, and 18 platform/tool/immutable-image
checks unavailable on this host. `.venv/bin/python scripts/test_local.py -q` was
**not executed**: this worktree has no `.env` and no
`PHOTO_WALL_TEST_DATABASE_URL`. No database, container service or deployment was
provisioned for these tests.

No physical Pi boot, HDMI output, graphics pressure, timing, package closure launch,
real reboot, network protocol interoperability, mixed-version rollout or rollback
qualification was performed. The production gates remain closed. Existing V1
protocols and T1/T2 routes were not relabeled or activated.
