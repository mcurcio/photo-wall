"""Authoritative OS inputs shared by package preparation and baseline identity."""

import json
from pathlib import Path

OS_DEFINITION = json.loads(Path(__file__).with_name("os_definition.json").read_text())
BASE_BYTES = OS_DEFINITION["base_bytes"]
BASE_SHA256 = OS_DEFINITION["base_sha256"]
RUNTIME_PACKAGES = tuple(OS_DEFINITION["runtime_packages"])
# 0009 slice 5 (p3-base-image): the minimal, GENERIC base OS's own runtime
# package set -- Python plus its bootstrapper's one third-party dependency
# closure (`python3-zeroconf`, which pulls in `python3-ifaddr`) and the
# same boot/network tooling RUNTIME_PACKAGES already carries. Deliberately
# excludes every Player-rendering package (GTK, GStreamer, Mesa, weston):
# the base carries no application. Installed the same apt way as
# RUNTIME_PACKAGES (see `install_runtime_packages`'s `packages` parameter)
# rather than vendored as a wheelhouse -- the bootstrapper's dependency
# needs no per-app hash pin, unlike the Player's wheelhouse (gate #6).
BASE_RUNTIME_PACKAGES = tuple(OS_DEFINITION["base_runtime_packages"])
SNAPSHOT = OS_DEFINITION["snapshot"]
