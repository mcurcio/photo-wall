"""Real local FFmpeg/Pillow preparation checks using public generated fixtures.

This verifies native conversion on the executing host, not Pi playback or the
production container build. Every image and video is generated under tmp_path.
"""

import asyncio
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from uuid import UUID

import pytest

from contracts.time import ManualClock
from media.models import MediaError, OriginalAsset
from media.prepare import PreparationLimits, PreparationRecipe, Preparer

try:
    from PIL import Image, ImageChops, ImageCms, ImageDraw, ImageOps, ImageStat
except ImportError:
    Image = ImageChops = ImageCms = ImageDraw = ImageOps = ImageStat = None


@pytest.fixture(scope="session")
def native_tools():
    if Image is None:
        pytest.skip("Pillow is required for real preparation integration tests")
    paths = []
    for name in ("ffmpeg", "ffprobe"):
        path = shutil.which(name)
        if path is None and Path(f"/opt/homebrew/bin/{name}").is_file():
            path = f"/opt/homebrew/bin/{name}"
        if path is None:
            pytest.skip(f"{name} is required for real preparation integration tests")
        paths.append(path)
    return tuple(paths)


def run_tool(arguments):
    result = subprocess.run(arguments, capture_output=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr.decode(errors="replace")[-2000:]
    return result.stdout


def probe(path, tools):
    return json.loads(run_tool([
        tools[1], "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path),
    ]))


def original_asset(path, *, width=96, height=64, orientation=1, kind="image", duration=None):
    data = path.read_bytes()
    return OriginalAsset(
        connection_id="fixture", upstream_id=str(UUID(int=1)),
        original_sha1=hashlib.sha1(data).hexdigest(), kind=kind,
        raw_width=width, raw_height=height, orientation=orientation,
        captured_at=1767225600, file_size=len(data), duration=duration,
    )


def preparer(tools, **changes):
    return Preparer(ffmpeg=tools[0], ffprobe=tools[1], clock=ManualClock(1767225600), **changes)


def prepare(tools, asset, original, destination, **changes):
    return asyncio.run(preparer(tools, **changes).prepare(asset, original, destination))


def corners(size=(96, 64)):
    image = Image.new("RGB", size)
    width, height = size
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width // 2 - 1, height // 2 - 1), fill=(230, 25, 35))
    draw.rectangle((width // 2, 0, width - 1, height // 2 - 1), fill=(25, 215, 50))
    draw.rectangle((0, height // 2, width // 2 - 1, height - 1), fill=(30, 45, 225))
    draw.rectangle((width // 2, height // 2, width - 1, height - 1), fill=(225, 205, 30))
    draw.rectangle((width // 8, height // 8, width // 4, height // 4), fill="white")
    return image


def save_jpeg(path, *, orientation=1, size=(96, 64), **options):
    exif = Image.Exif()
    exif[274] = orientation
    exif[270] = "PUBLIC_SYNTHETIC_PRIVATE_METADATA_MARKER"
    corners(size).save(path, "JPEG", quality=96, subsampling=0, exif=exif, **options)
    return original_asset(path, width=size[0], height=size[1], orientation=orientation)


def assert_same_pixels(actual, expected, tolerance=10):
    assert actual.size == expected.size
    difference = ImageChops.difference(actual.convert("RGB"), expected.convert("RGB"))
    assert max(ImageStat.Stat(difference).mean) < tolerance


def assert_variant_file(result, destination, source_bytes):
    assert result.path == destination
    assert destination.is_file()
    data = destination.read_bytes()
    assert result.variant.size == len(data)
    assert result.variant.sha256 == hashlib.sha256(data).hexdigest()
    assert result.original_sha256 == hashlib.sha256(source_bytes).hexdigest()
    assert result.recipe_id
    assert result.build is not None


@pytest.mark.parametrize("orientation", range(1, 9))
def test_all_exif_orientations_match_decoded_original_exactly_once(tmp_path, native_tools, orientation):
    source = tmp_path / "original.jpg"
    destination = tmp_path / "variant.jpg"
    asset = save_jpeg(source, orientation=orientation)
    original_bytes = source.read_bytes()
    with Image.open(source) as decoded:
        expected = ImageOps.exif_transpose(decoded).convert("RGB")

    result = prepare(native_tools, asset, source, destination)
    assert_variant_file(result, destination, original_bytes)
    assert result.variant.media_type == "image/jpeg"
    assert result.variant.duration is None
    assert (result.variant.width, result.variant.height) == expected.size
    assert source.read_bytes() == original_bytes
    with Image.open(destination) as output:
        output.load()
        assert output.format == "JPEG"
        assert output.getexif().get(274, 1) == 1
        assert 270 not in output.getexif()
        assert_same_pixels(output, expected)
    assert b"PUBLIC_SYNTHETIC_PRIVATE_METADATA_MARKER" not in destination.read_bytes()


def test_still_resize_preserves_aspect_ratio_and_does_not_upscale(tmp_path, native_tools):
    source = tmp_path / "original.jpg"
    asset = save_jpeg(source, size=(240, 120))
    small = prepare(native_tools, asset, source, tmp_path / "small.jpg",
                    recipe=PreparationRecipe(image_long_edge=80))
    assert (small.variant.width, small.variant.height) == (80, 40)
    native = prepare(native_tools, asset, source, tmp_path / "native.jpg")
    assert (native.variant.width, native.variant.height) == (240, 120)
    assert small.recipe_id != native.recipe_id


def test_recipe_identity_is_stable_for_same_build_and_changes_with_quality(tmp_path, native_tools):
    source = tmp_path / "original.jpg"
    asset = save_jpeg(source)
    first = prepare(native_tools, asset, source, tmp_path / "first.jpg")
    same = prepare(native_tools, asset, source, tmp_path / "same.jpg")
    changed = prepare(native_tools, asset, source, tmp_path / "changed.jpg",
                      recipe=PreparationRecipe(jpeg_quality=7))
    assert first.recipe_id == same.recipe_id
    assert first.build == same.build
    assert first.recipe_id != changed.recipe_id


@pytest.mark.parametrize("fault", ["sha1", "size", "width", "height", "orientation"])
def test_original_revision_and_metadata_mismatch_fail_before_publication(tmp_path, native_tools, fault):
    source = tmp_path / "original.jpg"
    destination = tmp_path / "variant.jpg"
    asset = save_jpeg(source)
    replacement = {
        "sha1": {"original_sha1": "0" * 40}, "size": {"file_size": asset.file_size + 1},
        "width": {"raw_width": 97}, "height": {"raw_height": 65}, "orientation": {"orientation": 6},
    }[fault]
    with pytest.raises(MediaError) as error:
        prepare(native_tools, asset.model_copy(update=replacement), source, destination)
    assert not destination.exists()
    if fault == "sha1":
        assert error.value.code == "asset_integrity"
    if fault in {"width", "height", "orientation"}:
        assert error.value.code == "metadata_mismatch"


def png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def save_truecolor_16bit_png(path):
    # Pillow exposes this legal 16-bit RGB PNG as RGB; IHDR must still be inspected.
    width, height = 96, 64
    row = b"\x00" + struct.pack(">HHH", 50000, 20000, 10000) * width
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 16, 2, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(row * height))
        + png_chunk(b"IEND", b"")
    )


def nonidentity_rgb_profile():
    profile = bytearray(ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
    # Retain a valid RGB profile and its sRGB name while changing a red colorant.
    # The declared name therefore cannot replace a transform-based color check.
    count = struct.unpack_from(">I", profile, 128)[0]
    for index in range(count):
        position = 132 + index * 12
        if profile[position:position + 4] == b"rXYZ":
            offset = struct.unpack_from(">I", profile, position + 4)[0]
            red_x = struct.unpack_from(">i", profile, offset + 8)[0]
            struct.pack_into(">i", profile, offset + 8, red_x * 3 // 2)
            return bytes(profile)
    raise AssertionError("synthetic sRGB profile lacks its red colorant")


@pytest.mark.parametrize("profile", [
    "alpha", "animated", "rgb16", "cmyk", "nonidentity_icc", "hdr_png", "conflicting_gamma",
])
def test_unsupported_still_formats_and_color_are_rejected(tmp_path, native_tools, profile):
    source = tmp_path / "original"
    destination = tmp_path / "variant.jpg"
    if profile == "alpha":
        corners().convert("RGBA").save(source, "PNG")
    elif profile == "animated":
        corners().save(source, "PNG", save_all=True, append_images=[corners().transpose(Image.Transpose.FLIP_LEFT_RIGHT)],
                       duration=100, loop=0)
    elif profile == "rgb16":
        save_truecolor_16bit_png(source)
        with Image.open(source) as decoded:
            assert decoded.mode == "RGB"
    elif profile == "cmyk":
        corners().convert("CMYK").save(source, "JPEG")
    elif profile == "nonidentity_icc":
        corners().save(source, "JPEG", icc_profile=nonidentity_rgb_profile())
    else:
        corners().save(source, "PNG")
        raw = source.read_bytes()
        chunk = png_chunk(b"cICP", bytes([9, 16, 9, 1])) if profile == "hdr_png" else png_chunk(
            b"gAMA", struct.pack(">I", 100000),
        )
        source.write_bytes(raw[:33] + chunk + raw[33:])
    with pytest.raises(MediaError):
        prepare(native_tools, original_asset(source), source, destination)
    assert not destination.exists()


def test_valid_srgb_icc_and_opaque_png_produce_jpeg(tmp_path, native_tools):
    source = tmp_path / "original.png"
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    corners().save(source, "PNG", icc_profile=profile)
    result = prepare(native_tools, original_asset(source), source, tmp_path / "variant.jpg")
    assert result.variant.media_type == "image/jpeg"
    assert result.variant.duration is None
    with Image.open(result.path) as decoded:
        assert_same_pixels(decoded, corners())


@pytest.mark.parametrize("budget", ["original", "pixels", "dimension", "output", "staging"])
def test_preparation_budgets_remove_output_and_preserve_original(tmp_path, native_tools, budget):
    source = tmp_path / "original.jpg"
    destination = tmp_path / "variant.jpg"
    asset = save_jpeg(source)
    original_bytes = source.read_bytes()
    changes = {
        "original": {"max_original_bytes": 1}, "pixels": {"max_pixels": 10},
        "dimension": {"max_dimension": 10}, "output": {"max_image_bytes": 100},
        "staging": {"max_staging_bytes": len(original_bytes) + 1},
    }[budget]
    with pytest.raises(MediaError):
        prepare(native_tools, asset, source, destination, limits=PreparationLimits(**changes))
    assert not destination.exists()
    assert source.read_bytes() == original_bytes


@pytest.mark.parametrize("existing", ["file", "symlink"])
def test_existing_destination_is_never_followed_or_changed(tmp_path, native_tools, existing):
    source = tmp_path / "original.jpg"
    asset = save_jpeg(source)
    destination = tmp_path / "variant.jpg"
    target = tmp_path / "existing"
    target.write_bytes(b"existing local bytes")
    if existing == "symlink":
        destination.symlink_to(target)
    else:
        destination.write_bytes(b"existing local bytes")
    with pytest.raises(MediaError):
        prepare(native_tools, asset, source, destination)
    assert destination.read_bytes() == target.read_bytes() == b"existing local bytes"
    assert destination.is_symlink() == (existing == "symlink")


def test_symlink_original_is_rejected_without_modifying_target(tmp_path, native_tools):
    target = tmp_path / "real-original.jpg"
    asset = save_jpeg(target)
    source = tmp_path / "original.jpg"
    source.symlink_to(target)
    original_bytes = target.read_bytes()
    destination = tmp_path / "variant.jpg"
    with pytest.raises(MediaError):
        prepare(native_tools, asset, source, destination)
    assert not destination.exists()
    assert target.read_bytes() == original_bytes


def test_corrupt_source_is_coded_and_leaves_no_output(tmp_path, native_tools):
    source = tmp_path / "PUBLIC_SYNTHETIC_PRIVATE_FILENAME.jpg"
    source.write_bytes(b"not a decodable media file: PUBLIC_SYNTHETIC_PRIVATE_CONTENT")
    destination = tmp_path / "variant.jpg"
    with pytest.raises(MediaError) as error:
        prepare(native_tools, original_asset(source), source, destination)
    assert not destination.exists()
    assert "PRIVATE" not in str(error.value)


def test_preparation_wall_deadline_is_real_with_stationary_manual_clock(tmp_path, native_tools):
    source = tmp_path / "original.jpg"
    asset = save_jpeg(source)
    destination = tmp_path / "variant.jpg"
    with pytest.raises(MediaError):
        prepare(native_tools, asset, source, destination, limits=PreparationLimits(wall_seconds=.01))
    assert not destination.exists()


def tool_wrapper(path, executable, body):
    path.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "arguments = sys.argv[1:]\n"
        + body + "\n"
        + f"os.execv({executable!r}, [{executable!r}, *arguments])\n"
    )
    path.chmod(0o700)
    return str(path)


def test_describe_recipe_matches_prepared_result_and_reuses_unchanged_build(tmp_path, native_tools):
    source = tmp_path / "original.jpg"
    asset = save_jpeg(source)
    marker = tmp_path / "build-inspection"
    wrapper = tool_wrapper(tmp_path / "ffmpeg-wrapper", native_tools[0],
                           "if '-version' in arguments:\n"
                           f"    with Path({str(marker)!r}).open('a') as output: output.write('check\\n')")

    async def perform():
        instance = preparer((wrapper, native_tools[1]))
        recipe_id, build = await instance.describe_recipe()
        assert await instance.describe_recipe() == (recipe_id, build)
        result = await instance.prepare(asset, source, tmp_path / "variant.jpg")
        assert (result.recipe_id, result.build) == (recipe_id, build)
        assert build.preparation_sha256

    asyncio.run(perform())
    assert marker.read_text().splitlines() == ["check"]


def test_describe_recipe_invalidates_cache_when_executable_bytes_change(tmp_path, native_tools):
    wrapper = Path(tool_wrapper(tmp_path / "ffmpeg-wrapper", native_tools[0], ""))

    async def perform():
        instance = preparer((str(wrapper), native_tools[1]))
        first_id, first_build = await instance.describe_recipe()
        with wrapper.open("a") as output:
            output.write("\n# changed trusted tool deployment\n")
        second_id, second_build = await instance.describe_recipe()
        assert first_id != second_id
        assert first_build.ffmpeg_sha256 != second_build.ffmpeg_sha256
        assert first_build.ffmpeg_version == second_build.ffmpeg_version

    asyncio.run(perform())


def test_recipe_identity_includes_encoder_thread_setting(native_tools):
    first_id, _ = asyncio.run(preparer(native_tools).describe_recipe())
    second_id, _ = asyncio.run(preparer(native_tools, limits=PreparationLimits(threads=2)).describe_recipe())
    assert first_id != second_id


def test_build_change_during_conversion_fails_and_removes_output(tmp_path, native_tools):
    source = tmp_path / "original.jpg"
    destination = tmp_path / "variant.jpg"
    asset = save_jpeg(source)
    wrapper = tool_wrapper(tmp_path / "ffmpeg-wrapper", native_tools[0],
                           "if '-i' in arguments:\n"
                           "    with Path(__file__).open('a') as output: output.write('\\n# mutated\\n')")
    with pytest.raises(MediaError) as error:
        prepare((wrapper, native_tools[1]), asset, source, destination)
    assert error.value.code == "preparation_build_changed"
    assert not destination.exists()


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_tool_output_overflow_is_bounded_and_cleans_destination(tmp_path, native_tools, stream):
    source = tmp_path / "original.jpg"
    destination = tmp_path / "variant.jpg"
    asset = save_jpeg(source)
    wrapper = tool_wrapper(tmp_path / "ffmpeg-wrapper", native_tools[0],
                           "if '-i' in arguments:\n"
                           f"    sys.{stream}.write('x' * (128 * 1024))\n"
                           f"    sys.{stream}.flush()\n"
                           "    time.sleep(30)")
    with pytest.raises(MediaError) as error:
        prepare((wrapper, native_tools[1]), asset, source, destination)
    assert error.value.code == "preparation_limit"
    assert not destination.exists()


@pytest.mark.parametrize("stage", ["probe", "decode"])
def test_failed_output_verification_removes_encoded_file_and_sanitizes_tool_error(tmp_path, native_tools, stage):
    source = tmp_path / "original.mp4"
    destination = tmp_path / "variant.mp4"
    marker = tmp_path / "verification-attempted"
    make_video(source, native_tools)
    asset = video_asset(source, native_tools)
    if stage == "probe":
        condition = f"{str(destination)!r} in arguments"
        index = 1
    else:
        condition = (
            f"'-i' in arguments and arguments[arguments.index('-i') + 1] == {str(destination)!r}"
        )
        index = 0
    body = (
        f"if {condition}:\n"
        f"    Path({str(marker)!r}).write_text('attempted')\n"
        "    sys.stderr.write('PUBLIC_SYNTHETIC_PRIVATE_TOOL_ERROR')\n"
        "    raise SystemExit(2)"
    )
    tools = list(native_tools)
    tools[index] = tool_wrapper(tmp_path / "fault-tool", tools[index], body)
    with pytest.raises(MediaError) as error:
        prepare(tuple(tools), asset, source, destination)
    assert marker.is_file(), "the fixture must reach output validation before failing"
    assert not destination.exists()
    assert "PRIVATE" not in str(error.value)


def test_cancellation_reaps_active_native_process_and_cleans_destination(tmp_path, native_tools):
    source = tmp_path / "original.jpg"
    destination = tmp_path / "variant.jpg"
    marker = tmp_path / "active-pid"
    asset = save_jpeg(source)
    wrapper = tool_wrapper(tmp_path / "slow-ffmpeg", native_tools[0],
                           "if '-i' in arguments:\n"
                           f"    Path({str(marker)!r}).write_text(str(os.getpid()))\n"
                           "    time.sleep(30)")

    async def perform():
        task = asyncio.create_task(preparer((wrapper, native_tools[1])).prepare(asset, source, destination))
        try:
            async with asyncio.timeout(5):
                while not marker.exists():
                    if task.done():
                        await task
                        pytest.fail("preparation ended before the fault process started")
                    await asyncio.sleep(.01)
            process_id = int(marker.read_text())
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return process_id
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    process_id = asyncio.run(perform())
    assert not destination.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(process_id, 0)


def make_video(path, tools, *, hdr=False):
    run_tool([
        tools[0], "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
        "testsrc2=size=160x96:rate=48:duration=0.5", "-f", "lavfi", "-i",
        "sine=frequency=440:sample_rate=48000:duration=0.5", "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", "-profile:v", "high",
        "-color_primaries", "bt2020" if hdr else "bt709", "-color_trc",
        "smpte2084" if hdr else "bt709", "-colorspace", "bt2020nc" if hdr else "bt709",
        "-c:a", "aac", "-t", "0.5", "-movflags", "+faststart", str(path),
    ])


def video_asset(path, tools, *, orientation=1):
    stream = next(stream for stream in probe(path, tools)["streams"] if stream["codec_type"] == "video")
    return original_asset(path, width=stream["width"], height=stream["height"], kind="video",
                          duration=float(stream["duration"]), orientation=orientation)


def first_video_frame(path, tools):
    data = run_tool([
        tools[0], "-nostdin", "-v", "error", "-i", str(path), "-frames:v", "1", "-f",
        "image2pipe", "-c:v", "png", "pipe:1",
    ])
    with Image.open(io.BytesIO(data)) as decoded:
        return decoded.convert("RGB")


def test_video_with_audio_becomes_silent_sdr_h264_and_is_fully_decodable(tmp_path, native_tools):
    source = tmp_path / "original.mp4"
    destination = tmp_path / "variant.mp4"
    make_video(source, native_tools)
    original_bytes = source.read_bytes()
    assert {stream["codec_type"] for stream in probe(source, native_tools)["streams"]} == {"audio", "video"}
    result = prepare(native_tools, video_asset(source, native_tools), source, destination)
    assert_variant_file(result, destination, original_bytes)
    assert result.variant.media_type == "video/mp4"
    assert result.variant.duration > 0
    assert (result.variant.width, result.variant.height) == (160, 96)
    output = probe(destination, native_tools)
    assert len(output["streams"]) == 1
    stream = output["streams"][0]
    assert stream["codec_type"] == "video"
    assert stream["codec_name"] == "h264"
    assert stream["profile"] == "High"
    assert stream["level"] == 42
    assert stream["pix_fmt"] == "yuv420p"
    assert stream["sample_aspect_ratio"] == "1:1"
    assert (stream["color_space"], stream["color_transfer"], stream["color_primaries"]) == ("bt709",) * 3
    numerator, denominator = map(int, stream["avg_frame_rate"].split("/"))
    assert numerator / denominator <= 30
    assert all(side.get("rotation", 0) == 0 for side in stream.get("side_data_list", []))
    assert abs(result.variant.duration - float(stream["duration"])) < .05
    data = destination.read_bytes()
    assert data.index(b"moov") < data.index(b"mdat")
    run_tool([native_tools[0], "-nostdin", "-v", "error", "-xerror", "-i", str(destination), "-f", "null", "-"])
    assert source.read_bytes() == original_bytes


def test_rotated_video_matches_the_displayed_original_without_residual_orientation(tmp_path, native_tools):
    unrotated = tmp_path / "unrotated.mp4"
    source = tmp_path / "rotated.mp4"
    make_video(unrotated, native_tools)
    run_tool([native_tools[0], "-nostdin", "-v", "error", "-y", "-i", str(unrotated),
              "-map", "0", "-c", "copy", "-metadata:s:v:0", "rotate=90", str(source)])
    stream = next(stream for stream in probe(source, native_tools)["streams"] if stream["codec_type"] == "video")
    if not any("rotation" in side for side in stream.get("side_data_list", [])):
        # FFmpeg 8 sets this input display property instead of the legacy rotate tag.
        run_tool([native_tools[0], "-nostdin", "-v", "error", "-y", "-display_rotation", "90",
                  "-i", str(unrotated), "-map", "0", "-c", "copy", str(source)])
        stream = next(stream for stream in probe(source, native_tools)["streams"] if stream["codec_type"] == "video")
    rotation = next(side["rotation"] for side in stream["side_data_list"] if "rotation" in side)
    assert rotation in (-90, 90)
    orientation = 8 if rotation == 90 else 6
    result = prepare(native_tools, video_asset(source, native_tools, orientation=orientation), source,
                     tmp_path / "variant.mp4")
    assert (result.variant.width, result.variant.height) == (96, 160)
    assert_same_pixels(first_video_frame(result.path, native_tools), first_video_frame(source, native_tools),
                       tolerance=15)
    output = probe(result.path, native_tools)["streams"][0]
    assert all(side.get("rotation", 0) == 0 for side in output.get("side_data_list", []))


def test_hdr_video_transfer_is_rejected_before_output(tmp_path, native_tools):
    source = tmp_path / "hdr.mp4"
    destination = tmp_path / "variant.mp4"
    make_video(source, native_tools, hdr=True)
    with pytest.raises(MediaError):
        prepare(native_tools, video_asset(source, native_tools), source, destination)
    assert not destination.exists()


def test_sdr_hevc_mov_normalizes_sample_aspect_ratio_to_square_pixels(tmp_path, native_tools):
    source = tmp_path / "original.mov"
    run_tool([
        native_tools[0], "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
        "testsrc2=size=160x96:rate=12:duration=0.5", "-vf", "setsar=2", "-an",
        "-c:v", "libx265", "-threads", "1", "-x265-params", "pools=1:frame-threads=1:log-level=error",
        "-pix_fmt", "yuv420p", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-colorspace", "bt709", "-t", "0.5", str(source),
    ])
    source_stream = probe(source, native_tools)["streams"][0]
    assert source_stream["codec_name"] == "hevc"
    assert source_stream["sample_aspect_ratio"] == "2:1"
    result = prepare(native_tools, video_asset(source, native_tools), source, tmp_path / "variant.mp4")
    assert (result.variant.width, result.variant.height) == (160, 48)
    output_stream = probe(result.path, native_tools)["streams"][0]
    assert output_stream["codec_name"] == "h264"
    assert output_stream["sample_aspect_ratio"] == "1:1"
    assert float(output_stream["duration"]) == pytest.approx(.5)


@pytest.mark.parametrize("recipe", [
    {"image_long_edge": 0}, {"video_fps": 0}, {"video_crf": -1}, {"gop_seconds": float("nan")},
])
def test_invalid_recipe_settings_are_rejected_without_native_tools(recipe):
    with pytest.raises(ValueError):
        PreparationRecipe(**recipe)


@pytest.mark.parametrize("limits", [
    {"max_pixels": 0}, {"wall_seconds": 0}, {"memory_bytes": -1},
    {"probe_seconds": float("inf")}, {"threads": 0},
])
def test_invalid_preparation_limits_are_rejected_without_native_tools(limits):
    with pytest.raises(ValueError):
        PreparationLimits(**limits)
