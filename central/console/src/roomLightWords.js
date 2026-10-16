/*
 * Room lights and Sensors in the console's words (decision 0028; Settings › Lights and a Frame's
 * lighting): the one home of each line, so the House page, the Frame's lighting and the Position
 * history say a room light the same way. Pure, so Node runs it (tests/test_console_room_lights.py).
 * Shapes: roomLightsApi.ts.
 */

/** A room light id's form (contracts/room_light.py ROOM_LIGHT_ID_PATTERN): what an id may be. */
export const ID_PATTERN = /^[a-z0-9][a-z0-9-]{0,31}$/;

/**
 * The id a new room light or Sensor gets from its name: lower case, each run of other characters
 * one hyphen, at most 32 characters ("Bookshelf spot" is "bookshelf-spot"); "" when the name has no
 * letter or digit.
 *
 * @param {string} name
 * @returns {string}
 */
export function idFromName(name) {
  return name.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase()
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+/, "").slice(0, 32).replace(/-+$/, "");
}

/**
 * A room light's last state in words, read from the state alone (never from when it was heard):
 * "On, 60 %", "Off", "Unavailable", "Not reported yet", "320 lx".
 *
 * @param {import("./roomLightsApi").RoomLightState} state
 * @returns {string}
 */
export function stateWords(state) {
  switch (state.kind) {
    case "dimmer":
      return state.on ? `On, ${Math.round(state.brightness * 100)} %` : "Off";
    case "measured":
      return luxWords(state.lux);
    case "unavailable":
      return "Unavailable";
    default:
      return "Not reported yet";
  }
}

/**
 * Lux as the console writes it: whole lux with thousands separated ("1,000 lx"), one decimal below
 * 10 lx ("2.5 lx").
 *
 * @param {number} lux
 * @returns {string}
 */
export function luxWords(lux) {
  if (lux < 10) return `${Number(lux.toFixed(1))} lx`;
  return `${String(Math.round(lux)).replace(/\B(?=(\d{3})+$)/g, ",")} lx`;
}

/**
 * Who reports a room light, in words: "Home Assistant: light.bookshelf", "Sensor: Hall meter" (the
 * Sensor's name from `sensorNames`, else its id), "Always on".
 *
 * @param {import("./roomLightsApi").Reporter} reporter
 * @param {Readonly<Record<string, string>>} [sensorNames]
 * @returns {string}
 */
export function reporterWords(reporter, sensorNames = {}) {
  switch (reporter.kind) {
    case "home-assistant-light":
      return `Home Assistant: ${reporter.entity}`;
    case "sensor":
      return `Sensor: ${sensorNames[reporter.sensor] ?? reporter.sensor}`;
    default:
      return "Always on";
  }
}

/**
 * A room light's colour in words: "2700 K", or "Reported by the bulb (red 50 %, green 30 %, blue 20 %)".
 *
 * @param {import("./roomLightsApi").Colour} colour
 * @returns {string}
 */
export function colourWords(colour) {
  if (colour.kind === "fixed") return `${colour.kelvin} K`;
  const [red, green, blue] = colour.channel_shares.map((share) => Math.round(share * 100));
  return `Reported by the bulb (red ${red} %, green ${green} %, blue ${blue} %)`;
}

/**
 * Where a Sensor is, in words: "On Pi …1a2b3c" (`piName` names the Pi), "Home Assistant: sensor.hall_lux".
 *
 * @param {import("./roomLightsApi").SensorPlace} at
 * @param {(playerId: string) => string} [piName]
 * @returns {string}
 */
export function placeWords(at, piName = (id) => id) {
  return at.kind === "pi" ? `On Pi ${piName(at.player_id)}` : `Home Assistant: ${at.entity}`;
}

/**
 * A Sensor's last reading in words: "320 lx", "Unavailable", "Not reported yet".
 *
 * @param {number|"unavailable"|null} reading
 * @returns {string}
 */
export function readingWords(reading) {
  if (reading === null) return "Not reported yet";
  return reading === "unavailable" ? "Unavailable" : luxWords(reading);
}

/** Each House › Lights refusal (central/room_light/model.py `SetupRefusal`) in plain words. */
const REFUSALS = {
  "named-by-a-frame": "A Frame's lighting uses this light. Remove it from those Frames' lighting first.",
  "sensor-reports-a-light": "This Sensor reports a room light. Change or remove that light first; a Sensor reports one light at most.",
  "reported-colour-needs-home-assistant": "Only a Home Assistant light reports its colour. Choose a colour in kelvin instead.",
  "entity-reports-another": "That Home Assistant entity already reports another light or Sensor. Each entity reports one.",
  "unknown-sensor": "That Sensor is no longer there. Choose another.",
  "unknown-room-light": "That light is no longer there.",
  "too-many": "The house has as many lights or Sensors as Photo Wall keeps. Remove one first.",
  unreachable: "Central did not answer. Try again.",
  http_422: "Photo Wall refused these values. Check the name, the entity and the numbers.",
};

/**
 * A refusal's words, with what was refused first ("Not removed: …").
 *
 * @param {string} done what did not happen ("Not saved", "Not removed")
 * @param {string} code Central's refusal code
 * @returns {string}
 */
export function refusalWords(done, code) {
  return `${done}: ${REFUSALS[code] ?? `Central refused it (${code}).`}`;
}

/** Why the Home Assistant link did not record a message (central/room_light/model.py `Refusal`). */
const MESSAGE_REFUSALS = {
  "unknown-entity": "No room light or Sensor here names this entity yet. Add it, or leave it out of the automation.",
  "not-its-reporter": "This entity does not report that light or Sensor.",
  "out-of-range": "Not a message Photo Wall reads: check the automation's topic and payload.",
};

/**
 * A refused message's reason in words; a reason Central gives without words is shown as it is.
 *
 * @param {string} reason
 * @returns {string}
 */
export function messageRefusalWords(reason) {
  return MESSAGE_REFUSALS[reason] ?? `Not read: ${reason}.`;
}
