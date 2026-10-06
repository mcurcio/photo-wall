# Run brief: E1 layered codebase

**Owner go:** 2026-10-06, after PR 46 merged (b277000). Branch `claude/e1-layered-codebase` from `origin/main`.
**Design:** r3 (working state `/Volumes/Dock/tmp/node-redesign/design-r3.md`); decision 0017 records it in bead E1-4.
**Owner answers:** one big-bang move; tests move with their code; integration tests over internal unit tests, and a test may be deleted when moving or maintaining it is too expensive (never the only proof of a functional requirement; list each deletion); delete `tests/node_ipc_pid1_probe.py`; where PR 44's docs conflict with the 2026-10-06 discussion, today's wins.
**Env:** `source ~/.nvm/nvm.sh && nvm use 20` before pytest; `.venv/bin/python`; TMPDIR and scratch under `/Volumes/Dock/tmp`; never write files at the repo root; Linux-only tests in a container with the source mounted read-only and a venv built inside (never bind-mount `.venv`, errata E-ENV-1); revert `uv.lock` if anything rewrites it.

## Ledger

| Bead | Status | Sha | Note |
|---|---|---|---|
| E1-1 | open | | |
| E1-2 | open | | |
| E1-3 | open | | |
| E1-4 | open | | |
| Final verify | open | | |

---

# E1 beads: the layered Node codebase (4 beads, then one verify)

**Status:** bead cut for E1, written 2026-10-06 against the owner's answers of that day. It replaces the bead list in [e1-approach.md](e1-approach.md) §B3 and [refactor-plan.md](refactor-plan.md) §6. Those two files remain evidence only.
**Base:** `main` after PR 46 merges. PR 46's tip is **4c4cc2e**, not the 5b50186 the drafts simulated (see §0).
**Rules:** `~/.claude/skills/implementation-workflow/SKILL.md`. Each bead is green alone, gets one full verify, and carries its own frozen page.
**Owner answers applied:**
1. One big-bang move bead.
2. Tests move with their code in that bead.
3. No numeric test budget, and no new unit tests of internals. E1 is accepted on the existing suites plus the import-contract guards.
4. A unit test that pins internal code may be deleted when moving it costs more than it is worth. Integration and scenario tests are never deleted.

**Evidence:** a fresh re-simulation, called *sim4*, on a git copy of 4c4cc2e. It reused the sim2/sim3 rewrite tool, then applied every fix-up this document names. It lives in this session's scratchpad, which is ephemeral, so every number it produced is copied into the text below.

---

## 0. Where the real tree contradicts the drafts

| # | Draft said | Real tree (measured) | Consequence |
|---|---|---|---|
| C1 | Base 5b50186, 6 node-pid1 legs, an E-RACE-1 triage rule for `unresponsive` | 4c4cc2e removed the `unresponsive` leg. The legs are now `success, failure, outage, reboot, refused` (`tests/test_node_pid1.py:45`, `node-pid1.yml:90`). The inline import string moved from `:361` to `:315`. | The triage rule is obsolete and is dropped. CI needs 5 legs plus display-harness. |
| C2 | Owner list: "handle systemd units, meson install lists, `hashFiles` paths" | **None of them names a moved path.** Units name only `/usr/lib/photo-wall-<launcher>`, and the launcher names do not change. Meson lists only `display_host/overlay/*.py`, which does not move. The 21 `hashFiles` patterns name no moved file. | Zero edits in the move bead. Bead 1 adds guards instead, so a future move fails loudly. |
| C3 | The stage-time string checks are `build_node_base_deb.py:58,60` | There is a third check at `scripts/build_node_manager_deb.py:27` (`manager_effect_import_forbidden`). It has its own dotted deny entries at `:15-16`. | Bead 2 rewrites it. Bead 3 deletes it, together with the other two. |
| C4 | The `node-pid1` suite needs `appliance/{kernel,host,apps,boot}/**` in its paths | **Not needed.** With only the package globs, `test_release_plan` + `test_module_closure` + `test_package_closures` gave **125 passed**. The suite is due through its `node-base-deb` package. | One fewer hot-file edit. |
| C5 | sim3's rewrite scope was "all code" | That scope rewrote `contracts/node_faults.py:7`, which schedules **central-image and media-worker-image**. | The rewrite scope excludes `contracts/` and `player/`. Those two stay byte-identical, and an acceptance criterion holds it. |
| C6 | The test-rig bead extracts about 36 cross-test edges to `tests/support/` | After relocation, only **7 edges cross a directory**. A pytest `pythonpath` line keeps all bare imports resolving in any order. | No rig extraction. The later epic replaces these tests anyway (owner). |
| C7 | The docs bead adds "superseded by 0016" headers | **0015 and 0016 exist only on PR 44** (`claude/player-architecture-doc`, open). They are on neither `main` nor PR 46. A link to 0016 breaks `check_docs`. | Bead 4 depends on PR 44 (Q1). |
| C8 | The PID1 helpers are pinned scenario files | `tests/node_ipc_pid1_probe.py` is **never executed**. No workflow, script or test names it, on this branch or on `origin/main`. Only `.claude/errata.md` mentions it. | The move bead's one deleted test file (Q2). |
| C9 | 6 docs cite the old paths | 7 files: `node-4gb-memory-design.md` (15), `operator-console-ddd.md` (8, not in the drafts), the evidence proposal (5 relative links), `module-design-r8.md` (4), `player-node-domain-model.md` (3), `player-fleet-implementation-map.md` (3), `AGENTS.md` (1) | The 5 relative links must be fixed **in the move bead**: `check_docs` is a static gate and fails without them (measured). |
| C10 | sim3 "mutation-probed the feed fence" | sim3's end tree has no feed-fence contract. | Re-measured on sim4: 13 kept with both fences before the move, 17 kept at the end, and each fence breaks on its probe. |
| C11 | — | The rewrite tool leaves 28 `I001` (import order) ruff errors. | The move bead runs `ruff check --fix`. |
| C12 | `appliance/kernel/**` in `node-manager-deb` | Correct. It also needs `appliance/apps/__init__.py`, because the manager closure reaches `apps.environment`. | Kept as measured. |

---

## 1. Shared facts every bead uses

**Gates** (from AGENTS.md):

| Gate | Command |
|---|---|
| G-static | `.venv/bin/python -m ruff check .` · `.venv/bin/lint-imports` · `python3 scripts/check_docs.py` |
| G-unit | `PHOTO_WALL_TEST_REQUIRE_DATABASE=1 .venv/bin/python -m pytest -q -m "not db and not browser" -n 4 --dist worksteal` |
| G-db | `docker compose -f tests/integration/compose.test-database.yml up -d --wait`, then `.venv/bin/python scripts/test_local.py -q -m db -n 4 --dist loadgroup` |
| G-release | `pytest -q tests/test_release_plan.py tests/test_module_closure.py tests/test_package_closures.py` |
| G-CI | the PR's `pipeline.yml` run |

**macOS baseline.** About 74 failures and 22 errors are environmental on the Mac: AF_UNIX path length, console Node v16, real sockets.
- A bead's local acceptance is therefore "**the same failure set as the base tip on the same machine**", and CI is authoritative.
- The verifier records the base set once, before bead 1.

**Retiring code is never moved:**
- `appliance/node/{__init__,app_link,manager,manager_desired,manager_launcher,manager_observation,manager_runner,preparer,recovery,recovery_linux}.py`
- `appliance/central_session/`
- `appliance/feed.py`, `appliance/feed_socket.py`
- V1 (`app_*`, `linux_app_proof`, `online_activation`, `os_agent`, `provision`)
- stage 1 (`netboot_init`, `bootstrap`, `boot_offer`, `central_post`, `node_boot_handoff`)
- `process_identity`

**Byte-frozen in E1:** `contracts/`, `player/`, `appliance/systemd/`, `appliance/display_host/meson.build`.

**Budget gates** (implementation-workflow §5):
- Per bead: 90 min, 8 agents.
- Feature: about 4.5 h of beads, with a 6 h ceiling and a warning at 4.8 h.
- One fix cycle per gate, then `wip/<bead>` and stop.

---

## Bead E1-1: Guards and deny matching (no moves)

**Goal:** before anything moves, make every failure class of the move fail loudly. That covers stale deny entries, NATS bypass, new feed importers, unlisted root packages, stale PID1 inline imports, vacuous path literals, empty cache keys, and an incomplete meson list.

**Est.:** 40 min, including one full verify. **Depends on:** nothing.

### Frozen page

**1. `scripts/module_closure.py`: deny matching** (the one behaviour change to a build tool). The `compute_closure` signature is unchanged:

```python
def compute_closure(roots: Sequence[str], *, repo: Path, first_party: Sequence[str],
                    forbidden: Sequence[str] = (),
                    third_party: Mapping[str, str] = MappingProxyType({})) -> Closure:
    """... as today, except:
    - A `forbidden` entry matches a reached module that equals it or sits under it as a dotted
      prefix: "appliance.apps" matches "appliance.apps.broker"; "appliance.node.host" does NOT
      match "appliance.node.host_runner". Top-level entries ("player", "gi") behave as today.
    - A dotted entry whose top-level name is first-party must name an existing module or
      package under `repo`, else ClosureError(f"forbidden entry {e} names no module under {repo}").
    - A crossing raises ClosureError(f"{importer} imports {name}: {entry} is forbidden here")."""
```

- **Where:** `:172` (crossings), `:174-178` (message). Prior art is `sim2/repair.py`, 13 lines.
- The `ClosurePolicy.forbidden` comment (`:52`) becomes `# top-level names, or dotted first-party modules/packages`.

**2. `pyproject.toml`: two fences**, appended after the existing Node contracts. The text is exact:

```toml
[[tool.importlinter.contracts]]
# The Node API library (nodeapi, created by E3) is the one NATS client (node redesign r3 §3.3.4).
name = "Only the Node API library talks to NATS"
type = "forbidden"
source_modules = ["central", "contracts", "media", "player", "appliance", "uplink"]
forbidden_modules = ["nats"]

[[tool.importlinter.contracts]]
# Retiring (r3 §16.1 B2b/B10a): the feed gains no importer; each line leaves with its importer.
name = "The feed is retiring: it gains no importer"
type = "protected"
protected_modules = ["appliance.feed", "appliance.feed_socket"]
allowed_importers = ["appliance.feed", "appliance.feed_socket"]
ignore_imports = [
  "appliance.display_host.runner -> appliance.feed",
  "appliance.display_host.runner -> appliance.feed_socket",
  "appliance.health.judge -> appliance.feed",
  "appliance.health.runner -> appliance.feed",
  "appliance.health.runner -> appliance.feed_socket",
  "appliance.node.app_link -> appliance.feed",
  "appliance.node.broker_runner -> appliance.feed",
  "appliance.node.broker_runner -> appliance.feed_socket",
  "appliance.node.probe_channel -> appliance.feed",
]
```

**3. `tests/test_import_contracts.py`: additions.** The existing three tests stay unchanged.

```python
NATS_CONTRACT: Final = "Only the Node API library talks to NATS"
FEED_CONTRACT: Final = "The feed is retiring: it gains no importer"
NODE_API_PACKAGE: Final = "nodeapi"
FROZEN_FEED_EXEMPTIONS: Final[frozenset[str]]       # exactly the 9 lines above
def test_root_packages_are_every_first_party_package() -> None: ...
    # set(root_packages) == set(scripts.module_closure.first_party_packages(REPO))
def test_the_nats_fence_covers_every_root_package_but_the_node_api() -> None: ...
    # set(source_modules) == set(root_packages) - {NODE_API_PACKAGE}; forbidden == ["nats"]
def test_feed_exemptions_only_shrink() -> None: ...
```

**4. `tests/test_staged_launcher_imports.py`** (new). This is the PID1 inline-string guard:

```python
@dataclass(frozen=True, slots=True)
class StagedImportSite:
    path: str                  # repo-relative
    line: int
    launcher: str              # <name> of sys.path.insert(0, "/usr/lib/photo-wall-<name>")
    modules: frozenset[str]    # appliance.* modules imported; `from P import n` resolves to P.n
def launcher_policies() -> Mapping[str, ClosurePolicy]: ...
    # build_node_base_deb.POLICIES | {"node-manager": build_node_manager_deb.POLICY} | module_closure.POLICIES
def staged_import_sites(path: Path) -> Iterator[StagedImportSite]: ...
    # a MODULE-LEVEL sys.path.insert statement (the file's own imports), or a str/bytes constant
    # that ast-parses and contains one (decode bytes with errors="replace")
def test_the_sites_are_found() -> None: ...                       # >= 10 sites across tests/ and scripts/
def test_every_site_names_a_known_launcher(site: StagedImportSite) -> None: ...
def test_every_staged_import_is_in_its_launchers_closure(site: StagedImportSite) -> None: ...
```

- **Measured on today's tree:** 12 sites and 0 misses, in 9 files:
  - `scripts/`: `build_bootstrapper_deb` ×2, `node_service_probe`, `os_agent_service_probe`, `player_start_probe`;
  - `tests/`: `node_ipc_pid1_probe` ×2, `node_manager_pid1_probe`, `node_pid1_central_inner`, `node_pid1_stop_diagnostic`, `node_worker_pid1_probe`, `test_node_pid1` (the inline string).
- After E1-2 deletes `node_ipc_pid1_probe` there are 10 sites, hence the floor of 10. The same count and 0 misses hold on the moved tree.
- A "site" covers the whole file only when the insert is a module-level statement. Otherwise `scripts/player_start_probe.py` gives a false miss (measured).

**5. `tests/test_named_paths.py`** (new). These are existence guards: the cure for vacuous negative assertions and empty cache keys.

```python
KNOWN_ABSENT: Final[frozenset[str]] = frozenset({
    "appliance/build.py", "appliance/updates.py", "appliance/sub/provision.py"})  # deliberate fixtures
def appliance_path_literals(path: Path) -> Iterator[tuple[int, str]]: ...
    # every match of r"(?<![\w.])(appliance/(?:[a-z_]+/)*[a-z_]+\.py)\b" in a str constant,
    # INCLUDING after a "/" (e.g. "usr/lib/photo-wall-host-core/appliance/node/broker.py")
def test_every_appliance_path_literal_in_tests_and_scripts_names_a_tracked_file() -> None: ...
def test_every_hashed_pattern_in_a_workflow_matches_a_tracked_file() -> None: ...   # via release_plan.matches
def test_the_overlay_install_list_is_every_overlay_module() -> None: ...
    # meson.build install_data('overlay/*.py' ...) == tracked appliance/display_host/overlay/*.py
```

- **Measured:** 75 literals, of which 7 occurrences of the 3 `KNOWN_ABSENT` paths. All 21 `hashFiles` patterns match. Meson lists 6 files, and 6 are tracked.

### Files touched

| File | Change |
|---|---|
| `scripts/module_closure.py` | modified |
| `tests/test_module_closure.py` | 3 cases added to the existing conformance suite of the closure tool: a dotted entry catches a two-hop chain; it does not match a sibling that shares its prefix; an entry naming no module is refused |
| `pyproject.toml` | modified |
| `tests/test_import_contracts.py` | modified |
| `tests/test_staged_launcher_imports.py` | new |
| `tests/test_named_paths.py` | new |

The release scheduler releases every deb plus the base bundle, because `module_closure.py` is a `_DEB_BUILD` input. Every closure and the bytes of every closure are unchanged.

### Acceptance, each with its proving gate and one mutation probe

Restore every probe by reversing the edit, never by `git checkout`.

| AC | Criterion | Proved by | Mutation probe |
|---|---|---|---|
| 1 | Today's dotted deny entries become live, and all 11 closures still pass | G-release (measured: 232 passed, 5 AF_UNIX errors as on base, on a copy with the repair) + `test_package_closures` | Change root-import's `"appliance.node.broker"` to `"appliance.node.brokerx"` → ClosureError "names no module" |
| 2 | A two-hop chain into a dotted entry is refused | `test_module_closure` (new case) | Restore the top-level-only comparison at `:172` → the new case fails |
| 3 | No root package but `nodeapi` may import NATS | G-static `lint-imports` (13 kept) | `import nats` in `player/service.py` → "Only the Node API library talks to NATS BROKEN" (measured) |
| 4 | The retiring feed gains no importer, and a stale line fails | `lint-imports` | (a) `appliance/node/host.py` imports `appliance.feed` → BROKEN (measured on the moved tree). (b) Drop `probe_channel`'s feed import → "No matches for ignored import" (measured) |
| 5 | Every top-level package is under contract | `test_root_packages_are_every_first_party_package` | Add `nodeapi/__init__.py` on a scratch tree → fails |
| 6 | Staged PID1 imports resolve in their launcher's closure | `test_staged_launcher_imports` | `tests/test_node_pid1.py:315` → `appliance.node.manager_desired` → fails (not in node-bootstrap's closure) |
| 7 | Path literals, cache keys and the meson list name real files | `test_named_paths` | (a) `tests/test_node_linux_adapters.py:182` → `.../appliance/node/brokerx.py` → fails. (b) `'player/nope.py'` in `base-image.yml:283` → fails. (c) Drop `'overlay/paint.py'` from meson → fails |

**Stated costs:**
- The NATS fence sees only `root_packages`. `scripts/` and `tests/` are namespace packages outside it (measured: `import nats` in `scripts/release_plan.py` stays 13 kept).
- A dynamic `importlib.import_module("nats")` bypasses lint.
- Dotted string literals that are only ever compared (for example `"appliance.x" not in modules`) are not existence-checked. The rewrite residual (E1-2) covers them once.

---

## Bead E1-2: Big-bang move (code and tests together; no shims)

**Goal:** move every surviving Node module into its context directory, and every single-context Node test into `tests/node/<ctx>/`, in one green commit. There are no compatibility shims and no behaviour changes.

**Est.:** 90 min, including the tool run, the fix-ups and one full verify. **Depends on:** E1-1 landed, because its guards are this bead's safety net.

**CI gate before E1-3:**
- node-components, base-image, node-pid1 (5 legs + display-harness), netboot-e2e and e2e green on the PR head.
- This is the first contact of the method with deb builds and PID1. No simulation ran those.

### Frozen page

**1. Module map** (the single input to the rewrite tool, the closure check and the residual grep). There are 25 `git mv`, plus 1 split and 5 new files.

| Old (`appliance/…`) | New (`appliance/…`) | Context |
|---|---|---|
| `clock.py`, `boot_store.py`, `unix_credentials.py` | `kernel/` (same names) | kernel |
| `node/capacity.py`, `node/boot_stage.py` | `kernel/` | kernel |
| `node/probe.py:23-27` and `:35-48` (the 5 timing constants, `ProbeTiming`, `SHIPPED_TIMING`), byte for byte | `kernel/probe_timing.py` (new) | kernel |
| `node/host.py`, `host_linux.py`, `host_runner.py`, `host_storage.py`, `base_status.py` | `host/` | host |
| `node/bootstrap.py` | `boot/node_bootstrap.py` (**the one rename**: stage 1 owns `bootstrap`) | boot |
| `node/storage_mount.py` | `boot/` | boot |
| `node/broker.py`, `broker_runner.py`, `online_broker.py`, `online_runner.py`, `import_worker.py`, `root_import.py`, `environment.py`, `process_linux.py`, `stop_linux.py`, `stop_operation.py`, `lifecycle_storage.py`, `probe.py` (the rest), `probe_channel.py` | `apps/` | apps |
| new: `kernel/__init__.py`, `host/__init__.py`, `boot/__init__.py`, `apps/__init__.py` | one-line ownership docstring each | |

- `apps/probe.py` keeps `:28-32` (the boot-store comment, `RECOVERY_ARMED`, `RECOVERY_ACKNOWLEDGED`, `_SETTLED`) and re-imports the moved names from `appliance.kernel.probe_timing`.
- `health/runner.py:47` imports `SHIPPED_TIMING` from `appliance.kernel.probe_timing`. That removes the upward Health → Apps edge.

```python
# appliance/kernel/probe_timing.py  (frozen surface; bodies byte-identical to node/probe.py today)
PROBE_PERIOD_MS = 2000      # T
MISS_LIMIT = 5              # k
STARTUP_BUDGET_MS = 20000   # S
KILL_AFTER_MS = 35000       # K
OUTSTANDING_LIMIT = 8
@dataclass(frozen=True, slots=True)
class ProbeTiming:
    period_ms: int = PROBE_PERIOD_MS
    miss_limit: int = MISS_LIMIT
    startup_ms: int = STARTUP_BUDGET_MS
    kill_after_ms: int = KILL_AFTER_MS
    def __post_init__(self) -> None: ...   # ValueError("probe_timing") on a non-int or < 1
SHIPPED_TIMING: Final = ProbeTiming()
```

**2. Run tools** (`.claude/runs/node-e1/`, unshipped under `.claude/**`, committed with the ledger). Prior art is `lintprobe/simulate.py` and `sim2/sim2.py`, proven on sim2, sim3 and sim4. rope and libcst cannot rewrite patch strings, YAML or path literals, and adding either changes `uv.lock`.

```python
# module_map.py
MOVES: Final[tuple[tuple[str, str], ...]]          # (old dotted, new dotted): exactly the table above
def renamed(module: str) -> str: ...                # longest dotted-prefix match; identity if unmoved
# rewrite.py
@dataclass(frozen=True, slots=True)
class RewriteReport:
    moved: tuple[tuple[str, str], ...]
    edited: tuple[str, ...]
    residual: tuple[tuple[str, int, str], ...]      # old names still present in scope; must be empty
def rewrite_tree(root: Path, *, check_only: bool = False) -> RewriteReport: ...
# closure_equivalence.py
def closures(tree: Path) -> Mapping[str, frozenset[str]]: ...   # all 11 launcher policies
def unexpected_deltas(before: Mapping[str, frozenset[str]], after: Mapping[str, frozenset[str]]) -> tuple[str, ...]: ...
```

**Rewrite rules:**
1. Dotted names, longest first, with `(?<![\w.])old(?![\w])`. This covers imports and patch strings.
2. `from <pkg> import a, b` is split per name, aliasing a renamed name (`from appliance.boot import node_bootstrap as bootstrap`).
3. Slash paths **including after a `/`**. The sim tool's `(?<![\w/])` look-behind missed the `usr/lib/photo-wall-<x>/appliance/node/…` literals. Fix it to `(?<![\w.])`.
4. **Scope:** `appliance/`, `scripts/`, `tests/`, `.github/`, `pyproject.toml`, and `docs/evidence/2026-09-30-node-stop-observation-proposal.md` (its 5 links).
5. **Never** touch `contracts/`, `player/`, `docs/` (else E1-4's), `.claude/` (history), or `central/` and `media/` (no references, measured).
6. Then run `ruff check --fix` (28 `I001` measured).

**3. Hand fix-ups** (each measured on sim4):

| Where | What |
|---|---|
| `tests/test_node_linux_adapters.py:196-197` | Split form `root / "appliance/node" / name` over `host.py`, `host_linux.py`, `broker.py`, `process_linux.py`. It is vacuous after the move. Write 4 full literals instead (`appliance/host/host.py`, …), so E1-1's path guard sees them. |
| `tests/test_health_runner.py::test_the_judge_closure_is_small_and_the_base_ships_its_unit` | Expected set: −`appliance.node`, −`appliance.node.probe`, +`appliance.kernel`, +`appliance.kernel.probe_timing` (the one expected co-change) |
| `pyproject.toml` `[tool.importlinter]` (the 4 existing Node contracts) | Renamed through the map. `appliance.host`, `appliance.boot` and `appliance.apps` are added to the session contract's `source_modules` and to the kernel contract's `forbidden_modules`. The kernel contract's sources become `appliance.kernel.{clock,boot_store,unix_credentials}`. The feed fence lines `node.broker_runner` and `node.probe_channel` become `apps.*`. Measured: **13 kept**. |
| `tests/test_import_contracts.py` | Frozen strings renamed by the tool (measured: 3 passed) |

**4. Pins the tool rewrites** (the list is exhaustive per `git grep`; the verifier ticks each one):

| Pin | Change |
|---|---|
| `scripts/build_node_base_deb.py:23-35` | Roots and deny entries |
| `scripts/build_node_base_deb.py:58,60` | `stage_tree` prefix strings |
| `scripts/build_node_manager_deb.py:14-16,27` | Deny entries and prefix string |
| `scripts/build_app_environment.py:20,114` | Import, and the path literal. The literal is the **player-environment cache key**, so that package rebuilds. |
| `scripts/node_service_probe.py:94-95` | Inline script (base-bundle) |
| `scripts/node_control_demo.py:159,182-184` | |
| `tests/test_node_pid1.py:60` (comment), `:315` (inline) | |
| `tests/node_pid1_stop_diagnostic.py:11-12` | |
| `tests/node_worker_pid1_probe.py:14-17` | |
| `tests/node_manager_pid1_probe.py:15` | |
| `tests/node_pid1_central_inner.py:17` | |
| `tests/native_display_service_probe.py:19` | |
| `tests/test_node_component_inputs.py` | Parametrized `appliance/node/environment.py` |
| Slash literals after a `/` | `tests/test_node_boot_stage.py:478-479`, `tests/test_node_linux_adapters.py:182,184` |

**5. Release globs** (`scripts/release_plan.py:266-281`):
- `node-base-deb`: the per-file kernel entries are replaced by `appliance/kernel/**`, `appliance/host/**`, `appliance/apps/**`, `appliance/boot/**`. `appliance/node/**` stays for the retiring modules.
- `node-manager-deb`: `appliance/kernel/**`, `appliance/apps/__init__.py`, `appliance/apps/environment.py`.
- `player-environment`: `appliance/apps/environment.py`.
- **No change to the `node-pid1` suite paths** (C4).

**6. Test relocation:** 20 `git mv`, no content change beyond the rewrite.

**Placement rule:**
- A test goes to `tests/node/<ctx>/` when it imports **only what `<ctx>`'s code may import** under E1-3's contracts. Test infrastructure, `contracts`, `uplink`, `scripts` and retiring modules do not count.
- A test that imports two contexts that may not import each other goes to `tests/node/`.
- A test stays at the root when any of these holds:
  - it imports `central`, `media` or `player`;
  - its subject is retiring code;
  - a pin names it (`release_plan.py` suite paths, `node_service_probe.py:108`, the workflows).

| Destination | Files |
|---|---|
| `tests/node/apps/` | `test_node_online_broker`, `test_node_probe`, `test_node_probe_channel`, `test_node_probe_kill`, `test_node_stop_operation` |
| `tests/node/boot/` | `test_node_boot_linux` |
| `tests/node/display/` | `test_display_overlay_instruction`, `test_display_overlay_render`, `test_node_display_runner` |
| `tests/node/health/` | `test_display_overlay_health` |
| `tests/node/host/` | `test_node_host_facts`, `test_node_host_numbers` |
| `tests/node/` (cross-context) | `test_health_judge`, `test_health_runner` (both import `apps.probe`), `test_node_boot_stage`, `test_node_control_m1`, `test_node_host_recovery`, `test_node_linux_adapters`, `test_node_memory_class`, `test_node_probe_broker` |
| stay at root: retiring subject | `test_feed`, `test_feed_socket`, `test_node_session`, `test_node_manager_observation`, `test_node_recovery_transport`, `test_node_app_link_local`, `test_node_preparer_download` |
| stay at root: imports Central | `test_node_fleet_hosts`, `test_node_host_cadence`, `test_node_reenroll`, `test_node_switch_convergence` |
| stay at root: pinned | `test_node_pid1`, `node_pid1_*`, `node_manager_pid1_probe`, `node_worker_pid1_probe`, `native_display_*`, `display_harness_*`, `test_node_component_inputs`, `test_import_contracts`, `test_release_plan` |
| **deleted** | `tests/node_ipc_pid1_probe.py` (224 lines). **Why:** it is never executed (C8). **Functional cover:** none needed; it has run nowhere, so it proves nothing today. Owner confirmed deletion (2026-10-06). |

**Import mechanics:**
- **pytest `pythonpath`:**

  ```toml
  [tool.pytest.ini_options]
  testpaths = ["tests"]
  pythonpath = ["tests", "tests/node", "tests/node/apps", "tests/node/boot", "tests/node/display",
                "tests/node/health", "tests/node/host"]
  addopts = "-ra"
  ```

  This keeps the 7 cross-directory bare imports order-independent:
  - `linux_adapters.store` → `app_link_local`, `online_broker`, `probe_kill`, `stop_operation`;
  - `probe_broker` → `probe_kill`;
  - `display_runner.FakeBackend` → `health_runner`;
  - `online_broker.Driver` → `switch_convergence`.

  `pyproject`'s pytest table is not a build table, so it releases nothing.
- **Dotted to bare:** `from tests.test_node_boot_linux` (`tests/node/test_node_boot_stage.py:225,243`, `tests/test_node_preparer_download.py:257`) and `from tests.test_node_host_facts` (`test_node_boot_stage.py:326,426`) become bare imports.
- **Repo root:** new `tests/support/repo.py` holds `REPO: Final = Path(__file__).resolve().parents[2]`. It replaces `Path(__file__)…parents[1]` in the 7 relocated files that use it: `test_node_boot_linux:171`, `test_display_overlay_render:40`, `test_node_display_runner:26`, `test_node_memory_class:28`, `test_node_boot_stage:26`, `test_health_runner:41`, `test_node_linux_adapters:179,192,333`, `test_node_probe_broker:28`.

### Files touched (measured on sim4)

- 25 code renames and 20 test renames, which is **45 moves**.
- 6 new files: 4 `__init__`, `kernel/probe_timing.py`, `tests/support/repo.py`.
- 1 deleted file.
- About 36 edited files:
  - `appliance/`: 13 (display_host 3, health 1, node retiring 6, central_session 1, feed_socket 1, plus the split);
  - `scripts/`: 6;
  - `tests/`: 15;
  - `pyproject.toml`;
  - 1 evidence doc.
- Release scheduled: node-base-deb, node-manager-deb, node-display-deb, player-environment, **bootstrapper-deb and player-payload** (C13 below), base-bundle. Suites: checks, e2e, base-image, netboot-e2e, node-pid1.

### Acceptance, each with its proving gate and one mutation probe

| AC | Criterion | Proved by | Mutation probe |
|---|---|---|---|
| 1 | Every contract and fence line is renamed | `lint-imports`: 13 kept, exit 0 (measured) | Leave `appliance.node.probe_channel -> appliance.feed` unrenamed → exit 1, "No matches for ignored import" |
| 2 | No importer was missed | G-unit: failure set identical to the base tip (measured on sim4: 861 passed, the same failures and errors in the node subset after the 3 named fixes) | Add `from appliance.node.capacity import StorageShort` to `apps/root_import.py` → ImportError in the unit tier |
| 3 | Each launcher ships the same code, renamed | `closure_equivalence.unexpected_deltas` is empty against the expected table below | The same probe as AC2 → root-import's closure gains `appliance.node` and the check flags it |
| 4 | Every moved file is claimed | G-release: **125 passed** (measured) | Drop `appliance/apps/**` from `node-base-deb` → `test_every_node_deb_closure_file_is_claimed_by_its_package` fails (measured before the globs were added) |
| 5 | The tests are the same tests | Collection equivalence: (basename::id) set before = after (**5416 = 5416**, measured), and the skip-reason multiset is equal | Drop `"tests/node"` from `pythonpath` → 3 files in `tests/node/apps/` fail to collect with `ModuleNotFoundError: test_node_linux_adapters` (measured) |
| 6 | No old name remains in scope; frozen packages are untouched | `rewrite.py --check` residual empty; `git diff --quiet origin/main -- contracts player appliance/systemd appliance/display_host/meson.build` | Hand-edit `contracts/node_faults.py:7` → the diff check fails |
| 7 | PID1 inline imports and path literals are current | E1-1's `test_staged_launcher_imports` and `test_named_paths` | Revert `tests/test_node_linux_adapters.py:182` to `.../appliance/node/broker.py` → `test_named_paths` fails. Measured: without the guard, that test still passes, vacuously. |
| 8 | Doc links resolve | `check_docs` (measured red with the 5 evidence links unfixed) | Revert one link → fails |
| 9 | Real debs, PID1 and netboot behave | G-CI: node-components, base-image, node-pid1 ×5 + display-harness, netboot-e2e, e2e | No CI mutation run (≈20 min per probe). The class is pre-caught locally by AC7 and AC3. This is a stated gap. |
| 10 | DB-tier tests that import moved tests still run | G-db (`test_node_switch_convergence` imports the moved `Driver`) | Same as AC5: the collection error appears in the DB tier |

**Expected closure deltas** (AC3; measured on sim4, old modules mapped through the map):

| Launcher | Gains | Loses |
|---|---|---|
| root-import | +`appliance.apps`, +`appliance.kernel` | −`appliance.node` |
| node-bootstrap | +`appliance.apps`, +`appliance.boot`, +`appliance.kernel` | — |
| display-controller | +`appliance.kernel` | — |
| host-core | +`appliance.host`, +`appliance.kernel` | — |
| app-broker | +`appliance.apps`, +`appliance.kernel`, +`appliance.kernel.probe_timing` | — |
| manager-supervisor | +`appliance.apps`, +`appliance.host`, +`appliance.kernel` | — |
| health-judge | +`appliance.kernel`, +`appliance.kernel.probe_timing` | −`appliance.apps.probe`, −`appliance.node` |
| node-manager | +`appliance.apps`, +`appliance.kernel` | — |
| initrd, bootstrapper, player | none | none |

### Test deletions (owner rule)

- **Expected:** 1 file, 224 lines (`tests/node_ipc_pid1_probe.py`, C8).
- No other deletion was needed in sim4. Every relocated test passed after mechanical rewriting.
- The implementer may delete more only by the rule:
  - an internal unit test that costs more to fix than it is worth;
  - never the only proof of a functional requirement;
  - never a node-pid1, browser, wall e2e or DB-tier test.

  Each extra deletion is listed in the bead's report as file, one-line reason and the functional test that still covers it, or "none (internal only)".
- **Ceiling before stopping to ask:** 3 files.

### Split seam (if the bead passes 60 min without green, or 90 in total)

| Part | Content | Acceptance | Why it is green alone |
|---|---|---|---|
| **E1-2a** | Code moves, the split, every rewrite and every pin, with tests rewritten **in place at the root**. | AC1–4 and AC6–9 | The sim3/sim4 commits S2–S4 plus globs were green this way (125 passed). |
| **E1-2b** | The 20 `git mv`, `pythonpath`, `REPO`, the 2 dotted-to-bare imports and the deletion | AC5 and AC10 | — |

If E1-2a itself fails, re-cut it as the drafts' kernel tracer (kernel rows and the split) followed by host, boot and apps.

---

## Bead E1-3: Strict layer contracts (after the moves)

**Goal:** replace today's four interim Node contracts with the final layers and narrowing contracts. Turn the launcher deny entries into whole-package boundaries. Delete the three stage-time string checks, now that the deny entries are proven to catch the same chains and more. Ratchet everything.

**Est.:** 35 min. **Depends on:** E1-2 with its CI green.

### Frozen page

**1. `pyproject.toml`.** The block from `# Player health frame` (the "Node contexts point down" layers contract) through "Shared node kernel knows no context" is replaced by the following text, verbatim. E1-1's two fences stay after it.

```toml
[[tool.importlinter.contracts]]
# One directory per Node context (node redesign r3 §3.4). A layer imports only the layers below.
# `exhaustive`: a new appliance module that names no layer fails.
name = "Node contexts point down"
type = "layers"
containers = ["appliance"]
layers = ["node", "boot : netboot_init : bootstrap : boot_offer : central_post : node_boot_handoff",
          "apps", "health", "display_host | host", "central_session", "kernel : feed : feed_socket"]
exhaustive = true
ignore_imports = [
  # Retiring (r3 §10): each line leaves with the module it names.
  "appliance.apps.broker_runner -> appliance.node.app_link",
  "appliance.apps.broker_runner -> appliance.node.recovery_linux",
  "appliance.apps.online_broker -> appliance.node.recovery",
  "appliance.apps.stop_linux -> appliance.node.recovery",
  "appliance.apps.lifecycle_storage -> appliance.node.manager",
  "appliance.host.host_runner -> appliance.node.recovery",
  "appliance.host.host_runner -> appliance.node.recovery_linux",
  "appliance.boot.node_bootstrap -> appliance.node.preparer",
]
exhaustive_ignores = ["app_evidence", "app_executor", "app_launcher", "app_payload",
                      "app_process_proof", "app_proof_service", "linux_app_proof",
                      "online_activation", "os_agent", "provision", "process_identity"]

[[tool.importlinter.contracts]]
name = "Boot is an island"
type = "forbidden"
source_modules = ["appliance.boot", "appliance.netboot_init", "appliance.bootstrap",
                  "appliance.boot_offer", "appliance.central_post", "appliance.node_boot_handoff"]
forbidden_modules = ["appliance.apps", "appliance.health", "appliance.display_host",
                     "appliance.host", "appliance.central_session"]
ignore_imports = [
  # Retiring: the prepare verb (r3 §10).
  "appliance.boot.node_bootstrap -> appliance.apps.environment",
  "appliance.boot.node_bootstrap -> appliance.node.preparer",
]

[[tool.importlinter.contracts]]
name = "App lifecycle reaches no sibling context"
type = "forbidden"
source_modules = ["appliance.apps"]
forbidden_modules = ["appliance.health", "appliance.display_host", "appliance.host"]

[[tool.importlinter.contracts]]
name = "Health reaches only Display among contexts"
type = "forbidden"
source_modules = ["appliance.health"]
forbidden_modules = ["appliance.host"]

[[tool.importlinter.contracts]]
# The judge alone maps faults to household lines; Display draws the instruction it is given.
name = "Display never reads the fault catalogue"
type = "forbidden"
source_modules = ["appliance.display_host"]
forbidden_modules = ["contracts.node_faults"]

[[tool.importlinter.contracts]]
# Retiring (r3 §10): the programme deletes each worker and its line with it.
name = "The Central session is retiring: only the listed workers import it"
type = "protected"
protected_modules = ["appliance.central_session"]
allowed_importers = ["appliance.central_session"]
ignore_imports = [
  "appliance.host.host_runner -> appliance.central_session.*",
  "appliance.node.manager_desired -> appliance.central_session.*",
  "appliance.display_host.service -> appliance.central_session.*",
  "appliance.apps.broker_runner -> appliance.central_session.*",
  "appliance.node.app_link -> appliance.central_session.*",
  "appliance.apps.online_broker -> appliance.central_session.*",
]

[[tool.importlinter.contracts]]
name = "The base never imports the guest"
type = "forbidden"
source_modules = ["appliance"]
forbidden_modules = ["player"]
ignore_imports = ["appliance.os_agent -> player.mdns_discovery",
                  "appliance.provision -> player.mdns_discovery"]

[[tool.importlinter.contracts]]
name = "Node contexts never reach V1"
type = "forbidden"
source_modules = ["appliance.kernel", "appliance.feed", "appliance.feed_socket",
                  "appliance.central_session", "appliance.host", "appliance.display_host",
                  "appliance.health", "appliance.apps", "appliance.boot", "appliance.process_identity",
                  "appliance.netboot_init", "appliance.bootstrap", "appliance.boot_offer",
                  "appliance.central_post", "appliance.node_boot_handoff"]
forbidden_modules = ["appliance.app_evidence", "appliance.app_executor", "appliance.app_launcher",
                     "appliance.app_payload", "appliance.app_process_proof",
                     "appliance.app_proof_service", "appliance.linux_app_proof",
                     "appliance.online_activation", "appliance.os_agent", "appliance.provision"]
ignore_imports = ["appliance.process_identity -> appliance.app_launcher"]
```

This drops the `(appliance.authority)` placeholder and the "Central I/O belongs to Authority" text: both are Central-in-charge artefacts.

**2. Deny entries as packages.** The three stage-time checks are deleted: `build_node_base_deb.py:58-61` and `build_node_manager_deb.py:27-28`.

| Policy | `forbidden` after (the first four entries are as today) |
|---|---|
| root-import | `"player", "central", "gi", "appliance.host", "appliance.apps.broker", "appliance.apps.broker_runner", "appliance.apps.process_linux"` |
| host-core | `…, "appliance.apps", "appliance.node.manager", "appliance.node.manager_desired", "appliance.node.manager_launcher", "appliance.node.manager_observation", "appliance.node.manager_runner"` |
| app-broker | `…, "appliance.host"` |
| node-manager | `…, "appliance.host", "appliance.apps.broker", "appliance.apps.broker_runner", "appliance.apps.process_linux"` |

The old checks matched `startswith("…broker")`, which also caught `broker_runner`. The new dotted semantics do not, so `broker_runner` is listed explicitly.

**3. Co-change.** `tests/node/test_node_probe_broker.py::test_a_host_module_in_the_broker_closure_is_refused` no longer monkeypatches `closure_for`. It proves the real policy:

```python
def test_a_host_module_in_the_broker_closure_is_refused() -> None:
    # closure_for(replace(POLICIES["app-broker"], roots=(*roots, "appliance.host.host_linux")), repo=REPO)
    # raises ClosureError matching "appliance.host is forbidden here"
```

**4. `tests/test_import_contracts.py` rewritten as a two-way ratchet.** E1-1's three tests are kept.

```python
LAYERS_CONTRACT: Final = "Node contexts point down"
SESSION_CONTRACT: Final = "The Central session is retiring: only the listed workers import it"
FROZEN_NODE_LAYERS: Final[list[str]]                       # exact, as above
FROZEN_EXEMPTIONS: Final[Mapping[str, frozenset[str]]]     # contract name -> ignore lines; only shrink
FROZEN_EXHAUSTIVE_IGNORES: Final[frozenset[str]]           # only shrink
FROZEN_FORBIDDEN_CONTRACTS: Final[Mapping[str, tuple[frozenset[str], frozenset[str]]]]  # only grow
RETIRING_FILES: Final[frozenset[str]]   # appliance/node/{__init__,app_link,manager,manager_desired,manager_launcher,
                                        # manager_observation,manager_runner,preparer,recovery,recovery_linux}.py,
                                        # appliance/central_session/{__init__,http,session}.py
def test_exemptions_only_shrink() -> None: ...
def test_node_layers_are_the_frozen_list() -> None: ...
def test_forbidden_contracts_keep_every_frozen_source_and_target() -> None: ...
def test_no_node_contract_silences_unmatched_exemptions() -> None: ...   # no unmatched_ignore_imports_alerting key
def test_retiring_packages_only_shrink() -> None: ...                     # *.py under both dirs ⊆ RETIRING_FILES
```

### Files touched

- `pyproject.toml`
- `scripts/build_node_base_deb.py`
- `scripts/build_node_manager_deb.py`
- `tests/node/test_node_probe_broker.py`
- `tests/test_import_contracts.py`

Release: the node-base and node-manager manifests change, because `closure.json` carries the deny list; so does base-bundle.

### Acceptance, each with its proving gate and one mutation probe (all measured on sim4)

| AC | Criterion | Proved by | Mutation probe → result |
|---|---|---|---|
| 1 | Contexts point down; new modules must name a layer | `lint-imports`: **17 kept**, 0 broken | `host/host.py` imports `apps.broker` → "Node contexts point down BROKEN". A new `appliance/stray.py` → the same. |
| 2 | Boot is an island | `lint-imports` | `boot/storage_mount.py` imports `health.judge` → "Boot is an island BROKEN" |
| 3 | Narrowing: apps has no sibling; health does not reach host | `lint-imports` | `apps/broker.py` imports `host.host` → BROKEN. `health/judge.py` imports `host.host` → BROKEN. |
| 4 | Node contexts never reach V1 | `lint-imports` | `apps/probe.py` imports `app_executor` → BROKEN |
| 5 | Chains through retiring exemptions are refused at build time | G-release + closures | `node/recovery.py` imports `apps.stop_operation` → host-core ClosureError "appliance.apps is forbidden here". `node/app_link.py` imports `host.base_status` → app-broker ClosureError. The deleted string list would have missed the first. |
| 6 | The app-broker deny entry is load-bearing | the rewritten broker-closure test | Remove `"appliance.host"` from app-broker → the test fails |
| 7 | No new code in retiring packages | `test_retiring_packages_only_shrink` | `appliance/node/stray2.py` importing `apps.broker` and `host.host` → lint stays **17 kept** (measured), and the ratchet test fails |
| 8 | Exemptions cannot be widened | `test_exemptions_only_shrink` | Add an ignore line → fails |
| 9 | Nothing else regresses | G-unit failure set = E1-2's; G-release passes (232 passed, 5 AF_UNIX errors in the closure and packaging subset, measured) | — |

---

## Bead E1-4: Docs sweep

**Goal:** make the documentation describe the layered tree and mark the 0015-era design as superseded by 0016.

**Est.:** 25 min. **Depends on:** E1-3, and **PR 44 merged** (0015 and 0016 on `main`, Q1).

### Frozen page

| File | Change |
|---|---|
| `AGENTS.md` | Code-map row `appliance/node/` becomes rows for `appliance/kernel/`, `host/`, `apps/`, `boot/`, `health/`, plus `appliance/node/` and `central_session/` marked *retiring*. The `tests/` row names `tests/node/<ctx>/`. The import-layering line adds the Node layers order, the boot island, and "only `nodeapi` imports NATS". |
| `docs/node-4gb-memory-design.md` (15), `docs/operator-console-ddd.md` (8), `docs/player-node-domain-model.md` (3), `docs/player-fleet-implementation-map.md` (3) | Path rewrite through `module_map`, using the tool's docs pass, followed by a prose read |
| `docs/design/player-health/system-design-r8.md`, `module-design-r8.md` | One header line: "**Superseded** wherever Central is assumed in charge: see [0016](../../decisions/0016-central-and-node-relationship.md) and the node redesign." Paths inside are left as historical. |
| `docs/decisions/0015-player-base-layer-and-health-overlay.md`, `docs/player-architecture.md` (from PR 44) | The same header line where they assume Central in charge |
| `.claude/runs/player-health-m1.md` | One dated note at the top: "leg `unresponsive` removed in 4c4cc2e ahead of r3; the 53 mentions below are historical". The history is not rewritten. One errata entry is appended to `.claude/errata.md`. |

**Owner note (2026-10-06), binding on this bead:** PR 44 (merged as fb2aefe: 0015, 0016, `docs/player-architecture.md`) is a couple of days older than the architecture discussed on 2026-10-06. Wherever a doc from PR 44 conflicts with today's information, today's wins. Today's information is design-r3, the epic list and `owner-answers.md` rounds r3 + E1. Examples: NATS leaf is the Node API, the memory plan comes from the real Pi readings, and integration tests are preferred over unit tests. The bead therefore also:
- adds `docs/decisions/0017-node-redesign-r3.md`. It is a short record of today's current choices (NATS leaf with one account per Node and the three structural rules; the memory cuts; the epic order E1→E9). Requirements and revisable choices are kept apart, and it links to 0016.
- puts the superseded header on any PR 44 section that today's choices replace, pointing to 0017.

### Acceptance, each with its proving gate and one mutation probe

| AC | Criterion | Proved by | Mutation probe |
|---|---|---|---|
| 1 | Links resolve | `check_docs` | Point the 0016 link at `0016-x.md` → fails |
| 2 | No current doc names a moved path | `rewrite.py --check --scope docs AGENTS.md` residual empty, except an allowlist of the two superseded r8 files | Leave one `` `appliance/node/capacity.py` `` in `node-4gb-memory-design.md` → residual non-empty |
| 3 | The code map covers every `appliance/` package | The verifier diffs `ls appliance/*/__init__.py` against the AGENTS rows (one shell line, no new test) | Drop the `apps/` row → the diff is non-empty |

---

## Final verify (one pass, after E1-4)

**Est.:** 60 min.

**Verifier:**
- G-static, G-unit, G-db.
- G-browser, or an explicit, reported skip: the console is untouched.
- One local node-pid1 leg (`success`) if the arm64 fixture is available; else it is reported as skipped.
- Head CI green.
- It re-runs one mutation probe per bead.

**Two lenses:**
- regression: what working path breaks;
- maintainability and simplicity: what in E1 is a wrong abstraction.

The architect course-correction rule ("every 5 implementers") is satisfied: 4 implementers, and the final verify carries the course-correction pass.

---

## Summary

| Bead | Minutes |
|---|---:|
| E1-1 guards and deny matching | 40 |
| E1-2 big-bang move (+ CI wait ≈ 15–20) | 90 |
| E1-3 strict contracts | 35 |
| E1-4 docs sweep | 25 |
| Final verify (+ head CI) | 60 |
| **Total** | **≈ 250 min (4.2 h), plus two CI waits** |

**Stated costs:**
- **C13.** Deleting the top-level `appliance/{clock,boot_store,unix_credentials}.py` matches `_BOOTSTRAPPER_DEB`'s `appliance/*.py`. It schedules a bootstrapper-deb and player-payload release whose closures are unchanged. Narrowing that glob is the V1 lane's work.
- **Every Node deb rebuilds twice:** once in E1-1, because `module_closure.py` is an input, and again in E1-2.
- **Test-to-test imports remain.** Holding them on `pythonpath` is a convention, but a missing entry fails at collection, loudly. The later epic replaces these tests.
- **Test placement is not machine-held after E1.** A misplaced new test is a readability cost only.
- **Not covered:** runtime coupling through strings (socket paths, uids, slice names), and dynamic imports.
- **Dropped from the drafts as right-sizing:**
  - the systemd unit-ownership table: units do not move, and E3 adds the bus unit;
  - the 36-edge rig extraction;
  - the cross-test-import guard.

## Owner questions

- **Q1 (answered 2026-10-06: PR 44 merged; today's information wins over PR 44 on conflict).** E1-4's "superseded by 0016" header needs 0016 on `main`, and today it is only on PR 44 (open).
  - **Recommended:** merge PR 44 (docs only) before E1-4.
  - **Alternative:** the header names 0016 without a link, and a later docs bead adds the link.
- **Q2 (answered 2026-10-06: delete).** `tests/node_ipc_pid1_probe.py` (224 lines) has a node-pid1-style name, but nothing has ever run it.
  - **Recommended:** delete it in E1-2.
  - **Alternative:** keep it at the root, rewritten, as an unexecuted script.
