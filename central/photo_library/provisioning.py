"""The Immich connection the deployment sets (owner, 2026-10-12, verbatim: "The immich url and api
key should be injected by iac"; he chose "Read-only in Settings").

Twelve-factor configuration: Central reads the Immich address and API key from its environment at
start and keeps one connection, marked managed, in step with them. Like Grafana's provisioned data
sources, a managed connection is shown read-only: Settings › Photo library shows its health and
Check now, and the operator API refuses to change or remove it (409 connection_managed). Changing
it means changing the deployment. A connection pasted in Settings stays the way for local and
development use.

| Variable | Holds |
|---|---|
| `PHOTO_WALL_IMMICH_URL` | The Immich address, as Settings takes it (`policy.normalise_url`) |
| `PHOTO_WALL_IMMICH_API_KEY` | The API key itself (a Kubernetes `secretKeyRef`, `op run`) |
| `PHOTO_WALL_IMMICH_API_KEY_FILE` | Or: a file holding the key (a mounted secret, Docker secrets' `_FILE` convention) |

`read_setting` turns the environment into one `ImmichSetting` (pure: the file read is passed in);
`apply_setting` applies it to the store and logs the outcome, never the key. An empty variable is
an unset one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Final, Literal

from pydantic import Field, SecretStr

from central.photo_library.policy import normalise_url
from central.photo_library.ports import LibraryRefused, ManagedConnectionCommands
from contracts.models import Model

URL_VARIABLE: Final = "PHOTO_WALL_IMMICH_URL"
KEY_VARIABLE: Final = "PHOTO_WALL_IMMICH_API_KEY"
KEY_FILE_VARIABLE: Final = "PHOTO_WALL_IMMICH_API_KEY_FILE"

LOG = logging.getLogger(__name__)


class SetByDeployment(Model):
    """Both the address and the key are set: Central keeps the managed connection in step."""

    kind: Literal["set"] = "set"
    url: str = Field(max_length=2048)     # policy.normalise_url's form
    key: SecretStr = Field(repr=False, exclude=True)


class NotSet(Model):
    """Neither is set: no connection is managed (a managed one becomes an ordinary one)."""

    kind: Literal["not_set"] = "not_set"


class Misconfigured(Model):
    """A partial or unreadable setting: logged as an error; nothing in the store changes."""

    kind: Literal["misconfigured"] = "misconfigured"
    reason: str


ImmichSetting = SetByDeployment | NotSet | Misconfigured


def read_setting(environ: Mapping[str, str], read_file: Callable[[str], str]) -> ImmichSetting:
    """The deployment's Immich setting. The key comes from `KEY_VARIABLE` or from the file
    `KEY_FILE_VARIABLE` names (never both); surrounding whitespace is dropped from each value.
    Misconfigured: only one of address and key, both key variables, an unreadable or empty key
    file, or an address `normalise_url` refuses. No reason names the key."""
    url = environ.get(URL_VARIABLE, "").strip()
    key = environ.get(KEY_VARIABLE, "").strip()
    key_file = environ.get(KEY_FILE_VARIABLE, "").strip()
    if key and key_file:
        return Misconfigured(reason=f"set {KEY_VARIABLE} or {KEY_FILE_VARIABLE}, not both")
    if not (url or key or key_file):
        return NotSet()
    if key_file:
        try:
            key = read_file(key_file).strip()
        except (OSError, UnicodeDecodeError) as error:
            return Misconfigured(reason=f"{KEY_FILE_VARIABLE} names a file Central cannot read "
                                        f"({type(error).__name__})")
        if not key:
            return Misconfigured(reason=f"{KEY_FILE_VARIABLE} names an empty file")
    if not url:
        return Misconfigured(reason=f"the API key is set but {URL_VARIABLE} is not")
    if not key:
        return Misconfigured(reason=f"{URL_VARIABLE} is set but {KEY_VARIABLE} "
                                    f"(or {KEY_FILE_VARIABLE}) is not")
    try:
        normalised = normalise_url(url)
    except ValueError as error:
        return Misconfigured(reason=f"{URL_VARIABLE} is not an Immich address: {error}")
    return SetByDeployment(url=normalised, key=SecretStr(key))


def apply_setting(store: ManagedConnectionCommands, setting: ImmichSetting) -> None:
    """At Central's start: keep the managed connection in step with `setting`. A misconfigured
    setting, or a store refusal, is an error log and Central still starts."""
    match setting:
        case Misconfigured(reason=reason):
            LOG.error("Immich connection from the environment not applied: %s. Central starts; "
                      "the photo library's connections are left as they were.", reason)
        case NotSet():
            released = store.release_managed_connection()
            if released is not None:
                LOG.warning("%s and %s are no longer set: connection %s is no longer set by the "
                            "deployment and can be changed in Settings › Photo library.",
                            URL_VARIABLE, KEY_VARIABLE, released)
        case SetByDeployment(url=url, key=key):
            try:
                provision = store.provision_connection(url, key)
            except LibraryRefused as refused:
                LOG.error("Immich connection from the environment not applied: %s. Central starts.",
                          refused.code)
                return
            LOG.info("Immich connection %s set by the deployment (%s) at %s.",
                     provision.connection_ref, provision.outcome, url)
