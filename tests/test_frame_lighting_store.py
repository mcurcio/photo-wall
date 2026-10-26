"""The house's one lock (central/infra/frame_lighting_store.py): a Frame-lighting check holds it
shared until its write commits, so a removal of what that lighting names waits, then sees the write
and is refused. Two connections on the test database; no sleeps (the test watches the remover's
backend wait on the lock)."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest
from psycopg.types.json import Jsonb
from test_registry import frame

from central.frame_lighting.paper_profiles import LUT_RECIPE_ID, PaperProfileRefused
from central.infra.frame_lighting_store import PgFrameLighting, PgHouseLock
from central.infra.paper_profile_store import PgPaperProfiles
from central.infra.room_light_store import PgRoomLights
from central.infra.transactions import PgTransaction
from central.room_light.model import FixedLight, RoomLightRefused, TypedColour
from contracts.frame_lighting import AT_FULL, FrameLighting, Paper, WashLight
from contracts.lut import LutRef

PROFILE = "e" * 64
WAIT_SECONDS = 10.0


def _waiting_on(registry, pid: int) -> bool:
    with registry.db.transaction() as conn:
        return conn.execute("SELECT count(*) AS n FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))",
                            (pid,)).fetchone()["n"] > 0


def _removal_waits_then_sees(registry, lighting: FrameLighting, remove: Callable[[], None]) -> BaseException | None:
    """Hold a Frame-lighting check and its write open, start `remove` on another connection, and
    commit once it waits on the lock (or WAIT_SECONDS pass). What `remove` raised."""
    outcome: list[BaseException | None] = []

    def removing() -> None:
        try:
            remove()
            outcome.append(None)
        except BaseException as error:   # noqa: BLE001 - the test reads what it raised
            outcome.append(error)

    with registry.db.transaction() as conn:
        fitted = PgFrameLighting(LUT_RECIPE_ID).fitted_within(PgTransaction(conn), lighting)
        conn.execute("UPDATE frames SET lighting=%s WHERE id='hall'", (Jsonb(fitted.model_dump(mode="json")),))
        pid = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        remover = threading.Thread(target=removing)
        remover.start()
        deadline = time.monotonic() + WAIT_SECONDS
        while not outcome and not _waiting_on(registry, pid) and time.monotonic() < deadline:
            pass
        assert not outcome, "the removal did not wait for the check's lock"
    remover.join(WAIT_SECONDS)
    return outcome[0]


def _lit(light: str = "room", profile: str | None = None) -> FrameLighting:
    return FrameLighting(mat_depth_mm=0, lights=(WashLight(light=light, lux=100.0, reference=AT_FULL),),
                         paper=Paper(whiteness=0.9, ink_density=2.3,
                                     profile=None if profile is None else LutRef(profile, LUT_RECIPE_ID)))


def test_a_room_light_removal_waits_for_the_check_then_is_refused(registry):
    """A4. Mutation probe M2: fitted_within without its shared lock -> the removal does not wait;
    remove_room_light without `changing_within` -> it does not wait either."""
    frame(registry, "hall")
    lights = PgRoomLights(registry.db, PgHouseLock())
    lights.save_room_light("lamp", FixedLight(name="Lamp", colour=TypedColour(kelvin=3000)))
    refused = _removal_waits_then_sees(registry, _lit("lamp"), lambda: lights.remove_room_light("lamp"))
    assert isinstance(refused, RoomLightRefused) and refused.code.value == "named_by_a_frame"
    assert "lamp" in lights.current().lights


def test_a_paper_profile_removal_waits_for_the_check_then_is_refused(registry):
    """A4 for a paper profile."""
    frame(registry, "hall")
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO paper_profiles(profile_sha256, name, profile, added_at) "
                     "VALUES (%s, 'Paper', %s, 0)", (PROFILE, bytes(128)))
    profiles = PgPaperProfiles(registry.db, registry.clock, PgHouseLock())
    refused = _removal_waits_then_sees(registry, _lit(profile=PROFILE), lambda: profiles.remove_paper_profile(PROFILE))
    assert isinstance(refused, PaperProfileRefused) and refused.code.value == "named_by_a_frame"
    assert refused.frames == ("Frame 1",)               # by the Frame's name


def test_the_check_refuses_what_the_house_does_not_have(registry):
    from central.frame_lighting.model import FrameLightingRefused

    with registry.db.transaction() as conn:
        check = PgFrameLighting(LUT_RECIPE_ID)
        with pytest.raises(FrameLightingRefused) as unknown:
            check.fitted_within(PgTransaction(conn), _lit("cellar"))
        assert (unknown.value.code.value, unknown.value.status) == ("unknown_room_light", 422)
        with pytest.raises(FrameLightingRefused) as unlisted:
            check.fitted_within(PgTransaction(conn), _lit(profile=PROFILE))
        assert unlisted.value.code.value == "unknown_paper_profile"
