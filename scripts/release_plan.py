#!/usr/bin/env python3
"""The release rule, written once: what ships, what a change releases, and whether a run passed.

`.github/workflows/pipeline.yml` runs this script for every event, and nothing else calls
commitizen. Three commands:

  pr --base SHA --head SHA  A pull request, checked out at its merge ref. Every commit in
                            base..head lands on main (merge and rebase keep them), so each must
                            be a Conventional Commit; the plan then reports what merging releases
                            and runs only its tests (a PullRequestRun has no release job).
  push                      main. Releases exactly when a shipped package changed, and refuses
                            at once a next version whose tag already exists.
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
from typing import ClassVar, Final

REPO: Final = Path(__file__).resolve().parents[1]

# The ONE commitizen pin. Every commitizen call in the pipeline is this script's.
COMMITIZEN: Final = "commitizen==4.9.1"
NO_COMMITS_TO_BUMP: Final = 21          # commitizen's exit status for "no commit bumps"
STRICT_VERSION: Final = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$")
STRICT_TAG: Final = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$")
FULL_SHA: Final = re.compile(r"^[0-9a-f]{40}$")


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
# scripts/package_release_artifacts.py writes it (the release-assets package).

# The locked Python project every build syncs. pyproject.toml counts only through DIGESTED below.
_PROJECT: Final = ("pyproject.toml", "uv.lock")
# How base-image.yml builds both .debs, and node-components.yml the node set: each builder
# `git archive`s its computed closure and the Debian declaration at the revision, stamps the
# pyproject version, and runs in the pinned uv environment. build_bootstrapper_deb imports
# build_player_deb and build_player.
_DEB_BUILD: Final = (*_PROJECT, ".github/actions/python-uv/action.yml",
                     ".github/workflows/base-image.yml", ".github/workflows/node-components.yml",
                     "scripts/build_player.py",
                     "scripts/build_player_deb.py", "scripts/module_closure.py",
                     "scripts/debian_packages.py", "scripts/device_root_checks.py")
_PLAYER_DEB: Final = (*_DEB_BUILD, "player/**", "contracts/**", "uplink/**",
                      "appliance/systemd/player.service", "appliance/systemd/weston.service",
                      "appliance/systemd/weston.ini")
_BOOTSTRAPPER_DEB: Final = (*_DEB_BUILD, "scripts/build_bootstrapper_deb.py",
                            "appliance/*.py", "contracts/**", "uplink/**", "player/**",
                            "appliance/systemd/photo-wall-provision.service",
                            "appliance/systemd/photo-wall-os-agent.service",
                            "appliance/systemd/player.service",
                            "appliance/systemd/weston.service",
                            "appliance/systemd/weston.ini")
_PLAYER_PAYLOAD: Final = (*_BOOTSTRAPPER_DEB, "scripts/build_player_payload.py")


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
           "scripts/service_base.py", ".github/workflows/service-base.yml"),
    Package("player-deb", "the Player .deb", _PLAYER_DEB),
    Package("node-manager-deb", "the exact versioned AppManager .deb",
            (*_DEB_BUILD, "scripts/build_node_manager_deb.py", "appliance/__init__.py",
             "appliance/node/__init__.py", "appliance/node/manager.py", "appliance/node/manager_runner.py",
             "appliance/node/manager_desired.py", "appliance/node/manager_observation.py",
             "appliance/node/preparer.py",
             "appliance/kernel/**", "appliance/apps/__init__.py", "appliance/apps/environment.py",
             "appliance/central_session/**",
             "contracts/**", "uplink/**")),
    Package("node-display-deb", "the isolated native Weston display .deb",
            (*_DEB_BUILD, "scripts/build_node_display_deb.py", "scripts/node_build_inputs.py", "appliance/display_host/**")),
    Package("player-environment", "the sealed Debian V2 Player environment",
            (*_PLAYER_DEB, "scripts/build_app_environment.py", "scripts/node_build_inputs.py", "appliance/apps/environment.py",
             "scripts/build_environment_image.py", "scripts/sealed_archive.py")),
    Package("node-base-deb", "the isolated V2 node base .deb",
            (*_DEB_BUILD, "scripts/build_node_base_deb.py", "appliance/node/**", "appliance/kernel/**",
             "appliance/host/**", "appliance/apps/**", "appliance/boot/**", "appliance/display_host/**", "contracts/**",
             "uplink/**", "appliance/__init__.py", "appliance/central_session/**", "appliance/feed.py",
             "appliance/feed_socket.py",
             "appliance/health/**", "appliance/node_boot_handoff.py", "appliance/process_identity.py", "appliance/app_launcher.py", "appliance/systemd/photo-wall-*.service",
             "appliance/systemd/photowall*.slice", "appliance/systemd/photo-wall-node.target",
             # The Node bus: its configuration and its pinned server (E3c).
             "appliance/bus/**", "scripts/nats_server.py", "scripts/pinned_fetch.py",
             # HostCore's nodeapi session and its vendored nats-py wheel (E3c S4).
             "nodeapi/**", "scripts/vendored_packages.py")),
    Package("player-payload", "the data-only Player application archive", _PLAYER_PAYLOAD),
    Package("bootstrapper-deb", "the bootstrapper .deb", _BOOTSTRAPPER_DEB),
    # The squashfs bakes the bootstrapper .deb, so the bundle reads everything that .deb does;
    # the rest of appliance/ is the image and initramfs definition, claimed whole.
    Package("base-bundle", "the netboot base bundle: squashfs, kernel, initrd and boot data",
            (*_BOOTSTRAPPER_DEB, "appliance/**", "scripts/build_netboot_bundle.sh",
             "scripts/build_boot_data.py", "scripts/verify_netboot_initrd.py",
             "scripts/build_node_components.py", "scripts/node_component_inputs.py",
             "scripts/node_release_artifacts.py",
             "scripts/build_app_environment.py", "scripts/build_environment_image.py",
             "scripts/sealed_archive.py", "scripts/build_node_base_deb.py", "scripts/nats_server.py", "scripts/pinned_fetch.py",
             "scripts/vendored_packages.py", "nodeapi/**",
             "scripts/build_node_display_deb.py", "scripts/build_node_manager_deb.py",
             "scripts/node_build_inputs.py", "scripts/package_release_artifacts.py",
             "scripts/initrd_mount_probe.py", "scripts/kernel_config_check.py",
             "scripts/eeprom_update.py", "scripts/player_start_probe.py",
             "scripts/os_agent_service_probe.py",
             "scripts/verify_boot_display.py")),
    # The published files beyond the .debs: the base bundle tarball, manifest.json and
    # SHA256SUMS, whose names, layout and contents this packager writes to the declaration.
    Package("release-assets", "the GitHub Release's operator asset set (base tarball, "
            "manifest.json, SHA256SUMS)", ("scripts/package_release_artifacts.py",
                                           "contracts/release.py", "contracts/player_payload.py", "contracts/node_release.py",
                                           "scripts/node_release_artifacts.py")),
)

# Every tracked path no package claims must match one of these, so a new top-level directory or
# script fails tests/test_release_plan.py until someone decides whether it ships.
NOT_SHIPPED: Final = (
    "*.md", "docs/**", "tests/**", ".claude/**", ".codegraph/**", ".gitignore", "compose.yaml",
    ".github/workflows/checks.yml", ".github/workflows/software-e2e.yml",
    ".github/workflows/netboot-e2e.yml",
    ".github/workflows/pipeline.yml",           # owner ruling: see the manifest's head
    # The service images' shared build and BuildKit cache wiring, which pipeline.yml runs: under
    # the same owner ruling, how an image is built is not a release input. The cache scope policy
    # also serves the node component builds, where a hit reuses a recorded layer: no byte changes.
    ".github/actions/service-image/action.yml", ".github/actions/buildkit-cache/action.yml",
    # The test jobs' console build; the images build their own bundle (the Dockerfile).
    ".github/actions/console-bundle/action.yml",
    # The software e2e jobs' shared setup; it builds test images only.
    ".github/actions/software-e2e-setup/action.yml",
    # Development, documentation and test-harness tooling; no build reads these.
    "scripts/boot_time_fixture.py", "scripts/check_docs.py", "scripts/check_player_unit.py",
    "scripts/configure.py", "scripts/container_build.py", "scripts/demo_wall.py",
    "scripts/node_control_demo.py",  # Opt-in software simulator, never a runtime artifact.
    "scripts/node_rollout_image_check.py", "scripts/node_rollout_ci_evidence.py",
    "scripts/docker_diagnostics.py", "scripts/harness_bundle.py", "scripts/harness_failure.py",
    "scripts/immich_actions.py", "scripts/immich_fixture.py", "scripts/immich_runtime.py",
    "scripts/provenance_models.py", "scripts/published_player_wire.py",
    "scripts/packaged_os_agent_probe.py",
    "scripts/release_plan.py", "scripts/release_seal.py",
    "scripts/runtime_provenance.py",
    "scripts/test_local.py", "scripts/test_netboot_e2e.py", "scripts/uplink_device_harness.py",
    "scripts/build_node_pid1_fixture.py",  # the node-pid1 scenarios' fixture; never shipped
    "scripts/run_display_harness.py",  # node-pid1's display-harness job runner; never shipped
    ".github/workflows/node-pid1.yml",
    "scripts/catalog_baselines.py",  # the console-catalog leg's baseline tool; never shipped
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
    Suite("base-image", packages=("base-bundle", "bootstrapper-deb", "player-deb",
                                  "player-payload")),
    # The tracer serves the Player .deb from a real Central: its content-serving layers, and the
    # rest of what its harness imports (tests/test_release_plan.py computes that closure).
    Suite("netboot-e2e", packages=("bootstrapper-deb", "player-deb"),
          paths=("scripts/test_netboot_e2e.py", "scripts/packaged_os_agent_probe.py",
                 "scripts/uplink_device_harness.py", "scripts/demo_wall.py",
                 "scripts/container_build.py", "scripts/docker_diagnostics.py",
                 "scripts/harness_bundle.py", "scripts/harness_failure.py",
                 "scripts/immich_actions.py", "scripts/immich_fixture.py",
                 "scripts/provenance_models.py", "scripts/runtime_provenance.py",
                 "tests/tls_fixture.py", ".github/workflows/netboot-e2e.yml",
                 "central/__init__.py", "central/app.py", "central/db.py", "central/fleet/**",
                 "central/catalog.py", "central/execution_outcomes.py", "central/media_ports.py",
                 "central/planner.py", "central/runtime.py", "media/__init__.py",
                 "media/models.py",
                 "central/migrations/041_fleet_app_observations.sql",
                 "central/content_routes.py", "central/content_catalog/**",
                 "central/assets/**", "central/infra/**", "Dockerfile", "uv.lock")),
    # The node lifecycle under real systemd (node-pid1.yml): the node packages it boots, the
    # Central it runs against (its fixture imports central.app, so all of Central's Python), and
    # its own builder, harness, the test modules the harness borrows from, and workflow; and the
    # display harness job's runner and fixture (the job builds appliance/display_host itself).
    Suite("node-pid1", packages=("node-base-deb", "node-manager-deb", "node-display-deb",
                                 "player-environment"),
          paths=("tests/test_node_pid1.py", "tests/node_pid1_*", "tests/node/apps/test_environment_image.py",
                 "tests/native_display_smoke.py", "tests/native_display_probe.c",
                 "tests/display_harness_health_client.py", "tests/display_harness_judge_feeder.py",
                 "scripts/run_display_harness.py",
                 "tests/content_db.py", "tests/runtime_fakes.py", "tests/test_assets_handlers.py",
                 "tests/test_fleet_attempts.py", "tests/test_fleet_rollout_gate.py",
                 "tests/test_node_boot.py", "tests/test_registry.py",
                 # the join's hub harness and its pinned nats-server
                 "tests/integration/bus_servers.py", "tests/systemd_environment.py",
                 "scripts/nats_server.py",
                 "scripts/build_node_pid1_fixture.py", "scripts/build_node_components.py",
                 "scripts/node_component_inputs.py", "scripts/node_release_artifacts.py",
                 "scripts/package_release_artifacts.py",
                 "scripts/container_build.py", "scripts/player_start_probe.py",
                 "scripts/initrd_mount_probe.py", "scripts/verify_netboot_initrd.py",
                 "scripts/build_boot_data.py", ".github/workflows/node-pid1.yml",
                 "central/**/*.py", "appliance/*.py", "media/__init__.py", "media/models.py",
                 "media/prepare.py", "central/migrations/*_node_*.sql", "uv.lock")),
)
SUITE_JOBS: Final = frozenset(suite.job for suite in SUITES)
# The jobs that build for others, and the jobs they build for: each runs when the plan lists one
# of its consumers, each of which needs it. A build is never listed itself, so the gate judges it
# through its consumers (each listed one must succeed, and cannot without it) and by its rule
# that no job fails. tests/test_release_plan.py holds pipeline.yml's wiring to this.
BUILD_JOBS: Final[Mapping[str, tuple[str, ...]]] = {
    "node-components": ("base-image", "node-pid1")}
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


# A run: the conditional pipeline jobs it starts (pipeline.yml's `if:`s read `jobs` and the gate
# requires each to succeed) and whether it releases. Two types, so that "which jobs run" is
# decided by the event's type, never by what the plan forecasts.

@dataclass(frozen=True, slots=True)
class PullRequestRun:
    """A pull request's run: the plan's test suites and nothing else. Its plan still reports what
    merging releases, but this type has no path to RELEASE_JOBS and outputs no tag or version."""
    plan: Plan
    subject: ClassVar[str] = "merging"
    releases: ClassVar[bool] = False

    @property
    def jobs(self) -> tuple[str, ...]:
        return tuple(sorted(self.plan.suites))


@dataclass(frozen=True, slots=True)
class ReleaseRun:
    """A push to main (main() builds one for nothing else): the plan's test suites and, when the
    plan releases, every release job."""
    plan: Plan
    subject: ClassVar[str] = "this push"

    @property
    def releases(self) -> bool:
        return self.plan.should_release

    @property
    def jobs(self) -> tuple[str, ...]:
        return tuple(sorted({*self.plan.suites, *(RELEASE_JOBS if self.releases else ())}))


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
    return "\n".join(lines) + "\n"


def outputs(run: Run) -> dict[str, str]:
    """The run's action: a run that does not release outputs no tag, version or `since` to act
    on. `since`, the last tag the plan diffed from (empty before the first release), is what the
    seal's compare-and-swap requires to still be the highest published one."""
    plan, releases = run.plan, run.releases
    return {"should_release": str(releases).lower(), "tag": plan.tag if releases else "",
            "version": (plan.version or "") if releases else "",
            "since": (plan.since or "") if releases else "", "revision": plan.revision,
            "packages": json.dumps(list(plan.packages)), "jobs": json.dumps(list(run.jobs))}


# --- the gate ----------------------------------------------------------------------------------

def _listed_jobs(outputs_: Mapping[str, object]) -> list[str] | None:
    """The plan's `jobs` output, when it is a non-empty JSON list of job names."""
    try:
        jobs = json.loads(str(outputs_.get("jobs") or ""))
    except ValueError:
        return None
    if not isinstance(jobs, list) or not jobs or not all(isinstance(job, str) for job in jobs):
        return None
    return jobs


def gate(needs: Mapping[str, Mapping[str, object]]) -> list[str]:
    """What is wrong with a run whose jobs finished as `needs` (`toJSON(needs)`): the plan must
    succeed and list its jobs, every job it listed and every ALWAYS_JOBS job must succeed, and no
    other job may fail or be cancelled. An empty list is a pass."""
    plan = needs.get("plan") or {}
    problems, expected = [], set(ALWAYS_JOBS)
    if plan.get("result") != "success":
        problems.append(f"plan: {plan.get('result', 'missing')}")
    elif (listed := _listed_jobs(plan.get("outputs") or {})) is None:
        problems.append("plan: succeeded but its `jobs` output is missing, empty or not a JSON "
                        "list of job names, so this run's required jobs are unknown")
    else:
        expected |= set(listed)
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=REPO)
    commands = parser.add_subparsers(dest="command", required=True)
    pr = commands.add_parser("pr", help="plan a pull request's merge ref (HEAD)")
    pr.add_argument("--base", required=True)
    pr.add_argument("--head", required=True)
    commands.add_parser("push", help="plan main's HEAD")
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
            run = ReleaseRun(refuse_a_taken_tag(args.repo, plan_head(args.repo, cz)))
        _publish(run)
    except PlanError as error:
        for line in str(error).splitlines():
            print(f"::error title=release plan::{line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
