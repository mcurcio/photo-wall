"""Displays: which display is on each Output, whether a Frame is ready, and what each Output's power
should be (roadmap 1b; run ledger .claude/runs/display-1b.md, slices C1-C3).

Pure domain: no psycopg, no FastAPI, no nodeapi (pyproject's import-linter forbids them). The
PostgreSQL edge is central/infra/display_store.py, the bus edge is the Output report judge and the
Output document source it implements (`central.infra.node_link_store.RecordJudge`,
`nodeapi.hub.DocumentSource`), and the operator edge is central/display_routes.py.
"""
