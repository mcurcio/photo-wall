import { everydayCommands } from "./domain/everyday-api.ts";
import { rebootRequest, rebootTarget, sendReboot } from "./fleetCommands.js";
import { checkConnection } from "./libraryApi.js";
import { hold, resumeSchedule } from "./powerScheduleApi.js";

/**
 * Home's writes (decision 0036): each is its owner's own route, reached through the owner's own
 * module, and handed to the Home page as one port by its route (neutralRoutes.jsx), so the page
 * and its domain components name no write themselves. Nothing here is a new way to write: All off,
 * All on and Resume schedule are the week page's (powerScheduleApi.js), Check now is Settings ›
 * Photo library's (libraryApi.js), and a reboot goes through the fleet's ONE send path
 * (fleetCommands.js `sendReboot`), its request frozen from the Pi's newest node read as the Pi
 * page's is. Restart app is the Home page's own request (domain/pi-actions.tsx). The everyday
 * actions (decision 0036, slice 3: Skip and Previous on a wall or a Frame, Never show and Show
 * again, Pause and Carry on, Back to normal) are domain/everyday-api.ts's.
 */
export const homeCommands = Object.freeze({
  hold,
  resumeSchedule,
  checkConnection,
  ...everydayCommands,

  /**
   * Ask a Pi to reboot (domain/pi-actions.tsx `AskReboot`): null when Central recorded the
   * request, else why it was not asked or not taken, in the fleet module's words.
   *
   * @param {{deviceId: string, playerId: string|null, requestId: string, snapshot: object|null,
   *          node: {latest: () => import("./nodeRead.js").NodeDevice}}} request
   * @returns {Promise<string|null>}
   */
  async askReboot({ deviceId, playerId, requestId, node, snapshot }) {
    const latest = node.latest();
    if (latest.read == null) {
      return "Still reading this Pi's state from Central. Try again in a moment.";
    }
    const request = rebootRequest(rebootTarget(latest), latest.read.reboot_commands ?? null, snapshot,
      latest.readAt, { playerId, commandId: requestId });
    if ("refused" in request) {
      return `Not asked: ${request.refused}.`;
    }
    const result = await sendReboot(deviceId, request, node);
    return result.outcome === "done" || result.outcome === "already" ? null : result.message;
  },
});
