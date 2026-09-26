"""next_hop, the one redirect policy (design §2.3 checks 1-7, in order): a pure table."""

import pytest

from uplink.causes import Cause, UplinkError
from uplink.origin import Origin, Url
from uplink.redirects import MAX_REDIRECTS, REDIRECT_STATUSES, next_hop

LOCATE = "/v1/locate"


def url(text: str) -> Url:
    return Origin.parse_root(text).url(LOCATE)


HTTP_A, HTTPS_A = url("http://a/"), url("https://a/")


def refused(current: Url, status: int, location: str | None, visited=None) -> UplinkError:
    with pytest.raises(UplinkError) as caught:
        next_hop(current, status, location, visited or [current])
    assert caught.value.cause is Cause.REDIRECT
    return caught.value


@pytest.mark.parametrize("status", sorted(REDIRECT_STATUSES))
def test_every_redirect_status_is_followed(status):
    assert next_hop(HTTP_A, status, "https://b/v1/locate", [HTTP_A]) == url("https://b/")


@pytest.mark.parametrize("status", [200, 300, 304, 305, 306, 404, 502])
def test_check_1_any_other_status_is_bad_status(status):
    error = refused(HTTP_A, status, "https://b/v1/locate")
    assert (error.reason, error.host, error.detail) == ("bad_status", "a", f"status={status}")


@pytest.mark.parametrize("location", [
    None,                                   # missing
    "https://b/v1/locate" + "x" * 2048,     # over 2048 characters
    "https://b/v1/ locate",                 # whitespace
    " https://b/v1/locate",                 # leading whitespace urlsplit would strip
    "https://b/v1/locate\t",                # a tab urlsplit would drop
    "https://b/v1/locate\x7f",              # a control character
    "https://b\\v1/locate",                 # a backslash
])
def test_check_2_a_missing_or_unclean_location_is_bad_location(location):
    error = refused(HTTP_A, 301, location)
    assert (error.reason, error.host) == ("bad_location", "a")   # current's host: no target yet


@pytest.mark.parametrize("location", [
    "ftp://b/v1/locate",                    # scheme not http/https
    "file:///v1/locate",
    "https:v1/locate",                      # another scheme with no host
    "https://user@b/v1/locate",             # userinfo
    "https://user:secret@b/v1/locate",
    "https://b/v1/locate#top",              # fragment
    "https://b:0/v1/locate",                # port zero
    "https://b:99999/v1/locate",            # port out of range
])
def test_check_3_an_unusable_resolved_url_is_bad_location(location):
    error = refused(HTTP_A, 302, location)
    assert (error.reason, error.host) == ("bad_location", "a")


def test_check_3_a_relative_location_resolves_against_current():
    current = url("http://a:8080/")
    assert next_hop(current, 301, "//b/v1/locate", [current]) == url("http://b/")
    # A relative path resolves to the current URL itself: that is a loop, not a new hop.
    assert refused(current, 301, "locate").reason == "loop"


def test_check_3_default_port_and_case_are_canonical():
    hop = next_hop(HTTP_A, 301, "HTTPS://B.Example:443/v1/locate", [HTTP_A])
    assert hop == Url(Origin("https", "b.example", 443), LOCATE)
    assert str(hop) == "https://b.example/v1/locate"


def test_check_4_https_to_http_is_a_downgrade_named_by_the_target():
    error = refused(HTTPS_A, 302, "http://b/v1/locate")
    assert (error.reason, error.host) == ("downgrade", "b")


def test_check_4_http_to_https_is_an_upgrade():
    assert next_hop(HTTP_A, 301, "https://a/v1/locate", [HTTP_A]) == HTTPS_A


@pytest.mark.parametrize("location", [
    "https://b/central/v1/locate",          # a sub-path move
    "https://b/v1/locate/",
    "https://b/",
    "https://b",
    "https://b/v1/locate?next=1",           # a query change
])
def test_check_5_the_target_must_not_change(location):
    error = refused(HTTP_A, 301, location)
    assert (error.reason, error.host) == ("path_changed", "b")


def test_check_5_a_downgrade_is_reported_before_a_path_change():
    assert refused(HTTPS_A, 301, "http://b/elsewhere").reason == "downgrade"


def test_check_6_a_revisited_url_is_a_loop_by_canonical_comparison():
    b = url("https://b/")
    error = refused(b, 301, "https://A:443/v1/locate", [HTTPS_A, b])
    assert (error.reason, error.host, error.detail) == ("loop", "a", "hops=a,b")


def chain(length: int) -> list[Url]:
    return [url(f"https://h{index}/") for index in range(length)]


def test_check_7_ten_redirects_are_followed_and_the_eleventh_is_refused():
    visited = chain(MAX_REDIRECTS)                        # the 10th redirect arrives
    assert next_hop(visited[-1], 301, "https://next/v1/locate", visited) == url("https://next/")
    visited = chain(MAX_REDIRECTS + 1)                    # the 11th redirect arrives
    error = refused(visited[-1], 301, "https://next/v1/locate", visited)
    assert (error.reason, error.host) == ("limit", "next")


def test_check_6_runs_before_check_7():
    visited = chain(MAX_REDIRECTS + 1)
    assert refused(visited[-1], 301, "https://h0/v1/locate", visited).reason == "loop"
