"""The Home Assistant link's broker address and identity (decision 0028; central/infra/home_assistant.py):
PHOTO_WALL_MQTT_URL names the broker (mqtt or mqtts, default ports, percent-encoded credentials), no URL
runs no link, anything else is refused, and two links never share a client id (a clean session needs no
stable one, and two Centrals on one broker would evict each other)."""
from __future__ import annotations

import pytest

from central.infra.home_assistant.link import (
    CLIENT_ID_PREFIX,
    MQTT_URL_ENV,
    Broker,
    broker_from,
    build_home_assistant_link,
)


class Reports:
    """A RoomLightReports that records nothing (the link is only built here)."""


@pytest.mark.parametrize("url, broker", [
    ("mqtt://broker.lan", Broker("broker.lan", 1883, None, None, False)),
    ("mqtts://broker.lan", Broker("broker.lan", 8883, None, None, True)),
    ("mqtt://photo%40wall:p%3Ass@10.0.0.5:1884", Broker("10.0.0.5", 1884, "photo@wall", "p:ss", False)),
])
def test_a_broker_url_names_its_broker(url, broker):
    assert broker_from(url) == broker


@pytest.mark.parametrize("url", ["http://broker.lan", "mqtt://", "broker.lan:1883", "ws://broker.lan/mqtt"])
def test_any_other_url_is_refused(url):
    with pytest.raises(ValueError, match="mqtt_url"):
        broker_from(url)


def test_no_url_runs_no_link_and_two_links_never_share_a_client_id():
    """Mutation probe: a fixed identifier -> the two links' ids are equal."""
    assert build_home_assistant_link({}, Reports(), lambda: None) is None
    assert build_home_assistant_link({MQTT_URL_ENV: ""}, Reports(), lambda: None) is None
    first, second = (build_home_assistant_link({MQTT_URL_ENV: "mqtt://broker.lan"}, Reports(), lambda: None)
                     for _ in range(2))
    assert first is not None and second is not None
    assert first.client_id != second.client_id
    assert first.client_id.startswith(CLIENT_ID_PREFIX + "-")
