# Source connection discovery and first-run action — 2026-09-29

**Evidence class:** local software and database/browser integration. This change
is part of draft [PR #38](https://github.com/mcurcio/photo-wall/pull/38). It has
not been deployed to the user's Kubernetes installation or checked against its
real Immich account.

The media worker now includes only its configured connection IDs in an
authenticated central media-health projection. The Photo Sources flow can use
that list before any Source has been saved. A null list means the worker has
not reported it; an empty list means the worker has reported no configured
connection. The private URL, owner, API key and CA path remain in the worker.
The list says what the worker loaded, while a Source refresh remains the test
of upstream compatibility, permission and query results.

The Source card displays the outcome of the last refresh in operator terms
while retaining the separate last-successful-refresh history. Its edit form
identifies a removed connection and asks for a current configured choice.

The Wall's first-run guidance now offers **Add first frame**, which opens the
existing measured Frame creation form and focuses its ID field. It uses the
same action as the plan's existing button and leaves the guidance dismissible.

Focused backend PostgreSQL integration (`.venv/bin/python scripts/test_local.py -q
tests/test_media_repository.py tests/test_media_worker.py`) passed **68 tests**,
including projection validation, empty and unreported lists, and a worker
report that contains IDs without a key or hostname. Targeted Ruff and
`git diff --check` passed at that point. Focused console model tests passed
**3 tests**; the Chromium Source-flow module passed **19 tests** (three
warnings); and the production console build passed under the bundled Node 24
runtime. The browser module includes the first failed scheduled-refresh case,
an empty reported worker configuration, a single configured connection, and
an edit whose saved connection was removed. A focused Chromium Wall test for
the first-run action passed (one selected test; 20 deselected); it checked the
opened form and keyboard focus. The console build passed with that action.

The full portable suite passed **2,548 tests**, skipped 919 opt-in cases and
reported three warnings with `UV_CACHE_DIR` and `UV_TOOL_DIR` directed to
writable temporary storage. An initial run without those overrides failed 25
release-planning cases because `uvx` could not read a sandboxed cache path;
the 79-case release-planning module then passed with the corrected cache path
before the complete rerun. The skipped cases include PostgreSQL, Chromium,
Linux appliance and physical-hardware checks; the first two have separate
integration gates.

The local PostgreSQL suite (`UV_CACHE_DIR` and `UV_TOOL_DIR` set to writable
temporary paths, then `.venv/bin/python scripts/test_local.py -q
--disable-warnings`) passed **3,177 tests**, skipped 289 platform/browser or
credential opt-in checks, and recorded one documented expected failure and
four warnings. It exercised the new migration and the worker check-in
projection in the same disposable-schema setup as the remaining central
integration tests.

The first full Chromium run passed 274 cases and found one Source-card copy
failure: an untouched Source showed “Awaiting refresh” twice, making a
single-label assertion ambiguous. The card now shows that state once, and
the exact failing test passed after the console was rebuilt. The complete
Chromium rerun then passed **275 tests** with three warnings. No browser
tests were skipped in that run.

No physical Player, HDMI output or visible timing was exercised by this change.
