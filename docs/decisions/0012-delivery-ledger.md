# 0012 netboot base auto-mirror — delivery ledger

Design of record: [0012-netboot-base-auto-mirror.md](0012-netboot-base-auto-mirror.md)
(r8, committed 27dbb52). Frame passed adversarial re-review; two slice-level page
constraints folded into the bead pages (see errata E1/E2).

Verify gate (from repo runbook):
- fast: `.venv/bin/python -m ruff check . && .venv/bin/lint-imports && python3 scripts/check_docs.py && .venv/bin/python -m pytest -q`
- full: `docker compose up -d --wait && .venv/bin/python scripts/test_local.py -q`
- NEVER `uv run` (mutates uv.lock).

| Bead | Intent | Status | SHA / branch | Notes |
|---|---|---|---|---|
| 1 | TRACER: migration 018 + discovery + resolve_base_root + boot assert + select_base_for_serial(conn,serial) + fetch_base + latest-verified/discovered + base-health endpoint + second device follows; compose base volume | **CI-GREEN** | f50738a, ci-fix 62f39b0 | portable-and-postgres + linux-media + software-e2e all pass; E1/E3 applied |
| 2 | server-side rollback + sticky recovery | **CI-GREEN** | 78a3256 (rebuilt 8da167a) | MVP checks + software-e2e pass; review caught sweep fencing a never-served tag (fixed); E2b |
| 3 | empty-state bootstrap + boot re-hydrate | **CI-GREEN** | ede8109 (test-fix 4680f4f) | fail-loud assertion is fail-LOGGED not crash (E4; bead 9 surfaces it) |
| 4 | GC (gc_base_cache, keep-set, eviction_reason) | **CI-GREEN** | ede8109 (test-fix 4680f4f) | poll-tail only; bead 7 must add post-attachment GC trigger (E5) |
| 5 | per-device .deb carrying served tag (F4) | **CI-GREEN** | 4911d9b | new GET /v1/netboot/manifest (E6); review clean (F4 strict, 0010 untouched) |
| 6 | appliance wiring (serial on .deb + post base-health) | landing | — | E7 (manifest returns tag=last_served_tag) + E8 (player posts base-health, not appliance); opt-in PHOTO_WALL_PER_DEVICE_DEB; review clean; E9 docs notes; fires netboot-e2e.yml |
| 7 | attachment surface (set/clear attached_tag) | open | — | |
| 8 | genuine fresh-install e2e | open | — | MockTransport gate CI-runnable; real-GitHub needs a secret |
| 9 | observability + docs | open | — | docs bead |

Budget: per bead 90 min wall / 8 agents; stop-and-report on cap.

## Delivery process (learned on bead 1 — binding for beads 2–9)
- **No local Docker.** This sandbox's Docker VM can't reach container registries,
  so the DB/e2e gate CANNOT run locally — CI is the DB gate. Every bead brief must
  say: verify with ruff/lint-imports/check_docs/pytest-collection + non-DB tests only;
  DB tests must COLLECT clean; CI executes them. Never spawn docker/compose/until-loops.
- **Grep every consumer.** Any bead changing a signature/symbol must grep the WHOLE
  tree (src + all tests) for every call site before reporting — bead 1 wasted a CI
  round on one missed pre-existing test file. List the sweep in the report.
- **CI latency is the floor** (~10–15 min/round: queue + image build + Postgres suite +
  software-e2e). Beads gate on the prior bead's CI-green (serial file netboot_base.py
  can't safely parallelize). Expect ~15–25 min/bead when CI passes first try.
