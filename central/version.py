"""Central's own version, one home: the one value its hub states (CentralBirth), the Home Assistant
settings page shows beside Galleria's, and which side to update is judged by
(central/infra/home_assistant/advice.py). Read by each composition root (the API's `create_app`,
the worker's hub session).

It is the release Central was built as, never a hand-edited number: the automated release flow's
version (its plan's `vX.Y.Z` tag, pyproject `[tool.commitizen]`), which the image build passes in
as RELEASE_ENV. Without it Central runs from source, not as a release: the installed distribution's
version stands in (pyproject's decorative `version`), as it always did there."""
from __future__ import annotations

import os
from importlib.metadata import version as distribution_version
from typing import Final

from central.kernel.types import release_version

CENTRAL_DISTRIBUTION: Final = "photo-wall"
RELEASE_ENV: Final = "PHOTO_WALL_RELEASE"   # the release tag the image was built as ("v0.45.0")


def central_version() -> str:
    """The release Central was built as, without its "v" ("0.45.0"); ValueError when RELEASE_ENV
    holds no release tag. Running from source (RELEASE_ENV unset or empty): the installed
    distribution's version; PackageNotFoundError when it is not installed."""
    tag = os.environ.get(RELEASE_ENV, "")
    if not tag:
        return distribution_version(CENTRAL_DISTRIBUTION)
    release_version(tag)   # ValueError("invalid_tag") for anything but a vX.Y.Z[-pre] tag
    return tag.removeprefix("v")


__all__ = ["CENTRAL_DISTRIBUTION", "RELEASE_ENV", "central_version"]
