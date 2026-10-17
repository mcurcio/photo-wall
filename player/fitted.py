"""A layer's fitted picture: its photo as its Frame shows it (the Frame fit, the photo's crop, the
transfer to linear light, at full strength), drawn once into its own texture and then mixed by
each frame at the layer's strength, before the Display's picture settings (docs/module-native-
renderer.md, "Fitted pictures"). The one home of what makes a fitted picture stale and of when a
layer counts as prepared. Pure: no GL, GTK or renderer state.

A fitted picture is drawn for one `FitKey`. Whatever changes the pixels it holds is in the key, so
it is redrawn exactly when the key changes. A layer's strength (a fade) and the Display's picture
settings are not in it: a frame applies them to the fitted pictures it mixes, so neither redraws
one.
"""

from __future__ import annotations

from dataclasses import dataclass

from contracts.models import CornersFit, CoverFit, PhotoCrop

FITTED_BYTES = 8
"""Bytes per pixel of a fitted picture: RGBA at 16-bit floating point (GL_RGBA16F), linear light.
An 8-bit store (sRGB-encoded) misses the one-code bound by up to 3-11 codes once a print look
bends the light after it (/Volumes/Dock/tmp/render-cost/cache_format.py; design "Cache format")."""


@dataclass(frozen=True)
class FitKey:
    """Everything that changes a layer's fitted picture."""

    decode: tuple[int, int]
    """The decoder's incarnation and seek generation: another decode of the photo or video."""
    sample: int
    """The decoder's sample serial: a playing video's newest picture (constant for a still)."""
    fit: CornersFit | CoverFit
    """The drawn Frame fit: Position's calibration trial's while one is drawn, else the binding's."""
    aspect: float
    """The Frame profile's aspect, which sets Cover's rectangle and the photo's crop."""
    crop: PhotoCrop | None
    size: tuple[int, int]
    """The Output's drawn size in pixels, which is the fitted picture's size."""

    def same_picture(self, other: FitKey | None) -> bool:
        """`other` holds this key's picture but perhaps an older sample of its video: what a frame
        may show at once, redrawing only the newer sample."""
        return other is not None and (self.decode, self.fit, self.aspect, self.crop, self.size) == (
            other.decode, other.fit, other.aspect, other.crop, other.size)


def prepared(drawn: FitKey | None, now: FitKey, done: bool) -> bool:
    """The one readiness rule, which the executor's prepare() reads: the layer's decode has a
    fitted picture (`drawn`, the key it was drawn for) and the GPU has finished drawing it (`done`).
    An edit since (a Position trial, a resize) leaves it prepared: the picture is redrawn before
    it is shown, and a grant is never revoked by an edit."""
    return drawn is not None and drawn.decode == now.decode and done


def shown_at_once(drawn: FitKey | None, now: FitKey, done: bool) -> bool:
    """Whether a frame may show a layer that is not on screen yet (its first frame, such as a
    fade's first): its fitted picture is drawn for this picture and finished. Otherwise the frame
    holds what is on screen and the planning tick draws it (never a frame: a fit costs about a
    4K frame's GPU time)."""
    return now.same_picture(drawn) and done
