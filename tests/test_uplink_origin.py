"""Origin and Url: the one Central-root validator and canonical equality."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from uplink.causes import Cause, UplinkError
from uplink.origin import Origin, Url, parse_url


@pytest.mark.parametrize(("text", "origin"), [
    ("http://photo-wall.localdomain/", Origin("http", "photo-wall.localdomain", 80)),
    ("http://photo-wall.localdomain", Origin("http", "photo-wall.localdomain", 80)),
    ("HTTPS://Photo-Wall.Example:443/", Origin("https", "photo-wall.example", 443)),
    ("http://192.0.2.10:8080/", Origin("http", "192.0.2.10", 8080)),
    ("http://[2001:DB8::1]:8080/", Origin("http", "2001:db8::1", 8080)),
    ("https://bücher.example/", Origin("https", "xn--bcher-kva.example", 443)),
    ("http://host:/", Origin("http", "host", 80)),        # an empty port is the default
    ("http://host/?", Origin("http", "host", 80)),        # as today: an empty query is none
])
def test_parse_root_accepts_the_central_root_in_canonical_form(text, origin):
    assert Origin.parse_root(text) == origin


@pytest.mark.parametrize("text", [
    "http://photo-wall/app/",                 # a path
    "photo-wall.localdomain",                 # no scheme
    "@@PHOTOWALL_CENTRAL@@",                  # the template placeholder
    "",
    "ftp://host/",
    "http:///",                               # no host
    "http://user:secret@host/",               # userinfo
    "http://host/?next=1",                    # query
    "http://host/#top",                       # fragment
    "http://host:0/",
    "http://host:65536/",
    "http://host:port/",
    "http://[2001:db8::1/",                   # an unclosed bracket
    "http://host\\/",                         # backslash
    "http://ho st/",                          # whitespace
    "http://host/ ",                     # non-ASCII whitespace
    "http://host/\x00",                       # control characters
    "http://host/\x7f",
    "http://" + "h" * 2048 + "/",             # over 2048 characters
    None,
])
def test_parse_root_refuses_everything_else_as_configuration_invalid(text):
    with pytest.raises(UplinkError) as caught:
        Origin.parse_root(text)
    assert (caught.value.cause, caught.value.reason) == (Cause.CONFIGURATION, "invalid")


@pytest.mark.parametrize(("scheme", "host", "port"), [
    ("https", "Host", 443),                   # not lower case
    ("https", "[::1]", 443),                  # brackets
    ("https", "bücher.example", 443),         # not an A-label
    ("https", "", 443),
    ("https", "h/x", 443),
    ("ftp", "host", 21),
    ("https", "host", 0),
    ("https", "host", 65536),
    ("https", "host", True),
])
def test_a_non_canonical_origin_cannot_be_constructed(scheme, host, port):
    with pytest.raises(ValueError):
        Origin(scheme, host, port)


def test_text_omits_the_default_port_and_brackets_ipv6():
    assert str(Origin("https", "photo-wall.example", 443)) == "https://photo-wall.example"
    assert str(Origin("http", "photo-wall.example", 443)) == "http://photo-wall.example:443"
    assert str(Origin("http", "2001:db8::1", 8080)) == "http://[2001:db8::1]:8080"
    assert str(Origin("https", "a", 443).url("/v1/locate")) == "https://a/v1/locate"


@pytest.mark.parametrize("target", ["v1/locate", "", "/v1/locate#top", "/v1/ locate"])
def test_a_url_target_is_an_absolute_path_with_an_optional_query(target):
    with pytest.raises(ValueError):
        Url(Origin("https", "a", 443), target)


def test_parse_url_keeps_the_query_in_the_target():
    assert parse_url("https://a/v1/netboot/base?x=1") == Url(Origin("https", "a", 443),
                                                              "/v1/netboot/base?x=1")


LABEL = st.from_regex(r"[a-z0-9]([a-z0-9-]{0,14}[a-z0-9])?", fullmatch=True)
HOSTS = st.lists(LABEL, min_size=1, max_size=4).map(".".join)
SCHEMES = st.sampled_from(["http", "https"])
DEFAULT = {"http": 80, "https": 443}


def mixed_case(text: str, flips: list[bool]) -> str:
    return "".join(c.upper() if flip else c for c, flip in zip(text, flips + [False] * len(text)))


@given(scheme=SCHEMES, host=HOSTS, port=st.integers(1, 65535))
def test_text_round_trips_to_the_same_origin(scheme, host, port):
    origin = Origin(scheme, host, port)
    assert Origin.parse_root(str(origin)) == origin
    assert Origin.parse_root(f"{origin}/") == origin


@given(scheme=SCHEMES, host=HOSTS, flips=st.lists(st.booleans(), max_size=80),
       explicit=st.booleans(), slash=st.booleans())
def test_case_and_an_explicit_default_port_do_not_change_equality(scheme, host, flips,
                                                                  explicit, slash):
    port = f":{DEFAULT[scheme]}" if explicit else ""
    text = f"{mixed_case(scheme, flips)}://{mixed_case(host, flips[::-1])}{port}"
    text += "/" if slash else ""
    assert Origin.parse_root(text) == Origin.parse_root(f"{scheme}://{host}")
