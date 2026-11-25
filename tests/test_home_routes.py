"""Home's read (decision 0036; central/home_routes.py) over a hand-held store: the document with
its change number as the entity tag, and the conditional read that costs no body. No database;
the same routes over PostgreSQL are tests/test_frame_status_store.py's.

Mutation probes: answer the body whatever `If-None-Match` says -> the 304 fails; compose the tag
from anything but `change` -> the tag assertions fail.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from central.frame_status.model import Action, HouseStatus
from central.frame_status.views import HomeView, home_view
from central.home_routes import ACTION_PATHS, HOME, etag, mount_home_routes


class Home:
    """HomeQueries with one document, counting its reads."""

    def __init__(self) -> None:
        self.reads = 0
        self.view = home_view(house=HouseStatus(word="all-good"), walls=(), overrides=(), now_next=())

    def home(self) -> HomeView:
        self.reads += 1
        return self.view

    def frame(self, frame_id: str):
        raise LookupError(frame_id)


def _client(home: Home, asked: list[str]) -> TestClient:
    app = FastAPI()
    mount_home_routes(app, admin=lambda: asked.append("admin"), home=home)
    return TestClient(app)


def test_home_answers_its_document_tagged_with_its_change_number():
    home, asked = Home(), []
    response = _client(home, asked).get(HOME)
    assert response.status_code == 200 and HomeView.model_validate(response.json()) == home.view
    assert response.headers["etag"] == etag(home.view.change) == f'"{home.view.change}"'
    assert asked == ["admin"]                        # the read asks for the operator's `admin`


def test_an_unchanged_document_costs_no_body_and_a_changed_one_is_answered_in_full():
    home, asked = Home(), []
    client = _client(home, asked)
    tag = etag(home.view.change)
    for header in (tag, "W/" + tag, f'"1", {tag}', "*"):
        response = client.get(HOME, headers={"If-None-Match": header})
        assert (response.status_code, response.content, response.headers["etag"]) == (304, b"", tag), header
    assert client.get(HOME, headers={"If-None-Match": '"1"'}).status_code == 200
    home.view = home_view(house=HouseStatus(word="showing"), walls=(), overrides=(), now_next=())
    changed = client.get(HOME, headers={"If-None-Match": tag})
    assert changed.status_code == 200 and changed.headers["etag"] != tag
    assert changed.json()["house"]["word"] == "showing"


def test_the_change_number_is_of_the_document_alone():
    """Equal documents carry equal numbers (across reads, processes and restarts); any difference
    moves it; it is whole in a JavaScript number."""
    first = home_view(house=HouseStatus(word="all-good"), walls=(), overrides=(), now_next=())
    again = home_view(house=HouseStatus(word="all-good"), walls=(), overrides=(), now_next=())
    other = home_view(house=HouseStatus(word="resting"), walls=(), overrides=(), now_next=())
    assert first == again and first.change != other.change
    assert 0 <= first.change < 2 ** 53


def test_every_action_a_frame_can_offer_has_the_path_it_is_asked_at():
    """Each action has its path under /v1/operator/, naming what it is asked of: the Pi for a
    reboot and a restart, the Frame for Skip and Previous (slice 3), the photo in the body for
    Never show."""
    assert set(ACTION_PATHS) == set(Action)
    assert all(path.startswith("/v1/operator/") for path in ACTION_PATHS.values())
    named = {Action.REBOOT_PI: "{device_id}", Action.RESTART_APP: "{device_id}",
             Action.SKIP: "{frame_id}", Action.PREVIOUS: "{frame_id}"}
    assert all(placeholder in ACTION_PATHS[action] for action, placeholder in named.items())
    assert ACTION_PATHS[Action.NEVER_SHOW] == "/v1/operator/never-show"
