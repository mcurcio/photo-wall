import { callApi } from "../../apiWrite.js";
import type { Answer } from "../answer";
import type { OperatorAccessView, OperatorSessionView } from "./access";

/* Settings › People and access' two reads on Central (decision 0035). */

/** Who is signed in (GET …/session). */
export function readSession(): Promise<Answer<"session", OperatorSessionView>> {
  return callApi("session", "/v1/operator/session", { method: "GET" }) as Promise<Answer<"session", OperatorSessionView>>;
}

/** How this Photo Wall signs people in, and its provider's check (GET …/access). */
export function readAccess(): Promise<Answer<"access", OperatorAccessView>> {
  return callApi("access", "/v1/operator/access", { method: "GET" }) as Promise<Answer<"access", OperatorAccessView>>;
}
