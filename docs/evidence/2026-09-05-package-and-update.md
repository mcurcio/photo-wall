# Player packaging and update store benchmark — 2026-09-05

Status: released module benchmark; final appliance assembly and boot remain unqualified. This checkpoint adds the Player-only package builder and signed userspace A/B store to the same draft PR. The implementation was reviewed upward before publication; final cross-module review is still required.

## Player package

`pytest --noconftest tests/test_player_package.py -q` passed **64 tests**. The standalone command avoids importing the unrelated PostgreSQL fixture; every packaging fixture and assertion remains active. Scoped Ruff passed. Tests include exact lock selection, target markers/tags, package/metadata/RECORD boundaries, unsafe paths, corrupted downloads, archive size limits and subprocess timeouts.

A package built from core commit `dda8e98c5c54dc8ca9c007599f8a919eadbd5248` installed offline with `pip --require-hashes` in a fresh CPython 3.12.3 / Linux AArch64 / glibc 2.39 environment. `pip check`, 18 imports, Ed25519 operations and native Pydantic validation passed. Central/media/FastAPI/psycopg/Pillow and build tools were absent from that environment. The 16-wheel bundle is outside Git; its Player wheel SHA-256 is `11987db71bfb853c83b2570097692c1d1c0c0a80b1eaf61ae6c5dc5f06c85575`.

The orchestrator's archive-bound review identified that a completed `git archive` was initially captured before applying its 8 MiB limit. The final helper drains a bounded, timed subprocess and kills/reaps it on overflow or deadline. After that fix, a full offline rebuild produced byte-identical source archive, requirements and wheels; the inventory records the changed builder hash. See [the package module](../module-player-package.md) for exact identities and commands. The final appliance must regenerate its package at the appliance's own committed revision.

## Signed update store

The independent reviewer and orchestrator each ran the updater/Release checks. The final root command `.venv/bin/python -m pytest --noconftest tests/test_updates.py tests/test_release.py -q --tb=short` passed **62 tests in 26.43 seconds**: 41 updater tests and 21 shared Release tests already included in the core suite. Do not add overlapping counts as new coverage.

The tests use actual Ed25519 signatures and local files to cover canonical manifest parsing, ABI/configuration bounds, one-trial consumption, same-boot rejection/fallback, first trial without an active slot, selected-slot protection, failed/truncated/corrupt staging, ownership and symlink checks, state publication interruptions, concurrency and timed health gating. An isolated Ubuntu arm64 smoke also passed the production root-owner checks and `/usr/bin/openssl` 3.0.13 verification, using synthetic payload bytes. See [the update module](../module-appliance-release.md).

Review found and fixed duplicate active/pending release ambiguity. Bootstrap integration review additionally found missing `UpdateError` recovery, a minimal bootstrap contracts package shadowing the full Player package, and absent automatic invocation of the existing mark-good health gate. Those belong to the active builder/bootstrap checkpoint and remain separate from the verified store. No real rootfs mount, boot, physical power cut or HDMI/PXE result is claimed here.
