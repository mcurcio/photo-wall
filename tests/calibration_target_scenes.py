"""Synthetic photos of both calibration targets for the reader's tests (decision 0026), ported from
the design's prototype (scene3.py, simulate.py, run4.py) onto the committed layout.

The wall plane is the screen target's (contracts/calibration_target.py, mm). The screen draws
`screen_point` (rasterised exactly: it is evaluated once per region between the layout's edges)
with the QR of `player.calibration_target.target_modules` in its square; pre-software every point
is raw, post-software every point but the dark box passes the chain (appliance/display_host/
picture.py `operations_for` of the entry's software, player/geometry.py `final_pixel` as a photo
pixel, no shade). The paper target (`paper_ink`) lies on the panel in the dark box at
`paper_in_box`, with a placement error and a tilt. Light is linear sRGB (the camera's space after
the DNG's own colour matrix): the display's white is its level times `kelvin_white`, the paper's is
the room's. Reflections on the Frame glass add to both surfaces, the TV surface's own glare to the
screen only, room light and shadows multiply the paper, panel unevenness multiplies the screen.

The photo is a pinhole view at an angle, at most PHOTO_SIZE: RAW (lens shading, shot and read
noise, the sensor's clip at 1.0) or a normal JPEG (auto white balance, auto exposure, an S-curve,
8 bits).
"""
from __future__ import annotations

import functools
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Final

import cv2
import numpy as np

from appliance.display_host.picture import operations_for
from central.displays.picture_history import PictureEntryKind
from central.displays.views import (
    DisplayRefView,
    LutChoiceView,
    PictureEntryView,
    PictureValuesView,
    SoftwarePictureView,
)
from contracts.calibration_target import (
    LETTER_MM,
    MARKER_MODULES,
    PAPER_TARGET_MM,
    QR_QUIET_MODULES,
    SCREEN_TARGET_MM,
    GroupKind,
    Patch,
    Rect,
    Role,
    Surface,
    TargetCode,
    calibration_target,
    paper_ink,
    screen_point,
)
from contracts.kelvin import kelvin_white
from contracts.node_frame import srgb_to_linear
from contracts.node_picture import NEUTRAL_SOFTWARE, SignalRange, SoftwarePicture
from contracts.tuning import SIGNAL_LEVELS, TestPattern
from player.calibration_target import target_modules
from player.geometry import final_pixel
from scripts.calibration_target.model import Photo, PhotoKind

PX_PER_MM: Final = 4.0                  # the wall canvas
MARGIN_MM: Final = 20.0                 # of the opening round the screen target
PHOTO_SIZE: Final = (2000, 1700)        # the photo, at most (the brief's bound for the suite's time)
QR_SUPERSAMPLE: Final = 4
RAW_SHADING: Final = 0.01            # lens shading left after the DNG's own correction (r² at the corner, a guess:
                                    # every screen patch lies outside its paper twin, so it biases the white low)
CODE: Final = TargetCode("pi-wall-1", "HDMI-A-1", 41)

Field = Callable[[np.ndarray, np.ndarray], np.ndarray]     # wall (x_mm, y_mm) -> value


@dataclass(frozen=True)
class Display:
    """A display's own response to a raw signal: `white` its peak's level, `kelvin` its white's
    colour, `black` raw 0's light (a share of its white), `gamma` None for the sRGB curve, `crush`
    the codes 1..crush shown as raw 0, `edge_dim` how much dimmer it is at its top and bottom
    edges than at its middle."""
    white: float = 1.0
    kelvin: int = 6500
    black: float = 0.0008
    gamma: float | None = None
    crush: int = 0
    edge_dim: float = 0.0

    def light(self, signal: np.ndarray) -> np.ndarray:
        """Linear light for an encoded signal (..., 3)."""
        s = np.clip(signal, 0.0, 1.0)
        if self.crush:
            s = np.where(np.round(s * SIGNAL_LEVELS) <= self.crush, 0.0, s)
        if self.gamma is None:
            response = np.where(s <= 0.04045, s / 12.92, ((s + 0.055) / 1.055) ** 2.4)
        else:
            response = s ** self.gamma
        colour = self.white * np.array(kelvin_white(self.kelvin))
        return colour * (self.black + (1 - self.black) * response)

    def panel(self, y_mm: np.ndarray) -> np.ndarray:
        """The panel's own light across the target, 1 at its middle (edge-lit unevenness)."""
        middle = SCREEN_TARGET_MM[1] / 2
        return 1 - self.edge_dim * ((y_mm - middle) / middle) ** 2


@dataclass(frozen=True)
class Scene:
    display: Display = field(default_factory=Display)
    stage: TestPattern = TestPattern.TARGET_PRE_SOFTWARE
    software: SoftwarePicture = NEUTRAL_SOFTWARE
    lut: bytes | None = None
    code: TargetCode = CODE
    paper_white: float = 0.8            # bare paper's light under the room, against the display's white 1
    room_kelvin: int = 5000
    ink: float = 0.03                   # black ink's reflectance
    room: Field | None = None           # multiplies the paper's light; None: even
    reflection: Field | None = None     # the Frame glass: adds to both surfaces (a share of white 1)
    tv_glare: Field | None = None       # the TV's own surface: adds to the screen only
    paper_offset_mm: tuple[float, float] = (1.2, -0.8)
    paper_tilt_deg: float = 0.6
    paper_scale: float = 1.0            # the printer's scale
    hidden_markers: frozenset[int] = frozenset()
    qr_covered: bool = False
    yaw_deg: float = 12.0
    pitch_deg: float = 4.0
    exposure: float = 0.6               # RAW: white 1's green in the photo
    seed: int = 1


@dataclass(frozen=True)
class Truth:
    """What the reader must recover, from the scene with no reflection, glare or room unevenness
    and no camera: screen white over paper white (median over the groups), each grey's share of
    its group's screen white, and the lowest near-black code the display separates."""
    white: np.ndarray
    tone: dict[float, np.ndarray]
    lowest_distinct: int


def paper_matched(scene: Scene, *, level: float = 1.0, kelvin: int | None = None, **display: object) -> Display:
    """A display whose white is the scene's paper white as the reader reads it (the bare paper
    above its ink: read.py rule 1), times `level`; at `kelvin` (default the room's) with the same
    luminance. Other Display fields from `display`."""
    kelvin = scene.room_kelvin if kelvin is None else kelvin
    luminance = np.array([0.2126, 0.7152, 0.0722])
    same = float(luminance @ np.array(kelvin_white(scene.room_kelvin))) / float(luminance @ np.array(kelvin_white(kelvin)))
    return Display(white=level * scene.paper_white * (1 - scene.ink) * same, kelvin=kelvin, **display)  # type: ignore[arg-type]


# -- Fields ---------------------------------------------------------------------------------------


def blob(x_mm: float, y_mm: float, diameter_mm: float, peak: float) -> Field:
    """A round spot (a bulb's reflection): `peak` at its centre, about `diameter_mm` across."""
    radius = diameter_mm / 2
    return lambda x, y: peak * np.exp(-(((x - x_mm) / radius) ** 2 + ((y - y_mm) / radius) ** 2) * 2)


def band(x0: float, x1: float, y0: float, y1: float, peak: float, soft_mm: float = 0.8) -> Field:
    """A sharp-edged bright rectangle (a window pane's reflection)."""
    def edge(v: np.ndarray, lo: float, hi: float) -> np.ndarray:
        return (1 / (1 + np.exp(np.clip(-(v - lo) / soft_mm, -50, 50)))
                / (1 + np.exp(np.clip((v - hi) / soft_mm, -50, 50))))
    return lambda x, y: peak * edge(x, x0, x1) * edge(y, y0, y1)


def uniform(value: float) -> Field:
    return lambda x, y: np.full(np.shape(x), value, np.float32)


def summed(*fields: Field) -> Field:
    return lambda x, y: sum(f(x, y) for f in fields)


# -- Where things are -----------------------------------------------------------------------------


def paper_to_wall(scene: Scene) -> np.ndarray:
    """The paper content's plane (mm) to the wall's (mm), 2 x 3: the sheet centred in the dark box
    plus the placement error, tilted about the content's centre, at the printer's scale."""
    target = calibration_target()
    cu, cv = PAPER_TARGET_MM[0] / 2, PAPER_TARGET_MM[1] / 2
    t = math.radians(scene.paper_tilt_deg)
    k = scene.paper_scale
    rotation = k * np.array([[math.cos(t), -math.sin(t)], [math.sin(t), math.cos(t)]])
    centre = np.array(target.paper_in_box) + [cu, cv] + scene.paper_offset_mm
    offset = centre - rotation @ np.array([cu, cv])
    return np.c_[rotation, offset]


def wall_point(scene: Scene, patch: Patch) -> tuple[float, float]:
    """A patch's centre on the wall."""
    x, y = patch.rect.centre()
    if patch.surface is Surface.SCREEN:
        return x, y
    wx, wy = paper_to_wall(scene) @ np.array([x, y, 1.0])
    return float(wx), float(wy)


def group_patch(group_index: int, surface: Surface, role: Role) -> Patch:
    group = calibration_target().groups[group_index]
    return next(p for p in group.patches if p.surface is surface and p.role is role)


def paired_groups() -> tuple[int, ...]:
    return tuple(g.index for g in calibration_target().groups if g.kind is GroupKind.PAIRED)


def grey_groups(signal: float) -> tuple[int, ...]:
    return tuple(g.index for g in calibration_target().groups if g.kind is GroupKind.PAIRED
                 and any(p.role is Role.GREY and p.signal == signal for p in g.patches))


# -- The canvas ------------------------------------------------------------------------------------


def _grid() -> tuple[np.ndarray, np.ndarray]:
    width = int(round((SCREEN_TARGET_MM[0] + 2 * MARGIN_MM) * PX_PER_MM))
    height = int(round((SCREEN_TARGET_MM[1] + 2 * MARGIN_MM) * PX_PER_MM))
    xs = (np.arange(width) + 0.5) / PX_PER_MM - MARGIN_MM
    ys = (np.arange(height) + 0.5) / PX_PER_MM - MARGIN_MM
    return xs, ys


def _edges(rects: list[Rect], lines: list[float], low: float, high: float, axis: int) -> np.ndarray:
    values = {low, high, *lines}
    for r in rects:
        start, size = (r.x_mm, r.width_mm) if axis == 0 else (r.y_mm, r.height_mm)
        values.update((start, start + size))
    return np.array(sorted(values))


def _rasterised(function: Callable[[float, float], object], xs: np.ndarray, ys: np.ndarray,
                edges_x: np.ndarray, edges_y: np.ndarray) -> tuple[np.ndarray, np.ndarray, list]:
    """`function` evaluated once per cell between the edges; each pixel centre takes its cell's."""
    centres_x = np.r_[edges_x[0] - 1, (edges_x[:-1] + edges_x[1:]) / 2, edges_x[-1] + 1]
    centres_y = np.r_[edges_y[0] - 1, (edges_y[:-1] + edges_y[1:]) / 2, edges_y[-1] + 1]
    cells = [[function(float(cx), float(cy)) for cx in centres_x] for cy in centres_y]
    column = np.searchsorted(edges_x, xs, side="right")
    row = np.searchsorted(edges_y, ys, side="right")
    return row, column, cells


def _marker_lines(surface: Surface, axis: int, quiet: bool) -> list[float]:
    lines = []
    for marker in calibration_target().markers:
        if marker.surface is not surface:
            continue
        start = marker.rect.x_mm if axis == 0 else marker.rect.y_mm
        module = marker.rect.width_mm / MARKER_MODULES
        span = range(-1, MARKER_MODULES + 2) if quiet else range(MARKER_MODULES + 1)
        lines.extend(start + k * module for k in span)
    return lines


@functools.cache
def screen_canvas() -> tuple[np.ndarray, np.ndarray]:
    """The screen target's encoded signal and its raw flag at every canvas pixel, from
    `screen_point` (the QR's square left at 1.0: `_with_code` draws it)."""
    target = calibration_target()
    rects = [p.rect for g in target.groups for p in g.patches if p.surface is Surface.SCREEN]
    rects += [target.qr, target.dark_box]
    xs, ys = _grid()
    edges_x = _edges(rects, _marker_lines(Surface.SCREEN, 0, True), 0.0, SCREEN_TARGET_MM[0], 0)
    edges_y = _edges(rects, _marker_lines(Surface.SCREEN, 1, True), 0.0, SCREEN_TARGET_MM[1], 1)
    row, column, cells = _rasterised(screen_point, xs, ys, edges_x, edges_y)
    signal = np.array([[c.signal for c in line] for line in cells], np.float32)
    raw = np.array([[c.raw for c in line] for line in cells], bool)
    return signal[np.ix_(row, column)], raw[np.ix_(row, column)]


def _sheet() -> tuple[float, float]:
    """The sheet's margin round the content (Letter, centred)."""
    return (LETTER_MM[0] - PAPER_TARGET_MM[0]) / 2, (LETTER_MM[1] - PAPER_TARGET_MM[1]) / 2


@functools.cache
def paper_canvas() -> np.ndarray:
    """1 where the sheet is bare, 0 where `paper_ink` prints, over the sheet (content plane mm,
    PX_PER_MM, its origin at the sheet's top-left)."""
    target = calibration_target()
    margin_x, margin_y = _sheet()
    width, height = int(round(LETTER_MM[0] * PX_PER_MM)), int(round(LETTER_MM[1] * PX_PER_MM))
    xs = (np.arange(width) + 0.5) / PX_PER_MM - margin_x
    ys = (np.arange(height) + 0.5) / PX_PER_MM - margin_y
    rects = [p.rect for g in target.groups for p in g.patches if p.surface is Surface.PAPER]
    edges_x = _edges(rects, _marker_lines(Surface.PAPER, 0, False), 0.0, PAPER_TARGET_MM[0], 0)
    edges_y = _edges(rects, _marker_lines(Surface.PAPER, 1, False), 0.0, PAPER_TARGET_MM[1], 1)
    row, column, cells = _rasterised(lambda x, y: 0.0 if paper_ink(x, y) else 1.0, xs, ys, edges_x, edges_y)
    return np.array(cells, np.float32)[np.ix_(row, column)]


def _with_code(scene: Scene, signal: np.ndarray) -> np.ndarray:
    """The QR's modules drawn into its square (supersampled, then averaged), and covered markers
    drawn as the surround."""
    target = calibration_target()
    xs, ys = _grid()
    out = signal.copy()
    qr = target.qr
    columns = np.nonzero((xs >= qr.x_mm) & (xs < qr.x_mm + qr.width_mm))[0]
    rows = np.nonzero((ys >= qr.y_mm) & (ys < qr.y_mm + qr.height_mm))[0]
    if scene.qr_covered:
        out[np.ix_(rows, columns)] = 0.5
    else:
        modules = np.array(target_modules(scene.code), bool)
        quiet = QR_QUIET_MODULES
        count = modules.shape[0] + 2 * quiet
        padded = np.zeros((count, count), bool)
        padded[quiet:quiet + modules.shape[0], quiet:quiet + modules.shape[0]] = modules
        module_mm = qr.width_mm / count
        n = QR_SUPERSAMPLE
        sub = (np.arange(n) + 0.5) / (n * PX_PER_MM) - 0.5 / PX_PER_MM
        fine_x = (xs[columns][:, None] + sub[None, :]).ravel()
        fine_y = (ys[rows][:, None] + sub[None, :]).ravel()
        mx = np.clip(((fine_x - qr.x_mm) / module_mm).astype(int), 0, count - 1)
        my = np.clip(((fine_y - qr.y_mm) / module_mm).astype(int), 0, count - 1)
        fine = np.where(padded[np.ix_(my, mx)], 0.0, 1.0)
        out[np.ix_(rows, columns)] = fine.reshape(len(rows), n, len(columns), n).mean(axis=(1, 3))
    for marker in target.markers:
        if marker.surface is Surface.SCREEN and marker.marker_id in scene.hidden_markers:
            module = marker.rect.width_mm / MARKER_MODULES
            x0, y0 = marker.rect.x_mm - module, marker.rect.y_mm - module
            size = marker.rect.width_mm + 2 * module
            out[np.ix_((ys >= y0) & (ys < y0 + size), (xs >= x0) & (xs < x0 + size))] = 0.0
    return out


def _chain(scene: Scene, signal: np.ndarray) -> np.ndarray:
    """Post-software: each encoded value through the chain as a photo pixel (one call per value)."""
    operations = operations_for(scene.code.output_id, scene.code.picture_change, scene.software, None)
    values, inverse = np.unique(signal, return_inverse=True)
    mapped = np.array([final_pixel((srgb_to_linear(float(v)),) * 3, operations, inside=True,
                                   lut=scene.lut)[:3] for v in values], np.float32)
    return mapped[inverse.reshape(signal.shape)]


def wall_light(scene: Scene, *, clean: bool = False) -> np.ndarray:
    """The light leaving the wall (linear sRGB, H x W x 3). `clean`: no reflection, glare or room
    unevenness (the truth's)."""
    signal, raw = screen_canvas()
    signal = _with_code(scene, signal)
    if scene.stage is TestPattern.TARGET_PRE_SOFTWARE:
        drawn = np.repeat(signal[..., None], 3, axis=2)
    else:
        drawn = np.where(raw[..., None], signal[..., None], _chain(scene, signal))
    xs, ys = _grid()
    xx, yy = np.meshgrid(xs, ys)
    light = scene.display.light(drawn) * scene.display.panel(yy)[..., None]
    light = light.astype(np.float32)

    margin_x, margin_y = _sheet()
    to_wall = np.vstack([paper_to_wall(scene), [0, 0, 1]])
    sheet_px_to_mm = np.array([[1 / PX_PER_MM, 0, -margin_x], [0, 1 / PX_PER_MM, -margin_y], [0, 0, 1]])
    wall_mm_to_px = np.array([[PX_PER_MM, 0, MARGIN_MM * PX_PER_MM - 0.5],
                              [0, PX_PER_MM, MARGIN_MM * PX_PER_MM - 0.5], [0, 0, 1]])
    sheet_px_centre = np.array([[1, 0, 0.5], [0, 1, 0.5], [0, 0, 1]])
    transform = (wall_mm_to_px @ to_wall @ sheet_px_to_mm @ sheet_px_centre)[:2]
    size = (xx.shape[1], xx.shape[0])
    sheet = paper_canvas()
    reflectance = scene.ink + (1 - scene.ink) * sheet
    for marker in calibration_target().markers:
        if marker.surface is Surface.PAPER and marker.marker_id in scene.hidden_markers:
            r = marker.rect
            x0 = int((r.x_mm + margin_x - 1) * PX_PER_MM)
            y0 = int((r.y_mm + margin_y - 1) * PX_PER_MM)
            side = int((r.width_mm + 2) * PX_PER_MM)
            reflectance[y0:y0 + side, x0:x0 + side] = 1.0
    paper = cv2.warpAffine(reflectance, transform, size, flags=cv2.INTER_LINEAR)
    mask = cv2.warpAffine(np.ones_like(reflectance), transform, size, flags=cv2.INTER_LINEAR)
    room = np.ones_like(xx) if clean or scene.room is None else scene.room(xx, yy)
    paper_light = (scene.paper_white * np.array(kelvin_white(scene.room_kelvin), np.float32)
                   * (paper * room)[..., None])
    light = light * (1 - mask[..., None]) + paper_light * mask[..., None]
    if not clean and scene.tv_glare is not None:
        light += (scene.tv_glare(xx, yy) * (1 - mask))[..., None].astype(np.float32)
    if not clean and scene.reflection is not None:
        light += scene.reflection(xx, yy)[..., None].astype(np.float32)
    return light.astype(np.float32)


# -- The camera -----------------------------------------------------------------------------------


def _projection(scene: Scene, canvas: tuple[int, int]) -> np.ndarray:
    """Canvas pixels to photo pixels: a pinhole turned by yaw and pitch, framing the canvas."""
    width, height = PHOTO_SIZE
    focal = width / (2 * math.tan(math.radians(69 / 2)))
    w_mm, h_mm = canvas[0] / PX_PER_MM, canvas[1] / PX_PER_MM
    corners = np.array([[-w_mm / 2, -h_mm / 2, 0], [w_mm / 2, -h_mm / 2, 0],
                        [w_mm / 2, h_mm / 2, 0], [-w_mm / 2, h_mm / 2, 0]], np.float64)
    rotation = cv2.Rodrigues(np.array([math.radians(scene.pitch_deg), math.radians(scene.yaw_deg), 0.0]))[0]
    distance = 1.12 * focal * w_mm / width
    camera = np.array([[focal, 0, width / 2], [0, focal, height / 2], [0, 0, 1]])
    points, _ = cv2.projectPoints(corners, cv2.Rodrigues(rotation)[0], np.array([0.0, 0.0, distance]), camera, None)
    source = np.float32([[0, 0], [canvas[0], 0], [canvas[0], canvas[1]], [0, canvas[1]]])
    return cv2.getPerspectiveTransform(source, points.reshape(4, 2).astype(np.float32))


def _photographed(scene: Scene) -> np.ndarray:
    light = wall_light(scene)
    canvas = (light.shape[1], light.shape[0])
    view = cv2.warpPerspective(light, _projection(scene, canvas), PHOTO_SIZE, flags=cv2.INTER_AREA,
                               borderMode=cv2.BORDER_CONSTANT, borderValue=(0.05, 0.05, 0.05))
    return view


def _shading(strength: float) -> np.ndarray:
    width, height = PHOTO_SIZE
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    r2 = ((xx - width / 2) ** 2 + (yy - height / 2) ** 2) / (width / 2) ** 2
    return (1 - strength * r2)[..., None]


def raw_photo(scene: Scene, name: str = "scene.dng") -> Photo:
    """What a ProRAW DNG decodes to (photos.load_photo's RAW): linear, the clip at 1.0."""
    rng = np.random.default_rng(scene.seed)
    camera = _photographed(scene) * _shading(RAW_SHADING)
    camera *= scene.exposure / float(np.max(scene.display.light(np.ones(3))))
    camera = cv2.GaussianBlur(camera, (0, 0), 1.0)
    noise = rng.standard_normal(camera.shape).astype(np.float32)
    # The prototype's noise per 12 MP pixel (shot over a 4000-electron white, read 0.0003); a pixel
    # here stands for about four of the phone's (PHOTO_SIZE against 4032 x 3024), so half of it.
    camera = camera + noise * 0.5 * np.sqrt(np.maximum(camera, 0) / 4000 + 0.0003 ** 2)
    clipped = np.any(camera >= 1.0, axis=2)
    return Photo(name=name, kind=PhotoKind.RAW, pixels=np.clip(camera, 0, 1).astype(np.float32), clipped=clipped)


def raw_bytes(photo: Photo) -> np.ndarray:
    """A RAW photo as rawpy's 16-bit postprocess output (what load_photo scales back)."""
    return np.round(np.clip(photo.pixels, 0, 1) * 65535).astype(np.uint16)


def jpeg_photo(scene: Scene) -> bytes:
    """A normal phone photo as a JPEG file's bytes (the prototype's phone pipeline)."""
    rng = np.random.default_rng(scene.seed)
    camera = _photographed(scene) * _shading(0.08)
    width, height = PHOTO_SIZE
    lum = camera.mean(axis=2)
    bright = camera[lum >= np.percentile(lum, 98)]
    camera = camera / (bright.mean(axis=0) / bright.mean())
    centre = camera[height // 4:3 * height // 4, width // 4:3 * width // 4].mean(axis=2)
    camera = np.clip(camera * (0.8 / np.percentile(centre, 97)) * 0.5, 0, 1)
    encoded = np.where(camera <= .0031308, 12.92 * camera, 1.055 * camera ** (1 / 2.4) - .055)
    encoded = 0.75 * encoded + 0.25 * encoded * encoded * (3 - 2 * encoded)
    encoded = cv2.GaussianBlur(encoded.astype(np.float32), (0, 0), 1.0)
    encoded = encoded + rng.normal(0, 0.004, encoded.shape).astype(np.float32)
    out = (np.clip(encoded, 0, 1) * 255 + .5).astype(np.uint8)
    ok, data = cv2.imencode(".jpg", cv2.cvtColor(out, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 90])
    assert ok
    return data.tobytes()


# -- The truth and the entry ----------------------------------------------------------------------


def _at(light: np.ndarray, x_mm: float, y_mm: float, radius_mm: float = 2.0) -> np.ndarray:
    x0, x1 = int((x_mm - radius_mm + MARGIN_MM) * PX_PER_MM), int((x_mm + radius_mm + MARGIN_MM) * PX_PER_MM)
    y0, y1 = int((y_mm - radius_mm + MARGIN_MM) * PX_PER_MM), int((y_mm + radius_mm + MARGIN_MM) * PX_PER_MM)
    return np.median(light[y0:y1, x0:x1].reshape(-1, 3), axis=0)


def truth(scene: Scene) -> Truth:
    light = wall_light(scene, clean=True)
    whites, tone = [], {}
    for index in paired_groups():
        def at(surface: Surface, role: Role) -> np.ndarray:
            return _at(light, *wall_point(scene, group_patch(index, surface, role)))
        screen_black = at(Surface.SCREEN, Role.BLACK)
        screen_white = at(Surface.SCREEN, Role.WHITE) - screen_black
        whites.append(screen_white / (at(Surface.PAPER, Role.WHITE) - at(Surface.PAPER, Role.BLACK)))
        grey = group_patch(index, Surface.SCREEN, Role.GREY)
        tone.setdefault(grey.signal, []).append((at(Surface.SCREEN, Role.GREY) - screen_black) / screen_white)
    return Truth(white=np.median(whites, axis=0),
                 tone={s: np.median(v, axis=0) for s, v in tone.items()},
                 lowest_distinct=scene.display.crush + 1)


def entry(scene: Scene, *, test_pattern: TestPattern | None = None, lut_name: str = "Hahnemuhle") -> PictureEntryView:
    """The sent entry the scene's code names, its software the scene's."""
    software = scene.software
    lut = None if software.lut is None else LutChoiceView(profile_sha256=software.lut.profile_sha256, name=lut_name)
    view = SoftwarePictureView(
        brightness_percent=software.brightness_percent, contrast_percent=software.contrast_percent,
        colour_temperature_kelvin=software.colour_temperature_kelvin, gamma_tenths=software.gamma_tenths,
        saturation_percent=software.saturation_percent,
        highlight_rolloff_percent=software.highlight_rolloff_percent,
        black_floor_tenths=software.black_floor_tenths, lut=lut, lut_amount_percent=software.lut_amount_percent)
    return PictureEntryView(
        entry_id=uuid.UUID(int=scene.code.picture_change), display=DisplayRefView(id=uuid.UUID(int=7), name="Samsung"),
        kind=PictureEntryKind.SENT_DRAFT, number=scene.code.picture_change, at=datetime(2026, 10, 13, 20, tzinfo=UTC),
        frame=None, session_id=uuid.UUID(int=9), restored_from=None,
        picture=PictureValuesView(hardware={}, software=view, signal_range=SignalRange.FULL,
                                  lut_recipe_id=None if software.lut is None else software.lut.recipe_id),
        test_pattern=test_pattern or scene.stage)


def codes(share: np.ndarray) -> np.ndarray:
    """A linear share as 8-bit sRGB codes (the greys' tolerance unit)."""
    v = np.clip(share, 0, 1)
    return 255 * np.where(v <= .0031308, 12.92 * v, 1.055 * np.maximum(v, 1e-12) ** (1 / 2.4) - .055)


__all__ = ["CODE", "Display", "Scene", "Truth", "band", "blob", "codes", "entry", "grey_groups", "group_patch",
           "jpeg_photo", "paired_groups", "paper_to_wall", "raw_bytes", "raw_photo", "replace", "screen_canvas",
           "summed", "truth", "uniform", "wall_point"]
