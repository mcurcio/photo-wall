import { callApi } from "../../apiWrite.js";
import type { HomeAssistantStatesView } from "../../generated/contract";
import type { Answer } from "../answer";

const HOME_ASSISTANT = "/v1/operator/integrations/home-assistant";

/** Home Assistant's states as Central last heard them: when, each light and illuminance entity, and
 * the states it could not record (GET …/integrations/home-assistant). */
export function readHomeAssistant(): Promise<Answer<"view", HomeAssistantStatesView>> {
  return callApi("view", HOME_ASSISTANT, { method: "GET" }) as Promise<Answer<"view", HomeAssistantStatesView>>;
}
