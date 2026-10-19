"""The calibration target's QR on the Player (decision 0027): the modules of the code naming this
port's picture change, encoded by segno (Debian's python3-segno, prior art: ISO/IEC 18004).

The Player knows the three names the code carries: its own player id (its registration), the
Output and the picture change (contracts/node_frame.py PictureOperations.output_id,
picture_change), so the wire carries no QR. The screen board's layout is
contracts.calibration_target.board_raster (OpenCV drew it at build time); the scalar reference of
what is drawn is player/geometry.py `board_signal`, which draws these modules in every QR copy.
This module only encodes. segno is imported where it encodes, never at load: only the Pi has it
(Central's image loads player.service in its qualification; tests/test_node_rollout_image_check.py).
"""
from __future__ import annotations

from contracts.calibration_target import QR_ERROR, QrModules, TargetCode, code_text


def target_modules(code: TargetCode) -> QrModules:
    """The QR of contracts.calibration_target.code_text(code): segno.make(text, error=QR_ERROR,
    micro=False, boost_error=False), its matrix without the quiet zone, True for a dark module.
    Deterministic for one code (the reader decodes it with OpenCV's QRCodeDetector). Pure."""
    import segno

    symbol = segno.make(code_text(code), error=QR_ERROR, micro=False, boost_error=False)
    return tuple(tuple(bool(module) for module in row) for row in symbol.matrix_iter(scale=1, border=0))


__all__ = ["target_modules"]
