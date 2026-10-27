"""The wire shapes of the Frame showing Central's error when it blacks the Frame: the Player's
`impeded` report, Central's card on the shell, and the connection Galleria's birth names. Each strict
and round-tripping; the caps hold their largest values."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from contracts.frame_lighting import FRAME_LIGHTS_MAX
from contracts.home_assistant import (
    GalleriaBirth,
    GalleriaConnection,
    decode_galleria_birth,
    encode_galleria_birth,
)
from contracts.node_impeded import (
    IMPEDED_BYTES,
    Impeded,
    ImpededFrames,
    decode_impeded,
    encode_impeded,
)
from contracts.node_output import (
    CENTRAL_CARD_LINE_MAX,
    CentralCard,
    decode_central_card,
    encode_central_card,
)
from contracts.node_state import FRAMES_PER_PLAYER_MAX
from contracts.room_light import CENTRAL_LINE_MAX, CentralWords

WIDE = "\U0001F600"   # a four-byte character
WORDS = CentralWords(line1="Photo Wall can't read Family Room Lights · since 29 Oct 18:05",
                     line2="Its last word: off (26 Oct 15:35) · see Needs attention in Photo Wall")
GRANT = "0b8f7c4e-3a1d-4c2b-9e6f-5d4c3b2a1f0e"
IMPEDED = ImpededFrames(frames=(Impeded(frame_id="my-frame", output_id="HDMI-A-1", grant_id=GRANT,
                                        lights=("family-room",), words=WORDS),))


def test_the_impeded_report_round_trips():
    assert decode_impeded(encode_impeded(IMPEDED)) == IMPEDED
    assert decode_impeded(encode_impeded(ImpededFrames(frames=()))) == ImpededFrames(frames=())


def test_the_largest_impeded_report_fits_its_cap():
    """Mutation probe: IMPEDED_BYTES 16384 -> the largest valid report is refused."""
    words = CentralWords(line1=WIDE * CENTRAL_LINE_MAX, line2=WIDE * CENTRAL_LINE_MAX)
    frame = Impeded(frame_id="f" * 96, output_id="o" * 128, grant_id=GRANT,
                    lights=tuple(letter * 32 for letter in "abcd"[:FRAME_LIGHTS_MAX]), words=words)
    assert len(encode_impeded(ImpededFrames(frames=(frame,) * FRAMES_PER_PLAYER_MAX))) <= IMPEDED_BYTES


@pytest.mark.parametrize("change", [
    lambda frame: frame.update(lights=[]),
    lambda frame: frame.update(lights=["a", "b", "c", "d", "e"]),
    lambda frame: frame.update(grant_id="not-a-grant"),
    lambda frame: frame.update(grant_id=GRANT.upper()),
    lambda frame: frame.pop("grant_id"),
    lambda frame: frame.update(words={"line1": "x"}),
    lambda frame: frame.update(extra=1),
], ids=["no-light", "too-many-lights", "bad-grant", "upper-case-grant", "no-grant", "one-line", "extra-field"])
def test_the_impeded_report_is_strict(change):
    value = json.loads(encode_impeded(IMPEDED))
    change(value["frames"][0])
    with pytest.raises(ValueError, match="^impeded$"):
        decode_impeded(json.dumps(value).encode())


def test_centrals_card_carries_centrals_words_verbatim():
    card = CentralCard(output_id="HDMI-A-1", line1=WORDS.line1, line2=WORDS.line2)
    assert decode_central_card(encode_central_card(card)) == card
    widest = CentralCard(output_id="HDMI-A-2", line1=WIDE * CENTRAL_CARD_LINE_MAX, line2='"' * CENTRAL_CARD_LINE_MAX)
    assert decode_central_card(encode_central_card(widest)) == widest
    assert CENTRAL_CARD_LINE_MAX == CENTRAL_LINE_MAX   # the card holds every line Central words


@pytest.mark.parametrize("raw", [
    b'{"output_id":"HDMI-A-9","line1":"a","line2":"b"}',
    b'{"output_id":"HDMI-A-1","line1":"","line2":"b"}',
    b'{"output_id":"HDMI-A-1","line1":"a\\nb","line2":"b"}',
    json.dumps({"output_id": "HDMI-A-1", "line1": "a" * (CENTRAL_CARD_LINE_MAX + 1), "line2": "b"}).encode(),
    b'{"output_id":"HDMI-A-1","line1":"a"}',
], ids=["unknown-output", "empty-line", "newline", "too-long", "one-line"])
def test_centrals_card_is_strict(raw):
    with pytest.raises(ValueError, match="^central_card$"):
        decode_central_card(raw)


def test_galleria_names_its_hub_connection_in_its_birth():
    birth = GalleriaBirth(version="0.45.0", kinds=("light", "illuminance", "colour-temperature"),
                          connection=GalleriaConnection(server="N" + "A" * 55, client=2**53))
    assert decode_galleria_birth(encode_galleria_birth(birth)) == birth
    for raw in (b'{"component":"galleria","version":"0.40.1","kinds":["light"]}',   # v0.40.1's birth
                b'{"component":"galleria","version":"1","kinds":[],"connection":{"server":"N","client":0}}',
                b'{"component":"galleria","version":"1","kinds":[],"connection":{"server":"","client":1}}'):
        with pytest.raises(ValueError, match="^galleria_birth$"):
            decode_galleria_birth(raw)


def test_the_shell_protocol_carries_centrals_card_at_manager_version_6():
    xml = (Path(__file__).resolve().parents[1] / "appliance" / "display_host" / "native"
           / "photo-wall-frame-v1.xml").read_text()
    assert '<event name="central_card" since="6">' in xml
