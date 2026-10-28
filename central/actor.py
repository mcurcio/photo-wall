"""Who changed a record: the operator through the console (its signed session cookie), the
operator through the API (the bearer token), or Central's own scheduler. Every audit column a
store writes (`*_by`) takes one of these, from the request's principal (`OperatorAuth.admin`) or
from the scheduler's tick."""

from typing import Literal

Actor = Literal["console", "api-token", "scheduler"]
