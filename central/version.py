"""Central's own version, one home: the one value its hub states (CentralBirth) and the Home
Assistant settings page shows beside Galleria's. Read from the installed `photo-wall`
distribution by each composition root (the API's `create_app`, the worker's hub session); a
caller running from source without the distribution installed (a test harness) passes its own."""
from __future__ import annotations

from importlib.metadata import version as distribution_version
from typing import Final

CENTRAL_DISTRIBUTION: Final = "photo-wall"


def central_version() -> str:
    """The installed distribution's version; raises PackageNotFoundError when it is not installed."""
    return distribution_version(CENTRAL_DISTRIBUTION)


__all__ = ["CENTRAL_DISTRIBUTION", "central_version"]
