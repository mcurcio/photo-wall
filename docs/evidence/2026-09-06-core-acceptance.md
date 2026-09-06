# Frame and calendar acceptance review — 2026-09-06

Scope: MVP items 3 and 6, persistent Frame binding/calibration/replacement and
nested Scenes across an accelerated calendar boundary. This is independent
software review and executable PostgreSQL/recording-adapter evidence, not
physical display qualification or the final whole-MVP review.

At source `c5e60733858f7be3c35ed32943a42e0d0460a4ac`, the reviewer ran:

```sh
.venv/bin/python scripts/test_local.py -q --tb=short \
  tests/test_registry.py tests/test_runtime.py tests/test_planner.py \
  tests/test_executor.py tests/test_coordination.py tests/test_central_session.py \
  tests/test_authored_scene.py tests/test_authored_compatibility.py
```

**146 tests passed, no skips, one dependency warning, in 12.34s.** PostgreSQL
checks used the existing private configuration reader and isolated temporary
schemas. No concrete implementation defect was found in the audited scopes.

## Frame and equipment behavior

The checks cover replacement preserving Frame data, calibration revalidation,
retired token/key rejection, concurrent bindings, independent Output revisions,
preview/commit/revert, durable restart and local preview expiry. The earlier
[full wall run](2026-09-05-full-wall.md) separately supplies two-Player,
three-Output service evidence with 410 commitment checks. That run used
simulated Outputs and is not relabeled as a current physical test.

## Combined calendar and recording trace

The existing Runtime test crosses UTC January 1, 2027. Independent December and
January Programs continue under a manually activated priority-10 overlay with
nested portraits. Each portrait retains its exact asset and authored left/right
role; surrounding black is an explicit participant. Pure projection changes
neither Runtime state nor recording output. Applying future output as current
is rejected.

The review executed a combined RecordingActuator trace, now persisted in the
same test. The lamp records only these sampled current winners:

| Seconds from midnight | Owner | Value |
| --- | --- | --- |
| -10 | December | 0 |
| +10 | Overlay | 0.8 |
| +25 | January | 25/60 |
| +30 | January | 0.5 |

Reveal does not replay hidden intermediate cues. January reveals at cycle
position 25; December completes its current cycle at +30. Reapplying the same
current view produces no duplicate command. Separate Executor tests verify
covered-video reveal at position 32.

After persisting the combined trace and extending its final +30 sample, the
root ran:

```sh
.venv/bin/python -m pytest -q tests/test_runtime.py
.venv/bin/python -m ruff check tests/test_runtime.py
python3 scripts/check_docs.py
```

**33 Runtime tests passed in 0.11s**; Ruff, documentation links and whitespace
checks passed. There is no production code change in this test extension.
Recording Actuator tests satisfy the explicitly accepted initial scope; this
record does not establish production Actuator hardware, a networked calendar
presentation, or visible timing.
