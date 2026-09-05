"""Bounded Linux DRM connector discovery, independent of display initialization."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from contracts.enrollment import OutputReport


@dataclass(frozen=True)
class OutputDiscovery:
    outputs: tuple[OutputReport, ...]
    fault: str | None = None


def output_app_id(output_id: str) -> str:
    """Shared GTK app-ID/compositor routing convention, without native imports."""
    if not re.fullmatch(r"HDMI-A-[0-9]+", output_id):
        raise ValueError("invalid HDMI connector identity")
    return "photo-wall-" + output_id


def discover_outputs(root: Path = Path("/sys/class/drm")) -> OutputDiscovery:
    try:
        connectors: dict[str, OutputReport] = {}
        with os.scandir(root) as entries:
            for index, entry in enumerate(entries):
                if index >= 128:
                    raise ValueError("discovery_bound")
                match = re.fullmatch(r"card[0-9]+-(HDMI-A-[0-9]+)", entry.name)
                if not match:
                    continue
                output_id = match[1]
                if output_id in connectors or len(connectors) >= 2:
                    raise ValueError("ambiguous_outputs")
                with (Path(entry.path) / "status").open("rb") as stream:
                    status = stream.read(32).strip()
                if status not in (b"connected", b"disconnected", b"unknown"):
                    raise ValueError("invalid_connector_status")
                # DRM's modes lists available/preferred modes, not current scanout.
                # GTK's actual framebuffer allocation is authoritative for drawing.
                connectors[output_id] = OutputReport(output_id=output_id, width_px=0,
                    height_px=0, connected=status == b"connected")
        outputs = tuple(connectors[key] for key in sorted(connectors))
        return OutputDiscovery(outputs, None if outputs else "outputs_missing")
    except (OSError, ValueError):
        return OutputDiscovery((), "output_discovery")
