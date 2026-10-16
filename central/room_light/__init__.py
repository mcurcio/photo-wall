"""Room lights (decision 0028): the real lights of the house, observed, never driven. Which room
lights and Sensors exist, each light's one reporter (its Home Assistant light, one Sensor, or nobody:
fixed), each light's last reported state, and the room-lights document every Pi draws with.

One writer per value (design rule 1): a room light's state is written only by its reporter, and a
state stands until its reporter writes again (nobody judges a reading's age, so no two clocks judge
one fact). Light facts never ride the control State (design rule 2): they reach every Pi as one
document on the hub's WALL stream (contracts/room_light.py).

Pure domain: no psycopg, FastAPI, nodeapi or MQTT client (pyproject's import-linter forbids them).
The PostgreSQL edge is central/infra/room_light_store.py, the Home Assistant edge the worker's
central/infra/home_assistant.py, the WALL write the worker's bus wiring (central/node_bus_wiring.py),
the operator edge central/room_light_routes.py (House > Lights).
"""
