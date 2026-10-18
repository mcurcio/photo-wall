"""Bounded local preparation; source selection and blob publication live elsewhere."""

from __future__ import annotations

import asyncio
import hashlib
import io
import itertools
import json
import math
import os
import platform
import re
import resource
import shutil
import signal
import stat
import struct
import sys
import warnings
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Final, Literal

import pillow_heif
from pydantic import Field, TypeAdapter, ValidationError

from central.kernel.assets import MediaReady
from central.photo_library.model import Kind, LibraryAsset
from central.photo_library.policy import STILL_MEDIA_TYPES as STILL_MEDIA_TYPES  # re-exported
from central.photo_library.rendition import FetchStep, FrameNeed, need_step
from contracts.models import Digest, Model, Positive
from contracts.time import Clock, SystemClock
from media.library_client import FetchedRendition
from media.models import MediaError

_TRANSFORMS = {
    1: (), 2: ("hflip",), 3: ("hflip", "vflip"), 4: ("vflip",),
    5: ("transpose=clock", "hflip"), 6: ("transpose=clock",),
    7: ("transpose=clock", "vflip"), 8: ("transpose=cclock",),
}
_PIPES = {"JPEG": "jpeg_pipe", "PNG": "png_pipe", "WEBP": "webp_pipe"}
_MATRICES = {
    (1, 0, 0, 1): 1, (-1, 0, 0, 1): 2, (-1, 0, 0, -1): 3, (1, 0, 0, -1): 4,
    (0, 1, 1, 0): 5, (0, 1, -1, 0): 6, (0, -1, -1, 0): 7, (0, -1, 1, 0): 8,
}


# HEIC and HEIF through libheif (pillow-heif), once per process: this module's importers (the
# worker, the Immich client) and its bounded inspector, which runs it as `-m media.prepare`.
# AVIF is Pillow's own (libavif). Owner, 2026-10-11: "lean on strong foundational libraries".
pillow_heif.register_heif_opener()

# STILL_MEDIA_TYPES (central/photo_library/policy.py) are the still types preparation reads;
# STILL_FORMATS are the Pillow decoders that read them.
STILL_FORMATS: Final = ("JPEG", "PNG", "WEBP", "HEIF", "AVIF")
# Decoded by Pillow, then handed to ffmpeg as one lossless PNG (ffmpeg reads JPEG, PNG and WEBP).
_DECODED_ONLY: Final = frozenset({"HEIF", "AVIF"})
# H.273 transfer characteristics of HDR stills Photo Wall does not tone-map: PQ and HLG.
_HDR_TRANSFERS: Final = frozenset({16, 18})
# H.273 colour primaries an untagged still may carry and still be sRGB: BT.709 and unspecified.
_SRGB_PRIMARIES: Final = frozenset({1, 2})


class StillRecipe(Model):
    """How a photo's one copy is made: upright, at most `long_edge` pixels on its longer side
    (the wall need's step, central/media_repository.py `variant_recipe`), one JPEG at
    `jpeg_quality`. A video's encoding is not here, so changing it renames no photo's copy."""

    kind: Literal["image"] = "image"
    # 2: preparation reads the photo Immich shows (roadmap 4, M3): every preparation is redone once.
    # 3: HEIC, AVIF and wide-colour photos, converted to sRGB (follow-ups 2026-10-11, A5).
    version: Literal[3] = 3
    long_edge: int = Field(default=3840, ge=2, le=16384, strict=True)
    jpeg_quality: int = Field(default=3, ge=1, le=31, strict=True)


class VideoRecipe(Model):
    """How a video's one copy is made (owner, gate answer Q5, 2026-10-18: "HEVC now, one 1080p
    copy per video"; "1080p at 30 is fine"): upright, square pixels, at most `long_edge` on its
    longer side and `max_pixels` in all, at most `fps` frames a second, SDR BT.709 limited range,
    HEVC Main (8-bit 4:2:0) level 4.1 Main tier in an `hvc1` MP4 with `+faststart`, constant
    quality `crf` at x265's `preset`, a key frame every `gop_seconds`, no audio, subtitles, data
    or metadata. What the Pi 5's hardware decoder plays (player/decode.py `VIDEO_DECODER`)."""

    kind: Literal["video"] = "video"
    # 1-3 were the one recipe photos and videos shared (H.264 High 4.2).
    # 4: HEVC Main, level 4.1, hvc1, by libx265 (render planes S4): every video is prepared again.
    version: Literal[4] = 4
    long_edge: int = Field(default=1920, ge=2, le=1920, strict=True)
    max_pixels: int = Field(default=2_073_600, ge=4, le=2_073_600, strict=True)
    fps: Positive = Field(default=30, le=30)
    crf: int = Field(default=24, ge=1, le=51, strict=True)
    preset: Literal["medium", "fast", "slow"] = "medium"
    gop_seconds: Positive = Field(default=2, le=10)


@dataclass(frozen=True)
class HouseRecipes:
    """The recipes Photo Wall prepares by now, one per kind: what `Preparer` uses when no recipe
    is given and what the media repository names copies by."""

    still: StillRecipe = StillRecipe()
    video: VideoRecipe = VideoRecipe()

    def of(self, kind: Kind) -> StillRecipe | VideoRecipe:
        """`still` for "image", `video` for "video"; ValueError("recipe_kind") for anything
        else. Pure."""
        if kind == "image":
            return self.still
        if kind == "video":
            return self.video
        raise ValueError("recipe_kind")


# The recipe a copy is prepared by, either kind; `kind` tells them apart, so a stored recipe reads
# back as the model it was written by (`stored_recipe`).
PreparationRecipe = Annotated[StillRecipe | VideoRecipe, Field(discriminator="kind")]
_STORED: Final = TypeAdapter(PreparationRecipe)


def stored_recipe(value: object) -> StillRecipe | VideoRecipe | None:
    """A recipe as `media_recipes.recipe` stores it (`model_dump(mode="json")`) read back by the
    current models, or None when they refuse it (a recipe from before S4, one recipe for both
    kinds, has no `kind`): a variant named by it cannot be re-made. The one reader of a stored
    recipe. Pure; never raises."""
    try:
        return _STORED.validate_python(value)
    except ValidationError:
        return None


def recipe_id(recipe: PreparationRecipe, need: FrameNeed) -> str:
    """The one home of what a media variant's recipe id hashes: the recipe values and the step
    of the wall need they size for (`need_step`, its long edge; follow-up S1: one copy per
    photo for the whole wall, so photos of every shape on one step share a recipe;
    `<asset_id>.<recipe_id>` names a variant, kernel/types.py). The renderer build is left out
    (follow-up design, the owner's open choice, default "drop"): a worker upgrade does not
    re-name every variant. Keeping it is a change to this body only."""
    return hashlib.sha256(json.dumps(
        dict(recipe=recipe.model_dump(mode="json"), need=need_step(need)),
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


class PreparationLimits(Model):
    """What preparation accepts; the home of the size bounds the Immich client's fetch also
    checks (media.immich.LibraryLimits reads its defaults from here)."""

    max_original_bytes: int = Field(default=256 * 1024**2, ge=1, strict=True)
    max_pixels: int = Field(default=64_000_000, ge=1, strict=True)
    max_dimension: int = Field(default=16384, ge=1, strict=True)
    max_video_seconds: Positive = 300
    max_image_bytes: int = Field(default=16 * 1024**2, ge=1, strict=True)
    max_video_bytes: int = Field(default=256 * 1024**2, ge=1, strict=True)
    max_staging_bytes: int = Field(default=512 * 1024**2, ge=1, strict=True)
    wall_seconds: Positive = 180
    probe_seconds: Positive = 15
    max_tool_output_bytes: int = Field(default=64 * 1024, ge=1, strict=True)
    cpu_seconds: Positive = 180
    memory_bytes: int = Field(default=1024**3, ge=64 * 1024**2, strict=True)
    threads: int = Field(default=1, ge=1, le=4, strict=True)
    # Render planes S4: the HEVC encoder's threads (x265's pool); its process's CPU limit is
    # `cpu_seconds` times this. Measured 2026-10-18: x265 costs ~1.8x the H.264 encoder's CPU, and 4
    # threads cut its wall time ~3.5x, so a video's preparation still fits `wall_seconds`.
    video_threads: int = Field(default=4, ge=1, le=8, strict=True)


class BuildIdentity(Model):
    preparation_sha256: Digest
    ffmpeg_sha256: Digest
    ffprobe_sha256: Digest
    ffmpeg_version: str
    ffprobe_version: str
    python_version: str
    pillow_version: str
    littlecms_version: str
    libheif_version: str
    jpeg_version: str
    zlib_version: str
    platform: str
    memory_limit_enforced: bool


class PreparedMedia(Model):
    path: Path
    # The file's facts as the variant's record keeps them: its bytes, what a Player needs to
    # show it, and which of Immich's copies it was made from (`FetchedRendition.rendition`;
    # follow-ups 2026-10-11, owner item 6).
    ready: MediaReady
    original_sha256: Digest
    build: BuildIdentity


@dataclass
class _Deadline:
    clock: Clock
    start: float
    end: float

    def remaining(self) -> float:
        now = self.clock.monotonic()
        if not math.isfinite(now) or now < self.start:
            raise MediaError("clock_invalid")
        if now >= self.end:
            raise MediaError("preparation_timeout")
        return self.end - now


def _file_hashes(path: Path, maximum: int, deadline: _Deadline | None = None) -> tuple[int, str, str]:
    """Bounded regular-file read; refuse a symlink and mutation during the read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise MediaError("unsupported_media", "incompatible")
            if not 0 < before.st_size <= maximum:
                raise MediaError("preparation_limit", "incompatible")
            sha1, sha256, size = hashlib.sha1(), hashlib.sha256(), 0
            while chunk := stream.read(64 * 1024):
                if deadline:
                    deadline.remaining()
                size += len(chunk)
                if size > maximum:
                    raise MediaError("preparation_limit", "incompatible")
                sha1.update(chunk)
                sha256.update(chunk)
            after = os.fstat(stream.fileno())
            if (size != before.st_size or before.st_mtime_ns != after.st_mtime_ns
                    or before.st_size != after.st_size):
                raise MediaError("asset_integrity")
            return size, sha1.hexdigest(), sha256.hexdigest()
    except OSError:
        raise MediaError("preparation_io") from None


def _png_profile(path: Path) -> None:
    """Read only bounded headers; Pillow intentionally ignores some new HDR chunks."""
    with path.open("rb") as stream:
        if stream.read(8) != b"\x89PNG\r\n\x1a\n":
            raise MediaError("unsupported_media", "incompatible")
        count = 0
        while True:
            count += 1
            if count > 10000:
                raise MediaError("preparation_limit", "incompatible")
            header = stream.read(8)
            if len(header) != 8:
                raise MediaError("preparation_invalid", "incompatible")
            length, kind = struct.unpack(">I4s", header)
            if kind == b"IHDR":
                data = stream.read(length) if length == 13 else b""
                if len(data) != 13 or data[8] != 8 or data[9] not in (0, 2):
                    raise MediaError("unsupported_media", "incompatible")
            elif kind in (b"cICP", b"mDCv", b"cLLi"):
                raise MediaError("unsupported_color", "incompatible")
            else:
                stream.seek(length, os.SEEK_CUR)
            if len(stream.read(4)) != 4:
                raise MediaError("preparation_invalid", "incompatible")
            if kind == b"IEND":
                return


def _icc_kind(data: bytes) -> Literal["srgb", "rgb"]:
    """Whether an ICC profile is sRGB or another RGB profile LittleCMS converts from; anything
    else (not RGB, unreadable, a PQ or HLG transfer in its `cicp` tag) is unsupported_color.
    Validate profile behavior, never an attacker-controlled profile description."""
    from PIL import Image, ImageCms

    if len(data) > 1024**2:
        raise MediaError("preparation_limit", "incompatible")
    if _icc_transfer(data) in _HDR_TRANSFERS:
        raise MediaError("unsupported_color", "incompatible")
    try:
        source = ImageCms.ImageCmsProfile(io.BytesIO(data))
        if source.profile.xcolor_space.strip() != "RGB":
            raise MediaError("unsupported_color", "incompatible")
        levels = list(range(0, 256, 16)) + [255]
        colors = list(itertools.product(levels, repeat=3)) + [(i, i, i) for i in range(256)]
        grid = Image.new("RGB", (len(colors), 1))
        grid.putdata(colors)
        transformed = ImageCms.profileToProfile(
            grid, source, ImageCms.createProfile("sRGB"), renderingIntent=1, outputMode="RGB",
        )
    except (ValueError, TypeError, OSError, ImageCms.PyCMSError):
        raise MediaError("unsupported_color", "incompatible") from None
    same = all(abs(a - b) <= 2 for a, b in zip(grid.tobytes(), transformed.tobytes(), strict=True))
    return "srgb" if same else "rgb"


def _icc_transfer(data: bytes) -> int | None:
    """The transfer characteristic of an ICC profile's `cicp` tag (ICC 4.4), if it has one."""
    if len(data) < 132:
        return None
    count = struct.unpack(">I", data[128:132])[0]
    for index in range(min(count, (len(data) - 132) // 12)):
        signature, offset, size = struct.unpack(">4sII", data[132 + 12 * index:144 + 12 * index])
        if signature == b"cicp" and size >= 12 and offset + 12 <= len(data):
            return data[offset + 9]
    return None


def _bmff_colour(path: Path) -> tuple[set[int], set[int]]:
    """The colour primaries and transfer characteristics every `nclx` colour box of a HEIF or
    AVIF file's `meta` box names (ISO/IEC 23008-12; H.273 code points). Reads only the box
    headers and the `meta` box itself."""
    primaries: set[int] = set()
    transfers: set[int] = set()
    with path.open("rb") as stream:
        for _ in range(10000):
            header = stream.read(8)
            if len(header) < 8:
                return primaries, transfers
            size, kind = struct.unpack(">I4s", header)
            start = 8
            if size == 1:
                size, start = struct.unpack(">Q", stream.read(8))[0], 16
            elif size == 0:                       # the box runs to the end of the file
                size = start + 1024**2 + 1 if kind == b"meta" else 2**63
            if size < start:
                raise MediaError("preparation_invalid", "incompatible")
            if kind != b"meta":
                if size == 2**63:
                    return primaries, transfers
                stream.seek(size - start, os.SEEK_CUR)
                continue
            if size - start > 1024**2:
                raise MediaError("preparation_limit", "incompatible")
            box = stream.read(size - start)
            at = box.find(b"colrnclx")
            while at >= 0 and at + 14 <= len(box):
                primary, transfer = struct.unpack(">HH", box[at + 8:at + 12])
                primaries.add(primary)
                transfers.add(transfer)
                at = box.find(b"colrnclx", at + 8)
    raise MediaError("preparation_limit", "incompatible")


def _bounded_decoders(limits: dict) -> None:
    """Pillow's bounds for one inspector process (runs only there)."""
    from PIL import Image, ImageFile, PngImagePlugin

    Image.MAX_IMAGE_PIXELS = limits["max_pixels"]
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    PngImagePlugin.MAX_TEXT_CHUNK = 1024**2
    PngImagePlugin.MAX_TEXT_MEMORY = 2 * 1024**2
    warnings.simplefilter("error")


def _inspect_still(path: Path, limits: dict) -> dict:
    """This function runs only in the disposable, bounded inspector process.

    `intermediate`: the still goes to ffmpeg through one lossless sRGB PNG (`_intermediate`):
    a HEIC or AVIF (ffmpeg reads neither), or a still whose RGB ICC profile is not sRGB (owner,
    2026-10-11, item 3: LittleCMS converts it). A PQ or HLG still is refused; a gain-map photo
    is read as its base image (the decoders return the primary image)."""
    from PIL import Image

    _bounded_decoders(limits)
    with Image.open(path, formats=STILL_FORMATS) as image:
        width, height = image.size
        if (width > limits["max_dimension"] or height > limits["max_dimension"]
                or width * height > limits["max_pixels"]):
            raise MediaError("preparation_limit", "incompatible")
        if (image.mode not in ("RGB", "L") or getattr(image, "n_frames", 1) != 1
                or "transparency" in image.info):
            raise MediaError("unsupported_media", "incompatible")
        if image.format == "PNG":
            _png_profile(path)
        icc = image.info.get("icc_profile")
        if image.format in _DECODED_ONLY:
            primaries, transfers = _bmff_colour(path)
            if transfers & _HDR_TRANSFERS or (not icc and primaries - _SRGB_PRIMARIES):
                raise MediaError("unsupported_color", "incompatible")
        exif = image.getexif()
        orientation = exif.get(274, 1)
        if type(orientation) is not int or orientation not in _TRANSFORMS:
            raise MediaError("metadata_mismatch", "incompatible")
        color_space = exif.get(40961)
        # ColorSpace normally lives in the EXIF sub-IFD, not the top-level IFD.
        if 34665 in exif:
            color_space = exif.get_ifd(34665).get(40961, color_space)
        if color_space not in (None, 1, 65535):
            raise MediaError("unsupported_color", "incompatible")
        if icc is not None and not isinstance(icc, bytes):
            raise MediaError("unsupported_color", "incompatible")
        convert = bool(icc) and _icc_kind(icc) == "rgb"
        if color_space == 65535 and not icc:
            raise MediaError("unsupported_color", "incompatible")
        gamma = image.info.get("gamma")
        if gamma is not None and (not isinstance(gamma, (int, float)) or abs(gamma - .45455) > .0001):
            raise MediaError("unsupported_color", "incompatible")
        chroma = image.info.get("chromaticity")
        srgb = (.3127, .3290, .64, .33, .30, .60, .15, .06)
        if chroma is not None and (len(chroma) != 8 or any(
            abs(a - b) > .0001 for a, b in zip(chroma, srgb, strict=True)
        )):
            raise MediaError("unsupported_color", "incompatible")
        image.load()
        return dict(format=image.format, width=width, height=height, orientation=orientation,
                    intermediate=convert or image.format in _DECODED_ONLY)


def _intermediate(path: Path, output: Path, limits: dict) -> dict:
    """The still as one lossless sRGB PNG, upright, holding pixels only (runs only in the
    bounded inspector process, after `_inspect_still` accepted `path`): an RGB ICC profile is
    converted to sRGB by LittleCMS (perceptual intent)."""
    from PIL import Image, ImageCms, ImageOps

    _bounded_decoders(limits)
    with Image.open(path, formats=STILL_FORMATS) as image:
        icc = image.info.get("icc_profile")
        pixels = ImageOps.exif_transpose(image).convert("RGB")
    if icc and _icc_kind(icc) == "rgb":
        pixels = ImageCms.profileToProfile(
            pixels, ImageCms.ImageCmsProfile(io.BytesIO(icc)), ImageCms.createProfile("sRGB"),
            renderingIntent=ImageCms.Intent.PERCEPTUAL, outputMode="RGB")
    clean = Image.new("RGB", pixels.size)
    clean.paste(pixels)
    with output.open("xb") as stream:
        clean.save(stream, "PNG", compress_level=1)
    return dict(width=clean.width, height=clean.height)


def _worker(payload: dict) -> None:
    """Set limits after a fresh exec, avoiding preexec_fn in a threaded worker."""
    limits = payload["limits"]
    cpu = max(1, math.ceil(payload.get("cpu_seconds") or limits["cpu_seconds"]))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (payload["file_limit"], payload["file_limit"]))
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (limits["memory_bytes"], limits["memory_bytes"]))
    operation = payload["operation"]
    if operation == "exec":
        os.execv(payload["argv"][0], payload["argv"])
    if operation == "inspect":
        result = _inspect_still(Path(payload["path"]), limits)
    elif operation == "intermediate":
        result = _intermediate(Path(payload["path"]), Path(payload["output"]), limits)
    elif operation == "identity":
        import PIL
        from PIL import features
        result = dict(
            python_version=platform.python_version(), pillow_version=PIL.__version__,
            littlecms_version=features.version("littlecms2") or "unavailable",
            libheif_version=pillow_heif.libheif_version() or "unavailable",
            jpeg_version=features.version("jpg") or "unavailable",
            zlib_version=features.version("zlib") or "unavailable",
        )
    else:
        raise MediaError("preparation_invalid", "incompatible")
    print(json.dumps(result, allow_nan=False))


class Preparer:
    """Prepares one copy of a photo or a video by its kind's recipe (`HouseRecipes`, or the
    variant's own), each tool in a fresh process bounded by `limits`: a still to one upright sRGB
    JPEG, a video to one HEVC Main level 4.1 `hvc1` MP4 (`VideoRecipe`), checked against its
    recipe before it is handed back."""

    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe", *,
                 recipes: HouseRecipes = HouseRecipes(),
                 limits: PreparationLimits = PreparationLimits(), clock: Clock | None = None) -> None:
        self.recipes, self.limits, self.clock = recipes, limits, clock or SystemClock()
        self.ffmpeg, self.ffprobe = self._executable(ffmpeg), self._executable(ffprobe)
        self._build: BuildIdentity | None = None
        self._build_key: tuple[str, str, str] | None = None

    @staticmethod
    def _executable(name: str) -> Path:
        path = shutil.which(name)
        if path is None:
            raise MediaError("preparation_tool_missing", "incompatible")
        try:
            return Path(path).resolve(strict=True)
        except OSError:
            raise MediaError("preparation_tool_missing", "incompatible") from None

    async def _run(self, payload: dict, deadline: _Deadline, *, seconds: float | None = None,
                   file_limit: int | None = None, cpu_seconds: float | None = None) -> bytes:
        """`payload` in one bounded process; its CPU limit is `cpu_seconds` when given (the
        video encode's, for its threads), else `limits.cpu_seconds`."""
        timeout = min(deadline.remaining(), seconds or self.limits.wall_seconds)
        message = json.dumps({**payload, "limits": self.limits.model_dump(),
                              "file_limit": file_limit or self.limits.max_video_bytes,
                              "cpu_seconds": cpu_seconds}).encode()
        if len(message) > 64 * 1024:
            raise MediaError("preparation_limit", "incompatible")
        process = None
        completed = False
        tasks: list[asyncio.Task] = []
        try:
            async with asyncio.timeout(timeout):
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "media.prepare", "_worker",
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, start_new_session=True,
                )

                async def read(stream: asyncio.StreamReader) -> bytes:
                    chunks, size = [], 0
                    while chunk := await stream.read(16 * 1024):
                        deadline.remaining()
                        size += len(chunk)
                        if size > self.limits.max_tool_output_bytes:
                            raise MediaError("preparation_limit", "incompatible")
                        chunks.append(chunk)
                    return b"".join(chunks)

                tasks = [asyncio.create_task(read(process.stdout)),
                         asyncio.create_task(read(process.stderr)),
                         asyncio.create_task(process.wait())]
                process.stdin.write(message)
                await process.stdin.drain()
                process.stdin.close()
                output, _, returncode = await asyncio.gather(*tasks)
                deadline.remaining()
                if returncode:
                    code = ("preparation_limit" if returncode in (-signal.SIGXFSZ, -signal.SIGXCPU)
                            else "preparation_invalid")
                    raise MediaError(code, "incompatible")
                completed = True
                return output
        except TimeoutError:
            raise MediaError("preparation_timeout") from None
        except OSError:
            raise MediaError("preparation_io") from None
        finally:
            if process is not None and not completed:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if process.returncode is None:
                    await process.wait()
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _json(self, payload: dict, deadline: _Deadline, *, seconds: float | None = None,
                    file_limit: int | None = None) -> dict:
        data = await self._run(payload, deadline, seconds=seconds or self.limits.probe_seconds,
                               file_limit=file_limit)
        try:
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError
            if "error" in result:
                code = result["error"]
                if code not in {"preparation_limit", "preparation_invalid", "unsupported_media",
                                "unsupported_color", "metadata_mismatch"}:
                    code = "preparation_invalid"
                raise MediaError(code, "incompatible")
            return result
        except (ValueError, RecursionError, UnicodeError):
            raise MediaError("preparation_invalid", "incompatible") from None

    async def _identity(self, deadline: _Deadline) -> BuildIdentity:
        def fingerprint() -> tuple[str, str, str]:
            return (_file_hashes(self.ffmpeg, 128 * 1024**2, deadline)[2],
                    _file_hashes(self.ffprobe, 128 * 1024**2, deadline)[2],
                    _file_hashes(Path(__file__), 1024**2, deadline)[2])

        key = fingerprint()
        if self._build is None or self._build_key != key:
            ffmpeg_version = await self._run(dict(operation="exec", argv=[str(self.ffmpeg), "-version"]),
                                              deadline, seconds=self.limits.probe_seconds)
            ffprobe_version = await self._run(dict(operation="exec", argv=[str(self.ffprobe), "-version"]),
                                               deadline, seconds=self.limits.probe_seconds)
            inspector = await self._json(dict(operation="identity"), deadline)
            if fingerprint() != key:
                raise MediaError("preparation_build_changed")
            self._build = BuildIdentity(
                ffmpeg_sha256=key[0], ffprobe_sha256=key[1], preparation_sha256=key[2],
                ffmpeg_version=ffmpeg_version.decode("utf-8", errors="replace"),
                ffprobe_version=ffprobe_version.decode("utf-8", errors="replace"),
                **inspector, platform=platform.platform(),
                memory_limit_enforced=sys.platform.startswith("linux"),
            )
            self._build_key = key
        return self._build

    def _input_flags(self) -> list[str]:
        return ["-max_alloc", str(self.limits.memory_bytes // 2),
                "-protocol_whitelist", "file",
                "-format_whitelist", "jpeg_pipe,png_pipe,webp_pipe,mov",
                "-probesize", "5000000", "-analyzeduration", "1000000",
                "-threads", str(self.limits.threads)]

    async def _probe(self, path: Path, deadline: _Deadline) -> dict:
        entries = ("stream=index,codec_name,codec_type,width,height,duration,avg_frame_rate,"
                   "r_frame_rate,pix_fmt,sample_aspect_ratio,color_space,color_primaries,"
                   "color_transfer,color_range,profile,level,codec_tag_string:stream_side_data:"
                   "format=format_name,duration:stream_tags=rotate")
        return await self._json(dict(operation="exec", argv=[str(self.ffprobe), "-v", "error",
                                *self._input_flags(), "-show_entries", entries,
                                "-of", "json", str(path)]), deadline)

    @staticmethod
    def _orientation(stream: dict) -> int:
        matrices = [side for side in stream.get("side_data_list", [])
                    if side.get("side_data_type") == "Display Matrix"]
        if len(matrices) > 1:
            raise MediaError("unsupported_media", "incompatible")
        if matrices:
            value = matrices[0].get("displaymatrix")
            try:
                numbers = [int(n) for line in value.strip().splitlines()
                           for n in line.split(":", 1)[1].split()]
                if len(numbers) != 9 or any(numbers[i] for i in (2, 5, 6, 7)) or numbers[8] != 1 << 30:
                    raise ValueError
                corners = tuple(numbers[i] for i in (0, 1, 3, 4))
                if any(n not in (-65536, 0, 65536) for n in corners):
                    raise ValueError
                return _MATRICES[tuple(n // 65536 for n in corners)]
            except (ValueError, KeyError, IndexError, AttributeError, TypeError):
                raise MediaError("unsupported_media", "incompatible") from None
        rotation = stream.get("tags", {}).get("rotate", "0")
        try:
            number = float(rotation)
            if not math.isfinite(number) or number % 90:
                raise ValueError
            return {0: 1, 90: 8, 180: 3, 270: 6}[number % 360]
        except (ValueError, TypeError):
            raise MediaError("unsupported_media", "incompatible") from None

    @staticmethod
    def _number(value: object) -> float:
        try:
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ValueError
            if isinstance(value, str) and len(value) > 64:
                raise ValueError
            result = float(value)
            if not math.isfinite(result) or result <= 0:
                raise ValueError
            return result
        except (ValueError, OverflowError, TypeError):
            raise MediaError("preparation_invalid", "incompatible") from None

    @classmethod
    def _fraction(cls, value: object, *, default: str | None = None) -> float:
        if value in (None, "N/A", "0/0", "0:1") and default is not None:
            value = default
        try:
            if not isinstance(value, str) or len(value) > 32:
                raise ValueError
            return cls._number(float(Fraction(value.replace(":", "/"))))
        except (ValueError, ZeroDivisionError, OverflowError):
            raise MediaError("preparation_invalid", "incompatible") from None

    def _video(self, probe: dict, *, output: bool = False) -> dict:
        streams = probe.get("streams")
        if not isinstance(streams, list) or len(streams) > 16:
            raise MediaError("unsupported_media", "incompatible")
        videos = [s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"]
        if len(videos) != 1 or (output and len(streams) != 1):
            raise MediaError("unsupported_media", "incompatible")
        stream = videos[0]
        if stream.get("codec_name") not in (("hevc",) if output else ("h264", "hevc")):
            raise MediaError("unsupported_media", "incompatible")
        width, height = stream.get("width"), stream.get("height")
        if (type(width) is not int or type(height) is not int
                or not 0 < width <= self.limits.max_dimension
                or not 0 < height <= self.limits.max_dimension
                or width * height > self.limits.max_pixels):
            raise MediaError("preparation_limit", "incompatible")
        fmt = probe.get("format", {})
        if not isinstance(fmt, dict) or "mov" not in fmt.get("format_name", "").split(","):
            raise MediaError("unsupported_media", "incompatible")
        duration = self._number(stream.get("duration", fmt.get("duration")))
        if duration > self.limits.max_video_seconds:
            raise MediaError("preparation_limit", "incompatible")
        fps = self._fraction(stream.get("avg_frame_rate"), default=stream.get("r_frame_rate", "30/1"))
        if fps > 240:
            raise MediaError("preparation_limit", "incompatible")
        sar = self._fraction(stream.get("sample_aspect_ratio"), default="1:1")
        if not .1 <= sar <= 10:
            raise MediaError("unsupported_media", "incompatible")
        for field in ("color_space", "color_primaries", "color_transfer"):
            accepted = ("bt709",) if output else (None, "unknown", "unspecified", "bt709")
            if stream.get(field) not in accepted:
                raise MediaError("unsupported_color", "incompatible")
        for side in stream.get("side_data_list", []):
            name = str(side.get("side_data_type", "")).lower()
            if any(marker in name for marker in ("mastering", "light level", "dovi", "dolby", "hdr")):
                raise MediaError("unsupported_color", "incompatible")
        # The copy the Pi's decoder takes (VideoRecipe): HEVC Main, level 4.1 (123), `hvc1`.
        if output and (stream.get("codec_tag_string") != "hvc1" or stream.get("profile") != "Main"
                       or stream.get("level") != 123 or stream.get("pix_fmt") != "yuv420p"
                       or stream.get("color_range") != "tv"):
            raise MediaError("preparation_invalid", "incompatible")
        return dict(width=width, height=height, duration=duration, fps=fps, sar=sar,
                    orientation=self._orientation(stream),
                    color_range="pc" if stream.get("color_range") == "pc" else "tv")

    @staticmethod
    def _size(width: int, height: int, recipe: StillRecipe | VideoRecipe, *, sar: float = 1,
              orientation: int = 1) -> tuple[int, int]:
        video = isinstance(recipe, VideoRecipe)
        display_width, display_height = width * sar, float(height)
        if orientation >= 5:
            width, height = height, width
            display_width, display_height = display_height, display_width
        # Keep display aspect while fitting the original raster's oriented bounds.
        scale = min(1, width / display_width, height / display_height,
                    recipe.long_edge / max(display_width, display_height))
        if video:
            scale = min(scale, math.sqrt(recipe.max_pixels / (display_width * display_height)))
        step = 2 if video else 1
        result = (math.floor(display_width * scale / step) * step,
                  math.floor(display_height * scale / step) * step)
        if min(result) < step:
            raise MediaError("unsupported_media", "incompatible")
        return result

    async def prepare_rendition(self, asset: LibraryAsset, rendition: FetchedRendition,
                                destination: Path, *,
                                recipe: PreparationRecipe | None = None) -> PreparedMedia:
        """The photo as Immich shows it (C10): a preview, full-size copy or original of any
        STILL_MEDIA_TYPES type, or a video's original, encoded by `recipe` (the variant's; the
        house recipe of the asset's kind when None). A recipe of the other kind is
        preparation_invalid before the file is read. Integrity is the fetched file's own sha256
        and size; its size and orientation are the file's (a preview is upright, a JPEG full-size
        copy may carry EXIF)."""
        recipe = recipe or self.recipes.of(asset.kind)
        if recipe.kind != asset.kind:
            raise MediaError("preparation_invalid", "incompatible")

        def verify(size: int, sha1: str, sha256: str) -> None:
            if sha256 != rendition.sha256 or size != rendition.size:
                raise MediaError("asset_integrity")
        return await self._prepare(rendition.path, destination, verify=verify,
                                   duration=asset.duration, made_from=rendition.rendition,
                                   recipe=recipe)

    async def _prepare(self, original: Path, destination: Path, *, verify,
                       duration: float | None, made_from: FetchStep,
                       recipe: StillRecipe | VideoRecipe) -> PreparedMedia:
        video = isinstance(recipe, VideoRecipe)
        original, destination = Path(original).absolute(), Path(destination).absolute()
        start = self.clock.monotonic()
        if not math.isfinite(start):
            raise MediaError("clock_invalid")
        deadline = _Deadline(self.clock, start, start + self.limits.wall_seconds)
        created = False
        intermediate = destination.with_name(destination.name + ".png")
        decoded = False
        try:
            async with asyncio.timeout(self.limits.wall_seconds):
                size, sha1, original_sha256 = _file_hashes(original, self.limits.max_original_bytes, deadline)
                verify(size, sha1, original_sha256)
                cap = self.limits.max_video_bytes if video else self.limits.max_image_bytes
                if size + cap > self.limits.max_staging_bytes:
                    raise MediaError("preparation_limit", "incompatible")
                build = await self._identity(deadline)
                if video:
                    facts = self._video(await self._probe(original, deadline))
                    if abs(facts["duration"] - duration) > .15:
                        raise MediaError("metadata_mismatch", "incompatible")
                else:
                    facts = await self._json(dict(operation="inspect", path=str(original)), deadline)
                source = original
                if not video and facts["intermediate"]:
                    decoded = True
                    await self._json(dict(operation="intermediate", path=str(original),
                                          output=str(intermediate)), deadline,
                                     seconds=self.limits.wall_seconds,
                                     file_limit=self.limits.max_staging_bytes)
                    facts = await self._json(dict(operation="inspect", path=str(intermediate)),
                                             deadline)
                    if facts["format"] != "PNG" or facts["intermediate"] or facts["orientation"] != 1:
                        raise MediaError("preparation_invalid", "incompatible")
                    source = intermediate
                raw_width, raw_height, orientation = (facts["width"], facts["height"],
                                                      facts["orientation"])
                width, height = self._size(raw_width, raw_height, recipe,
                                           sar=facts.get("sar", 1), orientation=orientation)
                try:
                    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    raise MediaError("destination_exists", "incompatible") from None
                os.close(fd)
                created = True
                filters = [*_TRANSFORMS[orientation], f"scale={width}:{height}:flags=lanczos", "setsar=1"]
                argv = [str(self.ffmpeg), "-nostdin", "-hide_banner", "-v", "error", "-xerror", "-y",
                        "-filter_threads", str(self.limits.threads), *self._input_flags(), "-noautorotate"]
                major = re.search(r"ffmpeg version (\d+)", build.ffmpeg_version)
                if major and int(major.group(1)) >= 6:
                    argv += ["-display_rotation", "0"]
                if not video:
                    argv += ["-f", _PIPES[facts["format"]]]
                argv += ["-i", str(source), "-map", "0:v:0", "-an", "-sn", "-dn",
                         "-map_metadata", "-1", "-map_chapters", "-1", "-metadata:s:v:0", "rotate=0"]
                cpu_seconds = None
                if video:
                    fps = min(facts["fps"], recipe.fps)
                    threads = self.limits.video_threads
                    # x265 runs a pool of `video_threads`, so its process may spend that many
                    # times the CPU budget (PreparationLimits.video_threads).
                    cpu_seconds = math.ceil(self.limits.cpu_seconds * threads)
                    filters += [f"fps={fps:.8g}",
                                f"colorspace=iall=bt709:irange={facts['color_range']}:all=bt709:range=tv:format=yuv420p"]
                    argv += ["-vf", ",".join(filters), "-c:v", "libx265", "-preset", recipe.preset,
                             "-crf", str(recipe.crf), "-profile:v", "main",
                             "-g", str(max(1, math.ceil(fps * recipe.gop_seconds))),
                             "-threads", str(threads), "-x265-params",
                             f"log-level=error:pools={threads}:level-idc=4.1:high-tier=0",
                             "-tag:v", "hvc1", "-pix_fmt", "yuv420p",
                             "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
                             "-color_range", "tv", "-movflags", "+faststart", "-f", "mp4"]
                else:
                    argv += ["-vf", ",".join(filters), "-frames:v", "1", "-c:v", "mjpeg",
                             "-q:v", str(recipe.jpeg_quality), "-pix_fmt", "yuvj444p",
                             "-threads", str(self.limits.threads), "-f", "image2", "-update", "1"]
                await self._run(dict(operation="exec", argv=[*argv, str(destination)]), deadline,
                                file_limit=cap, cpu_seconds=cpu_seconds)
                output_size, _, digest = _file_hashes(destination, cap, deadline)
                if video:
                    verified = self._video(await self._probe(destination, deadline), output=True)
                    if (verified["fps"] > recipe.fps + .01 or verified["sar"] != 1
                            or abs(verified["duration"] - facts["duration"]) > 1 / fps + .05):
                        raise MediaError("preparation_invalid", "incompatible")
                    await self._run(dict(operation="exec", argv=[str(self.ffmpeg), "-nostdin", "-v", "error",
                                    "-xerror", *self._input_flags(), "-noautorotate", "-i", str(destination),
                                    "-map", "0:v:0", "-f", "null", "-"]), deadline)
                else:
                    verified = await self._json(dict(operation="inspect", path=str(destination)), deadline)
                    if verified.get("format") != "JPEG":
                        raise MediaError("preparation_invalid", "incompatible")
                if (verified["width"], verified["height"], verified["orientation"]) != (width, height, 1):
                    raise MediaError("preparation_invalid", "incompatible")
                verify(*_file_hashes(original, self.limits.max_original_bytes, deadline))
                with destination.open("rb") as stream:
                    os.fsync(stream.fileno())
                deadline.remaining()
                if await self._identity(deadline) != build:
                    raise MediaError("preparation_build_changed")
                return PreparedMedia(
                    path=destination, original_sha256=original_sha256, build=build,
                    ready=MediaReady(sha256=digest, size=output_size, width=width, height=height,
                                     media_type="video/mp4" if video else "image/jpeg",
                                     duration=verified["duration"] if video else None,
                                     made_from=made_from),
                )
        except TimeoutError:
            raise MediaError("preparation_timeout") from None
        except OSError:
            raise MediaError("preparation_io") from None
        finally:
            try:
                if decoded:
                    intermediate.unlink(missing_ok=True)
                if created and sys.exc_info()[0] is not None:
                    destination.unlink(missing_ok=True)
            except OSError:
                raise MediaError("staging_cleanup") from None


if __name__ == "__main__":
    if sys.argv[1:] != ["_worker"]:
        raise SystemExit(2)
    try:
        encoded = sys.stdin.buffer.read(64 * 1024 + 1)
        if len(encoded) > 64 * 1024:
            raise MediaError("preparation_limit", "incompatible")
        _worker(json.loads(encoded))
    except MediaError as error:
        print(json.dumps({"error": error.code}))
    except Exception:
        print(json.dumps({"error": "preparation_invalid"}))
