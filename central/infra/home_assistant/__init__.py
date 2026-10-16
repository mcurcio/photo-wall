"""The Home Assistant adapter: everything Home Assistant, in one package, at the edge.

Room lights (central/room_light/) speak only room-light facts; this package turns Home Assistant's
states, as the Galleria integration publishes them on Central's hub (contracts/home_assistant.py),
into them and back into the operator's Home Assistant page:

- `references`: the references (`ReporterRef`, `SensorRef`) whose source is SOURCE and whose address
  is an entity id;
- `messages`: a state to a room-light word or reading (the 0..255 and rgb scales);
- `states`: the listener Central's hub session hands every newest state to
  (nodeapi.hub `run_hub_session`), which records through `RoomLightReports` and `HeardEntities`;
- `settings`, `views`: the Home Assistant settings page's read and answer.

It imports no NATS client: Central reaches its hub only through nodeapi.hub. Nothing outside Central's
hub wiring, the API's composition root, the settings page's routes and the console contract imports
this package (pyproject's import-linter contracts).
"""
