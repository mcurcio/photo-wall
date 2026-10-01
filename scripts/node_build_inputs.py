"""Reviewed node builder inputs, shared by native and Debian closure builds.

The Debian snapshot/package declaration remains scripts.debian_packages.PIN.
Images must provide CA trust before authenticated snapshot normalization.
"""
from __future__ import annotations

import re

BUILDER_IMAGE = "python@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f"
ARCHITECTURES = ("arm64", "amd64")


def validate_builder(image: str, architecture: str, *, purpose: str) -> None:
    if not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64}", image):
        raise ValueError(purpose + "_builder_digest_required")
    if architecture not in ARCHITECTURES:
        raise ValueError(purpose + "_architecture")
