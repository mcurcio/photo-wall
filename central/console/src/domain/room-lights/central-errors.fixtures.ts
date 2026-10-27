import type { CentralErrorsView, FrameImpedimentView, SourceAttentionView } from "../../generated/contract";

/* Central's errors as Central serves them, for the catalogue's stories: tonight's case (Photo Wall
 * can't read Family Room Lights from an older Galleria, the living room's Frame black under Central's
 * card), a smart Sensor that can't tell and the smart home's link closed. */

/** Central's clock, seconds since the epoch. */
export const SINCE = 1_791_000_000;

export const FAMILY_ROOM: SourceAttentionView = {
  source: "home-assistant",
  address: "light.family_room",
  doubt: { reason: "cant-read", since: SINCE },
  lights: ["family-room-lights"],
  frames: ["living-room"],
  what_to_do: "Update Galleria in Home Assistant (HACS) to its newest release",
};

export const WINDOW_METER: SourceAttentionView = {
  source: "home-assistant",
  address: "sensor.window_illuminance",
  doubt: { reason: "cant-tell", since: SINCE - 3600 },
  lights: ["daylight"],
  frames: ["living-room", "hall"],
  what_to_do: "Check sensor.window_illuminance in Home Assistant",
};

export const HALL_LAMP: SourceAttentionView = {
  source: "home-assistant",
  address: "light.hall_lamp",
  doubt: { reason: "link-closed", since: SINCE - 600 },
  lights: ["hall-lamp"],
  frames: [],
  what_to_do: "Check Home Assistant and its Galleria integration",
};

export const IN_DOUBT: CentralErrorsView = { in_doubt: [FAMILY_ROOM, WINDOW_METER, HALL_LAMP] };

export const LIGHT_NAMES: Readonly<Record<string, string>> = {
  "family-room-lights": "Family Room Lights",
  daylight: "Daylight (windows)",
  "hall-lamp": "Hall lamp",
};

export const IMPEDED: FrameImpedimentView = {
  frame_id: "living-room",
  impediment: {
    kind: "impeded",
    by: {
      frame_id: "living-room",
      output_id: "HDMI-A-1",
      grant_id: "3f0c2a52-6a1e-4c0b-9a51-2d3c1f6b7e10",
      lights: ["family-room-lights"],
      words: {
        line1: "Photo Wall can't read Family Room Lights · since 29 Oct 18:05",
        line2: "Its last word: off (26 Oct 15:35) · see Needs attention in Photo Wall",
      },
    },
  },
};

export const CLEAR: FrameImpedimentView = { frame_id: "living-room", impediment: { kind: "clear" } };

export const NOT_REPORTED: FrameImpedimentView = { frame_id: "living-room", impediment: { kind: "not-reported" } };
