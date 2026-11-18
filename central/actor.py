"""Who changed a record: the operator through the console (its signed session cookie), the
operator through the API (the bearer token), Central's own scheduler, the media worker's pool
check, or a Node through its own authenticated request. Every audit column a store writes
(`*_by`) takes one of these: from the request's principal (`OperatorAuth.admin`, which answers
`console` or `api-token` only), from the scheduler's tick, or named by the store whose only
writer it is (`library-check`: the three pool-check tables, central/infra/photo_library_store.py;
`node`: a Pi's app link, central/fleet/node_app_links.py; decision 0033). `central`: Central or
its worker writing on its own account, not for a request or a tick: the house's seed at start, the
hub's reachability (decision 0034). `home-assistant`: Galleria's installPlayer on Central's hub,
its one writer (central/hub_api.py)."""

from typing import Literal

Actor = Literal["console", "api-token", "scheduler", "library-check", "node", "central", "home-assistant"]
