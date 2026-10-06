"""The private overlay client (Weston's spawned diagnostic client) and Display's overlay language.

Package-relative imports only: it runs in-repo as `appliance.display_host.overlay` and is installed
as the top-level `overlay` package beside the `diagnostic-client` launcher. `instruction` and
`render` are pure; `paint` (pycairo) and `client` (pywayland) import their libraries lazily.
"""
