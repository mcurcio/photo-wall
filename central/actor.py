"""Who changed a record: the operator through the console (its signed session cookie), the
operator through the API (the bearer token), Central's own scheduler, the media worker's pool
check, or a Node through its own authenticated request. Every audit column a store writes
(`*_by`) takes one of these: from the request's principal (`OperatorAuth.admin`, which answers
`console` or `api-token` only), from the scheduler's tick, or named by the store whose only
writer it is (`library-check`: the three pool-check tables, central/infra/photo_library_store.py;
`node`: a Pi's app link, central/fleet/node_app_links.py; decision 0033)."""

from typing import Literal

Actor = Literal["console", "api-token", "scheduler", "library-check", "node"]
