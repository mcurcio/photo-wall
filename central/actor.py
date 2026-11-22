"""Who changed a record: the one home of every audit value (`*_by`), one `TEXT` value.

An `Actor` is a small grammar (decision 0035): one of the fixed words below, or an operator
Central knows by its own id, as `encode` writes it and `parse` reads it. No other module splits
the string.

| Value | Meaning |
|---|---|
| `person:<operator_id>` | a signed-in person through the console (an identity provider's account) |
| `client:<operator_id>` | a machine with a provider token (a service account) |
| `console` | the console in token mode (no provider): its signed session cookie names nobody |
| `api-token` | the bearer token `PHOTO_WALL_ADMIN_TOKEN` (the break-glass token) |
| `scheduler` | Central's own scheduler's tick |
| `library-check` | the media worker's pool check (the three pool-check tables, central/infra/photo_library_store.py) |
| `node` | a Node through its own authenticated request (a Pi's boot offer, central/fleet/node_boot.py; decision 0033) |
| `central` | Central or its worker writing on its own account, not for a request or a tick: the house's seed at start, the hub's reachability (decision 0034), an operator first seen and a session re-judged (decision 0035) |
| `home-assistant` | Galleria on Central's hub: its installPlayer (central/hub_api.py) and the entities its states name (central/infra/room_light_store.py) |

Every audit column a store writes takes one of these: from the request's principal
(`OperatorAuth.admin` and `.operate`, which answer the first four kinds), from the scheduler's
tick, or named by the store whose only writer it is. Rows written before decision 0035 keep
`console`. A reader that shows "who" asks the operators repository for the name
(central/operator_access/ports.py `Operators.name_of`); the id is Central's own, never the
provider's subject, a name or an e-mail.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Final, Literal, get_args

from pydantic import StringConstraints

FixedActor = Literal["console", "api-token", "scheduler", "library-check", "node", "central", "home-assistant"]
FIXED_ACTORS: Final[frozenset[str]] = frozenset(get_args(FixedActor))

OperatorActorKind = Literal["person", "client"]
OPERATOR_ACTOR_KINDS: Final[frozenset[str]] = frozenset(get_args(OperatorActorKind))

OPERATOR_ID_PATTERN: Final = r"op_[a-z2-7]{16}"
"""Central's own id for a provider identity: `op_` and 80 random bits in lowercase base32."""

ACTOR_PATTERN: Final = (
    "^(?:" + "|".join(re.escape(word) for word in get_args(FixedActor))
    + "|(?:" + "|".join(get_args(OperatorActorKind)) + "):" + OPERATOR_ID_PATTERN + ")$")
"""The whole grammar as one pattern: exactly a fixed word, or `person:`/`client:` and an operator
id. Nothing else is an audit value."""

Actor = Annotated[str, StringConstraints(pattern=ACTOR_PATTERN)]
"""An audit value. A pydantic field of this type refuses anything outside the grammar."""

_OPERATOR_ID: Final = re.compile(OPERATOR_ID_PATTERN, re.ASCII)
_ACTOR: Final = re.compile(ACTOR_PATTERN, re.ASCII)


@dataclass(frozen=True)
class ParsedActor:
    """An audit value read: `kind` is `person` or `client` with its `operator_id`, or `fixed`
    with none; `value` is the value as written."""
    kind: OperatorActorKind | Literal["fixed"]
    operator_id: str | None
    value: str


def is_operator_id(value: object) -> bool:
    return isinstance(value, str) and _OPERATOR_ID.fullmatch(value) is not None


def encode(kind: OperatorActorKind, operator_id: str) -> Actor:
    """The audit value of an operator Central knows. ValueError for an unknown kind or an id that
    is not an operator id: a caller cannot write a value `parse` would not read."""
    if kind not in OPERATOR_ACTOR_KINDS or not is_operator_id(operator_id):
        raise ValueError("not an operator actor")
    return f"{kind}:{operator_id}"


def parse(value: str) -> ParsedActor | None:
    """The audit value read, or None for a value outside the grammar (`unknown`, which migration
    126 wrote for Position entries appended before anyone was recorded, is one)."""
    if not isinstance(value, str) or _ACTOR.fullmatch(value) is None:
        return None
    if value in FIXED_ACTORS:
        return ParsedActor("fixed", None, value)
    kind, _, operator_id = value.partition(":")
    return ParsedActor(kind, operator_id, value)  # type: ignore[arg-type]
