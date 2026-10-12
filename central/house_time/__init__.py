"""House time: the house's timezone and location, and the one calculator that turns a When (days,
a start and an end, each a clock time or sunrise/sunset plus minutes) into real moments (roadmap
2b E1; Power and the week, slice T).

Pure domain: no psycopg, no FastAPI, no nodeapi (pyproject's import-linter forbids them). Every
time of day in Central is worked out here and nowhere else: Runtime's Program windows, Night off's
set times, the week page and Why all call it; the console and the Pi work out no times. The
PostgreSQL edge is central/infra/house_store.py; the operator edge is central/house_routes.py.
"""
