"""The Player's LUT leg (print look, 2026-10-12, design rule 3 "Nothing waits on a LUT" and R11):
PlayerLuts (asked, wanted, held), PlayerService.fetch_lut against Central's LUT route faked with
httpx.MockTransport (the Variant rule: the served Repr-Digest and Content-Length decide) through a
real media cache, and the renderer's shown key and tag waiting for the LUT. No GL: the texture is
the pixel harness's (tests/native_renderer_pixel_harness.py)."""
from __future__ import annotations

import asyncio
import functools
import hashlib
import threading
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest
from test_player_service import Server, close, repr_digest, rig

import player.native as native_module
from contracts.lut import LUT_BYTES, LUT_MEDIA_TYPE, LutRef
from contracts.models import Calibration
from contracts.node_frame import LutMix, PictureOperations
from contracts.time import ManualClock
from player.cache import Cache
from player.luts import PlayerLuts
from player.rendering import PrepareResult
from player.service import ServiceError

REF = LutRef("ab" * 32, "lut33-perceptual-relcol-bpc-1")
TABLE = bytes(index * 7 % 256 for index in range(LUT_BYTES))


class LutServer(Server):
    """Central with the LUT route (lane central's C2 shape): GET LutRef.path, 200 with
    LUT_MEDIA_TYPE, Content-Length and Repr-Digest; or what a test sets in `lut_response`."""

    def __init__(self, clock):
        super().__init__(clock)
        self.lut_response = None
        self.lut_requests = 0

    def __call__(self, request):
        if request.url.path == REF.path:
            self.requests.append(request)
            self.lut_requests += 1
            return self.lut_response or httpx.Response(200, content=TABLE, headers={
                "Content-Type": LUT_MEDIA_TYPE, "Content-Length": str(LUT_BYTES),
                "Repr-Digest": repr_digest(TABLE)})
        return super().__call__(request)


def lut_rig(tmp_path):
    return rig(tmp_path, server=LutServer(ManualClock(100)))


# -- PlayerLuts ------------------------------------------------------------------------------------

def test_the_first_ask_is_loading_and_wanted_until_held():
    luts = PlayerLuts()
    assert luts.lut(REF) is None and luts.wanted() == (REF,) and luts.generation() == 0
    assert luts.lut(REF) is None and luts.wanted() == (REF,)
    with pytest.raises(ValueError, match="^lut_body$"):
        luts.hold(REF, TABLE[:-1])
    assert luts.generation() == 0
    luts.hold(REF, TABLE)
    assert luts.lut(REF) == TABLE and luts.wanted() == () and luts.generation() == 1


# -- fetch_lut: from Central only, the Variant rule --------------------------------------------------

def test_the_served_lut_is_checked_kept_in_the_media_cache_and_held(tmp_path):
    async def check():
        service, server = await lut_rig(tmp_path)
        try:
            service.luts.lut(REF)
            assert await service.fetch_lut(REF) is True
            assert service.luts.lut(REF) == TABLE and service.luts.wanted() == ()
            assert server.lut_requests == 1
            assert server.requests[-1].headers["authorization"].startswith("Bearer ")
            path = service.cache.path_named(REF.key)
            assert path is not None and path.read_bytes() == TABLE
        finally:
            await close(service)
    asyncio.run(check())


@pytest.mark.parametrize("name, headers, content", [
    ("tampered", {"Repr-Digest": repr_digest(TABLE[::-1])}, TABLE),
    ("short", {"Content-Length": str(LUT_BYTES - 3)}, TABLE[:-3]),
    ("other_type", {"Content-Type": "image/png"}, TABLE),
    ("encoded", {"Content-Encoding": "x-packed"}, TABLE),
    ("no_digest", {"Repr-Digest": ""}, TABLE),
])
def test_a_lut_body_that_is_not_what_central_served_is_refused_and_retried(tmp_path, name, headers, content):
    """R11 and the Variant rule: a body whose SHA-256 differs from the served Repr-Digest, a
    length other than LUT_BYTES, another media type or an encoding is refused, never held or
    cached, and stays wanted; the media loop's attempt faults "lut_download" and retries after
    the backoff. Mutation probe: skip the digest comparison -> the tampered body is held."""
    async def check():
        service, server = await lut_rig(tmp_path)
        try:
            base = {"Content-Type": LUT_MEDIA_TYPE, "Content-Length": str(len(content)),
                    "Repr-Digest": repr_digest(TABLE)}
            server.lut_response = httpx.Response(200, content=content, headers={**base, **headers})
            service.luts.lut(REF)
            with pytest.raises(ServiceError):
                await service.fetch_lut(REF)
            assert service.luts.lut(REF) is None and service.luts.wanted() == (REF,)
            assert service.cache.path_named(REF.key) is None
            attempts = {}
            await service._attempt(attempts, REF, functools.partial(service.fetch_lut, REF), "lut_download")
            assert service.last_fault == "lut_download"
            assert attempts[REF][1] > service.clock.monotonic()          # retried later, not at once
            server.lut_response = None
            await service._attempt(attempts, REF, functools.partial(service.fetch_lut, REF), "lut_download")
            assert service.luts.lut(REF) == TABLE
        finally:
            await close(service)
    asyncio.run(check())


def test_an_unknown_lut_is_a_refusal_and_stays_wanted(tmp_path):
    async def check():
        service, server = await lut_rig(tmp_path)
        try:
            server.lut_response = httpx.Response(404, json={"error": "lut_unknown"})
            service.luts.lut(REF)
            attempts = {}
            await service._attempt(attempts, REF, functools.partial(service.fetch_lut, REF), "lut_download")
            assert service.last_fault.startswith("lut_download") and service.luts.wanted() == (REF,)
        finally:
            await close(service)
    asyncio.run(check())


def test_a_whole_cached_lut_is_used_with_no_request_and_an_empty_cache_fetches_again(tmp_path):
    """The cache is ephemeral: a verified file under the LUT's name is used without asking
    Central; a damaged one is a miss; an empty cache directory means a fetch. Mutation probe:
    trust any file under the key without the digest in its name -> the damaged file is held."""
    async def check():
        service, server = await lut_rig(tmp_path)
        try:
            assert await service.fetch_lut(REF) is True
            assert server.lut_requests == 1
            service.luts = PlayerLuts()                     # a restarted Player: nothing held
            assert await service.fetch_lut(REF) is True
            assert server.lut_requests == 1 and service.luts.lut(REF) == TABLE
            service.luts = PlayerLuts()
            path = service.cache.path_named(REF.key)
            path.write_bytes(b"\0" * LUT_BYTES)              # damaged in place: same name, other bytes
            assert await service.fetch_lut(REF) is True
            assert server.lut_requests == 2 and service.luts.lut(REF) == TABLE
        finally:
            await close(service)
    asyncio.run(check())
    fresh = tmp_path / "fresh"
    fresh.mkdir(mode=0o700)

    async def empty():
        service, server = await lut_rig(fresh)
        try:
            assert await service.fetch_lut(REF) is True and server.lut_requests == 1
        finally:
            await close(service)
    asyncio.run(empty())


def test_the_cache_keeps_any_named_blob_by_its_served_digest(tmp_path):
    """One verified-write path for every kind: secure_named refuses bytes that are not the served
    digest or size, and path_named finds only what was secured."""
    cache = Cache(tmp_path / "blobs", 1024**2)
    try:
        assert cache.path_named(REF.key) is None
        with pytest.raises(ValueError):
            cache.secure_named(REF.key, (TABLE,), "luts", sha256=hashlib.sha256(b"other").hexdigest(),
                               size=LUT_BYTES)
        with pytest.raises(ValueError):
            cache.secure_named(REF.key, (TABLE,), "luts", sha256=hashlib.sha256(TABLE).hexdigest(),
                               size=LUT_BYTES + 1)
        assert cache.path_named(REF.key) is None
        path = cache.secure_named(REF.key, (TABLE,), "luts", sha256=hashlib.sha256(TABLE).hexdigest(),
                                  size=LUT_BYTES)
        assert cache.path_named(REF.key) == path and path.read_bytes() == TABLE
        assert cache.path_named(REF.key + "x") is None
    finally:
        cache.close()


# -- The renderer: the sliders draw at once, the LUT joins, only then the tag names the change -------

def test_the_luts_arrival_redraws_and_only_then_is_the_change_claimed():
    """Rule 3: a picture naming a LUT not yet held is drawn with the sliders (no LUT bound) and
    its tag names the previous complete change; hold() raises the generation, the surface draws
    again, the LUT is bound and the tag names the change. Mutation probe: leave the generation out
    of the shown key -> no redraw after hold."""
    luts = PlayerLuts()
    operations = PictureOperations("hdmi1", 9, (1.0, 1.0, 1.0), 1.0, 1.0, 1.0, lut=LutMix(REF, .8))

    class Area:
        renders = 0

        def queue_render(self):
            self.renders += 1

    class Frames:
        def grant(self, _output_id, _composition=None):
            return SimpleNamespace(grant_id=uuid.UUID(int=1), binding_generation=1, config_revision=1,
                                   admitted=True, matches=lambda _c: True)

        def trial(self, _composition, _grant):
            return None

        def picture(self, _output_id):
            return operations

    class GL:
        GL_TEXTURE0, GL_TEXTURE_3D = 0x84C0, 0x806F

        def __init__(self):
            self.uploads = 0

        def __getattr__(self, name):
            if name.startswith("GL_"):
                return 0
            if name == "glTexImage3D":
                return lambda *_: setattr(self, "uploads", self.uploads + 1)
            if name == "glGenTextures":
                return lambda _n: 5
            return lambda *_: None

    renderer = native_module.NativeRenderer.__new__(native_module.NativeRenderer)
    renderer._owner, renderer._closed, renderer._serial = threading.get_ident(), False, 0
    renderer._decoders, renderer._frames, renderer._luts, renderer.GL = {}, Frames(), luts, GL()
    surface = native_module._Surface(output=None, window=None, area=Area())
    renderer._surfaces = {"hdmi1": surface}
    renderer.capacity = lambda compositions: SimpleNamespace(available=True)
    renderer._prepare = lambda local, composition, seen: PrepareResult("prepared")
    photo = SimpleNamespace(binding=SimpleNamespace(output_id="hdmi1"), calibration=Calibration(),
                            fallback=False, timebase=None, layers=(SimpleNamespace(
                                layer=SimpleNamespace(assignment_id="a", variant=None), path=None, alpha=1.0),))

    def draw():
        """What _render does with the pending draw: bind the LUT if held, tag the change."""
        lut_drawn = renderer._bind_lut(surface, operations)
        surface.acknowledged, surface.pending = surface.pending, None
        surface.acknowledged.completed_at = time.monotonic()
        return lut_drawn, surface._tagged_change(operations, lut_drawn)

    assert renderer.present(photo).status == "pending" and surface.area.renders == 1
    assert draw() == (False, None)                         # the sliders alone; nothing claimed
    assert luts.wanted() == (REF,)
    assert renderer.present(photo).status == "presented" and surface.area.renders == 1
    luts.hold(REF, TABLE)
    renderer.present(photo)
    assert surface.area.renders == 2, "the LUT's arrival must redraw"
    assert draw() == (True, 9) and renderer.GL.uploads == 1
    assert renderer.present(photo).status == "presented" and surface.area.renders == 2
    assert renderer._bind_lut(surface, operations) and renderer.GL.uploads == 1    # bound, not re-uploaded
