# Player clock sampling correction — 2026-09-06

Status: reproduced measurement defect corrected and checked in an offline Linux
runtime. This does not establish the cause of the [hosted sustained-health
failure](2026-09-06-vm-media.md#completed-c5e6073-image-result), or qualify native
image rendering/rollback. No full Pi image was built locally.

The previous `poll_state` measured response time through local JSON parsing and
State schema validation. With aligned clocks and no transport delay, injecting
200ms into either local step made uncertainty 200ms and clock health false.
The correction captures UTC/monotonic receipt after the bounded response body
and before parsing. One HTTP implementation still owns status/type/size/JSON
validation; other request callers retain their dictionary return value.

Local parsing remains subject to the existing one-second sample-age and 10ms
clock-drift gates before application. The 100ms uncertainty threshold, updater
verification, 30-second continuous health interval and 180-second health
deadline are unchanged. The Player also samples a fixed `health_reason` with
its existing health conjunction, and the read-only VM observer validates the
optional reason without changing acceptance authority. The [service contract](../module-player-service.md)
owns these behaviors.

## Executed checks

The existing image
`sha256:203daf9af5f7a35d51815d54aed810e2c78c726c91377b5403174717a1e5385e`
ran copied current public source in a disposable container, as user `wall`,
with networking disabled. It built/pulled no image and used no host bind mount.
Source staging excluded private configuration, Git, environments and caches.

**72 Player-service tests passed / one expected PostgreSQL integration skip in
2.67s.** Nineteen added cases cover local processing, transport delay, stale
sample application, clock steps, ordered single-evaluation health reasons,
invalid reasons and actual control-loop/writer/VM-probe integration.

An in-memory replacement of only `poll_state` with the previous `c5e6073`
method made both 200ms local-processing regressions fail: **two expected
failures / 71 deselected in 0.26s**. The correction passes both with zero
uncertainty and healthy mapping. A 200ms body delay still fails with about
200ms uncertainty; parsing or dispatch beyond one second, and a post-receipt
20ms UTC step, still invalidate the sample.

Host/copy SHA-256 values matched:

| File | SHA-256 |
| --- | --- |
| `player/service.py` | `83cee1c83929704b1f30c2e2dd984c1e36cc99f9a13f71d9b926aab43f32436e` |
| `tests/test_player_service.py` | `0c1ad485c83e898eb56ddc77f3b1926f75877384171329ac5bd42a514c267eb8` |
| `scripts/vm_health_probe.py` | `d4f7e36215ce5545c0b880611ec14b141561796603b8466e4a0d91d1adda90a1` |

Pytest 8.3.5, Pydantic 2.11.4, HTTPX 0.28.1, FastAPI 0.115.12,
cryptography 44.0.3, Pillow 12.3.0, psycopg 3.2.9 and websockets 15.0.1
matched `uv.lock`. This was a scoped dependency comparison, not a complete
installed-closure audit. Successful test command inside the copied `/review`:

```sh
PYTHONPATH=/review python -m pytest -q --tb=short \
  -o cache_dir=/tmp/player-clock-pytest tests/test_player_service.py
```

The owned container was removed after both terminal results; no owned volume
remained. Detailed commands and the mutation procedure are retained in
`/private/tmp/photo-wall-player-clock-review.md`. Earlier host attempts were
interrupted or expired during dependency file reads and executed no tests;
they are not passing evidence.

The root's separate observer/host suite passed **97 tests**, and bounded
consumer review found no actionable issue in the closed reason schema or
private-value handling. Ruff, documentation and whitespace checks passed.
Full hosted CI remains required for the new production code, including the
PostgreSQL/real HTTP session skipped in the offline container.
