"""Central on its own hub, and Home Assistant's door to it (the Home Assistant system design, draft 4;
owner's answers Q1 A, Q2 A, Q3 A, 2026-10-16: "the goal is for NATS to become *the API*").
Contracts and pydantic only.

Central is a component on its hub (0017 C4's pattern): state keys anyone with the door's user reads,
and named methods `central.method.<name>`. They live in the hub account CENTRAL_ACCOUNT, beside the
bucket Home Assistant writes its states into (contracts/home_assistant.py). Home Assistant reaches the
account through Central's origin at "/" + DOOR_PATH, which Central's app relays, bytes untouched, to
the hub's WebSocket listener (as it relays the Pis' leaves, central/fleet/leaf_bridge.py). The door
admits one user, HOME_ASSISTANT_USER, and only over WebSocket; its permissions are the hub's
(HOME_ASSISTANT_PUBLISH, HOME_ASSISTANT_SUBSCRIBE): publish Home Assistant's states, read Central's
state one key at a time, call Central's methods, nothing else. The user has an account of its own,
HOME_ASSISTANT_ACCOUNT, which imports exactly those from CENTRAL_ACCOUNT as services
(HOME_ASSISTANT_IMPORTS: Central's subject -> Home Assistant's; Central's state is read through
nats-py's JetStream context with prefix CENTRAL_DOOR_API_PREFIX), so every reply (an
acknowledgement, a stored message, a method's answer) lands in Home Assistant's account: a reply
subject it names can never make JetStream or Central publish into CENTRAL_ACCOUNT (a confused
deputy). Central's own client, CENTRAL_USER, is
admitted only on the hub's plain client port, inside the deployment. Both user names are selectors
on the trusted network, not secrets (a secret is deferred; the design's costs).

Central's state (one key each, CENTRAL_STATE_TABLE): every value is level state Central puts again
whenever it changes and all again in a new epoch of the bucket (a hub restart starts empty). Each
value is read from the same Central services the console's routes call.

- BIRTH_KEY `CentralBirth`: Central's version.
- INSTALLATION_KEY `Installation`: every Frame with the Pi that drives it (serial, HDMI port and the
  binding generation its Player's report must carry), and every Pi with its name and address.
- LISTENING_KEY `Listening`: the entities Central's room-light setup names as reporters.
- ROOM_LIGHTS_KEY `RoomLightsListing`: each room light as the wall draws it (the room-lights
  document's entries, with their names).
- RELEASES_KEY `Releases`: Central's catalog, newest first.
- VERSIONS_KEY `Versions`: what each Pi is to boot, and an install's progress.
- STATES_EPOCH_KEY `StatesEpoch`: the epoch of Home Assistant's states bucket. Written by Central's
  hub session when it creates or finds the bucket; a publisher that last published every state into
  another epoch publishes them all again (the republish trigger after a hub restart).

Central's method (one, answered once): INSTALL_PLAYER, `InstallPlayer` -> `InstallAnswer`. It sets
that Pi's version, downloads the release if needed, then restarts that Pi onto it; nothing else
changes (H5: "apply only to that device"). Its refusals are `InstallRefusal`.

Rules this module owns (one home each): the door's account, users, route and permission lists; the
state keys, their largest values and shapes; the release tag's form on the wire; the method's name,
request, answer and refusals; the codecs. Rules it does NOT own: how Central computes any value
(central/), the bucket builders (nodeapi.buffers), what Home Assistant shows (custom_components/galleria).
"""
from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Final, Literal

from pydantic import Field

from contracts.home_assistant import EntityId
from contracts.identifiers import TargetIdentifier
from contracts.node_link import METHOD_TOKEN, SERIAL_PATTERN
from contracts.node_state import OutputId
from contracts.room_light import ROOM_LIGHTS_MAX, RoomLightEntry, RoomLightId
from contracts.wire_codec import encode
from contracts.wire_model import Model, decoded

# ---- The door ----

CENTRAL_ACCOUNT: Final = "CENTRAL"              # the hub account: Central's state and methods, Home Assistant's states
HOME_ASSISTANT_ACCOUNT: Final = "HOME_ASSISTANT"   # the door's account: imports HOME_ASSISTANT_PUBLISH from CENTRAL
CENTRAL_COMPONENT: Final = "central"            # Central as a component: its micro service and method subjects
CENTRAL_USER: Final = "central-api"             # Central's client in CENTRAL_ACCOUNT (plain client port only)
HOME_ASSISTANT_USER: Final = "home-assistant"   # the door's one user (WebSocket only); password = user name
# The origin's path relayed to the hub's client WebSocket: Home Assistant dials
# `ws(s)://<Central's origin>/` + DOOR_PATH, with Central's host and port unchanged.
DOOR_PATH: Final = "photo-wall/bus/door"
# The buckets in CENTRAL_ACCOUNT (NATS key-value naming: stream KV_<bucket>, subjects $KV.<bucket>.<key>).
# nodeapi.buffers builds both from these names (`central_state_bucket`, `home_assistant_states_bucket`).
CENTRAL_STATE_BUCKET: Final = "state_central"
CENTRAL_STATE_STREAM: Final = f"KV_{CENTRAL_STATE_BUCKET}"
CENTRAL_STATE_PREFIX: Final = f"$KV.{CENTRAL_STATE_BUCKET}."
HOME_ASSISTANT_STATES_BUCKET: Final = "home_assistant"
HOME_ASSISTANT_STATES_STREAM: Final = f"KV_{HOME_ASSISTANT_STATES_BUCKET}"
HOME_ASSISTANT_STATES_PREFIX: Final = f"$KV.{HOME_ASSISTANT_STATES_BUCKET}."   # + entity id
# Home Assistant's JetStream API prefix for Central's imported state read (as WALL's is ACC.WALL.API).
CENTRAL_DOOR_API_PREFIX: Final = "ACC.CENTRAL.API"
# What Home Assistant's account imports from CENTRAL_ACCOUNT, Central's subject -> Home Assistant's:
# its states (a JetStream publish, acknowledged to its inbox), Central's state one key at a time (a
# STREAM.MSG.GET by last subject), Central's methods. These are also its only publish permissions.
HOME_ASSISTANT_IMPORTS: Final = MappingProxyType({
    HOME_ASSISTANT_STATES_PREFIX + ">": HOME_ASSISTANT_STATES_PREFIX + ">",
    f"$JS.API.STREAM.MSG.GET.{CENTRAL_STATE_STREAM}":
        f"{CENTRAL_DOOR_API_PREFIX}.STREAM.MSG.GET.{CENTRAL_STATE_STREAM}",
    f"{CENTRAL_COMPONENT}.{METHOD_TOKEN}.>": f"{CENTRAL_COMPONENT}.{METHOD_TOKEN}.>",
})
HOME_ASSISTANT_PUBLISH: Final = tuple(HOME_ASSISTANT_IMPORTS.values())
HOME_ASSISTANT_SUBSCRIBE: Final = ("_INBOX.>",)

# ---- Central's state keys ----

BIRTH_KEY: Final = "birth"
INSTALLATION_KEY: Final = "installation"
LISTENING_KEY: Final = "listening"
ROOM_LIGHTS_KEY: Final = "room-lights"
RELEASES_KEY: Final = "releases"
VERSIONS_KEY: Final = "versions"
STATES_EPOCH_KEY: Final = "states-epoch"
# key -> its largest value in bytes: the bucket's key table (nodeapi.buffers KeyTable), every value
# under the leaf's largest stored message.
CENTRAL_STATE_TABLE: Final = MappingProxyType({
    BIRTH_KEY: 256,
    INSTALLATION_KEY: 64 * 1024,
    LISTENING_KEY: 24 * 1024,
    ROOM_LIGHTS_KEY: 24 * 1024,
    RELEASES_KEY: 8 * 1024,
    VERSIONS_KEY: 32 * 1024,
    STATES_EPOCH_KEY: 128,
})
FRAMES_MAX: Final = 256                   # Frames one Installation lists
PLAYERS_MAX: Final = 128                  # Pis one Installation lists (the fleet's ceiling)
LISTENED_MAX: Final = 64                  # entities the setup names (room lights + Sensors)
RELEASES_LISTED: Final = 20               # the newest releases Central lists
NAME_MAX: Final = 128

# A release's tag: `v` + SemVer 2.0.0 without build metadata (central/kernel/types.py release_version
# judges precedence; this is only the wire's form).
RELEASE_TAG_PATTERN: Final = r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?"
ReleaseTag = Annotated[str, Field(pattern=rf"^{RELEASE_TAG_PATTERN}$", max_length=128, title="ReleaseTag")]
Serial = Annotated[str, Field(pattern=rf"^{SERIAL_PATTERN}$", title="Serial")]
Name = Annotated[str, Field(min_length=1, max_length=NAME_MAX)]
ReadinessState = Literal["ready", "downloading", "not_downloaded", "failed"]   # central/kernel/ports.py's words
InstallProgress = Literal["downloading", "restarting"]   # downloading the release, then restarting the Pi


class CentralBirth(Model):
    """Central as a component on its hub: its running version."""
    component: Literal["central"] = "central"
    version: Annotated[str, Field(min_length=1, max_length=64)]


class DrivenBy(Model):
    """The Pi that drives a Frame: its serial, the HDMI port and the binding generation. A Frame
    report from that Pi counts only while it carries this generation."""
    serial: Serial
    output_id: OutputId
    generation: int = Field(ge=1)


class FrameListing(Model):
    """A Frame and who drives it (None: no Pi is bound to it now)."""
    id: TargetIdentifier
    name: Name
    driven_by: DrivenBy | None


class PlayerListing(Model):
    """An enrolled Pi: its serial, name and the IPv4 address HostCore last reported (None: none),
    where its read-only door is."""
    serial: Serial
    name: Name
    address: Annotated[str, Field(max_length=45)] | None


class Installation(Model):
    frames: tuple[FrameListing, ...] = Field(max_length=FRAMES_MAX)
    players: tuple[PlayerListing, ...] = Field(max_length=PLAYERS_MAX)


class Listening(Model):
    """The Home Assistant entities Central's room-light setup names as reporters; the rest of what
    Home Assistant publishes Central ignores."""
    entities: tuple[EntityId, ...] = Field(max_length=LISTENED_MAX)


class RoomLightListing(Model):
    """A room light as the wall draws it: its id, name and its room-lights document entry."""
    id: RoomLightId
    name: Name
    entry: RoomLightEntry


class RoomLightsListing(Model):
    """Every room light, in House › Lights order, at the room-lights document's `change`."""
    change: int = Field(ge=1)
    lights: tuple[RoomLightListing, ...] = Field(max_length=ROOM_LIGHTS_MAX)


class ReleaseListing(Model):
    """A release in Central's catalog: whether it is stable and whether Central holds it ready."""
    version: ReleaseTag
    stable: bool
    readiness: ReadinessState


class Releases(Model):
    """The newest RELEASES_LISTED releases, newest first."""
    releases: tuple[ReleaseListing, ...] = Field(max_length=RELEASES_LISTED)


class PiVersion(Model):
    """What one Pi is to boot: `version` (None: no release is ready to boot), `chosen` (Central keeps
    one for this Pi; False: it boots the newest ready stable release), and an install's progress
    (None: none running)."""
    version: ReleaseTag | None
    chosen: bool
    install: InstallProgress | None


class Versions(Model):
    pis: dict[Serial, PiVersion] = Field(max_length=PLAYERS_MAX)


class StatesEpoch(Model):
    """The epoch of Home Assistant's states bucket (nodeapi's stream epoch, 32 hex)."""
    epoch: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


# ---- Central's method ----

INSTALL_PLAYER: Final = "installPlayer"   # subject: central.method.installPlayer
INSTALL_BYTES: Final = 512


class InstallRefusal(StrEnum):
    """Why Central refused to install a release on a Pi."""
    UNKNOWN_PLAYER = "unknown_player"     # no enrolled Pi with this serial
    UNKNOWN_VERSION = "unknown_version"   # not in Central's catalog
    NOT_DEPLOYABLE = "not_deployable"     # the catalog rejected it (a malformed manifest, a tag conflict)


class InstallPlayer(Model):
    """installPlayer's request: put `version` on the Pi `serial`."""
    serial: Serial
    version: ReleaseTag


class InstallStarted(Model):
    """installPlayer's answer: the Pi's version is set and the install runs (its progress then reads
    in VERSIONS_KEY)."""
    kind: Literal["started"] = "started"
    serial: Serial
    version: ReleaseTag
    install: InstallProgress


class InstallRefused(Model):
    kind: Literal["refused"] = "refused"
    refusal: InstallRefusal


InstallAnswer = Annotated[InstallStarted | InstallRefused, Field(discriminator="kind", title="InstallAnswer")]


class _Answer(Model):
    answer: InstallAnswer


# ---- Codecs: canonical JSON (contracts.wire_codec), strict decoding ----

STATE_MODELS: Final = MappingProxyType({
    BIRTH_KEY: CentralBirth, INSTALLATION_KEY: Installation, LISTENING_KEY: Listening,
    ROOM_LIGHTS_KEY: RoomLightsListing, RELEASES_KEY: Releases, VERSIONS_KEY: Versions,
    STATES_EPOCH_KEY: StatesEpoch,
})
CentralStateValue = (CentralBirth | Installation | Listening | RoomLightsListing | Releases | Versions
                     | StatesEpoch)


def encode_central_state(key: str, value: CentralStateValue) -> bytes:
    """Canonical JSON of `value` under `key`'s largest value (CENTRAL_STATE_TABLE); ValueError
    ("central_state_key") for a key outside the table or a value of another key's model,
    ValueError("central_state_too_large") past its size."""
    if STATE_MODELS.get(key) is not type(value):
        raise ValueError("central_state_key")
    return encode(value.model_dump(mode="json"), CENTRAL_STATE_TABLE[key], "central_state_too_large")


def decode_central_state(key: str, raw: bytes) -> CentralStateValue:
    """The value `raw` encodes under `key`; ValueError("central_state") for anything else."""
    model = STATE_MODELS.get(key)
    if model is None:
        raise ValueError("central_state")
    return decoded(model, raw, CENTRAL_STATE_TABLE[key], "central_state")


def encode_install_player(request: InstallPlayer) -> bytes:
    return encode(request.model_dump(mode="json"), INSTALL_BYTES, "install_player_too_large")


def decode_install_player(raw: bytes) -> InstallPlayer:
    """ValueError("install_player") for anything but an InstallPlayer."""
    return decoded(InstallPlayer, raw, INSTALL_BYTES, "install_player")


def encode_install_answer(answer: InstallStarted | InstallRefused) -> bytes:
    return encode(_Answer(answer=answer).model_dump(mode="json"), INSTALL_BYTES, "install_answer_too_large")


def decode_install_answer(raw: bytes) -> InstallStarted | InstallRefused:
    """ValueError("install_answer") for anything but an InstallAnswer."""
    return decoded(_Answer, raw, INSTALL_BYTES, "install_answer").answer


__all__ = [
    "BIRTH_KEY", "CENTRAL_ACCOUNT", "CENTRAL_COMPONENT", "CENTRAL_STATE_BUCKET", "CENTRAL_STATE_PREFIX",
    "CENTRAL_STATE_STREAM", "CENTRAL_STATE_TABLE", "CENTRAL_USER", "DOOR_PATH", "FRAMES_MAX",
    "HOME_ASSISTANT_PUBLISH", "HOME_ASSISTANT_STATES_BUCKET", "HOME_ASSISTANT_STATES_PREFIX",
    "HOME_ASSISTANT_STATES_STREAM", "HOME_ASSISTANT_SUBSCRIBE", "HOME_ASSISTANT_USER", "INSTALLATION_KEY",
    "INSTALL_BYTES", "INSTALL_PLAYER", "LISTENED_MAX", "LISTENING_KEY", "NAME_MAX", "PLAYERS_MAX",
    "RELEASES_KEY", "RELEASES_LISTED", "RELEASE_TAG_PATTERN", "ROOM_LIGHTS_KEY", "STATES_EPOCH_KEY",
    "STATE_MODELS", "VERSIONS_KEY", "CentralBirth", "CentralStateValue", "DrivenBy", "FrameListing",
    "InstallAnswer", "InstallPlayer", "InstallProgress", "InstallRefusal", "InstallRefused", "InstallStarted",
    "Installation", "Listening", "Name", "PiVersion", "PlayerListing", "ReadinessState", "ReleaseListing",
    "ReleaseTag", "Releases", "RoomLightListing", "RoomLightsListing", "Serial", "StatesEpoch", "Versions",
    "decode_central_state", "decode_install_answer", "decode_install_player", "encode_central_state",
    "encode_install_answer", "encode_install_player",
]
