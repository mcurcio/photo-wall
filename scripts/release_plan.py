#!/usr/bin/env python3
"""The release rule, written once: what ships, what a change releases, and whether a run passed.

`.github/workflows/pipeline.yml` runs this script for every event, and nothing else calls
commitizen. Three commands:

  pr --base SHA --head SHA  A pull request, checked out at its merge ref. Every commit in
                            base..head lands on main (merge and rebase keep them), so each must
                            be a Conventional Commit; the plan then reports what merging releases
                            and runs only its tests (a PullRequestRun has no release job).
  push [--before SHA]       main. Releases exactly when a shipped package changed, and refuses
                            at once a next version whose tag already exists. With --before (the
                            branch's previous commit), it runs only the suites its pull request's
                            run did not pass on exactly this tree (passed_run), and outputs the
                            others as `reused`; any doubt runs them all.
  gate                      A run passed when every job the plan expected succeeded and no job
                            failed or was cancelled (reads NEEDS, the workflow's `toJSON(needs)`).

THE RULE. A release is due exactly when the inputs of a shipped package (PACKAGES) differ
between the last `v*` tag and the revision -- never because of a commit's type. The version is
commitizen's: `cz bump --get-next` under pyproject's [tool.commitizen] (feat -> minor;
fix/perf/refactor -> patch; a breaking change -> major, capped to minor while
`major_version_zero` holds). When packages changed but no commit bumps (chore:/ci:/test:, or a
commit commitizen cannot parse), the increment is PATCH. So a shipped change is never skipped,
and a release that failed is not forgotten: pipeline.yml's seal job (scripts/release_seal.py)
creates a tag only by publishing its GitHub Release, so a failed release leaves no tag, and the
next push still finds the packages changed since the last tag, whatever that push's own commits
are. That is the one recovery: there is no manual release path (no dispatch, no hand tag).

WHO WRITES. The seal alone: it claims the version (the plan's `since` must still be the highest
published tag), promotes the images, and publishes the release. This script only plans, and its
`since` output is what the seal's compare-and-swap reads.

WHO RELEASES. A Plan is a report: what releasing its revision would ship. Which jobs a run starts
is its run type's: a PullRequestRun lists only the plan's test suites, whatever the plan
forecasts; only a ReleaseRun, which main() builds for a push alone, adds RELEASE_JOBS.
No Suite may be a PUBLISH_JOBS job and no Plan may name a suite outside SUITES, so a pull
request's run cannot list a job that tags, publishes or pushes an image.

Stdlib only; runs on the runner's python3. commitizen runs ephemerally through `uvx`, pinned
by COMMITIZEN below -- its one pin; it is not a project dependency.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Final, Protocol

REPO: Final = Path(__file__).resolve().parents[1]

# The ONE commitizen pin. Every commitizen call in the pipeline is this script's.
COMMITIZEN: Final = "commitizen==4.9.1"
NO_COMMITS_TO_BUMP: Final = 21          # commitizen's exit status for "no commit bumps"
STRICT_VERSION: Final = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$")
STRICT_TAG: Final = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$")
FULL_SHA: Final = re.compile(r"^[0-9a-f]{40}$")
# The branch a release is cut from (pipeline.yml's release guard: refs/heads/main): its tip is
# the newest commit, whose seal wins an unpublished version; only a pull request merged into it
# can lend a push its pass (passed_run).
RELEASE_BRANCH: Final = "main"
# The one workflow whose pull request runs a push may reuse (passed_run).
PIPELINE: Final = "pipeline.yml"


class PlanError(Exception):
    """The plan refuses: a named commit, an unexpected commitizen answer, a bad or taken tag."""


# --- the root Dockerfile: the service images' inputs, read from the file that builds them -------
#
# A service image ships every build-context path its target stage's COPY/ADD (and RUN bind
# mounts) read, through every stage it builds on or copies from. Reading them from the Dockerfile
# makes "the image copies a file its package does not claim" unrepresentable: a new COPY claims
# its source for the image by construction. A form this reader cannot follow (a heredoc, a
# variable or glob-class source, a changed escape character) fails at import, before any plan.

DOCKERFILE: Final = "Dockerfile"


class DockerfileError(PlanError):
    """The Dockerfile uses a form this reader does not follow, so its inputs are unknown."""


@dataclass(frozen=True, slots=True)
class Stage:
    name: str
    reads: tuple[str, ...]          # build-context paths its instructions read
    after: tuple[str, ...]          # the stages it builds on (FROM) or reads (--from, from=)


def _instructions(text: str) -> list[tuple[str, str]]:
    """(KEYWORD, arguments) per instruction: comment lines dropped, continuations joined."""
    if re.search(r"^#\s*escape\s*=", text, flags=re.MULTILINE | re.IGNORECASE):
        raise DockerfileError(f"{DOCKERFILE}: an `escape` directive is not supported")
    found: list[tuple[str, str]] = []
    parts: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts.append(stripped[:-1] if stripped.endswith("\\") else stripped)
        if stripped.endswith("\\"):
            continue
        keyword, _, rest = " ".join(parts).partition(" ")
        if re.search(r"<<-?\s*[\"']?[A-Za-z_]", rest):
            raise DockerfileError(f"{DOCKERFILE}: a heredoc ({keyword} {rest[:40]}...) is not "
                                  "supported: its body would read as instructions")
        found.append((keyword.upper(), rest.strip()))
        parts = []
    if parts:
        raise DockerfileError(f"{DOCKERFILE} ends inside a continued line")
    return found


def _flags(rest: str) -> tuple[list[tuple[str, str]], str]:
    """An instruction's leading `--name[=value]` flags, and the rest."""
    flags = []
    while found := re.match(r"--([A-Za-z-]+)(?:=(\S*))?\s+", rest):
        flags.append((found[1].lower(), found[2] or ""))
        rest = rest[found.end():]
    return flags, rest


def _context_path(source: str) -> str:
    """A COPY/ADD source as a root-anchored manifest path; `**` is the whole context."""
    if "$" in source or re.search(r"[?\[\]\\]", source):
        raise DockerfileError(f"{DOCKERFILE}: source {source!r} uses a variable or glob class "
                              "this reader cannot follow")
    path = posixpath.normpath("/" + source).lstrip("/")
    return "**" if path in ("", ".") else path


def dockerfile_stages(text: str) -> dict[str, Stage]:
    """Every stage of a Dockerfile by lower-case name (an unnamed stage by its index)."""
    args: dict[str, str] = {}
    stages: list[tuple[str, list[str], list[str]]] = []
    for keyword, rest in _instructions(text):
        if keyword == "ARG" and not stages:
            name, _, default = rest.partition("=")
            args[name.strip()] = default.strip().strip("\"'")
        elif keyword == "FROM":
            _, rest = _flags(rest)
            words = rest.split()
            image = re.sub(r"\$\{?(\w+)\}?", lambda arg: args.get(arg[1], ""), words[0])
            named = len(words) == 3 and words[1].upper() == "AS"
            stages.append(((words[2] if named else str(len(stages))).lower(), [], [image.lower()]))
        elif not stages:
            continue
        elif keyword in ("COPY", "ADD"):
            flags, rest = _flags(rest)
            if source_stage := dict(flags).get("from"):
                stages[-1][2].append(source_stage.lower())
                continue
            words = json.loads(rest) if rest.startswith("[") else rest.split()
            if len(words) < 2:
                raise DockerfileError(f"{DOCKERFILE}: {keyword} {rest!r} names no destination")
            stages[-1][1].extend(_context_path(source) for source in words[:-1]
                                 if not (keyword == "ADD" and re.match(r"[a-z]+://|git@", source)))
        elif keyword == "RUN":
            for name, value in _flags(rest)[0]:
                if name != "mount":
                    continue
                mount = dict(option.partition("=")[::2] for option in value.split(","))
                if mount.get("from"):
                    stages[-1][2].append(mount["from"].lower())
                elif mount.get("type", "bind") == "bind":
                    stages[-1][1].append(_context_path(mount.get("source")
                                                       or mount.get("src") or "."))
    names = [name for name, _, _ in stages]

    def stage(reference: str) -> str | None:
        return names[int(reference)] if reference.isdigit() and int(reference) < len(names) \
            else reference if reference in names else None

    return {name: Stage(name, tuple(reads),
                        tuple(found for reference in after if (found := stage(reference))))
            for name, reads, after in stages}


def image_inputs(target: str, text: str | None = None) -> tuple[str, ...]:
    """The manifest patterns of the root Dockerfile's `target`: the Dockerfile, its ignore files
    and every context path the target's stage closure reads (a path claims what lies beneath)."""
    stages = dockerfile_stages((REPO / DOCKERFILE).read_text() if text is None else text)
    if target not in stages:
        raise DockerfileError(f"{DOCKERFILE} has no `{target}` stage")
    seen, todo = set(), [target]
    while todo:
        if (name := todo.pop()) not in seen:
            seen.add(name)
            todo += stages[name].after
    reads = sorted({path for name in seen for path in stages[name].reads})
    return (DOCKERFILE, ".dockerignore", f"{DOCKERFILE}.dockerignore",
            *(pattern for path in reads
              for pattern in ((path,) if path == "**" else (path, f"{path}/**"))))


# --- the package manifest ----------------------------------------------------------------------
#
# Each shipped package, and the paths its build reads. Patterns are anchored at the repository
# root: `**` crosses directories, `*` does not. Code shipped as a computed closure
# (scripts/module_closure.py) is claimed as whole top-level packages, never a file list that
# could fall behind the closure (decision 0014 §1); tests/test_release_plan.py proves every
# closure file and every script a build runs is claimed by the package it builds. The service
# images' paths are read from the Dockerfile (image_inputs), so editing it edits them.
#
# NOT A RELEASE INPUT (owner ruling): .github/workflows/pipeline.yml, and the seal it runs
# (scripts/release_seal.py: the claim, the promotion, the release notes and the publish). They
# hold the service images' build and push and how a release is written, so a pipeline change
# that alters how an artefact is built releases NOTHING by itself: it first reaches a release
# with the next change to a package's inputs. Accepted, so that CI-only edits never cut a
# version. What a release CONTAINS is a release input: contracts/release.py declares it and
# scripts/package_release_artifacts.py writes it (the release-assets package), Galleria's zip
# and the integration it is cut from included.

# The locked Python project every build syncs. pyproject.toml counts only through DIGESTED below.
_PROJECT: Final = ("pyproject.toml", "uv.lock")
# The base build's tooling: the pinned uv environment and the snapshot pin it reads.
_BASE_BUILD: Final = (*_PROJECT, ".github/actions/python-uv/action.yml",
                      ".github/workflows/node-components.yml", "scripts/module_closure.py",
                      "debian-packaging/snapshot.list", "debian-packaging/snapshot-epoch.sh",
                      "scripts/device_root_checks.py")
# The one source package (decision 0019): node-components.yml's `debs` job builds every binary
# package of debian/control in the pinned build container and the local repo beside upstream's
# nats-server .deb and python3-nats (debian-packaging/build-repo.sh). The build runs the import
# check (scripts/import_check.py and what it imports) and installs the import roots the
# debian/*.install files name; tests/test_release_plan.py holds both claimed.
_NODE_DEBS: Final = (*_PROJECT, ".github/workflows/node-components.yml", "debian/**",
                     "debian-packaging/**", "scripts/import_check.py",
                     "scripts/module_closure.py",
                     # stage 1's uplink on the device runtime, run in the build container
                     "scripts/uplink_device_harness.py",
                     "contracts/**", "nodeapi/**", "uplink/**", "player/**", "appliance/kernel/**",
                     "appliance/feed.py", "appliance/feed_socket.py",
                     "appliance/central_session/**", "appliance/host/**",
                     "appliance/display_host/**", "appliance/health/**", "appliance/apps/**",
                     "appliance/process_identity.py",
                     "appliance/boot/**", "appliance/node_boot_handoff.py",
                     "appliance/netboot_init.py", "appliance/bootstrap.py",
                     "appliance/central_post.py", "appliance/netboot_initramfs/**",
                     "appliance/node/**", "appliance/launchers/**", "appliance/bus/**",
                     "appliance/systemd/photo-wall-*.service",
                     "appliance/systemd/photo-wall-*.path",
                     "appliance/systemd/photowall*.slice",
                     "appliance/systemd/photo-wall-node.target")


@dataclass(frozen=True, slots=True)
class Package:
    name: str
    ships: str
    paths: tuple[str, ...]
    target: str | None = None               # the root Dockerfile stage it is, when an image

    def claims(self, path: str) -> bool:
        return any(matches(pattern, path) for pattern in self.paths)


def _image(name: str, ships: str, target: str, *also: str) -> Package:
    """A service image: its Dockerfile target's inputs, and `also` what builds its base."""
    return Package(name, ships, (*image_inputs(target), *also), target)


PACKAGES: Final = (
    _image("central-image", "the Central service image (ghcr.io .../central)", "central"),
    _image("media-worker-image", "the media worker service image (ghcr.io .../media-worker), "
           "FROM the media OS base service-base.yml prepares", "media-worker",
           "scripts/service_base.py", "scripts/registry_pull.py", "scripts/docker_diagnostics.py",
           ".github/workflows/service-base.yml"),
    # photo-wall-node, the composition (launchers, units, bus configuration), ships byte for byte
    # as the node release's node-base.deb; photo-wall-player and photo-wall-app-manager as its
    # app.deb and manager-primary.deb.
    Package("node-debs", "the Node's Debian packages from the one source package, and their "
            "local repo", _NODE_DEBS),
    # photo-wall-node-display from the one source package, built by debhelper's Meson build
    # system (decision 0019 P1), shipped byte for byte as node-display.deb.
    Package("node-display-deb", "the native Weston display .deb from the local repo",
            (*_PROJECT, "debian/**", "debian-packaging/**", "appliance/display_host/**",
             ".github/workflows/node-components.yml")),
    # The release roots (decision 0019, steps 3 and 4): mmdebstrap builds the app and manager
    # roots from the pin and the local repo (debian-packaging/build-root.sh), the seal
    # (debian-packaging/seal-hook.sh, scripts/seal_root.py) seals them and the release writer
    # (scripts/node_release_writer.py) writes the component set's components.json.
    Package("release-roots", "the app and manager release roots (squashfs images) and the "
            "component set's components.json",
            (*_NODE_DEBS, "appliance/__init__.py", "scripts/seal_root.py",
             "scripts/node_release_writer.py", "scripts/node_release_artifacts.py",
             "scripts/package_release_artifacts.py",
             "scripts/galleria_zip.py")),        # the packager's import, not a root input
    # The squashfs installs photo-wall-node from the local repo; the initrd build root installs
    # photo-wall-netboot-init from it, whose hook copies stage 1's package directories (contracts,
    # uplink and appliance's stage-1 modules) into the initrd; the rest of appliance/ is the image
    # definition, claimed whole.
    Package("base-bundle", "the netboot base bundle: squashfs, kernel, initrd and clock floor",
            (*_BASE_BUILD, ".github/workflows/base-image.yml", "appliance/**", "contracts/**",
             "uplink/**", "scripts/build_netboot_bundle.sh", "scripts/verify_netboot_initrd.py",
             "scripts/node_release_artifacts.py", "nodeapi/**",
             "debian/**", "debian-packaging/**", "scripts/package_release_artifacts.py",
             "scripts/galleria_zip.py",          # the packager's import, not a base input
             "scripts/initrd_mount_probe.py", "scripts/kernel_config_check.py",
             "scripts/eeprom_update.py", "scripts/verify_boot_display.py")),
    # The published files: the base and boot tarballs, galleria.zip, manifest.json, SHA256SUMS and
    # the node release, whose names, layout and contents this packager writes to the declaration.
    # Galleria ships in galleria.zip, which HACS installs (hacs.json's zip_release names it, and
    # HACS reads hacs.json at the release), so a change to either releases.
    Package("release-assets", "the GitHub Release's asset set (base and boot tarballs, "
            "galleria.zip, manifest.json, SHA256SUMS, the node release)",
            ("scripts/package_release_artifacts.py", "contracts/release.py",
             "contracts/node_release.py", "scripts/node_release_artifacts.py",
             "scripts/galleria_zip.py", "custom_components/**", "hacs.json")),
)

# Every tracked path no package claims must match one of these, so a new top-level directory or
# script fails tests/test_release_plan.py until someone decides whether it ships.
NOT_SHIPPED: Final = (
    "*.md", "docs/**", "tests/**", ".claude/**", ".codegraph/**", ".gitignore", "compose.yaml",
    ".github/workflows/checks.yml", ".github/workflows/software-e2e.yml",
    ".github/workflows/pipeline.yml",           # owner ruling: see the manifest's head
    # The service images' shared build and BuildKit cache wiring, which pipeline.yml runs: under
    # the same owner ruling, how an image is built is not a release input. The cache scope policy
    # also serves the node component builds, where a hit reuses a recorded layer: no byte changes.
    ".github/actions/service-image/action.yml", ".github/actions/buildkit-cache/action.yml",
    # The node builds' builder and cache wiring over that policy (the same ruling).
    ".github/actions/node-builder/action.yml",
    # The test jobs' console build; the images build their own bundle (the Dockerfile).
    ".github/actions/console-bundle/action.yml",
    # The software e2e jobs' shared setup; it builds test images only.
    ".github/actions/software-e2e-setup/action.yml",
    # CI's route for Docker Hub pulls (a pull-through mirror): digest pins are unchanged, so no
    # byte changes; the suites whose jobs use it list it, so a change to it runs them.
    ".github/actions/docker-hub-mirror/action.yml",
    # Development, documentation and test-harness tooling; no build reads these.
    "scripts/boot_time_fixture.py", "scripts/check_docs.py",
    "scripts/configure.py", "scripts/container_build.py", "scripts/demo_wall.py",
    "scripts/node_control_demo.py",  # Opt-in software simulator, never a runtime artifact.
    "scripts/node_rollout_image_check.py",
    "scripts/harness_bundle.py", "scripts/harness_failure.py",
    "scripts/immich_actions.py", "scripts/immich_fixture.py", "scripts/immich_runtime.py",
    "scripts/provenance_models.py",
    "scripts/release_plan.py", "scripts/release_seal.py",
    "scripts/runtime_provenance.py",
    "scripts/test_local.py",
    "scripts/run_display_harness.py",  # node-pid1's display-harness job runner; never shipped
    "scripts/nats_server.py",  # the bus tests' pinned nats-server binary; never shipped
    ".github/workflows/node-pid1.yml",
    "scripts/catalog_baselines.py",  # the console-catalog leg's baseline tool; never shipped
    "scripts/calibration_target/**",  # the agent's calibration target reader (decision 0026); never shipped
    "scripts/calibration_boards.py",  # builds contracts/calibration_boards.py (decision 0027); never shipped
    "scripts/render_bench/**",  # the render redesign's Pi tracer bench (run ledger .claude/runs/render-planes); never shipped
    "scripts/console_types.py",  # builds the console's generated contract.ts at build time; never shipped
    # Galleria's contract generator and its own test environment are development tooling (the
    # integration itself ships as release-assets' galleria.zip).
    "scripts/galleria_contract.py",  # writes custom_components/galleria/contract.py; never shipped
    "galleria_tests/**",
)


# A claimed file whose change counts only through a digest of the parts a build reads: a changed
# file whose digest is unchanged changed no package. pyproject.toml ships in every build, but
# only these tables shape what a build produces; the rest configure development tools (ruff,
# pytest, import-linter, commitizen). [dependency-groups] is left out: the lock records it.
PYPROJECT_BUILD_TABLES: Final = (("build-system",), ("project",), ("tool", "uv"),
                                 ("tool", "hatch"))


def pyproject_digest(text: str | None) -> str | None:
    """sha256 of pyproject's PYPROJECT_BUILD_TABLES, as parsed TOML; None for no file."""
    if text is None:
        return None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise PlanError(f"pyproject.toml is not valid TOML: {error}") from None
    tables = {}
    for keys in PYPROJECT_BUILD_TABLES:
        value: object = data
        for key in keys:
            value = value.get(key) if isinstance(value, dict) else None
        tables[".".join(keys)] = value
    return hashlib.sha256(json.dumps(tables, sort_keys=True, default=str).encode()).hexdigest()


DIGESTED: Final[Mapping[str, Callable[[str | None], str | None]]] = {
    "pyproject.toml": pyproject_digest}


# --- the pipeline's jobs -----------------------------------------------------------------------

# The jobs that write outside the run: the media OS base (service-base), the service images by
# digest (images), and the ONE job that writes a version -- its image tags, its git tag and its
# GitHub Release (seal). No test suite may be one, and pipeline.yml also guards each by event (a
# push to main), which tests/test_release_plan.py holds it to.
PUBLISH_JOBS: Final = ("service-base", "images", "seal")


@dataclass(frozen=True, slots=True)
class Suite:
    """A pipeline.yml test job and what makes it due: always, a change to one of its packages, or
    a change to one of its own paths (a harness no package claims). A suite never publishes."""
    job: str
    always: bool = False
    packages: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.job in PUBLISH_JOBS:
            raise ValueError(f"{self.job} publishes; a test suite cannot be a publishing job")


SUITES: Final = (
    Suite("checks", always=True),
    Suite("e2e", always=True),
    Suite("base-image", packages=("base-bundle", "node-debs")),
    # The node lifecycle under real systemd (node-pid1.yml): the node packages it boots, the
    # Central it runs against (its fixture imports central.app, so all of Central's Python), and
    # its own builder, harness, the test modules the harness borrows from, and workflow; and the
    # display harness job's runner and fixture (the job builds appliance/display_host itself).
    Suite("node-pid1", packages=("node-display-deb", "release-roots", "node-debs"),
          paths=("tests/test_node_pid1.py", "tests/node_pid1_*", "tests/node_pid1_fixture/**",
                 "tests/node/apps/test_environment_image.py", "tests/debs/test_roots.py",
                 "tests/native_display_smoke.py", "tests/native_display_probe.c",
                 "tests/native_screen_harness.py", "tests/native_fit_pass_harness.py",
                 "tests/native_picture_e2e_harness.py", "tests/harness_device.py",
                 "tests/native_display_media.py",
                 "tests/display_harness_health_client.py", "tests/display_harness_judge_feeder.py",
                 "scripts/run_display_harness.py",
                 "tests/content_db.py", "tests/runtime_fakes.py", "tests/test_assets_handlers.py",
                 "tests/test_fleet_attempts.py",
                 "tests/test_node_boot.py", "tests/test_registry.py",
                 # the join's hub harness and its pinned nats-server
                 "tests/integration/bus_servers.py", "tests/systemd_environment.py",
                 "scripts/nats_server.py", "debian-packaging/nats-server.env",
                 "scripts/node_release_artifacts.py",
                 "scripts/package_release_artifacts.py", "scripts/galleria_zip.py",
                 "scripts/container_build.py",
                 "scripts/initrd_mount_probe.py", "scripts/verify_netboot_initrd.py",
                 ".github/workflows/node-pid1.yml",
                 ".github/actions/docker-hub-mirror/action.yml",
                 ".github/actions/node-builder/action.yml",
                 "central/**/*.py", "appliance/*.py", "media/__init__.py", "media/models.py",
                 "media/prepare.py", "media/library_client.py", "central/migrations/*_node_*.sql",
                 "uv.lock")),
)
SUITE_JOBS: Final = frozenset(suite.job for suite in SUITES)
# The jobs that build for others, and the jobs they build for: each runs when the plan lists one
# of its consumers, each of which needs it. A build is never listed itself, so the gate judges it
# through its consumers (each listed one must succeed, and cannot without it) and by its rule
# that no job fails. tests/test_release_plan.py holds pipeline.yml's wiring to this.
BUILD_JOBS: Final[Mapping[str, tuple[str, ...]]] = {
    "node-components": ("e2e", "base-image", "node-pid1")}
# A release runs these; base-image doubles as the release build (its artifacts are what the
# seal packages), so a release always runs it.
RELEASE_JOBS: Final = ("base-image", *PUBLISH_JOBS)
# Jobs every run requires, whatever the plan lists: `tested`, the barrier every release job
# needs, and each suite that always runs.
ALWAYS_JOBS: Final = ("tested", *(suite.job for suite in SUITES if suite.always))


@functools.cache
def _pattern(pattern: str) -> re.Pattern[str]:
    parts, index = [], 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(parts) + r"\Z")


def matches(pattern: str, path: str) -> bool:
    return _pattern(pattern).match(path) is not None


def claimed_by(path: str) -> tuple[str, ...]:
    return tuple(package.name for package in PACKAGES if package.claims(path))


def changed_packages(paths: Iterable[str]) -> tuple[str, ...]:
    paths = tuple(paths)
    return tuple(package.name for package in PACKAGES
                 if any(package.claims(path) for path in paths))


def due_suites(packages: Sequence[str], paths: Sequence[str]) -> tuple[str, ...]:
    return tuple(suite.job for suite in SUITES
                 if suite.always or set(suite.packages) & set(packages)
                 or any(matches(pattern, path) for pattern in suite.paths for path in paths))


# --- git and commitizen ------------------------------------------------------------------------

def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise PlanError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _blob(repo: Path, revision: str, path: str) -> str | None:
    """`path`'s text at `revision`; None when the revision has no such file."""
    if not git(repo, "ls-tree", "--name-only", revision, "--", path).strip():
        return None
    return git(repo, "show", f"{revision}:{path}")


def changed_paths(repo: Path, since: str | None, revision: str) -> list[str]:
    """The paths that differ between `since` (every path, before the first tag) and `revision`,
    less each DIGESTED path whose digest is the same at both."""
    if since is None:
        return [path for path in git(repo, "ls-tree", "-r", "-z", "--name-only",
                                     revision).split("\0") if path]
    paths = [path for path in git(repo, "diff", "--no-renames", "-z", "--name-only", since,
                                  revision).split("\0") if path]
    return [path for path in paths if path not in DIGESTED
            or DIGESTED[path](_blob(repo, since, path))
            != DIGESTED[path](_blob(repo, revision, path))]


@dataclass(frozen=True, slots=True)
class Commitizen:
    """commitizen, pinned, run ephemerally in `repo` (it reads pyproject's [tool.commitizen])."""
    repo: Path

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        uvx = shutil.which("uvx")
        if uvx is None:
            raise PlanError("uvx is not on PATH (the pipeline installs the pinned uv with "
                            ".github/actions/python-uv)")
        return subprocess.run([uvx, "--from", COMMITIZEN, "cz", *args], cwd=self.repo,
                              capture_output=True, text=True)

    def current_version(self) -> str:
        """The latest reachable `v*` tag's version (version_provider = "scm"); 0.0.0 if none."""
        result = self._run("version", "--project")
        version = result.stdout.strip()
        if result.returncode != 0 or not STRICT_VERSION.match(version):
            raise PlanError(f"commitizen could not read the current version: "
                            f"{(result.stdout + result.stderr).strip()}")
        return version

    def next_version(self, increment: str | None = None) -> str | None:
        """commitizen's next version from the commits since the last tag; None when no commit
        bumps (NO_COMMITS_TO_BUMP). `increment` forces one."""
        forced = ("--increment", increment) if increment else ()
        result = self._run("bump", "--get-next", "--yes", *forced)
        if result.returncode == NO_COMMITS_TO_BUMP and increment is None:
            return None
        version = result.stdout.strip()
        if result.returncode != 0 or not STRICT_VERSION.match(version):
            raise PlanError(f"commitizen returned no next version (exit {result.returncode}): "
                            f"{(result.stdout + result.stderr).strip()}")
        return version

    def check(self, rev_range: str) -> None:
        """Every commit in `rev_range` is a Conventional Commit (Merge/Revert/fixup! allowed)."""
        result = self._run("check", "--rev-range", rev_range)
        if result.returncode != 0:
            raise PlanError("these commits are not Conventional Commits:\n"
                            + (result.stdout + result.stderr).strip())


# --- the plan ----------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Plan:
    """What releasing `revision` would ship: a report. It starts no job; a run type decides."""
    revision: str
    since: str | None                     # the last `v*` tag; None before the first release
    packages: tuple[str, ...]             # shipped packages changed since `since`
    suites: tuple[str, ...]               # test jobs due
    commits: tuple[str, ...] = ()         # "<short sha> <subject>" since `since`
    version: str | None = None            # the next version, when releasing
    increment: str | None = None          # MAJOR, MINOR or PATCH
    source: str | None = None             # who chose the increment

    def __post_init__(self) -> None:
        if unknown := set(self.suites) - SUITE_JOBS:
            raise ValueError(f"not test suites: {', '.join(sorted(unknown))}")

    @property
    def should_release(self) -> bool:
        return self.version is not None

    @property
    def tag(self) -> str:
        return f"v{self.version}" if self.version else ""


# --- reuse: a push to main takes its pull request's pass --------------------------------------
#
# A push to main whose tree its pull request's run already tested does not test it again: the
# plan lists only the suites that run did not pass, and outputs the rest as `reused`. The run
# must have checked out exactly this tree, so every workflow, action, test and script that gave
# the verdict is main's. Only a verdict is reused, never a byte: every build the release ships
# still runs in the push's own run. Anything in doubt runs the suites (fail closed).

class NoReuse(Exception):
    """This push runs its suites; the message says why it cannot take a pull request's pass."""


class ReuseUnavailable(NoReuse):
    """GitHub could not answer (no token, HTTP, network, an unreadable answer): run the suites."""


@dataclass(frozen=True, slots=True)
class MergedPull:
    """The pull request a push merged: its number, head commit, and whether that head branch is
    this repository's (a fork's run is never reused)."""
    number: int
    head: str
    same_repository: bool


@dataclass(frozen=True, slots=True)
class PipelineRun:
    """One pipeline.yml run: its id, its page, and its conclusion (None while it runs)."""
    id: int
    url: str
    conclusion: str | None


@dataclass(frozen=True, slots=True)
class PassedRun:
    """The pull request run a push reuses, and the suites it passed."""
    url: str
    suites: tuple[str, ...]


class RunHistory(Protocol):
    """GitHub's records, read only: one plain question per method. Raises ReuseUnavailable when
    it cannot answer."""

    def merged_pull(self, revision: str) -> MergedPull | None:
        """The pull request merged into RELEASE_BRANCH whose merge commit is `revision`, among
        the 100 most recently updated (None beyond them: the push runs its suites)."""

    def latest_pull_request_run(self, head: str) -> PipelineRun | None:
        """The newest PIPELINE run of event pull_request for head commit `head` whose head
        branch is this repository's."""

    def jobs(self, run_id: int) -> tuple[tuple[str, str | None], ...]:
        """(name, conclusion) of every job in the run's latest attempt."""


_PASSING: Final = frozenset({"success", "skipped"})


def passed_suites(jobs: Iterable[tuple[str, str | None]]) -> tuple[str, ...]:
    """The suites a run passed: a suite's jobs are named `<suite> / ...` (a called workflow's
    jobs) or `<suite>`; it passed when one of them succeeded and every one succeeded or was
    skipped."""
    seen: dict[str, list[str | None]] = {}
    for name, conclusion in jobs:
        suite = name.split(" / ", 1)[0]
        if suite in SUITE_JOBS:
            seen.setdefault(suite, []).append(conclusion)
    return tuple(sorted(suite for suite, results in seen.items()
                        if "success" in results and all(result in _PASSING for result in results)))


# How far back along the branch a pull request run's base is checked (tested_tree): further
# than this many first-parent commits since the head's history and the push runs its suites.
MAX_BASES: Final = 50


def _ok(repo: Path, *args: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True).returncode == 0


def tested_tree(repo: Path, head: str, before: str) -> None:
    """Raise NoReuse unless every pull request run of `head` checked out `head`'s own tree.

    A pull request run checks out GitHub's merge of the head into the branch as it was when the
    run started: some first-parent commit `base` of the branch up to `before`. Each candidate's
    merge is computed as git makes it (`git merge-tree --write-tree`, criss-cross histories and
    their virtual merge base included) and must be the head's tree, walking back from `before`
    until one is an ancestor of the head; every earlier one is too, so its merge is the head
    itself, and the head was pushed after them."""
    head_tree = git(repo, "rev-parse", f"{head}^{{tree}}").strip()
    base = before
    for _ in range(MAX_BASES):
        if _ok(repo, "merge-base", "--is-ancestor", base, head):
            return
        merged = subprocess.run(["git", "-C", str(repo), "merge-tree", "--write-tree", base, head],
                                capture_output=True, text=True)
        if merged.returncode != 0 or merged.stdout.split("\n", 1)[0].strip() != head_tree:
            raise NoReuse(f"a run based on {base[:12]} would have merged in changes the head "
                          "lacks, so it tested another tree")
        parent = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
                                 f"{base}^1"], capture_output=True, text=True)
        if parent.returncode != 0:
            raise NoReuse(f"{base[:12]} has no parent and shares no history with the head")
        base = parent.stdout.strip()
    raise NoReuse(f"the branch moved more than {MAX_BASES} commits past the head's history")


def passed_run(repo: Path, history: RunHistory, revision: str, before: str) -> PassedRun:
    """The pull request run whose pass `revision` (a push to RELEASE_BRANCH; `before` is where
    the branch was) may reuse. Raises NoReuse naming the first condition that fails:

      1. `revision` is the merge commit of a pull request from this repository;
      2. that pull request's head is in this checkout and has `revision`'s tree;
      3. every run of that head checked out that same tree (tested_tree);
      4. the head's newest PIPELINE pull request run concluded success;
      5. it passed at least one suite (passed_suites)."""
    if not FULL_SHA.match(before) or before == "0" * 40:
        raise NoReuse("the push names no earlier commit of the branch")
    if not _ok(repo, "merge-base", "--is-ancestor", before, revision):
        raise NoReuse(f"{before[:12]} is not an ancestor of {revision[:12]}")
    pull = history.merged_pull(revision)
    if pull is None:
        raise NoReuse(f"{revision[:12]} is not a pull request's merge into {RELEASE_BRANCH}")
    if not pull.same_repository:
        raise NoReuse(f"pull request #{pull.number} comes from a fork")
    if not _ok(repo, "cat-file", "-e", f"{pull.head}^{{commit}}"):
        raise NoReuse(f"pull request #{pull.number}'s head {pull.head[:12]} is not in this "
                      "checkout (a rebase merge whose branch is already deleted)")
    if git(repo, "rev-parse", f"{pull.head}^{{tree}}") != git(repo, "rev-parse",
                                                               f"{revision}^{{tree}}"):
        raise NoReuse(f"pull request #{pull.number}'s head {pull.head[:12]} has another tree: "
                      f"{RELEASE_BRANCH} moved before the merge")
    tested_tree(repo, pull.head, before)
    run = history.latest_pull_request_run(pull.head)
    if run is None:
        raise NoReuse(f"pull request #{pull.number}'s head has no {PIPELINE} run")
    if run.conclusion != "success":
        raise NoReuse(f"pull request #{pull.number}'s newest run ({run.url}) concluded "
                      f"{run.conclusion or 'nothing yet'}")
    suites = passed_suites(history.jobs(run.id))
    if not suites:
        raise NoReuse(f"pull request #{pull.number}'s run ({run.url}) passed no suite")
    return PassedRun(run.url, suites)


@dataclass(frozen=True, slots=True)
class GitHubRunHistory:
    """RunHistory over GitHub's REST API, through the seal's read-only GET (404 is None)."""
    get: Callable[[str], object]
    repository: str

    @classmethod
    def from_environment(cls) -> GitHubRunHistory:
        # Imported here: the seal imports this module, so this module cannot import it first.
        from scripts.release_seal import GitHubApi, SealError
        try:
            api = GitHubApi.from_env()
        except SealError as error:
            raise ReuseUnavailable(str(error)) from None

        def get(path: str) -> object:
            try:
                return api.get(path)
            except SealError as error:
                raise ReuseUnavailable(str(error)) from None
        return cls(get, api.repository)

    def _read(self, path: str) -> object:
        found = self.get(path)
        if found is None:
            raise ReuseUnavailable(f"GitHub answers 404 for {path}")
        return found

    def _from_here(self, head: object) -> bool:
        repo = head.get("repo") if isinstance(head, dict) else None
        return isinstance(repo, dict) and repo.get("full_name") == self.repository

    def merged_pull(self, revision: str) -> MergedPull | None:
        # The pull request records themselves, newest change first: a merge updates its pull
        # request, so it is on the first page. Never GitHub's commit-to-pull-request index
        # (/commits/{sha}/pulls): it is built apart from the records and answered an empty
        # list for PR 112's merge (12106f17), whose commits also sat on another open branch.
        pulls = self._read(f"/pulls?state=closed&base={RELEASE_BRANCH}&sort=updated"
                           "&direction=desc&per_page=100")
        if not isinstance(pulls, list):
            raise ReuseUnavailable(f"the closed pull requests into {RELEASE_BRANCH} are not a "
                                   "list")
        for pull in pulls:
            if (isinstance(pull, dict) and pull.get("merged_at")
                    and pull.get("merge_commit_sha") == revision
                    and isinstance(pull.get("base"), dict)
                    and pull["base"].get("ref") == RELEASE_BRANCH
                    and isinstance(pull.get("number"), int)
                    and isinstance(pull.get("head"), dict)
                    and FULL_SHA.match(str(pull["head"].get("sha")))):
                return MergedPull(pull["number"], str(pull["head"]["sha"]),
                                  self._from_here(pull["head"]))
        return None

    def latest_pull_request_run(self, head: str) -> PipelineRun | None:
        found = self._read(f"/actions/workflows/{PIPELINE}/runs?event=pull_request"
                           f"&head_sha={head}&per_page=100")
        runs = found.get("workflow_runs") if isinstance(found, dict) else None
        if not isinstance(runs, list):
            raise ReuseUnavailable(f"the {PIPELINE} runs of {head} are not a list")
        ours = [run for run in runs if isinstance(run, dict) and run.get("head_sha") == head
                and run.get("event") == "pull_request" and isinstance(run.get("id"), int)
                and self._from_here({"repo": run.get("head_repository")})]
        if not ours:
            return None
        newest = max(ours, key=lambda run: (str(run.get("created_at")), run["id"]))
        return PipelineRun(newest["id"], str(newest.get("html_url")),
                           newest.get("conclusion") if newest.get("status") == "completed"
                           else None)

    def jobs(self, run_id: int) -> tuple[tuple[str, str | None], ...]:
        found: list[tuple[str, str | None]] = []
        for page in range(1, 11):
            batch = self._read(f"/actions/runs/{run_id}/jobs?filter=latest&per_page=100"
                               f"&page={page}")
            jobs = batch.get("jobs") if isinstance(batch, dict) else None
            if not isinstance(jobs, list) or not all(isinstance(job, dict) for job in jobs):
                raise ReuseUnavailable(f"the jobs of run {run_id} (page {page}) are not a list")
            total = batch.get("total_count")
            if not isinstance(total, int):
                raise ReuseUnavailable(f"the jobs of run {run_id} have no total_count")
            found += [(str(job.get("name")), job.get("conclusion")) for job in jobs]
            if len(found) >= total or len(jobs) < 100:
                return tuple(found)
        raise ReuseUnavailable(f"run {run_id} has more than 1000 jobs")


# A run: the conditional pipeline jobs it starts (pipeline.yml's `if:`s read `jobs` and the gate
# requires each to succeed) and whether it releases. Two types, so that "which jobs run" is
# decided by the event's type, never by what the plan forecasts.

@dataclass(frozen=True, slots=True)
class PullRequestRun:
    """A pull request's run: the plan's test suites and nothing else. Its plan still reports what
    merging releases, but this type has no path to RELEASE_JOBS and outputs no tag or version,
    and it reuses no other run's pass."""
    plan: Plan
    subject: ClassVar[str] = "merging"
    releases: ClassVar[bool] = False
    passed: ClassVar[PassedRun | None] = None

    @property
    def jobs(self) -> tuple[str, ...]:
        return tuple(sorted(self.plan.suites))

    @property
    def reused(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True, slots=True)
class ReleaseRun:
    """A push to main (main() builds one for nothing else): the plan's test suites, less those
    its pull request's run passed (`passed`, see passed_run), and, when the plan releases, every
    release job."""
    plan: Plan
    passed: PassedRun | None = None
    subject: ClassVar[str] = "this push"

    @property
    def releases(self) -> bool:
        return self.plan.should_release

    @property
    def jobs(self) -> tuple[str, ...]:
        taken = set(self.passed.suites) if self.passed else set()
        return tuple(sorted({*(set(self.plan.suites) - taken),
                             *(RELEASE_JOBS if self.releases else ())}))

    @property
    def reused(self) -> tuple[str, ...]:
        """The due suites this run does not start because `passed` passed them."""
        if self.passed is None:
            return ()
        return tuple(sorted(set(self.plan.suites) & set(self.passed.suites) - set(self.jobs)))


Run = PullRequestRun | ReleaseRun


def _increment(current: str, following: str) -> str:
    old, new = ([int(part) for part in version.split("-", 1)[0].split(".")]
                for version in (current, following))
    return "MAJOR" if new[0] != old[0] else "MINOR" if new[1] != old[1] else "PATCH"


def plan_head(repo: Path, cz: Commitizen) -> Plan:
    """The release plan for the checked-out HEAD against the last `v*` tag."""
    revision = git(repo, "rev-parse", "HEAD").strip()
    current = cz.current_version()
    tag = f"v{current}"
    since = tag if git_tag_exists(repo, tag) else None
    paths = changed_paths(repo, since, revision)
    commits = tuple(line for line in git(repo, "log", "--format=%h %s",
                                         f"{since}..{revision}" if since else revision)
                    .splitlines() if line)
    packages = changed_packages(paths)
    suites = due_suites(packages, paths)
    if not packages:
        return Plan(revision, since, packages, suites, commits)
    version, source = cz.next_version(), "commitizen"
    if version is None:
        version = cz.next_version("PATCH")
        source = "automated: shipped packages changed and no commit bumps"
    return Plan(revision, since, packages, suites, commits, version,
                _increment(current, version), source)


def git_tag_exists(repo: Path, tag: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
                           f"refs/tags/{tag}^{{commit}}"], capture_output=True).returncode == 0


def refuse_a_taken_tag(repo: Path, plan: Plan) -> Plan:
    """A push's next version must be free. commitizen numbers from the last tag REACHABLE from
    the revision, so a tag off this history (a higher one, say) can already hold that number:
    every push would then build for an hour and fail at the seal. Refuse before any build."""
    if plan.should_release and git_tag_exists(repo, plan.tag):
        at = git(repo, "rev-parse", f"refs/tags/{plan.tag}^{{commit}}").strip()
        raise PlanError(f"this push would release {plan.tag}, but that tag already exists (at "
                        f"{at}), off this history: commitizen numbers from the last tag it can "
                        f"reach ({plan.since or 'none'}). Publishing would fail after the build, "
                        "so no version is released until the collision is resolved.")
    return plan


def check_commits(repo: Path, cz: Commitizen, base: str, head: str) -> None:
    if git(repo, "rev-list", f"{base}..{head}").strip():
        try:
            cz.check(f"{base}..{head}")
        except PlanError as error:
            raise PlanError(f"{error}\nEvery commit of a pull request lands on main (merge and "
                            "rebase keep them), and the release rule reads them: reword each "
                            "named commit as a Conventional Commit (git rebase -i).") from None


def headline(plan: Plan, subject: str) -> str:
    if not plan.should_release:
        since = f"since {plan.since}" if plan.since else "in the history"
        return f"{subject} releases nothing: no shipped package changed {since}"
    since = f" since {plan.since}" if plan.since else ""
    return (f"{subject} releases {plan.tag} (packages: {', '.join(plan.packages)}, increment: "
            f"{plan.increment} ({plan.source}), from commits: {len(plan.commits)}{since})")


def summary(run: Run) -> str:
    plan = run.plan
    lines = ["### Release plan", "", headline(plan, run.subject), ""]
    if plan.commits:
        lines += [f"Commits{f' since {plan.since}' if plan.since else ''}:", ""]
        lines += [f"- `{commit.split(' ', 1)[0]}` {commit.partition(' ')[2]}"
                  for commit in plan.commits]
        lines.append("")
    lines.append(f"Jobs: {', '.join(run.jobs) or 'none beyond plan and gate'}")
    if run.passed is not None:
        lines += ["", f"Reused from {run.passed.url}: {', '.join(run.reused) or 'nothing due'}"]
    return "\n".join(lines) + "\n"


def outputs(run: Run) -> dict[str, str]:
    """The run's action: a run that does not release outputs no tag, version or `since` to act
    on. `since`, the last tag the plan diffed from (empty before the first release), is what the
    seal's compare-and-swap requires to still be the highest published one."""
    plan, releases = run.plan, run.releases
    return {"should_release": str(releases).lower(), "tag": plan.tag if releases else "",
            "version": (plan.version or "") if releases else "",
            "since": (plan.since or "") if releases else "", "revision": plan.revision,
            "packages": json.dumps(list(plan.packages)), "jobs": json.dumps(list(run.jobs)),
            "reused": json.dumps(list(run.reused))}


# --- the gate ----------------------------------------------------------------------------------

def _names(outputs_: Mapping[str, object], key: str, *, default: str = "") -> list[str] | None:
    """The plan's `key` output as a JSON list of job names; None when it is anything else."""
    try:
        names = json.loads(str(outputs_.get(key) or default))
    except ValueError:
        return None
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        return None
    return names


def gate(needs: Mapping[str, Mapping[str, object]]) -> list[str]:
    """What is wrong with a run whose jobs finished as `needs` (`toJSON(needs)`): the plan must
    succeed and list its jobs, every job it listed and every ALWAYS_JOBS job it did not reuse
    must succeed, and no other job may fail or be cancelled. An empty list is a pass.

    The plan's outputs are the one decision: `reused` (the suites a push took from its pull
    request's run, passed_run) is never looked up again here. It only excuses an always-run
    suite from running, and it must name suites this run does not list."""
    plan = needs.get("plan") or {}
    problems, expected = [], set(ALWAYS_JOBS)
    outputs_ = plan.get("outputs") or {}
    listed = _names(outputs_, "jobs")
    reused = _names(outputs_, "reused", default="[]")
    if plan.get("result") != "success":
        problems.append(f"plan: {plan.get('result', 'missing')}")
    elif listed is None or reused is None or not (listed or reused):
        problems.append("plan: succeeded but its `jobs` output is missing, empty or not a JSON "
                        "list of job names, so this run's required jobs are unknown")
    elif set(reused) - SUITE_JOBS or set(reused) & set(listed):
        problems.append(f"plan: its `reused` output {reused} names a job that is not a suite or "
                        "that this run lists, so this run's required jobs are unknown")
    else:
        expected = (expected - set(reused)) | set(listed)
    for job, need in sorted(needs.items()):
        result = need.get("result")
        if job == "plan":
            continue
        if job in expected and result != "success":
            problems.append(f"{job}: {result}, but this run requires it")
        elif result in ("failure", "cancelled"):
            problems.append(f"{job}: {result}")
    return problems


# --- the command line --------------------------------------------------------------------------

def _publish(run: Run) -> None:
    text = summary(run)
    print(text, end="")
    print(f"::notice title=Release plan::{headline(run.plan, run.subject)}")
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as handle:
            handle.write(text)
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a") as handle:
            handle.writelines(f"{key}={value}\n" for key, value in outputs(run).items())


def _reuse(repo: Path, history: Callable[[], RunHistory], plan: Plan,
           before: str) -> PassedRun | None:
    """passed_run for a push, or None (its suites run), the outcome always a notice: the run
    reused, or why not."""
    passed: PassedRun | None
    try:
        if not before:
            raise NoReuse("the push names no earlier commit of the branch")
        passed = passed_run(repo, history(), plan.revision, before)
        text = f"reused from {passed.url}: {', '.join(passed.suites)}"
    except NoReuse as error:
        passed, text = None, f"no reuse, this push runs its own suites: {error}"
    print(f"::notice title=Reuse::{text}")
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as handle:
            handle.write(f"{text}\n\n")
    return passed


def main(argv: Sequence[str] | None = None,
         history: Callable[[], RunHistory] = GitHubRunHistory.from_environment) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=REPO)
    commands = parser.add_subparsers(dest="command", required=True)
    pr = commands.add_parser("pr", help="plan a pull request's merge ref (HEAD)")
    pr.add_argument("--base", required=True)
    pr.add_argument("--head", required=True)
    push = commands.add_parser("push", help="plan main's HEAD")
    push.add_argument("--before", help="the branch's commit before this push: reuse the suites "
                                       "its pull request's run passed (passed_run)")
    commands.add_parser("gate", help="judge the run from NEEDS (toJSON(needs))")
    args = parser.parse_args(argv)
    try:
        if args.command == "gate":
            problems = gate(json.loads(os.environ["NEEDS"]))
            for problem in problems:
                print(f"::error title=gate::{problem}")
            print("gate: " + ("failed" if problems else "passed"))
            return 1 if problems else 0
        cz = Commitizen(args.repo)
        run: Run
        if args.command == "pr":
            check_commits(args.repo, cz, args.base, args.head)
            run = PullRequestRun(plan_head(args.repo, cz))
        else:
            plan = refuse_a_taken_tag(args.repo, plan_head(args.repo, cz))
            run = ReleaseRun(plan, _reuse(args.repo, history, plan, args.before or ""))
        _publish(run)
    except PlanError as error:
        for line in str(error).splitlines():
            print(f"::error title=release plan::{line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
