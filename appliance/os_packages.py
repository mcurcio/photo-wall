"""Authoritative OS inputs shared by package preparation and baseline identity."""

import json
from pathlib import Path

OS_DEFINITION = json.loads(Path(__file__).with_name("os_definition.json").read_text())
BASE_BYTES = OS_DEFINITION["base_bytes"]
BASE_SHA256 = OS_DEFINITION["base_sha256"]
RUNTIME_PACKAGES = tuple(OS_DEFINITION["runtime_packages"])
SNAPSHOT = OS_DEFINITION["snapshot"]
