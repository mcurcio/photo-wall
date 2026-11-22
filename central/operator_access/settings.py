"""The identity provider as the deployment sets it (decision 0035, Configuration): six
environment variables, never the console, never the database. Pure: it reads a mapping.

| Variable | Meaning | Default |
|---|---|---|
| `PHOTO_WALL_OIDC_ISSUER` | the issuer; setting it turns provider mode on | unset: token mode |
| `PHOTO_WALL_OIDC_CLIENT_ID` | Central's client | required with the issuer |
| `PHOTO_WALL_OIDC_CLIENT_SECRET` | Central's client secret | required with the issuer |
| `PHOTO_WALL_OIDC_ROLE` | the provider's name for the role that lets someone in (Central's `owner`) | `owner` |
| `PHOTO_WALL_OIDC_ROLES_CLAIM` | dotted path to the list of roles in the access token | `resource_access.<client id>.roles` |
| `PHOTO_WALL_OIDC_NAME` | what the sign-in button calls the provider | the issuer's host |
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlsplit

from central.operator_access.model import Role

ISSUER: Final = "PHOTO_WALL_OIDC_ISSUER"
CLIENT_ID: Final = "PHOTO_WALL_OIDC_CLIENT_ID"
CLIENT_SECRET: Final = "PHOTO_WALL_OIDC_CLIENT_SECRET"
ROLE: Final = "PHOTO_WALL_OIDC_ROLE"
ROLES_CLAIM: Final = "PHOTO_WALL_OIDC_ROLES_CLAIM"
NAME: Final = "PHOTO_WALL_OIDC_NAME"
VARIABLES: Final = (ISSUER, CLIENT_ID, CLIENT_SECRET, ROLE, ROLES_CLAIM, NAME)
_REQUIRED_TOGETHER: Final = (ISSUER, CLIENT_ID, CLIENT_SECRET)

DEFAULT_ROLE: Final = "owner"


@dataclass(frozen=True)
class OidcSettings:
    """`role` is the provider's name for Central's `owner`. `roles_claim` is the path as parts,
    so a client id holding a dot still reads `resource_access.<client id>.roles`."""
    issuer: str
    client_id: str
    client_secret: str = field(repr=False)
    role: str = DEFAULT_ROLE
    roles_claim: tuple[str, ...] = ()
    name: str = ""

    def __post_init__(self) -> None:
        host = urlsplit(self.issuer).hostname
        if urlsplit(self.issuer).scheme not in ("http", "https") or not host:
            raise ValueError(f"{ISSUER} must be an http(s) address")
        if not self.client_id or not self.client_secret or not self.role:
            raise ValueError("the identity provider's client id, client secret and role must not be empty")
        if not self.roles_claim:
            object.__setattr__(self, "roles_claim", ("resource_access", self.client_id, "roles"))
        if not self.name:
            object.__setattr__(self, "name", host)

    @property
    def issuer_host(self) -> str:
        return urlsplit(self.issuer).hostname or ""

    @property
    def role_names(self) -> Mapping[str, Role]:
        """The provider's role names Central reads, each as Central's role."""
        return {self.role: "owner"}


def oidc_settings_in(environ: Mapping[str, str]) -> OidcSettings | None:
    """The provider the environment configures; None when it configures none (token mode).
    ValueError, one line naming every variable that is missing, for a partial setting: any of
    the six set without the issuer, its client id and its client secret."""
    stated = {name: environ[name].strip() for name in VARIABLES if environ.get(name, "").strip()}
    if not stated:
        return None
    missing = [name for name in _REQUIRED_TOGETHER if name not in stated]
    if missing:
        raise ValueError(
            f"identity provider partly configured ({', '.join(sorted(stated))} set): "
            f"{', '.join(missing)} missing")
    return OidcSettings(
        issuer=stated[ISSUER], client_id=stated[CLIENT_ID], client_secret=stated[CLIENT_SECRET],
        role=stated.get(ROLE, DEFAULT_ROLE),
        roles_claim=tuple(stated[ROLES_CLAIM].split(".")) if ROLES_CLAIM in stated else (),
        name=stated.get(NAME, ""))
