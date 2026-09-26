"""R1: which Central the kernel command line names. Present is Configured or an error, never a
fallback; only absent is Unconfigured; an unreadable cmdline is never absent."""

import os

import pytest

from uplink.causes import Cause, UplinkError
from uplink.origin import Origin
from uplink.resolver import (
    CENTRAL_KEY,
    MAX_COMMAND_LINE_BYTES,
    Configured,
    Unconfigured,
    parse_kernel_command_line,
    read_kernel_command_line,
    resolve_central,
)


def test_parse_keeps_key_value_tokens_first_wins_and_ignores_bare_flags():
    parsed = parse_kernel_command_line(
        "console=tty1 quiet photowall.central=http://a/ photowall.central=http://b/ "
        "photowall.debug=1 ip=dhcp\n")
    assert parsed == {"console": "tty1", "photowall.central": "http://a/",
                      "photowall.debug": "1", "ip": "dhcp"}


def test_a_bare_key_without_equals_counts_as_absent():
    assert resolve_central(parse_kernel_command_line("quiet photowall.central")) == \
        Unconfigured("absent")


@pytest.mark.parametrize(("value", "root"), [
    ("http://photo-wall.localdomain/", Origin("http", "photo-wall.localdomain", 80)),
    ("https://photo-wall.example", Origin("https", "photo-wall.example", 443)),
    ("http://192.0.2.10:8080/", Origin("http", "192.0.2.10", 8080)),
])
def test_a_valid_root_is_configured(value, root):
    assert resolve_central({CENTRAL_KEY: value}) == Configured(root)


@pytest.mark.parametrize("value", [
    "", "@@PHOTOWALL_CENTRAL@@", "photo-wall.localdomain", "http://photo-wall/app/",
    "http://user@photo-wall/", "ftp://photo-wall/", "http://photo-wall/?x=1",
])
def test_a_present_but_invalid_value_is_an_error_never_a_fallback(value):
    with pytest.raises(UplinkError) as caught:
        resolve_central({CENTRAL_KEY: value})
    assert (caught.value.cause, caught.value.reason) == (Cause.CONFIGURATION, "invalid")


def test_absent_and_no_cmdline_are_distinct_proofs():
    assert resolve_central({"console": "tty1"}) == Unconfigured("absent")
    assert resolve_central(None) == Unconfigured("no_cmdline")


def test_a_missing_cmdline_file_means_no_cmdline(tmp_path):
    assert read_kernel_command_line(tmp_path / "cmdline") is None


def test_the_cmdline_file_is_read_and_parsed(tmp_path):
    path = tmp_path / "cmdline"
    path.write_bytes(b"ip=dhcp photowall.central=http://a/\n")
    assert read_kernel_command_line(path) == {"ip": "dhcp", "photowall.central": "http://a/"}


def test_the_read_is_bounded(tmp_path):
    path = tmp_path / "cmdline"
    path.write_bytes(b"a=" + b"x" * MAX_COMMAND_LINE_BYTES + b" photowall.central=http://a/")
    assert CENTRAL_KEY not in read_kernel_command_line(path)


def test_undecodable_bytes_only_make_a_value_invalid(tmp_path):
    path = tmp_path / "cmdline"
    path.write_bytes(b"x=\xff photowall.central=http://a\xff/")
    with pytest.raises(UplinkError) as caught:
        resolve_central(read_kernel_command_line(path))
    assert caught.value.reason == "invalid"


def test_a_directory_is_unreadable_not_absent(tmp_path):
    with pytest.raises(UplinkError) as caught:
        read_kernel_command_line(tmp_path)
    assert (caught.value.cause, caught.value.reason, caught.value.detail) == (
        Cause.CONFIGURATION, "unreadable", "EISDIR")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_a_file_that_cannot_be_opened_is_unreadable_not_absent(tmp_path):
    path = tmp_path / "cmdline"
    path.write_bytes(b"photowall.central=http://a/")
    path.chmod(0)
    with pytest.raises(UplinkError) as caught:
        read_kernel_command_line(path)
    assert (caught.value.reason, caught.value.detail) == ("unreadable", "EACCES")
