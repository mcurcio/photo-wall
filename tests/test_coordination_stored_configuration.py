"""One stale stored document never stops the wall (production outage, 2026-10-12): a Player's last
configuration that no longer reads as the current contract is treated as absent and regenerated
under the next revision, with a warning that names the table and the Player, never the content."""

import logging

from central.coordination import Coordinator
from contracts.models import FrameProfile, OutputBinding, PlayerConfiguration
from contracts.time import ManualClock

PLAYER = "p-" + "a" * 32
BINDING = OutputBinding(output_id="HDMI-A-1", frame_id="frame-0", generation=1,
                        profile=FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24))
SECRET = "private-content-marker"


class Installation:
    def configuration_in(self, _conn, _player_id, _epoch):
        return {"bindings": [BINDING], "execution_bindings": [BINDING]}


class Rows:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class Conn:
    """player_configurations as one stored row; records what is written back."""

    def __init__(self, stored):
        self.stored, self.written = stored, None

    def execute(self, sql, params=()):
        if sql.startswith("SELECT * FROM player_configurations"):
            return Rows(self.stored)
        self.written = params
        return Rows()


def current() -> dict:
    return PlayerConfiguration(player_id=PLAYER, authority_epoch=1, configuration_revision=4,
                               bindings=(BINDING,), enabled_outputs=("HDMI-A-1",)
                               ).model_dump(mode="json")


def configure(stored: dict):
    coordinator = Coordinator(object(), ManualClock(1000), installation=Installation(),
                              media=object(), shown=object(), fit=object())
    conn = Conn({"player_id": PLAYER, "authority_epoch": 1, "revision": 4, "configuration": stored})
    return coordinator._configuration(conn, PLAYER, 1), conn


def test_a_current_stored_configuration_keeps_its_revision():
    configuration, _ = configure(current())
    assert configuration.configuration_revision == 4


def test_a_stored_configuration_that_no_longer_validates_is_regenerated(caplog):
    """Mutation probe: read the stored row with a bare `PlayerConfiguration.model_validate` ->
    ValidationError escapes and the tick stops (the outage)."""
    stale = current()
    stale["bindings"][0] |= {"preview": {"revision": 1, "gain": 1.0, "note": SECRET},
                             "preview_expires": 1234.5}
    with caplog.at_level(logging.WARNING, logger="central.coordination"):
        configuration, conn = configure(stale)
    assert configuration.bindings == (BINDING,)
    assert configuration.configuration_revision == 5     # the Player fetches the regenerated one
    assert conn.written[2] == 5
    assert "player_configurations" in caplog.text and PLAYER in caplog.text
    assert "bindings.0.preview" in caplog.text and SECRET not in caplog.text
