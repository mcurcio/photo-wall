import { callApi } from "../../apiWrite.js";
import type {
  CentralErrorsView, ColourSensor, ColourSensorView, DrawnView, FrameImpedimentView, RefusedReportsView, RoomLight,
  RoomLightsView, RoomLightView, Sensor, SensorView,
} from "../../generated/contract";
import type { Answer } from "../answer";

/* The house's lights' reads and writes on Central (the House › Lights routes); a refusal is a
 * RoomLightRefusal code. */

const LIGHTS = "/v1/operator/house/lights";
const SENSORS = "/v1/operator/house/sensors";
const COLOUR_SENSORS = "/v1/operator/house/colour-sensors";
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

/** Create or replace a colour sensor (PUT …/house/colour-sensors/{id}). */
export function saveColourSensor(id: string, sensor: ColourSensor): Promise<Answer<"colourSensor", ColourSensorView>> {
  return callApi("colourSensor", `${COLOUR_SENSORS}/${encodeURIComponent(id)}`, { method: "PUT", body: sensor }) as
    Promise<Answer<"colourSensor", ColourSensorView>>;
}

/** Remove a colour sensor (DELETE …/house/colour-sensors/{id}); refused while a room light follows it. */
export function removeColourSensor(id: string): Promise<Answer<"removed", null>> {
  return callApi("removed", `${COLOUR_SENSORS}/${encodeURIComponent(id)}`, { method: "DELETE" }) as
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

/* Central's errors (central/central_errors/: Needs attention's source rows and the Frame page's
 * "Central's error"): the room lights' references Central is in doubt of, and what a Frame's Pi said. */
const CENTRAL_ERRORS = "/v1/operator/central-errors";

/** Every reference Central is in doubt of now, with its lights, the Frames naming them and what to do
 * (GET …/central-errors). A store failure on Central is a failed read, never "nothing in doubt". */
export function readCentralErrors(): Promise<Answer<"view", CentralErrorsView>> {
  return callApi("view", CENTRAL_ERRORS, { method: "GET" }) as Promise<Answer<"view", CentralErrorsView>>;
}

/** What the Frame's Pi last said about Central's error on it (GET …/frames/{id}/central-error). */
export function readFrameImpediment(frameId: string): Promise<Answer<"view", FrameImpedimentView>> {
  return callApi("view", `/v1/operator/frames/${encodeURIComponent(frameId)}/central-error`, { method: "GET" }) as
    Promise<Answer<"view", FrameImpedimentView>>;
}
