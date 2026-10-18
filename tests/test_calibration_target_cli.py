"""The calibration target reader's command and edges (decisions 0026, 0027): `run` reads photo files
against a CentralReads and answers {"reading"} or {"refused"} with its exit status; `main` prints
it; `load_photo` decodes ProRAW DNG through rawpy and normal photos through Pillow, both to linear light;
HttpCentralReads reads the sent entry over the operator route with the admin token."""
from __future__ import annotations

import functools
import json
from dataclasses import replace
from pathlib import Path

import httpx
import numpy as np
import pytest
from calibration_target_scenes import CODE, Scene, entry, paper_matched, raw_bytes, raw_photo
from PIL import Image

from central.displays.views import PictureEntryView
from contracts.calibration_target import TargetCode
from contracts.node_picture import NEUTRAL_SOFTWARE
from contracts.tuning import TestPattern
from scripts.calibration_target import __main__ as command
from scripts.calibration_target import central_reads, photos
from scripts.calibration_target.model import PhotoKind

CLEAN = Scene()


class FakeCentral:
    """A CentralReads holding sent entries by code."""

    def __init__(self, *entries: tuple[TargetCode, PictureEntryView]) -> None:
        self.entries = dict(entries)
        self.asked: list[TargetCode] = []

    def sent_entry(self, code: TargetCode) -> PictureEntryView | None:
        self.asked.append(code)
        return self.entries.get(code)


class FakeRaw:
    """rawpy.imread's object for one synthetic photo: postprocess records its parameters."""

    def __init__(self, values: np.ndarray, calls: list) -> None:
        self.values, self.calls = values, calls

    def __enter__(self) -> FakeRaw:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def postprocess(self, **parameters: object) -> np.ndarray:
        self.calls.append(parameters)
        return self.values


@functools.cache
def scene_raw(scene: Scene) -> np.ndarray:
    return raw_bytes(raw_photo(scene))


def clean_raw() -> np.ndarray:
    return scene_raw(CLEAN)


@pytest.fixture
def dngs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """dngs(scene) -> a .dng path whose decode is the scene's synthetic RAW (rawpy.imread patched);
    the second value: every postprocess call's parameters."""
    import rawpy
    scenes: dict[str, Scene] = {}
    calls: list = []

    def imread(name: str) -> FakeRaw:
        return FakeRaw(scene_raw(scenes[Path(name).name]), calls)

    def make(scene: Scene) -> Path:
        path = tmp_path / f"IMG_{len(scenes):04d}.DNG"
        path.write_bytes(b"not a real DNG: rawpy.imread is patched")
        scenes[path.name] = scene
        return path

    monkeypatch.setattr(rawpy, "imread", imread)
    return make, calls


@pytest.fixture
def dng(dngs) -> tuple[Path, list]:
    """A .dng of the clean scene."""
    make, calls = dngs
    return make(CLEAN), calls


# -- run ------------------------------------------------------------------------------------------


def test_run_reads_a_raw_photo_against_its_sent_entry(dng: tuple[Path, list]) -> None:
    path, _ = dng
    central = FakeCentral((CODE, entry(CLEAN)))
    out, status = command.run(str(path), central)
    assert status == 0 and set(out) == {"reading", "suggestion"}
    assert central.asked == [CODE]
    assert json.loads(json.dumps(out)) == out
    assert out["reading"]["kind"] == "raw" and out["reading"]["lowest_distinct"] == 1
    assert out["reading"]["ink"] == pytest.approx(CLEAN.ink, abs=0.02)
    assert 0 <= out["reading"]["falloff"] <= 0.4
    assert out["reading"]["angle"]["focal"] == "estimated" and 0 < out["reading"]["angle"]["degrees"] < 60
    for dropped in out["reading"]["dropped"]:
        assert set(dropped["where"]) == {"surface", "row", "column", "x", "y", "words"}
    assert out["suggestion"]["stage"] == "pre-software"
    assert {step["property"] for step in out["suggestion"]["steps"]} == {"white-level", "white-colour", "tone", "black"}


POST = Scene(stage=TestPattern.TARGET_POST_SOFTWARE, software=replace(NEUTRAL_SOFTWARE, gamma_tenths=24))


def test_run_suggests_post_software(dngs) -> None:
    make, _ = dngs
    out, status = command.run(str(make(POST)), FakeCentral((CODE, entry(POST))))
    assert status == 0 and json.loads(json.dumps(out)) == out
    suggestion = out["suggestion"]
    assert suggestion["stage"] == "post-software" and set(suggestion) == {"stage", "to", "no_change", "unsure", "retake"}


def test_run_says_unsure_when_the_viewing_angle_can_explain_a_dim_screen(dngs) -> None:
    """A screen 10 % dim photographed at 30 degrees: the panel's own off-axis dimming could be all of
    it, so the white level is "unsure" before software, and after it contrast (the knob that would
    raise the white) is held, named in "unsure"."""
    make, _ = dngs
    dim = Scene(display=paper_matched(CLEAN, level=0.9), yaw_deg=30.0, pitch_deg=0.0)
    out, status = command.run(str(make(dim)), FakeCentral((CODE, entry(dim))))
    assert status == 0 and json.loads(json.dumps(out)) == out
    level = next(step for step in out["suggestion"]["steps"] if step["property"] == "white-level")
    assert level["direction"] == "unsure"
    dimmed = Scene(stage=TestPattern.TARGET_POST_SOFTWARE, display=paper_matched(CLEAN),
                   software=replace(NEUTRAL_SOFTWARE, contrast_percent=90), yaw_deg=30.0, pitch_deg=0.0)
    out, status = command.run(str(make(dimmed)), FakeCentral((CODE, entry(dimmed))))
    assert status == 0 and out["suggestion"]["unsure"] == ["white-level"]
    assert out["suggestion"]["to"]["contrast_percent"] == 90


def test_run_refuses_an_unknown_entry(dng: tuple[Path, list]) -> None:
    path, _ = dng
    out, status = command.run(str(path), FakeCentral())
    assert status == 2 and out["refused"]["why"] == "unknown-entry"


def test_run_refuses_an_unread_code_with_the_places_tried(dngs) -> None:
    make, _ = dngs
    out, status = command.run(str(make(replace(CLEAN, code_drawn=False))), FakeCentral())
    assert status == 2 and out["refused"]["why"] == "code-unreadable"
    assert out["refused"]["where"] and json.loads(json.dumps(out)) == out
    where = out["refused"]["where"][0]
    assert where["surface"] == "screen" and where["words"].startswith("the screen, the photo's ")
    assert set(where) == {"surface", "row", "column", "x", "y", "words"}


def test_run_refuses_a_photo_without_the_targets(tmp_path: Path) -> None:
    path = tmp_path / "wall.png"
    Image.new("RGB", (400, 300), (40, 40, 40)).save(path)
    out, status = command.run(str(path), FakeCentral())
    assert status == 2 and out["refused"]["why"] == "target-not-found"
    assert out["refused"]["photo"] == "wall.png"


def test_main_takes_one_photo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHOTO_WALL_ADMIN_TOKEN", "secret-token")
    with pytest.raises(SystemExit) as exited:
        command.main(["a.dng", "b.dng", "--central", "http://central.test"])
    assert exited.value.code == 2


def test_main_prints_the_json_and_returns_its_status(dng: tuple[Path, list], monkeypatch: pytest.MonkeyPatch,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    path, _ = dng
    built: list[tuple] = []

    def reads(base_url: str, token: str) -> FakeCentral:
        built.append((base_url, token))
        return FakeCentral((CODE, entry(CLEAN)))

    monkeypatch.setattr(command, "HttpCentralReads", reads)
    monkeypatch.setenv("PHOTO_WALL_ADMIN_TOKEN", "secret-token")
    assert command.main([str(path), "--central", "http://central.test"]) == 0
    assert built == [("http://central.test", "secret-token")]
    assert set(json.loads(capsys.readouterr().out)) == {"reading", "suggestion"}


def test_main_without_a_token_or_with_an_unreadable_photo_is_an_error(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PHOTO_WALL_ADMIN_TOKEN", raising=False)
    assert command.main(["x.jpg", "--central", "http://central.test"]) == 1
    garbage = tmp_path / "garbage.jpg"
    garbage.write_bytes(b"\x00not an image")
    monkeypatch.setenv("PHOTO_WALL_ADMIN_TOKEN", "secret-token")
    monkeypatch.setattr(command, "HttpCentralReads", lambda *a: FakeCentral())
    assert command.main([str(garbage), "--central", "http://central.test"]) == 1


# -- load_photo -------------------------------------------------------------------------------------


@pytest.mark.parametrize("suffix", [".png", ".jpg"])
def test_a_normal_photo_is_its_8_bit_values_decoded_to_linear_light(tmp_path: Path, suffix: str) -> None:
    values = np.zeros((4, 6, 3), np.uint8)
    values[0, 0] = (255, 128, 0)
    values[1, 1] = (51, 102, 204)
    path = tmp_path / f"photo{suffix}"
    Image.fromarray(values).save(path, quality=100) if suffix == ".jpg" else Image.fromarray(values).save(path)
    photo = photos.load_photo(path)
    assert photo.kind is PhotoKind.NORMAL and photo.name == path.name and photo.focal_px is None
    assert photo.pixels.shape == (4, 6, 3) and photo.pixels.dtype == np.float32
    if suffix == ".png":
        encoded = values / 255                      # the sRGB standard's decoding, written out as the reference
        linear = np.where(encoded <= 0.04045, encoded / 12.92, ((encoded + 0.055) / 1.055) ** 2.4)
        assert np.allclose(photo.pixels, linear, atol=1e-6)
        assert photo.clipped[0, 0] and not photo.clipped[1, 1]
    else:
        assert photo.clipped.shape == (4, 6) and photo.pixels.max() <= 1.0


def test_a_dng_is_decoded_linear_by_rawpy(dng: tuple[Path, list]) -> None:
    import rawpy
    path, calls = dng
    photo = photos.load_photo(path)
    assert photo.kind is PhotoKind.RAW
    assert calls == [dict(gamma=(1, 1), no_auto_bright=True, use_camera_wb=True,
                          output_color=rawpy.ColorSpace.sRGB, output_bps=16)]
    assert np.allclose(photo.pixels, clean_raw() / 65535, atol=1e-7)
    assert np.array_equal(photo.clipped, np.any(clean_raw() >= 65535, axis=2))


def test_a_garbage_file_is_photo_unreadable(tmp_path: Path) -> None:
    for name in ("garbage.jpg", "garbage.dng"):
        path = tmp_path / name
        path.write_bytes(b"\x00\x01 not a photo")
        with pytest.raises(ValueError, match="photo_unreadable"):
            photos.load_photo(path)


# -- HttpCentralReads --------------------------------------------------------------------------------


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch):
    """Central's operator API answered by a handler (httpx.MockTransport)."""
    seen: list[httpx.Request] = []
    answers: dict[str, httpx.Response] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answers.get(request.url.path, httpx.Response(404, json={"detail": "unknown_entry"}))

    real = httpx.Client
    monkeypatch.setattr(central_reads.httpx, "Client",
                        lambda **kw: real(transport=httpx.MockTransport(handle), **kw))
    return seen, answers


ROUTE = f"/v1/operator/players/{CODE.player_id}/outputs/{CODE.output_id}/picture-changes/{CODE.picture_change}"


def test_the_sent_entry_is_read_with_the_admin_token(transport) -> None:
    seen, answers = transport
    sent = entry(CLEAN)
    answers[ROUTE] = httpx.Response(200, json=sent.model_dump(mode="json"))
    reads = central_reads.HttpCentralReads("http://central.test", "secret-token")
    assert reads.sent_entry(CODE) == sent
    assert seen[0].headers["Authorization"] == "Bearer secret-token"
    assert seen[0].url == httpx.URL("http://central.test" + ROUTE)


def test_an_unknown_entry_is_none_and_another_answer_an_error(transport) -> None:
    _, answers = transport
    reads = central_reads.HttpCentralReads("http://central.test", "secret-token")
    assert reads.sent_entry(CODE) is None
    answers[ROUTE] = httpx.Response(500, text="boom")
    with pytest.raises(RuntimeError, match="500"):
        reads.sent_entry(CODE)
