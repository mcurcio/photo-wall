"""Room lights: the real lights of the house, observed, never driven. Which room lights and Sensors
exist, who reports each (an opaque reference to a reporting source, a Sensor, or nobody: fixed), the
last word each reference said, and the room-lights document every Pi draws with.

A word is kept by its reference and resolved to a room light only when the document is built, so a
light or Sensor set up after its source last spoke shows that word at once.

Pure domain: no psycopg, FastAPI, nodeapi or MQTT client, and no reporting source's names
(pyproject's import-linter). The PostgreSQL edge is central/infra/room_light_store.py; the sources'
adapters are central/infra/home_assistant/ and central/infra/light_sensor_judge.py; the WALL write is
the worker's bus wiring (central/node_bus_wiring.py); the operator edge central/room_light_routes.py.
"""
