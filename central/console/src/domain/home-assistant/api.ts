import { callApi } from "../../apiWrite.js";
import type { HomeAssistantView } from "../../generated/contract";
import type { Answer } from "../answer";

const HOME_ASSISTANT = "/v1/operator/integrations/home-assistant";

/** The Home Assistant link's health, its entities and its refused messages (GET …/integrations/home-assistant). */
export function readHomeAssistant(): Promise<Answer<"view", HomeAssistantView>> {
  return callApi("view", HOME_ASSISTANT, { method: "GET" }) as Promise<Answer<"view", HomeAssistantView>>;
}
