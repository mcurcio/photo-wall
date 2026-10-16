"""The Home Assistant adapter: everything Home Assistant and MQTT, in one package, at the edge.

Room lights (central/room_light/) speak only room-light facts; this package turns the house's
broker into them and back into the operator's Home Assistant page:

- `topics`: the topics and the one subscription, the entity id's form, and the references
  (`ReporterRef`, `SensorRef`) whose source is SOURCE and whose address is an entity id;
- `messages`: a payload to a room-light state or reading (the 0..255 and rgb scales, "unknown" as
  unavailable), or the reason it is refused;
- `health`: the link's health, judged from its heartbeat's age, never stored as a transition;
- `link`: the worker's one MQTT client (aiomqtt), which records through `RoomLightReports`;
- `settings`, `views`: the Home Assistant settings page's read and answer.

Only this package imports aiomqtt, and nothing outside the worker's wiring, the API's composition
root, the settings page's routes and the console contract imports this package (pyproject's
import-linter contracts). The package imports no module
of its own here, so importing `topics` never loads the MQTT client.
"""
