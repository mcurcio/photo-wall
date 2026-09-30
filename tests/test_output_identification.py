"""Central persistence, eligibility and delivery for output identification."""

from fastapi.testclient import TestClient
from test_registry import ADMIN, enroll

from central.app import create_app
from contracts.player_control import ControlHello


def negotiate(client, player):
    response = client.post("/v1/player/hello", headers={
        "Authorization": "Bearer " + player["token"],
    }, json={"authority_epoch": player["authority_epoch"], "schemas": [1, 2],
             "capabilities": ["identify_output"]})
    assert response.status_code == 200
    assert response.json() == {"authority_epoch": player["authority_epoch"],
                               "schema": 2, "capabilities": ["identify_output"]}


def test_identify_output_is_delivered_over_rest_and_websocket_then_expires(registry):
    player, _, _ = enroll(registry)
    app = create_app(registry.db, registry.clock, ADMIN)
    operator = {"Authorization": "Bearer " + ADMIN}
    appliance = {"Authorization": "Bearer " + player["token"]}
    with TestClient(app) as client:
        negotiate(client, player)
        accepted = client.post(
            f"/v1/operator/players/{player['player_id']}/outputs/HDMI-A-2/identify",
            headers=operator,
        )
        assert accepted.status_code == 200
        receipt = accepted.json()
        assert set(receipt) == {"request_id", "output_id", "expires_at"}
        assert receipt["output_id"] == "HDMI-A-2"
        assert receipt["expires_at"] == registry.clock.utc() + 15

        state = client.get("/v1/player/state", headers=appliance).json()
        assert state["identify_output"] == {
            "request_id": receipt["request_id"], "output_id": "HDMI-A-2",
            "authority_epoch": 1, "remaining_seconds": 15,
        }
        assert state["identify_expires_at"] == receipt["expires_at"]
        assert state["delivery_sequence"] == 1

        with client.websocket_connect("/v1/player/session", headers=appliance) as socket:
            sent = socket.receive_json()
            assert sent.pop("type") == "state"
            assert sent == state

        registry.clock.advance(6)
        later = client.get("/v1/player/state", headers=appliance).json()
        assert later["identify_output"]["remaining_seconds"] == 9
        assert later["identify_expires_at"] == receipt["expires_at"]
        assert later["delivery_id"] == state["delivery_id"]
        assert later["delivery_sequence"] == state["delivery_sequence"]
        registry.clock.advance(9)
        expired = client.get("/v1/player/state", headers=appliance).json()
        assert expired["identify_output"] is None
        assert expired["identify_expires_at"] is None
        assert expired["delivery_sequence"] == state["delivery_sequence"] + 1


def test_identify_output_supersedes_and_stops_when_bound_or_disconnected(registry):
    player, _, _ = enroll(registry)
    app = create_app(registry.db, registry.clock, ADMIN)
    operator = {"Authorization": "Bearer " + ADMIN}
    appliance = {"Authorization": "Bearer " + player["token"]}
    with TestClient(app) as client:
        negotiate(client, player)
        unauthorized = client.post(
            f"/v1/operator/players/{player['player_id']}/outputs/HDMI-A-1/identify")
        assert unauthorized.status_code == 401
        unknown = client.post(
            f"/v1/operator/players/{player['player_id']}/outputs/HDMI-A-9/identify",
            headers=operator,
        )
        assert unknown.status_code == 404

        url = f"/v1/operator/players/{player['player_id']}/outputs"
        first = client.post(f"{url}/HDMI-A-1/identify", headers=operator).json()
        second = client.post(f"{url}/HDMI-A-2/identify", headers=operator).json()
        assert first["request_id"] != second["request_id"]
        assert client.get("/v1/player/state", headers=appliance).json()["identify_output"][
            "request_id"] == second["request_id"]

        with registry.db.transaction() as conn:
            conn.execute("UPDATE outputs SET observation=jsonb_set(observation,'{connected}', 'false') "
                         "WHERE player_id=%s AND output_id='HDMI-A-2'", (player["player_id"],))
        assert client.get("/v1/player/state", headers=appliance).json()["identify_output"] is None
        disconnected = client.post(f"{url}/HDMI-A-2/identify", headers=operator)
        assert disconnected.status_code == 409
        assert disconnected.json() == {"error": "output_disconnected"}

        assert client.post("/v1/operator/frames", headers=operator, json={
            "id": "portrait", "width_mm": 300, "height_mm": 500,
            "profile": {"width_px": 1080, "height_px": 1920, "diagonal_inches": 24},
        }).status_code == 201
        active = client.post(f"{url}/HDMI-A-1/identify", headers=operator)
        assert active.status_code == 200
        assert client.put("/v1/operator/frames/portrait/binding", headers=operator, json={
            "player_id": player["player_id"], "output_id": "HDMI-A-1", "expected_generation": 0,
        }).status_code == 200
        assert client.get("/v1/player/state", headers=appliance).json()["identify_output"] is None
        bound = client.post(f"{url}/HDMI-A-1/identify", headers=operator)
        assert bound.status_code == 409 and bound.json() == {"error": "output_bound"}


def test_identify_output_epoch_is_fenced_by_reenrollment(registry):
    player, key, request = enroll(registry)
    registry.control_hello(player["player_id"], ControlHello(
        authority_epoch=player["authority_epoch"], schemas=(1, 2),
        capabilities=("identify_output",)))
    registry.identify_output(player["player_id"], "HDMI-A-2")
    current, _, _ = enroll(registry, key=key, device_id=request.device_id)
    app = create_app(registry.db, registry.clock, ADMIN)
    with TestClient(app) as client:
        state = client.get("/v1/player/state", headers={
            "Authorization": "Bearer " + current["token"],
        }).json()
        assert set(state) == {"configuration", "plan", "commits", "revocations"}

        retired = client.post(
            f"/v1/operator/players/{player['player_id']}/retire",
            headers={"Authorization": "Bearer " + ADMIN},
        )
        assert retired.status_code == 200
        assert client.post(
            f"/v1/operator/players/{player['player_id']}/outputs/HDMI-A-2/identify",
            headers={"Authorization": "Bearer " + ADMIN},
        ).json() == {"error": "unknown_or_retired_player"}
