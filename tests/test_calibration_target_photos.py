"""The photo files' edge (decision 0027, slice R1b): a normal photo is converted from its own colour
profile to sRGB before it is decoded to linear light (an iPhone's JPEG or HEIC is Display P3), and the
lens's focal length comes from the file's EXIF, so the reading's viewing angle uses it."""
from __future__ import annotations

import io
import math
import warnings
from pathlib import Path

import numpy as np
import pytest
from calibration_target_scenes import Scene, entry, focal_px, jpeg_photo
from PIL import Image, ImageCms

from scripts.calibration_target import photos
from scripts.calibration_target.locate import Located, locate
from scripts.calibration_target.model import FocalSource, Reading
from scripts.calibration_target.read import read_photo

with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message='"Matplotlib" related API')
    import colour

DISPLAY_P3: bytes = (Path(__file__).parent / "calibration_target_profiles" / "DisplayP3-v4.icc").read_bytes()
SRGB: bytes = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
EXIF_IFD, FOCAL_35MM, ORIENTATION = 0x8769, 0xA405, 0x0112


def srgb_decoded(values: np.ndarray) -> np.ndarray:
    """IEC 61966-2-1's sRGB decoding, written out as the reference."""
    encoded = values / 255
    return np.where(encoded <= 0.04045, encoded / 12.92, ((encoded + 0.055) / 1.055) ** 2.4)


def jpeg(path: Path, values: np.ndarray, *, icc: bytes | None = None, exif: Image.Exif | None = None) -> Path:
    keywords: dict[str, object] = {"quality": 100, "subsampling": 0}
    if icc is not None:
        keywords["icc_profile"] = icc
    if exif is not None:
        keywords["exif"] = exif
    Image.fromarray(values).save(path, "JPEG", **keywords)
    return path


def with_focal(f35: int, orientation: int | None = None) -> Image.Exif:
    exif = Image.Exif()
    exif.get_ifd(EXIF_IFD)[FOCAL_35MM] = f35
    if orientation is not None:
        exif[ORIENTATION] = orientation
    return exif


# -- PhA1, PhA2: the file's colour profile -------------------------------------------------------------


TRUTH = np.array([0.8, 0.3, 0.1])          # linear sRGB, inside both gamuts


def p3_patch() -> np.ndarray:
    """TRUTH as a Display P3 file holds it: P3's own primaries, the sRGB transfer function, 8 bits."""
    linear_p3 = colour.RGB_to_RGB(TRUTH, "sRGB", "Display P3")
    encoded = colour.cctf_encoding(linear_p3, function="sRGB")
    return np.tile(np.round(encoded * 255).astype(np.uint8), (16, 16, 1))


def test_a_display_p3_jpeg_is_read_in_srgb(tmp_path: Path) -> None:
    tagged = photos.load_photo(jpeg(tmp_path / "p3.jpg", p3_patch(), icc=DISPLAY_P3))
    untagged = photos.load_photo(jpeg(tmp_path / "bare.jpg", p3_patch()))
    read = tagged.pixels.reshape(-1, 3).mean(axis=0)
    assert np.all(np.abs(read - TRUTH) <= 0.01), read
    assert np.max(np.abs(untagged.pixels.reshape(-1, 3).mean(axis=0) - TRUTH)) > 0.03


def test_an_srgb_tagged_jpeg_reads_as_an_untagged_one_decoded_as_srgb(tmp_path: Path) -> None:
    rng = np.random.default_rng(3)
    values = rng.integers(0, 256, (16, 16, 3), dtype=np.uint8)
    tagged = photos.load_photo(jpeg(tmp_path / "srgb.jpg", values, icc=SRGB))
    untagged = photos.load_photo(jpeg(tmp_path / "bare.jpg", values))
    as_codes = colour.cctf_encoding(np.stack([tagged.pixels, untagged.pixels]), function="sRGB") * 255
    assert np.max(np.abs(as_codes[0] - as_codes[1])) <= 1.0
    with Image.open(tmp_path / "bare.jpg") as image:
        stored = np.asarray(image.convert("RGB"))
    assert np.allclose(untagged.pixels, srgb_decoded(stored), atol=1e-6)


# -- PhA3: the focal length --------------------------------------------------------------------------------


def test_the_focal_length_is_the_35_mm_equivalent_over_the_diagonal(tmp_path: Path) -> None:
    with Image.open(jpeg(tmp_path / "f.jpg", np.zeros((30, 40, 3), np.uint8), exif=with_focal(24))) as image:
        exif = image.getexif()
    assert photos.focal_px(exif, (4032, 3024)) == pytest.approx(24 / 43.27 * 5040, abs=0.1)
    with Image.open(jpeg(tmp_path / "none.jpg", np.zeros((30, 40, 3), np.uint8))) as image:
        assert photos.focal_px(image.getexif(), (4032, 3024)) is None


def test_a_turned_file_uses_its_turned_size(tmp_path: Path) -> None:
    photo = photos.load_photo(jpeg(tmp_path / "turned.jpg", np.zeros((30, 40, 3), np.uint8),
                                   exif=with_focal(26, orientation=6)))
    assert photo.pixels.shape == (40, 30, 3)
    assert photo.focal_px == pytest.approx(26 / 43.27 * math.hypot(30, 40), abs=1e-6)


def test_a_dngs_focal_length_is_read_from_its_exif(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A DNG's EXIF as Pillow opens it (here a file Pillow opens, named .dng; rawpy decodes the pixels)."""
    import rawpy

    class Raw:
        def __enter__(self) -> Raw:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def postprocess(self, **parameters: object) -> np.ndarray:
            return np.full((300, 400, 3), 1000, np.uint16)

    monkeypatch.setattr(rawpy, "imread", lambda name: Raw())
    path = jpeg(tmp_path / "IMG_0001.DNG", np.zeros((30, 40, 3), np.uint8), exif=with_focal(24))
    assert photos.load_photo(path).focal_px == pytest.approx(24 / 43.27 * 500, abs=1e-6)
    (tmp_path / "IMG_0002.DNG").write_bytes(b"not a TIFF Pillow opens")
    assert photos.load_photo(tmp_path / "IMG_0002.DNG").focal_px is None


# -- PhA4: end to end -------------------------------------------------------------------------------------


def test_a_jpeg_with_its_focal_length_reads_the_angle_from_exif(tmp_path: Path) -> None:
    scene = Scene()
    width, height = 2016, 1512
    f35 = round(focal_px(scene) * 43.27 / math.hypot(width, height))
    with Image.open(io.BytesIO(jpeg_photo(scene))) as image:
        image.save(tmp_path / "IMG_0003.jpg", "JPEG", quality=95, exif=with_focal(f35))
    found = locate(photos.load_photo(tmp_path / "IMG_0003.jpg"))
    assert isinstance(found, Located), found
    assert found.photo.focal_px == pytest.approx(focal_px(scene), rel=0.02)
    read = read_photo(found, entry(scene))
    assert isinstance(read, Reading), read
    assert read.angle.focal is FocalSource.EXIF
