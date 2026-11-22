"""What the operator's door answers (decision 0035): the shapes of `GET sign-in`, `GET session`,
`DELETE session` in provider mode and `GET access`. No token is a field of any of them; Log out's
address carries the person's own ID token as the provider's hint, and nothing else does."""

from __future__ import annotations

from typing import Final, Literal

from central.operator_access.model import Role
from central.operator_access.policy import HARD_END_SECONDS
from contracts.models import Instant, Model

SignInMethod = Literal["provider", "token"]
SESSION_CAP_DAYS: Final = HARD_END_SECONDS // (24 * 3600)


class SignInView(Model):
    """How to sign in: through the provider (`name`: what the button calls it), or the token."""
    method: SignInMethod
    name: str | None = None


class SessionView(Model):
    """Who is signed in. `method` is `token` for the shared token (its cookie or the break-glass
    Bearer), which names nobody and holds the owner's role."""
    method: SignInMethod
    name: str | None
    roles: tuple[Role, ...]
    provider: str | None


class LoggedOut(Model):
    """Provider mode's Log out: the provider's end-session address to go to, when it has one."""
    end_session: str | None


class ProviderView(Model):
    name: str
    issuer_host: str
    reachable: bool
    last_reached_at: Instant | None


class AccessView(Model):
    """Settings › People and access: how this Photo Wall lets people in."""
    method: SignInMethod
    provider: ProviderView | None
    session_cap_days: int = SESSION_CAP_DAYS
