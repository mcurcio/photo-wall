"""The Immich connection the deployment sets (owner, 2026-10-12: "The immich url and api key should
be injected by iac"; "Read-only in Settings"; central/photo_library/provisioning.py).

Each case starts Central the way a deployment does: create_app over a real database and
Procrastinate's real queue, its lifespan entered by FastAPI's TestClient with the environment set.
A "restart" is a second app over the same database.

Mutation probes (each turns its case red): drop `managed=TRUE` from the store's provisioning
UPDATE -> the 409 and the restart cases; drop the `connection_managed` refusal in
`change_connection` (or in `remove_connection`) -> the refusal case; compare only the URL in the
unchanged check -> the key-change case; return SetByDeployment for a URL without a key -> the
partial case; drop the release call -> the removed-variables case; drop `_name_if_unnamed`'s
UPDATE -> the cut-over cases.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from runtime_fakes import apply_procrastinate_schema

from central.app import create_app
from central.media_queue import CHECK_CONNECTION_TASK
from central.photo_library.model import ContactAnswer, ImmichUser, ImmichVersion, SourceQuery
from central.photo_library.provisioning import KEY_FILE_VARIABLE, KEY_VARIABLE, URL_VARIABLE

ADMIN = "managed-connection-operator-" + "t" * 32
AUTH = {"Authorization": "Bearer " + ADMIN}
KEY = "PUBLIC-FIXTURE-KEY-1234"
URL = "http://immich.invalid:2283"
CONNECTIONS = "/v1/operator/library/connections"
ANNA = ImmichUser(user_ref="5b0f3c0e-3d1c-4a8a-9f6e-2a8c1f0f7a11", name="Anna", email="anna@example.com")


@pytest.fixture
def start(registry, tmp_path, monkeypatch):
    """Start Central with `env` (None unsets a variable); yields the client and the store."""
    monkeypatch.setenv("PHOTO_WALL_CACHE_ROOT", str(tmp_path))
    apply_procrastinate_schema(registry.db.dsn)
    clients = []

    def started(**env):
        while clients:   # the running Central stops first
            clients.pop().__exit__(None, None, None)
        for name in (URL_VARIABLE, KEY_VARIABLE, KEY_FILE_VARIABLE):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        app = create_app(registry.db, registry.clock, ADMIN)
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client, app.state.photo_library

    yield started
    for client in clients:
        client.__exit__(None, None, None)


def _checks(db):
    with db.transaction() as conn:
        return conn.execute("SELECT args FROM procrastinate_jobs WHERE task_name=%s ORDER BY id",
                            (CHECK_CONNECTION_TASK,)).fetchall()


def _ran(db):
    with db.transaction() as conn:
        conn.execute("UPDATE procrastinate_jobs SET status='succeeded' WHERE status='todo'")


def _set(url=URL, key=KEY):
    return {URL_VARIABLE: url, KEY_VARIABLE: key}


def test_the_set_connection_exists_is_checked_and_survives_a_restart(start, caplog):
    caplog.set_level(logging.DEBUG)
    client, library = start(**_set())
    [row] = client.get(CONNECTIONS, headers=AUTH).json()
    assert (row["name"], row["url"], row["key_hint"], row["managed"]) == (
        "Immich", URL + "/api", "…1234", True)
    assert library.connection_secret(row["connection_ref"]).api_key.get_secret_value() == KEY
    assert [job["args"] for job in _checks(library.db)] == [{"connection_ref": row["connection_ref"]}]
    _ran(library.db)

    client, library = start(**_set())   # a restart with the same setting changes nothing
    assert client.get(CONNECTIONS, headers=AUTH).json() == [row]
    assert len(_checks(library.db)) == 1
    assert all(KEY not in record.getMessage() and KEY not in str(record.args)
               for record in caplog.records)


def test_a_key_or_address_change_updates_it_in_place_and_keeps_its_immich_user(start):
    client, library = start(**_set())
    [row] = client.get(CONNECTIONS, headers=AUTH).json()
    ref = row["connection_ref"]
    assert library.record_contact(library.begin_contact(ref), ContactAnswer(
        version=ImmichVersion(major=3, minor=1, patch=0), user=ANNA))
    _ran(library.db)

    client, library = start(**_set(key="NEW-KEY-5678"))
    [row] = client.get(CONNECTIONS, headers=AUTH).json()
    assert (row["connection_ref"], row["key_hint"], row["managed"]) == (ref, "…5678", True)
    assert library.connection_secret(ref).api_key.get_secret_value() == "NEW-KEY-5678"
    _ran(library.db)

    client, library = start(**_set(url="https://photos.example/immich/", key="NEW-KEY-5678"))
    [row] = client.get(CONNECTIONS, headers=AUTH).json()
    assert (row["connection_ref"], row["url"]) == (ref, "https://photos.example/immich/api")
    # A changed connection's rules: it is checked again, and it keeps the Immich user it is bound
    # to (another user's key reads Needs a look, other_user).
    assert row["immich_user"]["user_ref"] == ANNA.user_ref
    assert [job["args"] for job in _checks(library.db)] == [{"connection_ref": ref}] * 3


def test_the_api_refuses_to_change_or_remove_it_and_still_checks_it(start):
    client, _ = start(**_set())
    [row] = client.get(CONNECTIONS, headers=AUTH).json()
    path = f"{CONNECTIONS}/{row['connection_ref']}"
    for refused in (client.put(path, json={"name": "Family", "url": URL}, headers=AUTH),
                    client.put(path, json={"name": "Immich", "url": URL, "key": "X-9999"},
                               headers=AUTH),
                    client.delete(path, headers=AUTH)):
        assert (refused.status_code, refused.json()) == (409, {"error": "connection_managed"})
    assert client.get(CONNECTIONS, headers=AUTH).json() == [row]
    assert client.post(f"{path}/checks", headers=AUTH).status_code == 202
    # Another connection is still added and changed in Settings.
    other = client.post(CONNECTIONS, json={"name": "Family", "url": "http://other.invalid",
                                           "key": KEY}, headers=AUTH)
    assert other.status_code == 201
    assert client.put(f"{CONNECTIONS}/{other.json()['connection_ref']}",
                      json={"name": "Archive", "url": "http://other.invalid"},
                      headers=AUTH).status_code == 200


@pytest.mark.parametrize(("env", "names"), [
    ({URL_VARIABLE: URL}, f"{URL_VARIABLE} is set but {KEY_VARIABLE}"),
    ({KEY_VARIABLE: KEY}, f"the API key is set but {URL_VARIABLE} is not"),
    ({URL_VARIABLE: URL, KEY_VARIABLE: KEY, KEY_FILE_VARIABLE: "/run/secrets/immich"},
     f"set {KEY_VARIABLE} or {KEY_FILE_VARIABLE}, not both"),
    ({URL_VARIABLE: URL, KEY_FILE_VARIABLE: "/nonexistent/immich-key"},
     f"{KEY_FILE_VARIABLE} names a file Central cannot read"),
    ({URL_VARIABLE: "ftp://immich.invalid", KEY_VARIABLE: KEY},
     f"{URL_VARIABLE} is not an Immich address"),
], ids=["url-only", "key-only", "both-keys", "unreadable-file", "bad-url"])
def test_a_partial_setting_is_an_error_log_and_central_still_starts(start, caplog, env, names):
    """The log names what is missing or wrong, never the key."""
    caplog.set_level(logging.INFO)
    client, library = start(**env)
    assert client.get("/healthz").status_code in (200, 503)   # Central answers
    assert client.get(CONNECTIONS, headers=AUTH).json() == []
    assert _checks(library.db) == []
    [error] = [record for record in caplog.records if record.levelno == logging.ERROR
               and "Immich connection from the environment not applied" in record.getMessage()]
    assert names in error.getMessage() and KEY not in error.getMessage()


def test_a_partial_setting_leaves_the_set_connection_as_it_was(start, caplog):
    client, library = start(**_set())
    [row] = client.get(CONNECTIONS, headers=AUTH).json()
    caplog.set_level(logging.ERROR)
    client, _ = start(**{URL_VARIABLE: "http://elsewhere.invalid"})
    assert client.get(CONNECTIONS, headers=AUTH).json() == [row]
    assert any("not applied" in record.getMessage() for record in caplog.records)


def test_the_key_may_come_from_a_file(start, tmp_path):
    secret = tmp_path / "immich-key"
    secret.write_text(KEY + "\n")
    client, library = start(**{URL_VARIABLE: URL, KEY_FILE_VARIABLE: str(secret)})
    [row] = client.get(CONNECTIONS, headers=AUTH).json()
    assert (row["managed"], row["key_hint"]) == (True, "…1234")
    assert library.connection_secret(row["connection_ref"]).api_key.get_secret_value() == KEY


def test_removed_variables_make_it_an_ordinary_connection_and_keep_its_sources(start, caplog):
    client, library = start(**_set())
    [row] = client.get(CONNECTIONS, headers=AUTH).json()
    ref = row["connection_ref"]
    with library.db.transaction() as conn:
        conn.execute("INSERT INTO media_sources(source_ref,query) VALUES('family:1',%s)",
                     (Jsonb(SourceQuery(connection_ref=ref).model_dump(mode="json")),))
        conn.execute("INSERT INTO media_source_names(name,current_ref,revision) "
                     "VALUES('family','family:1',1)")
    caplog.set_level(logging.WARNING)
    client, library = start()
    [after] = client.get(CONNECTIONS, headers=AUTH).json()
    assert after == {**row, "managed": False}
    assert library.connection_secret(ref).api_key.get_secret_value() == KEY
    assert any(ref in record.getMessage() for record in caplog.records)
    # Now Settings changes it; and setting the variables again takes it back, in place.
    assert client.put(f"{CONNECTIONS}/{ref}", json={"name": "Family", "url": URL},
                      headers=AUTH).status_code == 200
    client, _ = start(**_set())
    [again] = client.get(CONNECTIONS, headers=AUTH).json()
    assert (again["connection_ref"], again["name"], again["managed"]) == (ref, "Family", True)


def test_it_takes_over_the_connection_waiting_since_the_cut_over(start, registry):
    """Delivery 4's cut-over left the deployment's old connection as a row with no address or
    key that its Sources point at; the setting fills that row rather than adding a second."""
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO library_connections(connection_ref,name,created_at) "
                     "VALUES('family-library','family-library',0)")
        conn.execute("INSERT INTO library_connections(connection_ref,name,url,api_key,created_at) "
                     "VALUES('library-000000000001','Immich','http://other.invalid/api','K-0000',1)")
    client, library = start(**_set())
    rows = {row["connection_ref"]: row for row in client.get(CONNECTIONS, headers=AUTH).json()}
    assert rows["family-library"]["managed"] and rows["family-library"]["url"] == URL + "/api"
    # Its name was only its ref (nobody chose it) and Settings cannot rename a managed one.
    assert rows["family-library"]["name"] == "Immich 2"
    assert not rows["library-000000000001"]["managed"]
    assert library.connection_secret("family-library").api_key.get_secret_value() == KEY


def test_a_set_connection_still_named_by_its_ref_is_named_immich_at_the_next_start(start, registry):
    """Production after delivery 4: the cut-over row `immich-main`, already managed, showed its ref
    as its name everywhere (owner, 2026-10-12: "immich photo source says "immich-main" instead of
    the more natural "Immich""). The next start names it; no check is queued for a rename."""
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO library_connections(connection_ref,name,url,api_key,managed,"
                     "created_at) VALUES('immich-main','immich-main',%s,%s,TRUE,0)",
                     (URL + "/api", KEY))
    client, library = start(**_set())
    [row] = client.get(CONNECTIONS, headers=AUTH).json()
    assert (row["connection_ref"], row["name"], row["managed"]) == ("immich-main", "Immich", True)
    assert _checks(library.db) == []


def test_with_a_connection_of_that_name_it_is_named_immich_2(start, registry):
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO library_connections(connection_ref,name,url,api_key,created_at) "
                     "VALUES('library-000000000001','Immich','http://other.invalid/api','K-0000',1)")
    client, _ = start(**_set())
    names = {row["name"]: row["managed"] for row in client.get(CONNECTIONS, headers=AUTH).json()}
    assert names == {"Immich": False, "Immich 2": True}
