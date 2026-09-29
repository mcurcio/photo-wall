# 2026-09-29 Frame deletion reference guard

Central refuses to delete a Frame while saved root Scene definitions or queued
activations still target it. Future Programs using a blocking Scene are included
in `program_ids`. The `409 frame_referenced` response carries sorted
`scene_ids`, `program_ids`, and `queued_activation_ids`; `frame_in_use` retains
its `run_ids`, and the existing `frame_bound` and `unknown_frame` outcomes remain
in place. The route serializes Runtime/Scene mutations with the delete check and
then applies the bound/reference guards within the same database operation.

The Equipment confirmation dialog lists the current saved Scene roots (including
nested child/outro targets) and upcoming Programs using those Scenes. Central
rechecks at confirmation time; if references changed since the displayed
snapshot, the refusal surfaces the current server IDs and tells the operator to
refresh, edit/remove saved Scene and Program references, and review queued
activations. An explicit Scene target for an absent Frame ID remains allowed so
future recovery content can be retained or authored. It cannot execute until a
Frame with an eligible binding exists.

## Local verification

Final portable checks and the earlier full PostgreSQL gate reported:

- Final portable suite: `.venv/bin/python -m pytest -q` — **2558 passed, 969 skipped, 3 warnings** in **183.05 s**.
- Full local Compose PostgreSQL suite: `.venv/bin/python scripts/test_local.py -q` — **3199 passed, 326 skipped, 1 xfailed, 4 warnings** in **488.37 s**.
- Ruff: `.venv/bin/python -m ruff check .` — passed after corrections.
- Documentation check: `python3 scripts/check_docs.py` — checked **105 Markdown files** after corrections.

The full Compose PostgreSQL suite ran before the pure current-time reference
projection and fail-closed Registry boundary corrections. It was not rerun after
those corrections and does not qualify the final tree.

Post-correction focused results:

- Runtime `frame_references` tests — **4 passed**.
- Targeted PostgreSQL tests, `tests/test_operator_frames.py` and
  `tests/test_registry.py` — **50 passed**, one warning, **18.29 s**.
- Two browser tests changed to use the authenticated operator route — **2 passed**.
- Queue-only and mixed-category Chromium deletion cases — **2 passed**.
- Production Vite build — passed.

The browser walkthrough used synthetic equipment and Runtime records. The
stale-ID case supplied a synthetic `409 frame_referenced` response to exercise
the confirmation feedback; the PostgreSQL-backed Central test separately
exercised the backend guard. These checks are local software/database evidence,
not a deployment test. The PostgreSQL full-suite count is explicitly pre-correction;
the targeted PostgreSQL and portable results above are post-correction.

## Qualification limits

No physical Frame, Player, Pi, or display was involved. These results do not
qualify panel output, playback continuity, or a Kubernetes deployment.
