"""The clock record's wire format: a round trip, and None for anything a reader cannot trust."""

import json
import os
import stat

import pytest

from contracts.clock_record import (
    MAX_RECORD_BYTES,
    ClockRecord,
    ClockState,
    encode_clock_record,
    parse_clock_record,
)
from uplink.clock import RunClockRecord

RECORD = ClockRecord(state=ClockState.SYNCED, floor=1790380800, raised_to_floor=True,
                     tier="dhcp", source="192.0.2.1", offset=3605.2, stepped=True,
                     tried=("dhcp:192.0.2.1:ok",), writer="netboot", written_at=1790384406.1)


def document(**changes) -> bytes:
    fields = json.loads(encode_clock_record(RECORD))
    fields.update(changes)
    return json.dumps(fields).encode()


def test_a_record_round_trips():
    assert parse_clock_record(encode_clock_record(RECORD)) == RECORD


def test_the_wire_form_is_the_documented_one():
    assert json.loads(encode_clock_record(RECORD)) == {
        "schema": 1, "state": "synced", "floor": 1790380800, "raised_to_floor": True,
        "tier": "dhcp", "source": "192.0.2.1", "offset": 3605.2, "stepped": True,
        "tried": ["dhcp:192.0.2.1:ok"], "writer": "netboot", "written_at": 1790384406.1}


def test_an_unsynced_record_round_trips():
    record = ClockRecord(state=ClockState.UNSYNCED, floor=1, raised_to_floor=False, tier=None,
                         source=None, offset=None, stepped=False, tried=(), writer="netboot",
                         written_at=2.0)
    assert parse_clock_record(encode_clock_record(record)) == record


def test_unknown_keys_are_ignored():
    assert parse_clock_record(document(added_later=[1, 2])) == RECORD


@pytest.mark.parametrize("data", [
    document(schema=2),
    document(schema=True),
    document(state="drifting"),
    document(floor=1.5),
    document(floor=True),
    document(tier="gps"),
    document(stepped="yes"),
    document(tried="dhcp:192.0.2.1:ok"),
    document(tried=["x"] * 9),
    document(tried=["has space"]),
    document(writer=""),
    b'{"schema":1,"schema":1}',
    encode_clock_record(RECORD).replace(b"3605.2", b"NaN"),
    encode_clock_record(RECORD)[:-1],
    b"[]",
    b'{"schema":1,"pad":"' + b"x" * MAX_RECORD_BYTES + b'"}',
])
def test_anything_untrustworthy_parses_to_none(data):
    assert parse_clock_record(data) is None


def test_a_record_cannot_be_built_invalid():
    with pytest.raises(ValueError):
        ClockRecord(state="synced", floor=1, raised_to_floor=False, tier=None, source=None,
                    offset=None, stepped=False, tried=(), writer="netboot", written_at=1.0)


def test_the_summary_names_state_floor_date_and_tries():
    assert RECORD.summary() == "clock=synced floor=2026-09-26 tried=dhcp:192.0.2.1:ok"


def test_the_run_file_is_world_readable_even_under_umask_077(tmp_path):
    store = RunClockRecord(tmp_path / "run" / "photo-wall-clock.json")
    previous = os.umask(0o077)
    try:
        store.write(RECORD)
    finally:
        os.umask(previous)
    path = tmp_path / "run" / "photo-wall-clock.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert store.read() == RECORD
    assert [entry.name for entry in path.parent.iterdir()] == [path.name]   # no temp left


def test_a_missing_or_broken_run_file_reads_as_none(tmp_path):
    store = RunClockRecord(tmp_path / "photo-wall-clock.json")
    assert store.read() is None
    (tmp_path / "photo-wall-clock.json").write_bytes(b"{")
    assert store.read() is None
