"""The paper calibration target's printable sheets, as committed files (decisions 0026, 0027; owner,
2026-10-13: "photo-wall could give me a printable asset"; 2026-10-14: "The printable calibration
target should land as a likable [linkable] asset on GitHub somewhere, probably even the README. I
don’t want the assets replaced by every GHA run if they haven’t changed."). Pure: names and paths,
no I/O.

One PDF per sheet (contracts.calibration_target PaperSize) and one PNG preview live in SHEETS_DIR,
committed: drawn at build time by scripts/calibration_boards.py (the same generator as the boards'
rasters, deterministic), which a test regenerates and compares byte for byte, so only a layout
change rewrites them. Central's print route (central/calibration_target_routes.py) answers these
same files; nothing draws a sheet at run time. The README links them.
"""
from __future__ import annotations

from pathlib import Path
from typing import Final

from contracts.calibration_target import PaperSize

SHEETS_DIR: Final = Path(__file__).resolve().parent / "calibration_sheets"
PREVIEW_NAME: Final = "photo-wall-calibration-target.png"
PDF_MEDIA_TYPE: Final = "application/pdf"


def sheet_name(paper: PaperSize) -> str:
    """The file name of `paper`'s sheet: photo-wall-calibration-target-<paper>.pdf (also the name the
    route offers the download under). Pure."""
    return f"photo-wall-calibration-target-{paper.value}.pdf"


def sheet_path(paper: PaperSize) -> Path:
    """Where `paper`'s committed sheet is: SHEETS_DIR / sheet_name(paper). Pure."""
    return SHEETS_DIR / sheet_name(paper)


__all__ = ["PDF_MEDIA_TYPE", "PREVIEW_NAME", "SHEETS_DIR", "sheet_name", "sheet_path"]
