"""The calibration target's code on the Player (decisions 0026, 0027): the registered player id
reaches the renderer and is cleared when the registration goes (player/service.py), and the native
renderer uploads the screen board's raster once per GL context and its QR once per code
(player/native.py `_bind_target`), never per frame. The pixels are the Linux pixel harness's
(tests/native_renderer_pixel_harness.py)."""
from __future__ import annotations

import asyncio
import itertools
import threading

import httpx
from test_player_service import close, rig

from contracts.calibration_target import Surface, TargetCode, board_raster
from contracts.lut import LutRef
from contracts.node_frame import LutMix, PictureOperations
from contracts.tuning import TestPattern
from player.calibration_target import target_modules
from player.native import NativeOutput, NativeRenderer, _Surface

PLAYER = "p-" + "0123456789abcdef" * 2


def test_the_registered_player_id_reaches_the_renderer_and_goes_with_the_registration(tmp_path, monkeypatch):
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)

    async def check():
        service, server = await rig(tmp_path)
        assert service.renderer.player_id == server.player_id == service.registration.player_id
        refused = asyncio.Event()

        def handle(request):
            if request.url.path in ("/v1/player/state", "/v1/enrollment/register"):
                refused.set()
                return httpx.Response(401 if request.url.path == "/v1/player/state" else 503)
            return server(request)

        await service.link.client.aclose()
        service.link.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        service.link.time_client = service.link.client
        task = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(refused.wait(), 3)
            for _ in range(300):
                if service.renderer.player_id is None:
                    break
                await asyncio.sleep(.01)
            assert service.registration is None
            assert service.renderer.player_id is None
        finally:
            service.stop()
            await asyncio.wait_for(task, 3)
            await close(service)
    asyncio.run(check())


class SpyGL:
    """The GL calls `_bind_target` makes: every texture upload recorded by its format."""

    def __init__(self):
        self.uploads: list[tuple[str, int, int, bytes]] = []
        self.deleted: list[int] = []
        self.names = itertools.count(1)

    def __getattr__(self, name: str):
        if name.startswith("GL_"):
            return name if name in ("GL_R8", "GL_RED") else 0x1000
        return lambda *args: None

    def glGenTextures(self, _count):
        return next(self.names)

    def glDeleteTextures(self, textures):
        self.deleted.extend(textures)

    def glTexImage2D(self, _target, _level, internal, width, height, _border, layout, _type, data):
        self.uploads.append((internal, layout, width, height, data))


def spy_renderer() -> NativeRenderer:
    """A NativeRenderer with no GTK: only what `_bind_target` and `set_player` read."""
    renderer = NativeRenderer.__new__(NativeRenderer)
    renderer.GL = SpyGL()
    renderer._owner = threading.get_ident()
    renderer._closed = False
    renderer._surfaces = {}
    renderer._player_id = None
    return renderer


def target_operations(change: int) -> PictureOperations:
    return PictureOperations("HDMI-A-1", change, (1.0, 1.0, 1.0), 1.0, 1.0, 1.0,
                             test_pattern=TestPattern.TARGET_PRE_SOFTWARE)


def uploads(renderer: NativeRenderer) -> tuple[int, int]:
    """(the board raster's uploads, the QR's uploads): both one byte a texel (R8 of RED), told
    apart by the raster's size and bytes."""
    raster = board_raster(Surface.SCREEN)
    board = ("GL_R8", "GL_RED", raster.width, raster.height, raster.pixels)
    boards = sum(1 for upload in renderer.GL.uploads if upload == board)
    qrs = sum(1 for upload in renderer.GL.uploads
              if upload != board and upload[:2] == board[:2] and upload[2] == upload[3])
    assert boards + qrs == len(renderer.GL.uploads), [upload[:4] for upload in renderer.GL.uploads]
    return boards, qrs


def test_the_layout_uploads_once_per_context_and_the_qr_once_per_code():
    renderer = spy_renderer()
    surface = _Surface(NativeOutput("HDMI-A-1", "photo-wall-hdmi-a-1"), None, None)
    # No player id yet: the board's raster, but no QR (every QR square draws light).
    assert renderer._bind_target(surface, target_operations(3), lut_drawn=False) == 0
    assert uploads(renderer) == (1, 0)
    renderer._player_id = PLAYER
    modules = target_modules(TargetCode(PLAYER, "HDMI-A-1", 3))
    for _ in range(5):                      # five frames of one code
        assert renderer._bind_target(surface, target_operations(3), lut_drawn=False) == len(modules)
    assert uploads(renderer) == (1, 1)
    qr = renderer.GL.uploads[-1]
    assert qr[2:] == (len(modules), len(modules), bytes(255 if d else 0 for row in modules for d in row))
    renderer._bind_target(surface, target_operations(4), lut_drawn=False)       # a new picture change: a new QR
    renderer._bind_target(surface, target_operations(4), lut_drawn=False)
    assert uploads(renderer) == (1, 2)
    assert renderer.GL.deleted == [2]                            # the old QR's texture freed
    surface.target_layout = surface.target_qr = 0                # a new GL context (`_unrealize`)
    surface.target_code = None
    renderer._bind_target(surface, target_operations(4), lut_drawn=False)
    assert uploads(renderer) == (2, 3)


def test_the_post_software_qr_waits_for_the_lut_it_names():
    """The code claims the entry's whole picture: while the LUT a post-software target names is not
    drawn (its bytes not held yet), the square draws light, so no photo is read against a look that
    was not on screen. The pre-software target draws no LUT, so its code never waits."""
    renderer = spy_renderer()
    renderer._player_id = PLAYER
    surface = _Surface(NativeOutput("HDMI-A-1", "photo-wall-hdmi-a-1"), None, None)
    lut = LutMix(LutRef("c0ffee" + "0" * 58, "lut33-test-recipe"), 0.6)
    post = PictureOperations("HDMI-A-1", 5, (1.0, 1.0, 1.0), 1.0, 1.0, 1.0, lut=lut,
                             test_pattern=TestPattern.TARGET_POST_SOFTWARE)
    modules = len(target_modules(TargetCode(PLAYER, "HDMI-A-1", 5)))
    assert renderer._bind_target(surface, post, lut_drawn=False) == 0
    assert renderer._bind_target(surface, post, lut_drawn=True) == modules
    pre = PictureOperations("HDMI-A-1", 5, (1.0, 1.0, 1.0), 1.0, 1.0, 1.0, lut=lut,
                            test_pattern=TestPattern.TARGET_PRE_SOFTWARE)
    assert renderer._bind_target(surface, pre, lut_drawn=False) == modules
    without = PictureOperations("HDMI-A-1", 5, (1.0, 1.0, 1.0), 1.0, 1.0, 1.0,
                                test_pattern=TestPattern.TARGET_POST_SOFTWARE)
    assert renderer._bind_target(surface, without, lut_drawn=False) == modules


def test_set_player_redraws_only_on_a_change():
    renderer = spy_renderer()
    queued = []
    area = type("Area", (), {"queue_render": lambda self: queued.append(1)})()
    renderer._surfaces = {"HDMI-A-1": _Surface(NativeOutput("HDMI-A-1", "photo-wall-hdmi-a-1"), None, area)}
    renderer.set_player(PLAYER)
    renderer.set_player(PLAYER)
    assert renderer._player_id == PLAYER and queued == [1]
    renderer.set_player(None)
    assert renderer._player_id is None and queued == [1, 1]
