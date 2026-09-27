"""Named causes: UplinkError's closed vocabulary and classify's rule table (design §2.1)."""

import errno
import http.client
import importlib
import socket
import ssl
import sys

import pytest

from uplink.causes import CONSOLE_LIMIT, REASONS, Cause, UplinkError, classify
from uplink.lookup import LookupTimeout


def verify_error(code: int | None) -> ssl.SSLCertVerificationError:
    error = ssl.SSLCertVerificationError(1, "certificate verify failed")
    if code is not None:
        error.verify_code = code
    return error


def ssl_error(reason: str | None) -> ssl.SSLError:
    error = ssl.SSLError(1, "handshake failed")
    error.reason = reason
    return error


def named(error, phase="connect"):
    classified = classify(error, phase=phase, host="central.example")
    return classified and (classified.cause, classified.reason, classified.detail)


def test_every_reason_belongs_to_exactly_one_cause_and_every_cause_has_reasons():
    assert set(REASONS) == set(Cause)
    assert all(REASONS[cause] for cause in Cause)


def test_a_reason_of_another_cause_is_a_type_error():
    with pytest.raises(TypeError):
        UplinkError(Cause.DNS, "downgrade")
    with pytest.raises(TypeError):
        UplinkError("dns", "failed")          # a bare string is not a Cause


@pytest.mark.parametrize("field", [{"host": "a b"}, {"detail": "status=404\nforged"},
                                   {"detail": "café"}])
def test_host_and_detail_are_single_printable_tokens(field):
    with pytest.raises(TypeError):
        UplinkError(Cause.HTTP, "status", **field)


def test_console_names_cause_reason_host_and_detail_within_its_bound():
    error = UplinkError(Cause.REDIRECT, "loop", host="a", detail="hops=a,b")
    assert error.console() == "cause=redirect reason=loop host=a detail=hops=a,b"
    assert str(error) == error.console()
    assert UplinkError(Cause.CONFIGURATION, "absent").console() == (
        "cause=configuration reason=absent")
    assert len(UplinkError(Cause.HTTP, "status", host="h" * 2048).console()) == CONSOLE_LIMIT


@pytest.mark.parametrize(("value", "kept"), [("base_artifact_pending", "base_artifact_pending"),
                                             ("Not_Allowed", None), ("x" * 65, None),
                                             ("", None), (7, None)])
def test_central_error_is_kept_only_in_its_grammar(value, kept):
    assert UplinkError(Cause.CENTRAL, "error", central_error=value).central_error == kept


@pytest.mark.parametrize(("error", "expected"), [
    # rules 1-4: OpenSSL verify codes
    (verify_error(9), (Cause.TIME, "not_yet_valid", "verify_code=9")),
    (verify_error(10), (Cause.TIME, "expired", "verify_code=10")),
    (verify_error(62), (Cause.TLS, "hostname", "verify_code=62")),
    (verify_error(64), (Cause.TLS, "hostname", "verify_code=64")),
    (verify_error(18), (Cause.TLS, "untrusted", "verify_code=18")),
    (verify_error(19), (Cause.TLS, "untrusted", "verify_code=19")),
    (verify_error(20), (Cause.TLS, "untrusted", "verify_code=20")),
    (verify_error(None), (Cause.TLS, "untrusted", "SSLCertVerificationError")),
    # rule 5
    (ssl_error("TLSV1_ALERT_PROTOCOL_VERSION"),
     (Cause.TLS, "handshake", "TLSV1_ALERT_PROTOCOL_VERSION")),
    (ssl_error("not a token"), (Cause.TLS, "handshake", "SSLError")),
    (ssl.SSLEOFError(8, "EOF"), (Cause.TLS, "handshake", "SSLEOFError")),
    # rule 6
    (LookupTimeout(errno.ETIMEDOUT, "slow"), (Cause.DNS, "timeout", "ETIMEDOUT")),
    (socket.gaierror(socket.EAI_NONAME, "unknown"), (Cause.DNS, "failed", "EAI_NONAME")),
    # rules 7-13
    (http.client.RemoteDisconnected("gone"), (Cause.CONNECT, "closed", "RemoteDisconnected")),
    (TimeoutError("timed out"), (Cause.CONNECT, "timeout", "TimeoutError")),
    (ConnectionRefusedError(errno.ECONNREFUSED, "refused"),
     (Cause.CONNECT, "refused", "ECONNREFUSED")),
    (ConnectionResetError(errno.ECONNRESET, "reset"), (Cause.CONNECT, "reset", "ECONNRESET")),
    (ConnectionAbortedError(errno.ECONNABORTED, "aborted"),
     (Cause.CONNECT, "reset", "ECONNABORTED")),
    (BrokenPipeError(errno.EPIPE, "writing the request"), (Cause.CONNECT, "reset", "EPIPE")),
    (OSError(errno.ENETUNREACH, "x"), (Cause.CONNECT, "unreachable", "ENETUNREACH")),
    (OSError(errno.EHOSTUNREACH, "x"), (Cause.CONNECT, "unreachable", "EHOSTUNREACH")),
    (OSError(errno.ENETDOWN, "x"), (Cause.CONNECT, "unreachable", "ENETDOWN")),
    (OSError(errno.EHOSTDOWN, "x"), (Cause.CONNECT, "unreachable", "EHOSTDOWN")),
    (OSError(errno.EADDRNOTAVAIL, "x"), (Cause.CONNECT, "unreachable", "EADDRNOTAVAIL")),
    (http.client.BadStatusLine("SSH-2.0-OpenSSH_9.6"),
     (Cause.CONNECT, "protocol", "BadStatusLine")),
    (http.client.LineTooLong("status line"), (Cause.CONNECT, "protocol", "LineTooLong")),
    (http.client.HTTPException("got more than 100 headers"),
     (Cause.CONNECT, "protocol", "HTTPException")),
    (OSError(errno.EACCES, "unlisted"), (Cause.CONNECT, "other", "EACCES")),
    (OSError("no errno"), (Cause.CONNECT, "other", "OSError")),
])
def test_connect_phase_rules(error, expected):
    assert named(error) == expected


@pytest.mark.parametrize(("error", "expected"), [
    (TimeoutError("read timed out"), (Cause.TRANSFER, "deadline", "TimeoutError")),
    (ssl_error("DECRYPTION_FAILED_OR_BAD_RECORD_MAC"),
     (Cause.TRANSFER, "tls", "DECRYPTION_FAILED_OR_BAD_RECORD_MAC")),
    (verify_error(9), (Cause.TRANSFER, "tls", "verify_code=9")),
    (http.client.IncompleteRead(b"", 10), (Cause.TRANSFER, "short", "IncompleteRead")),
    (ConnectionResetError(errno.ECONNRESET, "reset"), (Cause.TRANSFER, "short", "ECONNRESET")),
    (http.client.RemoteDisconnected("gone"), (Cause.TRANSFER, "short", "RemoteDisconnected")),
])
def test_transfer_phase_rules(error, expected):
    assert named(error, "transfer") == expected


def test_the_chain_is_walked_through_cause_and_context():
    wrapper = RuntimeError("an httpx-style wrapper")
    wrapper.__cause__ = verify_error(10)
    assert named(wrapper) == (Cause.TIME, "expired", "verify_code=10")
    wrapper = RuntimeError("raised while handling")
    wrapper.__context__ = socket.gaierror(socket.EAI_NONAME, "unknown")
    assert named(wrapper) == (Cause.DNS, "failed", "EAI_NONAME")


def test_rules_are_tried_in_order_across_the_whole_chain():
    outer = OSError(errno.EACCES, "outer")          # rule 13 on the error itself
    outer.__cause__ = verify_error(62)              # rule 3 one link down wins
    assert named(outer) == (Cause.TLS, "hostname", "verify_code=62")


def test_the_chain_walk_is_cycle_safe_and_bounded_to_eight_links():
    first, second = ValueError("a"), ValueError("b")
    first.__context__, second.__context__ = second, first
    assert classify(first, phase="connect", host=None) is None
    links = [ValueError(str(index)) for index in range(8)]
    for outer, inner in zip(links, links[1:]):
        outer.__cause__ = inner
    links[-1].__cause__ = OSError(errno.EACCES, "the ninth link")
    assert classify(links[0], phase="connect", host=None) is None


def test_the_host_is_carried_through():
    classified = classify(TimeoutError(), phase="connect", host="photo-wall.example")
    assert classified.console() == (
        "cause=connect reason=timeout host=photo-wall.example detail=TimeoutError")


@pytest.mark.parametrize("error", [ValueError("a programming error"), KeyError("x"),
                                   KeyboardInterrupt()])
def test_anything_else_is_not_a_network_cause(error):
    assert classify(error, phase="connect", host=None) is None
    assert classify(error, phase="transfer", host=None) is None


def stdlib_subclasses(base: type) -> set[type]:
    found, pending = set(), [base]
    while pending:
        cls = pending.pop()
        if cls in found:
            continue
        found.add(cls)
        pending += cls.__subclasses__()
    top = {"builtins", *sys.stdlib_module_names}
    return {cls for cls in found if cls.__module__.partition(".")[0] in top}


@pytest.mark.parametrize("phase", ["connect", "transfer"])
def test_classify_is_total_over_every_stdlib_network_error(phase):
    for module in ("asyncio", "concurrent.futures", "ftplib", "urllib.error", "smtplib"):
        importlib.import_module(module)       # load the stdlib's other OSError subclasses
    classes = stdlib_subclasses(OSError) | stdlib_subclasses(http.client.HTTPException)
    assert {ssl.SSLCertVerificationError, http.client.IncompleteRead, BrokenPipeError} <= classes
    unnamed = [cls for cls in classes
               if classify(cls.__new__(cls), phase=phase, host=None) is None]
    assert unnamed == []
