"""What a display offers: list settings from its capabilities string, and the list kept across
controller restarts (decision 0022; the picture split, slice S3; appliance/display_host/picture_list.py).

The capabilities string is a recorded one (node.display.fake_ddcutil.RECORDED_CAPABILITIES); the
asker runs over the fake `ddcutil` through the real `DdcChannel`.
"""
from __future__ import annotations

import hashlib

import pytest
from node.display.fake_ddcutil import OUTPUT, RECORDED_CAPABILITIES, Display

from appliance.display_host.ddc import DdcChannel, Urgency, VcpByte, VcpReading
from appliance.display_host.picture_list import (
    FileOfferedCache,
    PictureListAsker,
    offered_setting,
    parse_capability_choices,
    with_current,
)
from contracts.node_output import DisplayIdentity
from contracts.node_picture import (
    CHOICE_NAME_MAX,
    MAX_CHOICES,
    Choice,
    HardwareRange,
    HardwareSetting,
    RangeKind,
)

IDENTITY = DisplayIdentity("XYM", 5475, "MNN", None)
PRESETS = (Choice(4, "5000 K"), Choice(5, "6500 K"), Choice(8, "9300 K"), Choice(9, "10000 K"),
           Choice(11, "User 1"), Choice(12, "User 2"))


def capabilities(code: str, values: list[tuple[int, str]]) -> str:
    """A capabilities string naming `values` for `code`, ddcutil's layout."""
    lines = ["VCP Features:", "   Feature: 10 (Brightness)", f"   Feature: {code} (Some setting)", "      Values:"]
    lines += [f"         {value:02x}: {name}" for value, name in values]
    return "\n".join([*lines, "   Feature: 12 (Contrast)", ""])


def test_the_recorded_monitor_names_its_colour_presets():
    """Acceptance: the recorded string gives 14 its six named values, in value order; codes off the
    picture list (CC, D6) and values listed without names (the maker codes) give nothing."""
    assert parse_capability_choices(RECORDED_CAPABILITIES) == {"14": PRESETS}


def test_a_named_list_setting_is_a_choice_and_an_unnamed_one_read_only():
    named = offered_setting("14", VcpByte(5), PRESETS)
    assert named == HardwareSetting("14", HardwareRange(RangeKind.CHOICE, None, PRESETS), 5)
    assert offered_setting("DC", VcpByte(0), None).range.kind is RangeKind.READ_ONLY_CHOICE


def test_more_than_max_choices_leaves_the_setting_read_only():
    """Acceptance: 17 named values -> no choices, so READ_ONLY_CHOICE. Mutation probe: keep more
    than MAX_CHOICES -> 14 is offered as a CHOICE of 17 (and the range refuses it)."""
    sixteen = [(value, f"Mode {value}") for value in range(MAX_CHOICES)]
    assert len(parse_capability_choices(capabilities("14", sixteen))["14"]) == MAX_CHOICES
    seventeen = [(value, f"Mode {value}") for value in range(MAX_CHOICES + 1)]
    choices = parse_capability_choices(capabilities("14", seventeen))
    assert choices == {}
    assert offered_setting("14", VcpByte(1), choices.get("14")).range.kind is RangeKind.READ_ONLY_CHOICE


def test_a_long_or_non_ascii_name_is_cut_or_dropped():
    """Acceptance: a 30-character name is cut to CHOICE_NAME_MAX; non-ASCII characters and the two
    JSON escapes go; a name with nothing left is dropped, its value with it."""
    long = "A very long preset name, 30 c"
    named = parse_capability_choices(capabilities("14", [(1, long + "h"), (2, 'Cinéma "warm"'), (3, "暖色"),
                                                          (4, "  sRGB  ")]))
    assert named == {"14": (Choice(1, long[:CHOICE_NAME_MAX]), Choice(2, "Cinma warm"), Choice(4, "sRGB"))}


def test_odd_text_names_nothing_and_never_raises():
    assert parse_capability_choices("") == {}
    assert parse_capability_choices("Values:\n   05: orphan\nFeature: zz\n\x00\x01") == {}
    assert parse_capability_choices(capabilities("14", [(5, "6500 K"), (5, "again"), (2, "sRGB")])) == {
        "14": (Choice(2, "sRGB"), Choice(5, "6500 K"))}


def test_the_ask_reads_the_capabilities_string_only_when_a_list_setting_answered(tmp_path):
    display = Display(tmp_path)
    display.script(capabilities=RECORDED_CAPABILITIES)
    asker = PictureListAsker(display.channel)
    assert [setting.code for setting in asker.ask(OUTPUT)] == ["10", "12"]
    assert not any(args[0] == "capabilities" for args in display.started())      # levels only: no string
    display.script(snc={"14": 5, "DC": 0})
    offered = {setting.code: setting for setting in asker.ask(OUTPUT)}
    assert offered["14"] == HardwareSetting("14", HardwareRange(RangeKind.CHOICE, None, PRESETS), 5)
    assert offered["DC"].range.kind is RangeKind.READ_ONLY_CHOICE                # not named in the string
    assert sum(args[0] == "capabilities" for args in display.started()) == 1


def test_a_capabilities_string_that_does_not_come_leaves_list_settings_read_only_but_known(tmp_path):
    display = Display(tmp_path)
    display.script(snc={"14": 5}, capabilities=RECORDED_CAPABILITIES)
    asker = PictureListAsker(display.channel)
    display.channel.capabilities = lambda output_id, *, urgency, timeout: None   # did not answer in its hold
    offered = asker.ask(OUTPUT)
    assert offered is not None
    assert {setting.code: setting.range.kind for setting in offered}["14"] is RangeKind.READ_ONLY_CHOICE


def test_the_capabilities_string_is_one_picture_hold(tmp_path):
    """`ddcutil capabilities` runs as one command on the port, cut at the picture hold."""
    display = Display(tmp_path)
    display.script(capabilities=RECORDED_CAPABILITIES)
    assert display.channel.capabilities(OUTPUT, urgency=Urgency.PICTURE, timeout=5) == RECORDED_CAPABILITIES
    display.script(answer=False)
    assert display.channel.capabilities(OUTPUT, urgency=Urgency.PICTURE, timeout=5) is None


def test_reading_again_replaces_each_current_and_keeps_each_range(tmp_path):
    display = Display(tmp_path)
    asker = PictureListAsker(display.channel)
    offered = asker.ask(OUTPUT)
    display.script(vcp={"10": [35, 100]})                                       # 10 moved; 12 no longer answers
    again = asker.read_again(OUTPUT, offered)
    assert again == (HardwareSetting("10", HardwareRange(RangeKind.LEVEL, 100, ()), 35),
                     HardwareSetting("12", HardwareRange(RangeKind.LEVEL, 100, ()), None))
    display.script(hang=["10"], hang_seconds=4)
    assert asker.read_again(OUTPUT, offered) is None                           # never a partial read
    level = HardwareSetting("10", HardwareRange(RangeKind.LEVEL, 100, ()), 60)
    assert with_current(level, VcpReading(120, 200)).current is None             # past the range it was offered with


# The list kept across restarts --------------------------------------------------------------------

def test_the_cache_keeps_a_list_per_identity_and_is_correct_when_empty(tmp_path):
    """Mutation probe: `FileOfferedCache.get` ignores the serial -> the other display's get answers
    the first one's list."""
    cache = FileOfferedCache(tmp_path)
    offered = (HardwareSetting("10", HardwareRange(RangeKind.LEVEL, 100, ()), 60),
               HardwareSetting("14", HardwareRange(RangeKind.CHOICE, None, PRESETS), 5))
    assert cache.get(IDENTITY) is None
    cache.put(IDENTITY, offered)
    assert FileOfferedCache(tmp_path).get(IDENTITY) == offered
    assert cache.get(DisplayIdentity("XYM", 5475, "MNN", "123")) is None        # the same model, another unit
    assert cache.get(DisplayIdentity("XYM", 5475, "Other", None)) is None
    cache.put(IDENTITY, ())
    assert cache.get(IDENTITY) == ()                                           # asked: offers none
    assert [path.name for path in tmp_path.iterdir()] == [
        f"offered-XYM-5475-none-{hashlib.sha256(b'MNN').hexdigest()[:8]}.json"]


def test_a_serial_with_odd_characters_and_a_broken_file_are_none_kept(tmp_path):
    cache = FileOfferedCache(tmp_path)
    odd = DisplayIdentity("XYM", 1, "MNN", "../a b/c")
    cache.put(odd, ())
    assert cache.get(odd) == () and all(path.parent == tmp_path for path in tmp_path.iterdir())
    for path in tmp_path.iterdir():
        path.write_text("{not json")
    assert cache.get(odd) is None
    FileOfferedCache(tmp_path / "missing").put(IDENTITY, ())                    # a failed write is logged, never raised


@pytest.mark.parametrize("serial", [None, "7KQS0H1000016"])
def test_a_file_naming_another_identity_is_none_kept(tmp_path, serial):
    """Two displays whose file names meet never share a list: the file's own identity is checked."""
    one, two = DisplayIdentity("XYM", 1, "MNN", serial), DisplayIdentity("XYM", 1, "MNN", None if serial else "x")
    cache = FileOfferedCache(tmp_path)
    cache.put(one, ())
    path = next(tmp_path.iterdir())
    target = cache._path(two)
    path.rename(target)
    assert cache.get(two) is None


def test_an_output_with_no_ddc_bus_offers_nothing_known_and_is_never_unknown(tmp_path):
    """An Output with no DDC/CI bus is known to offer nothing ((), never None), so the console
    never says "Checking" for ever. Mutation probe: return None for no bus -> the ask is unknown."""
    ran: list[object] = []
    asker = PictureListAsker(DdcChannel(lambda argv, timeout: ran.append(argv), drm_root=tmp_path,
                                        cache_dir=tmp_path / "cache"))
    assert asker.ask("HDMI-A-1") == ()
    assert ran == []
