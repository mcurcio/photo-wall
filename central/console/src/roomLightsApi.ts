import { callApi } from "./weekApi.js";

/*
 * Settings › Lights' read and writes (decision 0028): the routes of central/room_light_routes.py,
 * answered with the shapes of central/room_light/views.py (their one home; the value forms are
 * central/room_light/model.py's and contracts/room_light.py's). Each returns `{ok: true, …}` or
 * `{ok: false, error}`: a refusal answers its code (409 `named-by-a-frame`, `sensor-reports-a-light`,
 * `reported-colour-needs-home-assistant`, `entity-reports-another`, `unknown-sensor`,
 * `unknown-room-light`, `too-many`; a body outside its shape `http_422`); a lost request answers
 * `unreachable`. Instants are Central's clock, seconds since the epoch.
 */

/** Who writes a room light's state: its Home Assistant light, one Sensor, or nobody (always on). */
export type Reporter =
  | { kind: "home-assistant-light"; entity: string }
  | { kind: "sensor"; sensor: string }
  | { kind: "fixed" };

/** A room light's colour: a fixed kelvin, or an RGB bulb's channel shares (reported by the bulb). */
export type Colour =
  | { kind: "fixed"; kelvin: number }
  | { kind: "reported"; channel_shares: [number, number, number] };

/** A room light's last state, as its one reporter said it. */
export type RoomLightState =
  | { kind: "dimmer"; on: boolean; brightness: number; rgb: [number, number, number] | null }
  | { kind: "measured"; lux: number }
  | { kind: "unavailable" }
  | { kind: "not-reported" };

/** Where a Sensor is: the meter wired to a Pi, or a Home Assistant illuminance entity. */
export type SensorPlace = { kind: "pi"; player_id: string } | { kind: "home-assistant"; entity: string };

export interface RoomLightView {
  id: string;
  name: string;
  reporter: Reporter;
  colour: Colour;
  state: RoomLightState;
  /** When Central recorded its state; null: never (a fixed light is always on, never heard). */
  heard_at: number | null;
  /** The Frames whose lighting names it. */
  named_by: string[];
}

export interface SensorView {
  id: string;
  name: string;
  at: SensorPlace;
  /** Its last reading in lux; "unavailable": it said it cannot tell; null: never heard. */
  reading: number | "unavailable" | null;
  heard_at: number | null;
  /** The room light it reports; null: none. */
  reports: string | null;
}

export interface HomeAssistantLinkView {
  state: "not-configured" | "connected" | "away";
  since: number | null;
}

export interface RoomLightsView {
  lights: RoomLightView[];
  sensors: SensorView[];
  home_assistant: HomeAssistantLinkView;
}

export interface RoomLightSave {
  name: string;
  reporter: Reporter;
  colour: Colour;
}

export interface SensorSave {
  name: string;
  at: SensorPlace;
}

export type Answer<K extends string, T> = ({ ok: true } & Record<K, T>) | { ok: false; error: string };

const LIGHTS = "/v1/operator/house/lights";
const SENSORS = "/v1/operator/house/sensors";

/** Every room light and Sensor, and the Home Assistant link (GET …/house/lights). */
export function readRoomLights(): Promise<Answer<"view", RoomLightsView>> {
  return callApi("view", LIGHTS, { method: "GET" }) as Promise<Answer<"view", RoomLightsView>>;
}

/** Create or replace a room light (PUT …/house/lights/{id}). */
export function saveRoomLight(id: string, body: RoomLightSave): Promise<Answer<"light", RoomLightView>> {
  return callApi("light", `${LIGHTS}/${encodeURIComponent(id)}`, { method: "PUT", body }) as
    Promise<Answer<"light", RoomLightView>>;
}

/** Remove a room light (DELETE …/house/lights/{id}); refused while a Frame's lighting names it. */
export function removeRoomLight(id: string): Promise<Answer<"removed", null>> {
  return callApi("removed", `${LIGHTS}/${encodeURIComponent(id)}`, { method: "DELETE" }) as
    Promise<Answer<"removed", null>>;
}

/** Create or replace a Sensor (PUT …/house/sensors/{id}). */
export function saveSensor(id: string, body: SensorSave): Promise<Answer<"sensor", SensorView>> {
  return callApi("sensor", `${SENSORS}/${encodeURIComponent(id)}`, { method: "PUT", body }) as
    Promise<Answer<"sensor", SensorView>>;
}

/** Remove a Sensor (DELETE …/house/sensors/{id}); refused while it reports a room light. */
export function removeSensor(id: string): Promise<Answer<"removed", null>> {
  return callApi("removed", `${SENSORS}/${encodeURIComponent(id)}`, { method: "DELETE" }) as
    Promise<Answer<"removed", null>>;
}
