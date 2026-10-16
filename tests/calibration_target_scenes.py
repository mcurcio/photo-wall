"""Synthetic phone photos of both calibration boards for the reader's tests (decision 0027), ported from
the design's prototype (proto-v3 scene_v3.py) onto the committed boards.

The wall plane is millimetres, its origin at the Frame opening's top-left, x right, y down. Layers
back to front: the wall, the Frame's border, the screen board (emissive), a black card, the paper
piece. The screen board is what the Player draws (player/geometry.py `board_point` places it in the
opening, `board_signal` gives each point, with the QR of player/calibration_target.py
`target_modules`); pre-software every point is raw, post-software every point passes the Display's
steps (appliance/display_host/picture.py `operations_for` of the entry's software, player/geometry.py
`display_steps`; decision 0028: never the room's light, the paper or the mat). The paper is a piece of contracts.calibration_target `board_raster(PAPER)` (rows and
columns of the sheet) at any scale and turn, its ink of any reflectance. The glass passes the
screen's light once and a paper's under it twice, and adds reflections (sharp windows, soft blobs,
a gradient); the room light falls across the wall. Optional: the panel's light falling with the
viewing angle (VA-like). The camera is a pinhole at a yaw and pitch with lens falloff 1 - k r^2:
RAW (shot and read noise, the sensor's clip at 1.0) or a phone JPEG (auto white balance and
exposure, local tone mapping, an S-curve, 8 bits). Light is linear sRGB: the display's white is its
level times `kelvin_white`, the paper's the room's.

No per-pixel Python loop: the screen board is evaluated once per region between its edges (the
raster's pixels and the QR's modules), cached per (columns, code), and the photo is drawn with
cv2.warpPerspective and numpy.
"""
from __future__ import annotations

import functools
import math
import uuid
import warnings
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Final

import cv2
import numpy as np

from appliance.display_host.picture import operations_for
from central.displays.picture_history import PictureEntryKind
from central.displays.views import (
    DisplayRefView,
    PictureEntryView,
    PictureValuesView,
    SoftwarePictureView,
)
from contracts.calibration_target import (
    PAPER_COLUMNS,
    PAPER_ROWS,
    PAPER_SQUARE_MM,
    QR_QUIET_MODULES,
    QR_SQUARES,
    SCREEN_ROWS,
    SQUARE_PX,
    Surface,
    TargetCode,
    board_raster,
    qr_copies,
)
from contracts.kelvin import kelvin_white
from contracts.node_frame import srgb_to_linear
from contracts.node_picture import (
    NEUTRAL_SOFTWARE,
    UPGRADE_DISPLAY_LIGHT,
    SignalRange,
    SoftwarePicture,
)
from contracts.tuning import SIGNAL_LEVELS, TestPattern
from player.calibration_target import target_modules
from player.geometry import BoardDraw, board_point, board_signal, display_steps
from scripts.calibration_target.model import Photo, PhotoKind

with warnings.catch_warnings():
    # colour-science warns at import that matplotlib (its plotting, unused here) is missing.
    warnings.filterwarnings("ignore", message='"Matplotlib" related API')
    import colour

PHOTO_SIZE: Final = (2016, 1512)        # a 12 MP photo at half its side: the suite's time
CANVAS_PX: Final = 48                   # the screen canvas's pixels a square (at least 40)
GLASS_T: Final = 0.92                   # the Frame glass's transmission
BORDER_MM: Final = 25.0                 # the Frame's border round the opening
CARD_MM: Final = 6.0                    # the black card beyond the paper on every side, by default
CODE: Final = TargetCode("pi-wall-1", "HDMI-A-1", 41)
TV43: Final = (950.0, 535.0)            # a 43-inch 16:9 opening
FIVE_INCH: Final = (110.0, 62.0)        # a 5-inch 16:9 opening

Reflection = tuple   # ("window", x, y, w, h, peak) | ("blob", x, y, sigma, peak) | ("grad", kx, ky), wall mm


@dataclass(frozen=True)
class Display:
    """A display's own response to a raw signal: `white` its peak's level, `kelvin` its white's
    colour, `black` raw 0's light (a share of its white), `gamma` None for the sRGB curve, `crush`
    the codes 1..crush shown as raw 0."""
    white: float = 1.0
    kelvin: int = 6500
    black: float = 0.0008
    gamma: float | None = None
    crush: int = 0

    def light(self, signal: np.ndarray) -> np.ndarray:
        """Linear light for an encoded signal (..., 3)."""
        s = np.clip(signal, 0.0, 1.0)
        if self.crush:
            s = np.where(np.round(s * SIGNAL_LEVELS) <= self.crush, 0.0, s)
        response = colour.cctf_decoding(s, function="sRGB").astype(s.dtype) if self.gamma is None \
            else s ** self.gamma
        return self.white * np.array(kelvin_white(self.kelvin), np.float32) * (self.black + (1 - self.black) * response)


@dataclass(frozen=True)
class Scene:
    display: Display = field(default_factory=Display)
    stage: TestPattern = TestPattern.TARGET_PRE_SOFTWARE
    software: SoftwarePicture = NEUTRAL_SOFTWARE
    code: TargetCode = CODE
    code_drawn: bool = True                 # False: the Player has no code (BoardDraw.modules None)
    screen_shown: bool = True               # False: the screen shows nothing (black)
    opening_mm: tuple[float, float] = TV43
    glass: bool = True
    # The paper: (row, column, rows, columns) of the sheet, its print scale, its middle (wall mm;
    # None: the opening's middle), its turn; None: no paper in the photo.
    piece: tuple[int, int, int, int] | None = (0, 0, PAPER_ROWS, PAPER_COLUMNS)
    paper_scale: float = 1.0
    paper_mm: tuple[float, float] | None = None
    paper_turn_deg: float = 0.0
    on_screen: bool = True                  # under the glass on the panel (with a card); else beside the Frame
    card_mm: float = CARD_MM                # the black card beyond the paper on every side
    card: float = 0.03                      # the card's reflectance
    paper_whites_covered: bool = False      # a black card over the paper's white squares
    paper_white: float = 0.8                # bare paper's light under the room, against the display's white 1
    room_kelvin: int = 5000
    room_gradient: tuple[float, float] = (0.00004, 0.00002)   # the room light's change per mm, x and y
    ink: float = 0.04                       # the print's ink reflectance
    reflections: tuple[Reflection, ...] = ()
    va_falloff: bool = False                # the panel's light falls with the viewing angle (VA-like)
    yaw_deg: float = 8.0
    pitch_deg: float = 3.0
    distance_mm: float | None = None        # None: the opening and its border fill most of the photo
    aim_mm: tuple[float, float] | None = None   # None: the opening's middle
    focal_widths: float = 0.75              # the lens's focal length in photo widths
    vignette: float = 0.03                  # lens falloff k of 1 - k r^2, r over half the width
    exposure: float = 0.6                   # RAW: the display's white 1 in the photo
    jpeg_exposure: float = 1.0
    seed: int = 1


@dataclass(frozen=True)
class Truth:
    """What the reader must recover, from the scene with no reflection, angle falloff or camera:
    screen white above its black over bare paper white, at the paper's middle (per channel)."""
    white: np.ndarray


def paper_matched(scene: Scene, *, level: float = 1.0, kelvin: int | None = None, **display: object) -> Display:
    """A display whose white reads as the scene's bare paper white (the reader's aim) at the paper's
    middle, times `level`; at `kelvin` (default the room's) with the same luminance. Other Display
    fields from `display`."""
    kelvin = scene.room_kelvin if kelvin is None else kelvin
    luminance = np.array([0.2126, 0.7152, 0.0722])
    same = float(luminance @ np.array(kelvin_white(scene.room_kelvin))) / float(luminance @ np.array(kelvin_white(kelvin)))
    black = float(display.get("black", Display.black))  # type: ignore[arg-type]
    glass = GLASS_T if scene.glass and scene.on_screen else 1.0
    white = level * scene.paper_white * _room(scene, *paper_middle(scene)) * glass * same / (1 - black)
    return Display(white=white, kelvin=kelvin, **display)  # type: ignore[arg-type]


# -- Where things are --------------------------------------------------------------------------------


def _move(dx: float, dy: float) -> np.ndarray:
    return np.array([[1, 0, dx], [0, 1, dy], [0, 0, 1]], np.float64)


def _scale(sx: float, sy: float | None = None) -> np.ndarray:
    return np.diag([sx, sx if sy is None else sy, 1.0])


def _turn(degrees: float) -> np.ndarray:
    a = math.radians(degrees)
    return np.array([[math.cos(a), -math.sin(a), 0], [math.sin(a), math.cos(a), 0], [0, 0, 1]])


def board_draw(scene: Scene) -> BoardDraw:
    width, height = scene.opening_mm
    modules = target_modules(scene.code) if scene.code_drawn else None
    return BoardDraw(aspect=max(width, height) / min(width, height), portrait=width < height, modules=modules)


def screen_to_wall(scene: Scene) -> np.ndarray:
    """The screen board's (column, row), in squares, to the wall (mm): board_point's map inverted
    (a landscape opening: affine)."""
    draw = board_draw(scene)
    origin = np.array(board_point(0.0, 0.0, draw))
    along_u = np.array(board_point(1.0, 0.0, draw)) - origin
    along_v = np.array(board_point(0.0, 1.0, draw)) - origin
    uv_to_board = np.array([[along_u[0], along_v[0], origin[0]], [along_u[1], along_v[1], origin[1]], [0, 0, 1]])
    return _scale(*scene.opening_mm) @ np.linalg.inv(uv_to_board)


def paper_middle(scene: Scene) -> tuple[float, float]:
    if scene.paper_mm is not None:
        return scene.paper_mm
    return scene.opening_mm[0] / 2, scene.opening_mm[1] / 2


def paper_to_wall(scene: Scene) -> np.ndarray:
    """The paper board's (column, row), in squares, to the wall (mm): the piece's middle at
    paper_middle, turned, at its print scale."""
    row, column, rows, columns = scene.piece  # type: ignore[misc]
    return (_move(*paper_middle(scene)) @ _turn(scene.paper_turn_deg) @ _scale(PAPER_SQUARE_MM * scene.paper_scale)
            @ _move(-(column + columns / 2), -(row + rows / 2)))


def camera(scene: Scene) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """K, the wall (mm) -> photo homography and the camera's centre (mm, z toward the viewer < 0)."""
    width, height = PHOTO_SIZE
    focal = scene.focal_widths * width
    k = np.array([[focal, 0, width / 2], [0, focal, height / 2], [0, 0, 1]])
    ow, oh = scene.opening_mm
    aim = np.array([*(scene.aim_mm or (ow / 2, oh / 2)), 0.0])
    distance = scene.distance_mm or focal * (ow + 2 * BORDER_MM) / (0.92 * width)
    yaw, pitch = math.radians(scene.yaw_deg), math.radians(scene.pitch_deg)
    axis = np.array([math.sin(yaw) * math.cos(pitch), math.sin(pitch), math.cos(yaw) * math.cos(pitch)])
    centre = aim - distance * axis
    x = np.cross([0, 1.0, 0], axis)
    x /= np.linalg.norm(x)
    rotation = np.stack([x, np.cross(axis, x), axis])
    to_photo = k @ np.column_stack([rotation[:, 0], rotation[:, 1], -rotation @ centre])
    return k, to_photo / to_photo[2, 2], centre


def focal_px(scene: Scene) -> float:
    """The scene's true focal length in photo pixels (what EXIF would say)."""
    return scene.focal_widths * PHOTO_SIZE[0]


def true_angle(scene: Scene, points: np.ndarray) -> float:
    """The median true viewing angle (degrees) at photo points (N x 2) on the wall."""
    _, to_photo, centre = camera(scene)
    wall = np.linalg.inv(to_photo) @ np.vstack([np.asarray(points, np.float64).T, np.ones(len(points))])
    x, y = wall[0] / wall[2], wall[1] / wall[2]
    cosine = abs(centre[2]) / np.sqrt((x - centre[0]) ** 2 + (y - centre[1]) ** 2 + centre[2] ** 2)
    return float(np.median(np.degrees(np.arccos(cosine))))


# -- The screen board as the Player draws it -------------------------------------------------------


@functools.cache
def _screen_signal(columns: int, aspect: float, modules: tuple | None) -> np.ndarray:
    """board_signal over the board's columns x SCREEN_ROWS at CANVAS_PX a square: evaluated once per
    region between the raster's pixel edges and the QR modules' edges, each canvas pixel its
    region's value."""
    draw = BoardDraw(aspect=aspect, portrait=False, modules=modules)
    edges_x = {k / SQUARE_PX for k in range(columns * SQUARE_PX + 1)}
    edges_y = {k / SQUARE_PX for k in range(SCREEN_ROWS * SQUARE_PX + 1)}
    if modules is not None:
        count = len(modules) + 2 * QR_QUIET_MODULES
        for copy in qr_copies(columns):
            edges_x |= {copy.column + j * QR_SQUARES / count for j in range(count + 1)}
            edges_y |= {copy.row + j * QR_SQUARES / count for j in range(count + 1)}
    xs, ys = np.array(sorted(edges_x)), np.array(sorted(edges_y))
    middles_x, middles_y = (xs[:-1] + xs[1:]) / 2, (ys[:-1] + ys[1:]) / 2
    cells = np.array([[board_signal(float(x), float(y), draw) for x in middles_x] for y in middles_y], np.float32)
    centres_x = (np.arange(columns * CANVAS_PX) + 0.5) / CANVAS_PX
    centres_y = (np.arange(SCREEN_ROWS * CANVAS_PX) + 0.5) / CANVAS_PX
    return cells[np.ix_(np.searchsorted(ys, centres_y, side="right") - 1,
                        np.searchsorted(xs, centres_x, side="right") - 1)]


def _chain(scene: Scene, signal: np.ndarray) -> np.ndarray:
    """Post-software: each encoded value through the Display's steps (one call per distinct value)."""
    operations = operations_for(scene.code.output_id, scene.code.picture_change, scene.software,
                                UPGRADE_DISPLAY_LIGHT, None)
    values, inverse = np.unique(signal, return_inverse=True)
    mapped = np.array([display_steps((srgb_to_linear(float(v)),) * 3, operations) for v in values], np.float32)
    return mapped[inverse.reshape(signal.shape)]


def screen_light(scene: Scene) -> np.ndarray:
    """The screen board's light on its canvas (H x W x 3, linear), before the glass."""
    draw = board_draw(scene)
    signal = _screen_signal(draw.columns, draw.aspect, draw.modules)
    if not scene.screen_shown:
        signal = np.zeros_like(signal)
    if scene.stage is TestPattern.TARGET_PRE_SOFTWARE:
        drawn = np.repeat(signal[..., None], 3, axis=2)
    else:
        drawn = _chain(scene, signal)
    return scene.display.light(drawn).astype(np.float32)


# -- The photo ------------------------------------------------------------------------------------------


def _room(scene: Scene, x: np.ndarray | float, y: np.ndarray | float) -> np.ndarray | float:
    """The room light's relative level across the wall (1 at the opening's middle)."""
    gx, gy = scene.room_gradient
    return 1 + gx * (x - scene.opening_mm[0] / 2) + gy * (y - scene.opening_mm[1] / 2)


def _warp(image: np.ndarray, matrix: np.ndarray, interpolation: int = cv2.INTER_LINEAR) -> np.ndarray:
    return cv2.warpPerspective(image, matrix, PHOTO_SIZE, flags=interpolation, borderMode=cv2.BORDER_CONSTANT,
                               borderValue=0)


def _paint(light: np.ndarray, value: np.ndarray, where: np.ndarray) -> None:
    """`light[where] = value[where]` (or a colour), in place without boolean indexing's copies: the
    same values, a fraction of the time on a whole photo."""
    np.copyto(light, value, casting="same_kind", where=where[..., None])


def radiance(scene: Scene) -> np.ndarray:
    """The light reaching the camera from each photo pixel (H x W x 3, linear), before the lens."""
    width, height = PHOTO_SIZE
    _, to_photo, centre = camera(scene)
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    inverse = np.linalg.inv(to_photo)
    w = inverse[2, 0] * xx + inverse[2, 1] * yy + inverse[2, 2]
    x = (inverse[0, 0] * xx + inverse[0, 1] * yy + inverse[0, 2]) / w
    y = (inverse[1, 0] * xx + inverse[1, 1] * yy + inverse[1, 2]) / w
    ow, oh = scene.opening_mm
    tint = np.array(kelvin_white(scene.room_kelvin), np.float32)
    room = (scene.paper_white * _room(scene, x, y))[..., None] * tint
    glass = ((x > -BORDER_MM) & (x < ow + BORDER_MM) & (y > -BORDER_MM) & (y < oh + BORDER_MM)) if scene.glass \
        else np.zeros_like(x, bool)
    opening = (x >= 0) & (x < ow) & (y >= 0) & (y < oh)
    t = GLASS_T if scene.glass else 1.0
    light = (0.55 * room).astype(np.float32)                       # the wall
    border = glass & ~opening
    _paint(light, 0.10 * room * t * t, border)
    # The screen, emissive.
    canvas = screen_light(scene)
    to_canvas = to_photo @ screen_to_wall(scene) @ _scale(1 / CANVAS_PX) @ _move(-0.5, -0.5)
    shown = _warp(canvas, to_canvas)
    inside = _warp(np.ones(canvas.shape[:2], np.float32), to_canvas) > 0.5
    gain = t
    if scene.va_falloff:
        cosine = np.abs(centre[2]) / np.sqrt((x - centre[0]) ** 2 + (y - centre[1]) ** 2 + centre[2] ** 2)
        gain = gain * np.maximum(0.2, 1 - 0.8 * (1 - cosine ** 2))[..., None]
    screen = opening & inside
    _paint(light, scene.display.light(np.zeros(3)) * t, opening)   # the surround is raw black
    _paint(light, shown * gain, screen)
    # The card and the paper.
    if scene.piece is not None:
        row, column, rows, columns = scene.piece
        paper_t = t * t if scene.on_screen else 1.0
        to_wall = paper_to_wall(scene)
        if scene.on_screen:
            grow = scene.card_mm / (PAPER_SQUARE_MM * scene.paper_scale)
            card = np.ones((int((rows + 2 * grow) * 20), int((columns + 2 * grow) * 20)), np.float32)
            covered = _warp(card, to_photo @ to_wall @ _move(column - grow, row - grow) @ _scale(1 / 20)) > 0.5
            _paint(light, scene.card * room * paper_t, covered)
        raster = board_raster(Surface.PAPER)
        sheet = np.frombuffer(raster.pixels, np.uint8).reshape(raster.height, raster.width).astype(np.float32) / 255
        sheet = sheet[row * SQUARE_PX:(row + rows) * SQUARE_PX, column * SQUARE_PX:(column + columns) * SQUARE_PX]
        if scene.paper_whites_covered:
            sheet = _whites_covered(sheet, row, column)
        reflectance = scene.ink + (1 - scene.ink) * sheet
        to_sheet = to_photo @ to_wall @ _move(column, row) @ _scale(1 / SQUARE_PX) @ _move(-0.5, -0.5)
        paper = _warp(np.ascontiguousarray(reflectance), to_sheet)
        on = _warp(np.ones_like(reflectance), to_sheet) > 0.5
        _paint(light, paper[..., None] * room * paper_t, on)
    else:
        on = np.zeros_like(opening)
    # Reflections on the glass: additive, in the panel white's units.
    added = np.zeros((height, width), np.float32)
    for reflection in scene.reflections:
        if reflection[0] == "window":
            _, x0, y0, w_mm, h_mm, peak = reflection
            soft = 1.5
            added += peak * (np.clip((x - x0) / soft, 0, 1) * np.clip((x0 + w_mm - x) / soft, 0, 1)
                             * np.clip((y - y0) / soft, 0, 1) * np.clip((y0 + h_mm - y) / soft, 0, 1))
        elif reflection[0] == "blob":
            _, x0, y0, sigma, peak = reflection
            added += peak * np.exp(-((x - x0) ** 2 + (y - y0) ** 2) / (2 * sigma * sigma))
        elif reflection[0] == "grad":
            _, kx, ky = reflection
            added += np.maximum(0, kx * x / ow + ky * y / oh)
    under_glass = glass & (np.bool_(scene.on_screen) | ~on)
    light += (added * under_glass)[..., None] * np.array([1.0, 0.98, 0.93], np.float32)
    return light


def _whites_covered(sheet: np.ndarray, row: int, column: int) -> np.ndarray:
    """The paper's bare white squares (odd rows, white parity, no marker) painted as ink."""
    out = sheet.copy()
    for r in range(sheet.shape[0] // SQUARE_PX):
        for c in range(sheet.shape[1] // SQUARE_PX):
            board_row, board_column = row + r, column + c
            if board_row % 2 == 1 and (board_row + board_column) % 2 == 1:
                out[r * SQUARE_PX:(r + 1) * SQUARE_PX, c * SQUARE_PX:(c + 1) * SQUARE_PX] = 0.0
    return out


def _lens(scene: Scene) -> np.ndarray:
    width, height = PHOTO_SIZE
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    r2 = ((xx - width / 2) ** 2 + (yy - height / 2) ** 2) / (width / 2) ** 2
    return (1 - scene.vignette * r2)[..., None]


@functools.cache
def raw_photo(scene: Scene, name: str = "scene.dng") -> Photo:
    """What a ProRAW DNG decodes to (photos.load_photo's RAW): linear, the clip at 1.0, focal_px None
    (the file's EXIF is the edge's: tests put focal_px(scene) on it)."""
    rng = np.random.default_rng(scene.seed)
    light = radiance(scene) * _lens(scene)
    light *= scene.exposure / float(np.max(scene.display.light(np.ones(3))))
    light = cv2.GaussianBlur(light, (0, 0), 0.9)
    noise = rng.standard_normal(light.shape).astype(np.float32)
    # About a quarter of a 12 MP phone pixel's noise is left in a pixel standing for four of them.
    light = light + noise * 0.5 * np.sqrt(np.maximum(light, 0) / 4000 + 0.0003 ** 2)
    clipped = np.any(light >= 1.0, axis=2)
    return Photo(name=name, kind=PhotoKind.RAW, pixels=np.clip(light, 0, 1).astype(np.float32), clipped=clipped,
                 focal_px=None)


def raw_bytes(photo: Photo) -> np.ndarray:
    """A RAW photo as rawpy's 16-bit postprocess output (what load_photo scales back)."""
    return np.round(np.clip(photo.pixels, 0, 1) * 65535).astype(np.uint16)


def jpeg_photo(scene: Scene) -> bytes:
    """A normal phone photo as a JPEG file's bytes (the prototype's phone pipeline)."""
    rng = np.random.default_rng(scene.seed)
    light = radiance(scene) * _lens(scene)
    width, height = PHOTO_SIZE
    level = light.mean(axis=2)
    bright = light[level >= np.percentile(level, 98)]
    light = light / (bright.mean(axis=0) / bright.mean())
    light = light * (0.85 / np.percentile(light.mean(axis=2), 99.7)) * 0.6 * scene.jpeg_exposure
    level = np.maximum(light.mean(axis=2), 1e-4)
    local = cv2.GaussianBlur(np.log(level), (0, 0), width / 12)
    light = np.clip(light * np.exp(-0.3 * (local - np.log(level).mean()))[..., None], 0, 1)
    encoded = colour.cctf_encoding(light, function="sRGB")
    encoded = 0.75 * encoded + 0.25 * encoded * encoded * (3 - 2 * encoded)
    encoded = cv2.GaussianBlur(encoded.astype(np.float32), (0, 0), 1.0)
    encoded = encoded + rng.normal(0, 0.004, encoded.shape).astype(np.float32)
    out = (np.clip(encoded, 0, 1) * 255 + .5).astype(np.uint8)
    ok, data = cv2.imencode(".jpg", cv2.cvtColor(out, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 90])
    assert ok
    return data.tobytes()


# -- The truth and the entry --------------------------------------------------------------------------


def truth(scene: Scene) -> Truth:
    """Screen white above black over bare paper white at the paper's middle: no reflection, no angle
    falloff, no camera."""
    t = GLASS_T if scene.glass else 1.0
    screen = scene.display.white * np.array(kelvin_white(scene.display.kelvin)) * (1 - scene.display.black) * t
    paper_t = t * t if scene.on_screen else 1.0
    paper = scene.paper_white * float(_room(scene, *paper_middle(scene))) * np.array(kelvin_white(scene.room_kelvin))
    return Truth(white=screen / (paper * paper_t))


# TRANSIT (decision 0028): the print look's view fields at their neutral values, for the view as it is
# until lane central deletes them; `_declared` drops them once it has. The end review deletes this.
_TRANSIT_SOFTWARE: Final = dict(zip(("brightness_percent", "saturation_percent", "highlight_rolloff_percent",
                                     "black_floor_tenths", "lut", "lut_amount_percent"),
                                    (100, 100, 0, 0, None, 100), strict=True))


def _declared(model: type, values: dict[str, object]) -> dict[str, object]:
    """`values` the view model declares. Central's picture view and these scenes change in one
    delivery (decision 0028: the view's software group is contrast, colour temperature and gamma,
    beside the Display's light); built from its declared fields, a scene's entry is the view's
    whichever lane's commit it meets."""
    return {name: value for name, value in values.items() if name in model.model_fields}


def entry(scene: Scene, *, test_pattern: TestPattern | None = None) -> PictureEntryView:
    """The sent entry the scene's code names, its software the scene's, at the upgrade's light."""
    software = scene.software
    view = SoftwarePictureView(**_declared(SoftwarePictureView, {
        "contrast_percent": software.contrast_percent, "colour_temperature_kelvin": software.colour_temperature_kelvin,
        "gamma_tenths": software.gamma_tenths, **_TRANSIT_SOFTWARE}))
    picture = PictureValuesView(**_declared(PictureValuesView, {
        "hardware": {}, "software": view, "signal_range": SignalRange.FULL, "lut_recipe_id": None,
        "light": {"matched_lux": UPGRADE_DISPLAY_LIGHT.matched_lux, "black": UPGRADE_DISPLAY_LIGHT.black}}))
    return PictureEntryView(
        entry_id=uuid.UUID(int=scene.code.picture_change), display=DisplayRefView(id=uuid.UUID(int=7), name="Samsung"),
        kind=PictureEntryKind.SENT_DRAFT, number=scene.code.picture_change, at=datetime(2026, 10, 13, 20, tzinfo=UTC),
        frame=None, session_id=uuid.UUID(int=9), restored_from=None, picture=picture,
        test_pattern=test_pattern or scene.stage)


__all__ = ["CODE", "FIVE_INCH", "PAPER_SQUARE_MM", "PHOTO_SIZE", "TV43", "Display", "Scene", "Truth", "board_draw",
           "camera", "entry", "focal_px", "jpeg_photo", "paper_matched", "paper_middle", "paper_to_wall", "radiance",
           "raw_bytes", "raw_photo", "replace", "screen_to_wall", "true_angle", "truth"]
