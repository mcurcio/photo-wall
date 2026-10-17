import { callApi } from "../../apiWrite.js";
import type {
  DrawnView, RefusedReportsView, RoomLight, RoomLightsView, RoomLightView, Sensor, SensorView,
} from "../../generated/contract";
import type { Answer } from "../answer";

/* The house's lights' reads and writes on Central (the House › Lights routes); a refusal is a
 * RoomLightRefusal code. */

const LIGHTS = "/v1/operator/house/lights";
const SENSORS = "/v1/operator/house/sensors";
const DRAWN = `${LIGHTS}/drawn`;
const REFUSED = "/v1/operator/house/refused-reports";

/** Every room light and Sensor, with their last states (GET …/house/lights). */
export function readRoomLights(): Promise<Answer<"view", RoomLightsView>> {
  return callApi("view", LIGHTS, { method: "GET" }) as Promise<Answer<"view", RoomLightsView>>;
}

/** Create or replace a room light (PUT …/house/lights/{id}). */
export function saveRoomLight(id: string, light: RoomLight): Promise<Answer<"light", RoomLightView>> {
  return callApi("light", `${LIGHTS}/${encodeURIComponent(id)}`, { method: "PUT", body: light }) as
    Promise<Answer<"light", RoomLightView>>;
}

/** Remove a room light (DELETE …/house/lights/{id}); refused while a Frame's lighting names it. */
export function removeRoomLight(id: string): Promise<Answer<"removed", null>> {
  return callApi("removed", `${LIGHTS}/${encodeURIComponent(id)}`, { method: "DELETE" }) as
    Promise<Answer<"removed", null>>;
}

/** Create or replace a Sensor (PUT …/house/sensors/{id}). */
export function saveSensor(id: string, sensor: Sensor): Promise<Answer<"sensor", SensorView>> {
  return callApi("sensor", `${SENSORS}/${encodeURIComponent(id)}`, { method: "PUT", body: sensor }) as
    Promise<Answer<"sensor", SensorView>>;
}

/** Remove a Sensor (DELETE …/house/sensors/{id}); refused while it measures a room light. */
export function removeSensor(id: string): Promise<Answer<"removed", null>> {
  return callApi("removed", `${SENSORS}/${encodeURIComponent(id)}`, { method: "DELETE" }) as
    Promise<Answer<"removed", null>>;
}

/** Which house's lights each Pi draws with, judged by Central (GET …/house/lights/drawn). */
export function readDrawn(): Promise<Answer<"drawn", DrawnView>> {
  return callApi("drawn", DRAWN, { method: "GET" }) as Promise<Answer<"drawn", DrawnView>>;
}

/** The one refusal log: the newest reports each source could not record (GET …/house/refused-reports). */
export function readRefusedReports(): Promise<Answer<"refused", RefusedReportsView>> {
  return callApi("refused", REFUSED, { method: "GET" }) as Promise<Answer<"refused", RefusedReportsView>>;
}
