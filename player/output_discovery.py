"""Bounded Linux DRM connector discovery, independent of display initialization."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from contracts.enrollment import OutputReport
from contracts.node_output import OUTPUT_IDS


@dataclass(frozen=True)
class OutputDiscovery:
    outputs: tuple[OutputReport, ...]
    fault: str | None = None


def output_app_id(output_id: str) -> str:
    """The screen's xdg app ID, shared by the Player (player/plane_screen.py) and the compositor's
    routing, without native imports."""
    if not (re.fullmatch(r"HDMI-A-[0-9]+", output_id)
            or output_id in ("Virtual-1", "Virtual-2")):
        raise ValueError("invalid connector identity")
    return "photo-wall-" + output_id


def weston_ini() -> str:
    """Return the bounded compositor routing for every configured connector."""
    sections = ["[core]", "idle-time=0", "require-input=false", "xwayland=false",
                "", "[shell]", "background-color=0xff000000"]
    for output_id in OUTPUT_IDS:
        sections.extend(("", "[output]", f"name={output_id}",
                         f"app-ids={output_app_id(output_id)}"))
    return "\n".join(sections) + "\n"


def discover_outputs(root: Path = Path("/sys/class/drm")) -> OutputDiscovery:
    try:
        connectors: dict[str, OutputReport] = {}
        with os.scandir(root) as entries:
            for index, entry in enumerate(entries):
                if index >= 128:
                    raise ValueError("discovery_bound")
                match = re.fullmatch(r"card[0-9]+-(HDMI-A-[0-9]+|Virtual-[0-9]+)", entry.name)
                if not match:
                    continue
                output_id = match[1]
                output_app_id(output_id)
                if output_id in connectors or len(connectors) >= 2:
                    raise ValueError("ambiguous_outputs")
                with (Path(entry.path) / "status").open("rb") as stream:
                    status = stream.read(32).strip()
                if status not in (b"connected", b"disconnected", b"unknown"):
                    raise ValueError("invalid_connector_status")
                # DRM's modes lists available/preferred modes, not current scanout: the
                # mode as built is Weston's, measured once the screens connect (`measured`).
                connectors[output_id] = OutputReport(output_id=output_id, width_px=0,
                    height_px=0, connected=status == b"connected")
        outputs = tuple(connectors[key] for key in sorted(connectors))
        return OutputDiscovery(outputs, None if outputs else "outputs_missing")
    except (OSError, ValueError):
        return OutputDiscovery((), "output_discovery")


def measured(outputs: tuple[OutputReport, ...], modes: Mapping[str, tuple[int, int]]) -> tuple[OutputReport, ...]:
    """`outputs` with each connected Output's size from `modes` (output id -> its mode as built,
    width and height in physical pixels, never turned: player/plane_screen.py `PlaneScreens.mode`),
    in order: what the Player enrolls with, so Central's `outputs.observation` holds the size that
    sizes photos and checks a rectangle on its screen (render redesign S3, bead B6; S2's erratum, a
    Pi enrolled 0 x 0). An Output absent from `modes`, or disconnected, keeps 0 x 0. Pure."""
    sized = []
    for output in outputs:
        mode = modes.get(output.output_id) if output.connected else None
        sized.append(output if mode is None else
                     output.model_copy(update={"width_px": mode[0], "height_px": mode[1]}))
    return tuple(sized)
